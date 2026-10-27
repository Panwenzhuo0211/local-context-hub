import json
import os
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from context_hub import live_refresh
from context_hub.session_sync import _db

NODE=shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
REPO=Path(__file__).resolve().parents[1]

class RefreshTests(unittest.TestCase):
    def test_queue_is_opt_in_durable_and_status_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=_db(root)
            state.execute("INSERT INTO pairs(id,zcode_id,codex_id,path,offset,prefix,enabled,created,backup) VALUES('p','z','c','x',0,'',1,0,'b')")
            pair=state.execute('SELECT * FROM pairs').fetchone()
            config={'sources':{'codex':str(root/'c'),'zcode':str(root/'z')}}
            live_refresh.enqueue(state,pair,'codex',['x','x'],1)
            live_refresh.enqueue(state,pair,'codex',['x'],1);state.commit()
            self.assertEqual(len(live_refresh.summary(state)),1)
            with patch.object(live_refresh,'directory',return_value=root/'connections'):
                self.assertEqual(live_refresh.dispatch(root,state,config)[0]['status'],'waiting_runtime_activation')
                state.execute('UPDATE pairs SET enabled=0');state.commit()
                self.assertEqual(live_refresh.dispatch(root,state,config),[])
            self.assertFalse(json.loads((root/'runtime-pairs.json').read_text())['pairs'][0]['enabled'])
            state.close()

    def run_relay(self,adapter,stale=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);connections=root/'connections';data=root/'data.json';log=root/'log'
            item={'id':'copied','type':'agentMessage','text':'同步测试'}
            part={'partId':'part','messageId':'copied','sessionId':'fixture','type':'text','text':'同步测试'}
            snapshot={'session':{'sessionId':'fixture','traceId':'trace','workspace':{'workspacePath':str(root)}},
                      'projection':{'status':'idle'},'messages':[]}
            content={'turns':[],'snapshot':snapshot,'stalePage':stale};data.write_text(json.dumps(content))
            (root/'runtime-pairs.json').write_text(json.dumps({'schema':1,'pairs':[{'enabled':True,adapter:'fixture','homes':{adapter:str(root)}}]}))
            proc=subprocess.Popen([NODE,str(REPO/'runtime/stdio-bridge.cjs'),'--adapter',adapter,'--hub',str(root),
                '--runtime',str(connections),'--home',str(root),'--',NODE,str(REPO/'tests/fixtures/refresh_backend.cjs'),str(data),str(log),adapter,'app-server'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
            out=queue.Queue()
            reader=threading.Thread(target=lambda:[out.put(json.loads(line)) for line in proc.stdout],daemon=True);reader.start()
            def send(method,params=None,id=1):
                proc.stdin.write(json.dumps({'id':id,'method':method,'params':params or {}})+'\n');proc.stdin.flush()
                reply=out.get(timeout=5);self.assertEqual(reply['id'],id)
            def http(body,token=None,origin=False):
                headers={'Authorization':'Bearer '+(token or endpoint['token']),'Content-Type':'application/json'}
                if origin:headers['Origin']='https://example.com'
                req=urllib.request.Request('http://127.0.0.1:'+str(endpoint['port'])+'/refresh',json.dumps(body).encode(),headers)
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=3) as res:return json.load(res)
            try:
                send('initialize');send('thread/resume' if adapter=='codex' else 'session/resume',{'threadId' if adapter=='codex' else 'sessionId':'fixture'},2)
                deadline=time.monotonic()+5;endpoint=None
                while time.monotonic()<deadline:
                    paths=list(connections.glob('*.json'))
                    if paths:
                        endpoint=json.loads(paths[0].read_text());break
                    time.sleep(.02)
                self.assertIsNotNone(endpoint)
                body={'session':'fixture','revision':'v1','refs':['copied']}
                for token,origin in [('0'*64,False),(None,True)]:
                    with self.assertRaises(urllib.error.HTTPError) as e:http(body,token,origin)
                    self.assertEqual(e.exception.code,403)
                    e.exception.close()
                with self.assertRaises(urllib.error.HTTPError) as e:http({**body,'session':'unpaired'})
                e.exception.close()
                send('fixture/busy',id=3);out.get(timeout=5)
                self.assertEqual(http(body)['status'],'waiting_idle')
                send('fixture/idle',id=4);out.get(timeout=5)
                content['turns']=[{'id':'turn','status':'completed','items':[item]}]
                snapshot['messages']=[{'info':{'messageId':'copied'},'parts':[part]}]
                data.write_text(json.dumps(content))
                self.assertEqual(http(body)['status'],'refreshing')
                received=[out.get(timeout=5),out.get(timeout=5)]
                self.assertEqual([r['method'] for r in received],['item/completed','turn/completed'] if adapter=='codex' else ['session/event','session/event'])
                if adapter=='zcode':self.assertEqual(received[1]['params']['payload']['part'],part)
                for _ in range(30):
                    result=http(body)
                    if result['status']=='notifications_sent':break
                    time.sleep(.01)
                self.assertEqual(result['status'],'notifications_sent')
                self.assertFalse(result['model_started']);self.assertEqual(result['sent'],1)
                self.assertEqual(http(body)['sent'],1)
                self.assertTrue(out.empty())
                methods=[json.loads(r)['method'] for r in log.read_text().splitlines()]
                self.assertFalse(set(methods)&{'turn/start','session/send','thread/inject_items'})
            finally:
                proc.stdin.close()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
                reader.join(timeout=2);proc.stdout.close();proc.stderr.close()

    @unittest.skipUnless(Path(NODE).exists() or shutil.which(NODE),'Node required')
    def test_codex_relay(self):self.run_relay('codex')

    @unittest.skipUnless(Path(NODE).exists() or shutil.which(NODE),'Node required')
    def test_zcode_relay(self):self.run_relay('zcode')

    @unittest.skipUnless(Path(NODE).exists() or shutil.which(NODE),'Node required')
    def test_codex_stale_page_uses_native_full_reader(self):self.run_relay('codex',stale=True)
