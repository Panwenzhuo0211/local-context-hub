"""Durable refresh requests for explicitly paired, desktop-owned native relays.

No model submission, runtime spawning or conversation opening occurs here.
The relay must already be attached through the user's desktop launcher.
"""
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path
from .store import atomic

SCHEMA='''CREATE TABLE IF NOT EXISTS live_refresh (
 pair_id TEXT NOT NULL, adapter TEXT NOT NULL, revision TEXT NOT NULL,
 session_id TEXT NOT NULL, refs TEXT NOT NULL, status TEXT NOT NULL,
 updated REAL NOT NULL, error TEXT, PRIMARY KEY(pair_id,adapter,revision));'''


def directory():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'ContextHubApp/runtime/connections'


def publish_allowlist(root,state,config):
    pairs=[{'id':r['id'],'codex':r['codex_id'],'zcode':r['zcode_id'],'enabled':bool(r['enabled']),
            'homes':config['sources']} for r in state.execute('SELECT * FROM pairs')]
    doc=json.dumps({'schema':1,'pairs':pairs},ensure_ascii=False,sort_keys=True)
    path=Path(root)/'runtime-pairs.json'
    if not path.exists() or path.read_text(encoding='utf-8')!=doc:atomic(path,doc)


def enqueue(state,pair,adapter,refs,watermark):
    refs=sorted(set(refs))
    if not refs:return
    for start in range(0,len(refs),500):
        selected=refs[start:start+500];raw=json.dumps(selected,separators=(',',':'))
        revision=hashlib.sha256((str(watermark)+raw).encode()).hexdigest()
        state.execute('INSERT OR IGNORE INTO live_refresh VALUES (?,?,?,?,?,?,?,NULL)',
            (pair['id'],adapter,revision,pair[adapter+'_id'],raw,'pending',time.time()))


def endpoints(config,adapter,sid):
    folder=directory()
    if not folder.exists():return []
    expected=Path(config['sources'][adapter]).resolve()
    result=[]
    for path in folder.glob(adapter+'-*.json'):
        try:
            if path.is_symlink() or path.stat().st_size>65536:continue
            item=json.loads(path.read_text(encoding='utf-8'))
            if item.get('schema')!=1 or item.get('adapter')!=adapter:continue
            if Path(item['home']).resolve()!=expected or time.time()-item['heartbeat']>15:continue
            if not isinstance(item['port'],int) or not 1<=item['port']<=65535:continue
            token=item.get('token','')
            if len(token)!=64 or any(c not in '0123456789abcdef' for c in token):continue
            result.append(item)
        except (OSError,ValueError,KeyError,TypeError):continue
    return sorted(result,key=lambda e:(sid in e.get('sessions',[]),e.get('heartbeat',0)),reverse=True)


def dispatch(root,state,config):
    from . import runtime_delivery
    runtime_delivery.publish(root,state)
    publish_allowlist(root,state,config)
    rows=state.execute('SELECT r.* FROM live_refresh r JOIN pairs p ON p.id=r.pair_id '
        "WHERE p.enabled=1 AND r.status NOT IN ('notifications_sent','superseded') ORDER BY r.updated LIMIT 8").fetchall()
    results=[]
    for row in rows:
        found=endpoints(config,row['adapter'],row['session_id']);error=None
        status='waiting_runtime_activation'
        if found:
            endpoint=found[0]
            status='waiting_session_open'
            if endpoint.get('ready') and row['session_id'] in endpoint.get('sessions',[]):
                body=json.dumps({'session':row['session_id'],'revision':row['revision'],'refs':json.loads(row['refs'])}).encode()
                request=urllib.request.Request('http://127.0.0.1:'+str(endpoint['port'])+'/refresh',data=body,
                    headers={'Authorization':'Bearer '+endpoint['token'],'Content-Type':'application/json'},method='POST')
                try:
                    # Never send local refresh requests through an environment proxy.
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=.5) as reply:
                        data=json.loads(reply.read(65536))
                    status=data.get('status','retry_pending')
                    if status not in ('notifications_sent','refreshing','waiting_idle','waiting_session_open','paused','retry_pending'):
                        status='retry_pending'
                    error=data.get('error')
                except (OSError,ValueError):status='retry_pending';error='runtime_connection_unavailable'
        with state:
            if status=='notifications_sent':runtime_delivery.acknowledged(state,row['pair_id'],json.loads(row['refs']))
            state.execute('UPDATE live_refresh SET status=?,updated=?,error=? WHERE pair_id=? AND adapter=? AND revision=?',
                (status,time.time(),error,row['pair_id'],row['adapter'],row['revision']))
        results.append({'pair':row['pair_id'],'adapter':row['adapter'],'status':status})
    return results


def summary(state):
    tables={r[0] for r in state.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'live_refresh' not in tables:return []
    return [dict(r) for r in state.execute('SELECT pair_id,adapter,status,count(*) AS count FROM live_refresh GROUP BY pair_id,adapter,status')]
