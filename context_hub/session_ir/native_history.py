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


def _id(thread, ordinal, suffix):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'context-hub:{thread}:{ordinal}:{suffix}'))


def publish(home, thread_id, rollout):
    history = Path(home) / 'thread_history_1.sqlite'
    state = Path(home) / 'state_5.sqlite'
    if not history.exists() or not state.exists():
        return False
    records = []
    offset = 0
    with Path(rollout).open('rb') as source:
        for ordinal, line in enumerate(source):
            records.append((ordinal, offset, offset + len(line), json.loads(line)))
            offset += len(line)
    if any(record.get('ordinal') != ordinal for ordinal, _, _, record in records):
        raise ValueError('Paginated rollout requires contiguous record ordinals')
    if not records or records[0][3].get('payload', {}).get('originator') != 'context-hub':
        raise ValueError('Refusing to project a non-Context-Hub rollout')
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
                           end_ordinal=len(records), end_offset=offset)
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
    try:
        # Fail before publishing if a client upgrade changes the required schema.
        required = {'thread_id','next_rollout_byte_offset','next_rollout_ordinal'}
        if not required <= {r[1] for r in con.execute('PRAGMA table_info(thread_history_projection_state)')}:
            raise ValueError('Unsupported Codex pagination schema')
        with con:
            if con.execute('SELECT 1 FROM thread_items WHERE thread_id=? LIMIT 1', (thread_id,)).fetchone():
                raise ValueError('History already exists; export to a new mirror instead of overwriting')
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
            con.execute('''INSERT INTO thread_history_projection_state
                (thread_id,next_rollout_byte_offset,next_rollout_ordinal) VALUES (?,?,?)''',
                (thread_id,offset,len(records)))
    finally:
        con.close()
    con = sqlite3.connect(state, timeout=15)
    try:
        with con:
            con.execute("UPDATE threads SET history_mode='paginated' WHERE id=?", (thread_id,))
    finally:
        con.close()
    return True
