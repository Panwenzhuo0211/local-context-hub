"""Publish message history for Codex's paginated desktop reader.

The rollout remains the model-history authority. This is the desktop read
projection, with byte offsets and ordinals into that exact UTF-8 file.
Only newly emitted, Context Hub-owned mirrors may be passed here.
"""
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path


def covers(home,thread_id,rollout, *, expected_ordinal=None):
    path=Path(home)/'thread_history_1.sqlite'
    if not path.exists():return False
    con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        row=con.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?',(thread_id,)).fetchone()
        return bool(row and row[0]==Path(rollout).stat().st_size and (expected_ordinal is None or row[1]==expected_ordinal))
    except sqlite3.Error:return False
    finally:con.close()


def _id(thread, ordinal, suffix):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'context-hub:{thread}:{ordinal}:{suffix}'))


def ready_for_append(home, thread_id, tail):
    """Native client must have projected the current source before we extend it.

    Original paginated sessions use global ordinals across continuation files.
    Never rebuild their existing native turns from a single segment.
    """
    path=Path(home)/'thread_history_1.sqlite'
    if not path.exists():return False
    con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        row=con.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?',(thread_id,)).fetchone()
        return bool(row and tuple(row)==(tail.seal[2],tail.ordinal+tail.data.count(b'\n')))
    finally:con.close()


