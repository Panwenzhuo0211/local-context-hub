"""Native v2 presentation envelopes for attributed, copied history.

Legacy user_message/agent_message events are insufficient for the v2 resume
reader. Completed native items must belong to explicit, completed native turns.
This serializes historical records only; it never submits a prompt or model job.
"""
import json
import uuid
from datetime import datetime
from .live import MARKER,encode_codex
from ..sync_tail import token


def _id(pair,event,suffix):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'context-hub-native:'+pair+':'+event+':'+suffix))


def envelope(text_rows, *, pair,event,thread_id):
    if not text_rows:return []
    tid=_id(pair,event,'turn');stamp=text_rows[0]['timestamp']
    ms=int(datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()*1000)
    marker={'pair':pair,'event':event,'native_view':True,'copied_history':True}
    def row(payload):return {'timestamp':stamp,'type':'event_msg','payload':payload,MARKER:dict(marker)}
    rows=[row({'type':'task_started','turn_id':tid,'root_turn_id':tid,'started_at':ms//1000,
        'turn_attribution':{'turn_id':tid,'root_turn_id':tid,'turn_trigger':'composer',
                            'parent_turn_id':None,'initiating_agent_path':None}})]
    final=None
    for index,source in enumerate(text_rows):
        p=source['payload'];kind=p['type'];iid=_id(pair,event,'item:'+str(index))
        if kind=='user_message':
            item={'type':'UserMessage','id':iid,'client_id':_id(pair,event,'client:'+str(index)),
                  'content':[{'type':'text','text':p.get('message',''),'text_elements':[]}]}
        else:
            text=p.get('message','');final=text
            item={'type':'AgentMessage','id':iid,'content':[{'type':'Text','text':text}],
                  'phase':p.get('phase') or 'final_answer'}
        rows.append(row({'type':'item_completed','turn_id':tid,'thread_id':thread_id,'item':item,
                         'started_at_ms':ms,'completed_at_ms':ms}))
    rows.append(row({'type':'task_complete','turn_id':tid,'root_turn_id':tid,
        'started_at':ms//1000,'completed_at':ms//1000,'duration_ms':0,'last_agent_message':final}))
    return rows


def encode(moments, *, pair,event,cwd,thread_id):
    legacy=encode_codex(moments,pair=pair,event=event,cwd=cwd)
    text_rows=[r for r in legacy if r.get('type')=='event_msg' and
               r.get('payload',{}).get('type') in ('user_message','agent_message')]
    view=envelope(text_rows,pair=pair,event=event,thread_id=thread_id)
    retained=[r for r in legacy if r not in text_rows]
    if not view:return retained
    tid=_id(pair,event,'turn')
    for row in retained:
        payload=row.get('payload') or {}
        if row.get('type')=='response_item' and payload.get('type')=='message':
            payload['id']=payload.get('id') or _id(pair,event,'model-message')
            payload['internal_chat_message_metadata_passthrough']={
                'turn_id':tid,'content_item_kinds':['user.text' if payload.get('role')=='user' else 'unknown']}
    return [view[0],*retained,*view[1:]]


_UPGRADES={}


def legacy_views(path,pair,tail):
    """Append missing native views of already-delivered legacy text once.

    Model response items are not repeated. Existing raw history stays byte-for-
    byte intact. Events with an existing native item are never upgraded again.
    """
    key=(str(path),pair,tail.seal)
    if key in _UPGRADES:return _UPGRADES[key]
    groups={};native=set()
    if token(path.stat())!=tail.seal:raise ValueError('Source changed before native view upgrade')
    with path.open('rb') as source:
        header=json.loads(source.readline());thread_id=header.get('payload',{}).get('id')
        if not thread_id:raise ValueError('Native view upgrade source has no identity')
        for raw in source:
            if not raw.endswith(b'\n'):return []
            if MARKER.encode() not in raw:continue
            row=json.loads(raw);marker=row.get(MARKER) or {}
            if marker.get('pair')!=pair:continue
            p=row.get('payload') or {};event=marker.get('event')
            if row.get('type')=='event_msg' and p.get('type')=='item_completed':native.add(event)
            elif row.get('type')=='event_msg' and p.get('type') in ('user_message','agent_message'):
                groups.setdefault(event,[]).append(row)
    if token(path.stat())!=tail.seal:raise ValueError('Source changed during native view upgrade')
    result=[]
    for event,rows in groups.items():
        if event and event not in native:result.extend(envelope(rows,pair=pair,event=event,thread_id=thread_id))
    if len(_UPGRADES)>=8:_UPGRADES.pop(next(iter(_UPGRADES)))
    _UPGRADES[key]=result
    return result


def project(con,thread_id,rows):
    """Project the same source-authored native IDs without rebuilding old turns."""
    from .native_reconcile import _item
    turns={};items=[]
    for ordinal,start,end,row in rows:
        if row.get('type')!='event_msg':continue
        p=row.get('payload') or {};kind=p.get('type');tid=p.get('turn_id')
        if kind=='task_started':
            turns[tid]={'id':tid,'ordinal':ordinal,'start':start,'started':p['started_at'],
                        'first':None,'final':None,'completed':None,'end_ordinal':None,'end':None}
        elif kind=='item_completed':
            if tid not in turns:raise ValueError('Native sync item has no attributed turn')
            item=_item(p['item']);iid=item['id'];ms=p['started_at_ms'];done=p['completed_at_ms']
            if item['type']=='userMessage':turns[tid]['first']=iid
            else:turns[tid]['final']=iid
            items.append((thread_id,tid,iid,ordinal,ms,json.dumps(item,ensure_ascii=False),item['type'],ordinal,ms,done))
        elif kind=='task_complete':
            if tid not in turns:raise ValueError('Native sync completion has no attributed turn')
            turns[tid].update(completed=p['completed_at'],end_ordinal=ordinal+1,end=end)
    if not turns:return False
    if any(t['completed'] is None for t in turns.values()):raise ValueError('Incomplete native sync envelope')
    for t in turns.values():
        con.execute('''INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at,completed_at,duration_ms,
            first_user_item_id,final_agent_item_id,rollout_byte_offset,rollout_end_ordinal,rollout_end_byte_offset,root_turn_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (thread_id,t['id'],t['ordinal'],'completed',t['started'],t['completed'],0,t['first'],t['final'],t['start'],t['end_ordinal'],t['end'],t['id']))
    con.executemany('''INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,
        updated_at_ordinal,started_at_ms,completed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)''',items)
    return True
