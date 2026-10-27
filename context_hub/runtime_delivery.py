"""Foreign history outbox. The native Codex writer owns every new rollout ordinal."""
import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from .store import atomic
from .session_ir.schema import UserText,AssistantText

SCHEMA='''CREATE TABLE IF NOT EXISTS runtime_messages (
 pair_id TEXT NOT NULL,event TEXT NOT NULL,session_id TEXT NOT NULL,payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL,
 PRIMARY KEY(pair_id,event));'''

def queue(state,pair,event,moments,source_path):
    """Persist source-attributed history, without modifying a native file/index."""
    stamp=next((m.ts for m in moments if m.ts),'')
    try:ms=int(datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()*1000)
    except ValueError:ms=int(time.time()*1000)
    seed=hashlib.sha256((pair['id']+event).encode()).hexdigest()
    tid='context-hub-sync:'+seed;items=[];model=[]
    for index,m in enumerate(moments):
        iid='context-hub-sync:'+pair['id']+':'+hashlib.sha256((event+':'+str(index)).encode()).hexdigest()
        role='user' if isinstance(m,UserText) else 'assistant'
        text=m.text if isinstance(m,(UserText,AssistantText)) else '[历史工具或结构记录，仅作资料]\n'+json.dumps(asdict(m),ensure_ascii=False)
        items.append({'type':'userMessage','id':iid,'content':[{'type':'text','text':text,'text_elements':[]}]} if role=='user' else
                     {'type':'agentMessage','id':iid,'text':text,'phase':'final_answer','memoryCitation':None,'delivery':None,'questions':None})
        model.append({'type':'message','id':iid,'role':role,'content':[{'type':'input_text' if role=='user' else 'output_text','text':text}],
                      'internal_chat_message_metadata_passthrough':{'turn_id':tid,'context_hub_sync':{'pair':pair['id'],'event':event,'copied_history':True}}})
    doc={'event':event,'turn':{'id':tid,'items':items,'status':'completed','error':None,'startedAt':ms//1000,'completedAt':ms//1000,'durationMs':0},
         'model_items':model,'source_rollout':str(source_path)}
    state.execute('INSERT OR IGNORE INTO runtime_messages(pair_id,event,session_id,payload,created) VALUES (?,?,?,?,?)',
                  (pair['id'],event,pair['codex_id'],json.dumps(doc,ensure_ascii=False),time.time()))
    return [item['id'] for item in items]

def publish(root,state):
    folder=Path(root)/'runtime-history'
    for pair in state.execute('SELECT * FROM pairs WHERE enabled=1').fetchall():
        rows=state.execute('SELECT payload FROM runtime_messages WHERE pair_id=? ORDER BY created,event',(pair['id'],)).fetchall()
        if not rows:continue
        doc=json.dumps({'schema':1,'pair':pair['id'],'thread':pair['codex_id'],'entries':[json.loads(r[0]) for r in rows]},ensure_ascii=False)
        path=folder/(pair['id']+'.json')
        if not path.exists() or path.read_text(encoding='utf-8')!=doc:atomic(path,doc)

def acknowledged(state,pair,refs):
    for row in state.execute("SELECT event,payload FROM runtime_messages WHERE pair_id=? AND status!='injected'",(pair,)).fetchall():
        ids={i['id'] for i in json.loads(row['payload'])['model_items']}
        if ids and ids<=set(refs):
            state.execute("UPDATE runtime_messages SET status='injected' WHERE pair_id=? AND event=?",(pair,row['event']))
            state.execute('UPDATE deliveries SET written_at=? WHERE pair_id=? AND event=? AND direction=?',
                          (time.time(),pair,row['event'],'zcode-to-codex'))