def publish_synced_tail(home, thread_id, rollout, pair, *, expected_ordinal=None):
    """Project only new, attributed sync rows; preserve every native turn/item.

    If the client projected them already, do nothing. If native records are
    interleaved, defer to its own projector instead of guessing its turn state.
    File append and SQLite cannot be one transaction: the durable sync journal
    retries this step after interruption without appending another copy.
    """
    history=Path(home)/'thread_history_1.sqlite'
    if not history.exists():return False
    from ..sync_tail import token
    from .live import MARKER
    path=Path(rollout);seal=token(path.stat())
    con=sqlite3.connect(history,timeout=5)
    try:
        con.execute('BEGIN IMMEDIATE')
        checkpoint=con.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?',(thread_id,)).fetchone()
        if not checkpoint:return False
        offset,ordinal=checkpoint
        if offset>seal[2]:return False
        if offset==seal[2]:return expected_ordinal is None or ordinal==expected_ordinal
        rows=[]
        with path.open('rb') as source:
            # A segment-relative checkpoint must land at an exact record edge.
            if offset:
                source.seek(offset-1)
                if source.read(1)!=b'\n':return False
            source.seek(offset)
            for raw in source:
                if not raw.endswith(b'\n'):return False
                row=json.loads(raw)
                if row.get('ordinal')!=ordinal or (row.get(MARKER) or {}).get('pair')!=pair:return False
                rows.append((ordinal,offset,offset+len(raw),row))
                offset+=len(raw);ordinal+=1
        if token(path.stat())!=seal:return False
        if expected_ordinal is not None and ordinal!=expected_ordinal:return False
        if any(r.get('type')=='event_msg' and r.get('payload',{}).get('type')=='task_started' for _,_,_,r in rows):
            from .native_sync import project
            if not project(con,thread_id,rows):return False
            if token(path.stat())!=seal:raise ValueError('Native writer changed during projection; retry pending')
            con.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?',(offset,ordinal,thread_id))
            con.commit();return True
        turns=[];items=[];current=None
        for number,start,end,row in rows:
            payload=row.get('payload') or {};kind=payload.get('type')
            if row.get('type')!='event_msg' or kind not in ('user_message','agent_message'):continue
            stamp=int(datetime.fromisoformat(row['timestamp'].replace('Z','+00:00')).timestamp()*1000)
            if current is None or kind=='user_message':
                if current:current['end_ordinal'],current['end_offset']=number,start
                current=dict(id=_id(thread_id,number,'synced-turn'),ordinal=number,offset=start,
                             ms=stamp,completed=stamp,first=None,final=None,end_ordinal=ordinal,end_offset=offset)
                turns.append(current)
            iid=_id(thread_id,number,'synced-item')
            if kind=='user_message':
                item={'type':'userMessage','id':iid,'content':[{'type':'text','text':payload.get('message',''),'text_elements':[]}]}
                current['first']=iid
            else:
                item={'type':'agentMessage','id':iid,'text':payload.get('message',''),
                      'phase':payload.get('phase') or 'final_answer','memoryCitation':None,'delivery':None,'questions':None}
                current['final']=iid
            current['completed']=max(stamp,current['completed'])
            items.append((thread_id,current['id'],iid,number,stamp,json.dumps(item,ensure_ascii=False),item['type'],number,stamp,stamp))
        con.executemany('''INSERT INTO thread_turns
            (thread_id,turn_id,rollout_ordinal,status,started_at,completed_at,duration_ms,
             first_user_item_id,final_agent_item_id,rollout_byte_offset,rollout_end_ordinal,
             rollout_end_byte_offset,root_turn_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            [(thread_id,t['id'],t['ordinal'],'completed',t['ms']//1000,t['completed']//1000,
              max(0,t['completed']-t['ms']),t['first'],t['final'],t['offset'],t['end_ordinal'],t['end_offset'],t['id']) for t in turns])
        con.executemany('''INSERT INTO thread_items
            (thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,
             updated_at_ordinal,started_at_ms,completed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)''',items)
        if token(path.stat())!=seal:raise ValueError('Native writer changed during projection; retry pending')
        con.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?',(offset,ordinal,thread_id))
        con.commit();return True
    finally:con.close()


def publish(home, thread_id, rollout, *, rebuild=False, incremental=False):
    """Project (or re-project) a Context Hub mirror into the paginated reader.

    rebuild=True is for mirrors this tool owns after session sync appended
    new records: existing rows for the thread are replaced so the reader
    sees messages delivered after the original export.
    """
    history = Path(home) / 'thread_history_1.sqlite'
    state = Path(home) / 'state_5.sqlite'
    if not history.exists() or not state.exists():
        return False
    records = []
    offset = 0; first_ordinal=0; checkpoint=None
    if rebuild and incremental:
        probe=sqlite3.connect(history)
        try:
            checkpoint=probe.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?',(thread_id,)).fetchone()
            last=probe.execute('SELECT rollout_byte_offset,rollout_ordinal FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1',(thread_id,)).fetchone()
            if checkpoint and last and all(isinstance(v,int) and v>=0 for v in (*checkpoint,*last)) and last[0]<checkpoint[0]<=Path(rollout).stat().st_size and last[1]<checkpoint[1]:
                offset,first_ordinal=last
            else:checkpoint=None
        finally:probe.close()
    from ..sync_tail import token
    seal=token(Path(rollout).stat())
    with Path(rollout).open('rb') as source:
        header=json.loads(source.readline())
        if header.get('payload',{}).get('originator')!='context-hub':
            raise ValueError('Refusing to project a non-Context-Hub rollout')
        source.seek(offset)
        for ordinal, line in enumerate(source,first_ordinal):
            records.append((ordinal, offset, offset + len(line), json.loads(line)))
            offset += len(line)
    if token(Path(rollout).stat())!=seal:
        raise ValueError('Codex source changed while projecting; retry pending')
    if any(record.get('ordinal') != ordinal for ordinal, _, _, record in records):
        raise ValueError('Paginated rollout requires contiguous record ordinals')
    total_records=first_ordinal+len(records)
    turns, items = [], []
    current = None
    for ordinal, start, end, record in records:
        if record.get('type') != 'event_msg':
            continue
        payload = record.get('payload', {})
        kind = payload.get('type')
        if kind not in ('user_message', 'agent_message'):
            continue
        stamp = int(datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp() * 1000)
        if kind == 'user_message' or current is None:
            if current is not None:
                current['end_ordinal'], current['end_offset'] = ordinal, start
            tid = _id(thread_id, ordinal, 'turn')
            current = dict(id=tid, ordinal=ordinal, offset=start, ms=stamp,
                           completed=stamp, first=None, final=None,
                           end_ordinal=total_records, end_offset=offset)
            turns.append(current)
        iid = _id(thread_id, ordinal, 'item')
        if kind == 'user_message':
            item = {'type': 'userMessage', 'id': iid, 'content': [
                {'type': 'text', 'text': payload.get('message', ''), 'text_elements': []}]}
            current['first'] = iid
        else:
            item = {'type': 'agentMessage', 'id': iid, 'text': payload.get('message', ''),
                    'phase': payload.get('phase') or 'final_answer', 'memoryCitation': None,
                    'delivery': None, 'questions': None}
            current['final'] = iid
        current['completed'] = max(current['completed'], stamp)
        items.append((thread_id, current['id'], iid, ordinal, stamp,
                      json.dumps(item, ensure_ascii=False), item['type'], ordinal, stamp, stamp))
    con = sqlite3.connect(history, timeout=15)
    con.row_factory=sqlite3.Row
    try:
        # Fail before publishing if a client upgrade changes the required schema.
        required = {'thread_id','next_rollout_byte_offset','next_rollout_ordinal'}
        if not required <= {r[1] for r in con.execute('PRAGMA table_info(thread_history_projection_state)')}:
            raise ValueError('Unsupported Codex pagination schema')
        with con:
            if checkpoint:
                current_checkpoint=con.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?',(thread_id,)).fetchone()
                if not current_checkpoint or tuple(current_checkpoint)!=tuple(checkpoint):
                    raise ValueError('Native projection changed concurrently; retry pending')
            exists = con.execute('SELECT 1 FROM thread_items WHERE thread_id=? LIMIT 1',
                                 (thread_id,)).fetchone()
            if exists and not rebuild:
                raise ValueError('History already exists; export to a new mirror instead of overwriting')
            if exists:
                # Text replay must not delete tool/plan/artifact items already
                # produced by the native client. Reassign only their turn link
                # against the unchanged rollout ordinal boundaries.
                nontext=[dict(r) for r in con.execute("SELECT * FROM thread_items WHERE thread_id=? AND rollout_ordinal>=? AND item_type NOT IN ('userMessage','agentMessage')",(thread_id,first_ordinal))]
                con.execute('DELETE FROM thread_items WHERE thread_id=? AND rollout_ordinal>=?', (thread_id,first_ordinal))
                con.execute('DELETE FROM thread_turns WHERE thread_id=? AND rollout_ordinal>=?', (thread_id,first_ordinal))
            else:nontext=[]
            con.executemany('''INSERT INTO thread_items
                (thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,
                 updated_at_ordinal,started_at_ms,completed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)''', items)
            con.executemany('''INSERT INTO thread_turns
                (thread_id,turn_id,rollout_ordinal,status,started_at,completed_at,duration_ms,
                 first_user_item_id,final_agent_item_id,rollout_byte_offset,rollout_end_ordinal,
                 rollout_end_byte_offset,root_turn_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [(thread_id,t['id'],t['ordinal'],'completed',t['ms']//1000,t['completed']//1000,
                  max(0,t['completed']-t['ms']),t['first'],t['final'],t['offset'],
                  t['end_ordinal'],t['end_offset'],t['id']) for t in turns])
            for item in nontext:
                matching=[t for t in turns if t['ordinal']<=item['rollout_ordinal']<t['end_ordinal']]
                if not matching:
                    raise ValueError('Existing non-text native item has no verifiable turn; refusing destructive rebuild')
                item['turn_id']=matching[-1]['id']
                keys=list(item)
                con.execute('INSERT INTO thread_items('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',[item[k] for k in keys])
            con.execute('''INSERT OR REPLACE INTO thread_history_projection_state
                (thread_id,next_rollout_byte_offset,next_rollout_ordinal) VALUES (?,?,?)''',
                (thread_id,offset,total_records))
    finally:
        con.close()
    con = sqlite3.connect(state, timeout=15)
    try:
        with con:
            columns = {r[1] for r in con.execute('PRAGMA table_info(threads)')}
            if 'history_mode' in columns:
                con.execute("UPDATE threads SET history_mode='paginated' WHERE id=?", (thread_id,))
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()
    return True
