"""Repair a stale native text projection from verified native completion events.

Never infer a native item/turn ID from text or an obsolete byte checkpoint.
Existing items, including tools and interrupted turns from older segments,
are preserved. Unknown unprojected item variants fail closed.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from ..sync_tail import token


def _ms(row):
    return int(datetime.fromisoformat(row['timestamp'].replace('Z','+00:00')).timestamp()*1000)


def _item(wire):
    kind=wire.get('type');iid=wire.get('id')
    if not isinstance(iid,str) or not iid:raise ValueError('Native completed item has no stable ID')
    if kind=='UserMessage':
        # Native user content already uses the public input-content schema.
        result={'type':'userMessage','id':iid,'content':wire.get('content',[])}
        if wire.get('client_id'):result['clientId']=wire['client_id']
        return result
    if kind=='AgentMessage':
        blocks=wire.get('content') or []
        if any(b.get('type')!='Text' or not isinstance(b.get('text'),str) for b in blocks):
            raise ValueError('Unsupported native agent content; projection left unchanged')
        return {'type':'agentMessage','id':iid,'text':''.join(b['text'] for b in blocks),
                'phase':wire.get('phase') or 'final_answer','memoryCitation':None,
                'delivery':None,'questions':None}
    raise ValueError('Unsupported unprojected native item: '+str(kind))


def reconcile(home, thread_id, rollout, tail):
    """Catch up only from complete, correctly attributed native turn events.

    A completed writer can leave the projection at an old file's byte offset.
    Read the enrolled current segment from its header, check global ordinals,
    and publish source-authored IDs in one guarded transaction. Do not change
    or append the source file. True incomplete turns remain queued.
    """
    history=Path(home)/'thread_history_1.sqlite';path=Path(rollout)
    if not history.exists():return False
    if token(path.stat())!=tail.seal:raise ValueError('Native source changed before projection repair; retry')
    con=sqlite3.connect(history,timeout=5);con.row_factory=sqlite3.Row
    try:
        con.execute('BEGIN IMMEDIATE')
        checkpoint=con.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?',(thread_id,)).fetchone()
        if not checkpoint:return False
        existing={(r['turn_id'],r['item_id']):dict(r) for r in con.execute('SELECT * FROM thread_items WHERE thread_id=?',(thread_id,))}
        turns={};items={};offset=0;number=None
        with path.open('rb') as source:
            for raw in source:
                if not raw.endswith(b'\n'):return False
                end=offset+len(raw)
                if number is None:
                    header=json.loads(raw)
                    if header.get('type')!='session_meta' or header.get('payload',{}).get('id')!=thread_id:
                        raise ValueError('Native projection repair identity mismatch')
                    number=header.get('ordinal')
                    if not isinstance(number,int):raise ValueError('Native projection repair requires explicit ordinals')
                # Full ordinal validation was already performed by the verified
                # tail reader. Decode only events, not huge model/tool payloads.
                if b'"event_msg"' in raw:
                    row=json.loads(raw);p=row.get('payload') or {};kind=p.get('type')
                    if row.get('ordinal')!=number:raise ValueError('Native projection repair ordinal mismatch')
                    if row.get('type')=='event_msg' and kind in ('task_started','turn_started'):
                        tid=p.get('turn_id');root=p.get('root_turn_id') or tid
                        if not tid or tid in turns:raise ValueError('Ambiguous native turn identity')
                        turns[tid]={'id':tid,'root':root,'ordinal':number,'offset':offset,
                            'started':p.get('started_at') or _ms(row)//1000,'status':None,
                            'first':None,'final':None,'completed':None,'duration':None,'end_ordinal':None,'end_offset':None}
                    elif row.get('type')=='event_msg' and kind=='item_completed':
                        tid=p.get('turn_id');wire=p.get('item') or {};iid=wire.get('id')
                        if p.get('thread_id') not in (None,thread_id):raise ValueError('Native item belongs to another thread')
                        if tid not in turns:return False
                        if (tid,iid) in existing:
                            # Preserve exact native tool/plan/artifact JSON.
                            item=json.loads(existing[(tid,iid)]['item_json'])
                        else:item=_item(wire)
                        if item['type']=='userMessage':turns[tid]['first']=turns[tid]['first'] or iid
                        if item['type']=='agentMessage' and item.get('phase') in ('final','final_answer'):
                            turns[tid]['final']=iid
                        started=p.get('started_at_ms') or _ms(row);completed=p.get('completed_at_ms') or started
                        items[(tid,iid)]=(thread_id,tid,iid,number,started,json.dumps(item,ensure_ascii=False),item['type'],number,started,completed)
                    elif row.get('type')=='event_msg' and kind in ('task_complete','turn_complete','turn_aborted'):
                        tid=p.get('turn_id') or p.get('root_turn_id')
                        if tid not in turns:return False
                        turns[tid].update(status='interrupted' if kind=='turn_aborted' else 'completed',
                            completed=p.get('completed_at') or _ms(row)//1000,duration=p.get('duration_ms'),
                            end_ordinal=number+1,end_offset=end)
                number+=1;offset=end
        if not turns or any(t['status'] is None for t in turns.values()):return False
        expected=tail.ordinal+tail.data.count(b'\n')
        if number!=expected or offset!=tail.seal[2] or checkpoint['next_rollout_ordinal']>number:
            return False
        if token(path.stat())!=tail.seal:raise ValueError('Native source changed while repairing projection; retry')
        for t in turns.values():
            old=con.execute('SELECT 1 FROM thread_turns WHERE thread_id=? AND turn_id=?',(thread_id,t['id'])).fetchone()
            if old:
                con.execute('UPDATE thread_turns SET status=?,completed_at=?,duration_ms=?,first_user_item_id=?,final_agent_item_id=?,rollout_end_ordinal=?,rollout_end_byte_offset=? WHERE thread_id=? AND turn_id=?',
                    (t['status'],t['completed'],t['duration'],t['first'],t['final'],t['end_ordinal'],t['end_offset'],thread_id,t['id']))
            else:
                con.execute('''INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at,completed_at,duration_ms,
                    first_user_item_id,final_agent_item_id,rollout_byte_offset,rollout_end_ordinal,rollout_end_byte_offset,root_turn_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (thread_id,t['id'],t['ordinal'],t['status'],t['started'],t['completed'],t['duration'],t['first'],t['final'],t['offset'],t['end_ordinal'],t['end_offset'],t['root']))
        for key,row in items.items():
            if key in existing:continue
            con.execute('''INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,
                updated_at_ordinal,started_at_ms,completed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)''',row)
        if token(path.stat())!=tail.seal:raise ValueError('Native writer changed before projection commit; retry')
        con.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?',(offset,number,thread_id))
        con.commit();return True
    finally:con.close()
