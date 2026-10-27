import concurrent.futures
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from context_hub.agents import Registry
from context_hub.agent_server import AgentServer
from context_hub.agent_gateway import Gateway
from context_hub.matters import Matters
from context_hub.model import envelope
from context_hub.store import Store


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        m=Matters(self.root)
        self.a=m.create('共享开发事项')['id'];self.b=m.create('私人事项')['id']
        m.relate(self.a,self.b,'不可泄漏的关联');m.close()
        self.registry=Registry(self.root)
        self.registry.enroll('muse',[self.a],True)
        self.registry.enroll('peer',[self.a],True)
        self.token=self.read_token('muse')
        self.muse=AgentServer(self.root,'muse',self.token)
        self.peer=AgentServer(self.root,'peer',self.read_token('peer'))

    def tearDown(self):self.temp.cleanup()

    def read_token(self,name):
        return json.loads(self.registry.credential_path(name).read_text())['token']

    def note(self,client=None,**extra):
        args={'matter':self.a,'kind':'opinion','topic':'检索方案','body':'需要保留分歧',**extra}
        return (client or self.muse).call('remember',args)

    def test_enrollment_fixed_identity_and_scoped_conversation(self):
        first=self.note(request_id='muse-view-1')
        second=self.note(self.peer,body='采用另一种方案',request_id='peer-view-1',reply_to=[first['note_id']])
        rows=self.muse.call('discussion',{'matter':self.a})['contributions']
        self.assertEqual({x['author'] for x in rows},{'agent:muse','agent:peer'})
        self.assertEqual(rows[0]['reply_to'],[first['note_id']])
        self.assertEqual(rows[0]['id'],second['note_id'])
        with self.assertRaises(ValueError):self.note(author='user')
        with self.assertRaises(ValueError):self.muse.call('create_matter',{'name':'越权'})
        with self.assertRaises(ValueError):self.muse.call('discussion',{'matter':self.b})
        result=self.muse.call('continue_matter',{'query':'共享开发事项'})
        self.assertEqual(result['related_matters'],[])
        self.assertEqual(result['unlinked_candidates'],[])
        self.assertNotIn('私人事项',json.dumps(self.muse.call('list_matters',{}),ensure_ascii=False))

    def test_retry_after_other_write_uses_durable_receipt(self):
        first=self.note(request_id='same-request')
        self.note(body='更新后的另一观点',request_id='next-request')
        restarted=AgentServer(self.root,'muse',self.token)
        retry=self.note(restarted,request_id='same-request')
        self.assertEqual(first['note_id'],retry['note_id']);self.assertTrue(retry['replayed'])
        self.assertEqual(len(self.muse.call('discussion',{'matter':self.a})['contributions']),2)
        with self.assertRaises(ValueError):self.note(body='伪造重试',request_id='same-request')

    def test_concurrent_retries_create_single_note(self):
        def submit(_):return self.note(AgentServer(self.root,'muse',self.token),request_id='concurrent')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(submit,range(8)))
        self.assertEqual(len({x['note_id'] for x in results}),1)
        self.assertEqual(len(self.muse.call('discussion',{'matter':self.a})['contributions']),1)

    def test_revocation_rotation_and_grants_apply_to_existing_client(self):
        self.registry.change('muse',allow_remember=False)
        with self.assertRaises(ValueError):self.note()
        self.assertTrue(self.muse.call('list_matters',{}))
        self.registry.change('muse',matters=[self.b])
        with self.assertRaises(ValueError):self.muse.call('read_notes',{'matter':self.a})
        self.registry.change('muse',rotate=True)
        with self.assertRaises(ValueError):self.muse.call('list_matters',{})
        fresh=AgentServer(self.root,'muse',self.read_token('muse'))
        self.assertEqual(fresh.call('list_matters',{})[0]['id'],self.b)
        self.registry.change('muse',revoke=True)
        with self.assertRaises(ValueError):fresh.handle({'jsonrpc':'2.0','id':1,'method':'initialize'})

    def test_expiry_malformed_policy_and_no_secret_in_bundle(self):
        public=self.registry.bundle('muse')
        self.assertNotIn(self.token,json.dumps(public))
        directory=Path(public['connection_directory'])
        self.assertNotIn(self.token,(directory/'INSTRUCTIONS.md').read_text())
        self.assertNotIn(self.token,(directory/'mcp.json').read_text())
        if os.name!='nt':self.assertEqual(self.registry.credential_path('muse').stat().st_mode&0o777,0o600)
        config=json.loads(self.registry.path.read_text());config['agents']['muse']['expires_at']=0
        self.registry.path.write_text(json.dumps(config))
        with self.assertRaises(ValueError):self.muse.call('list_matters',{})
        self.registry.path.write_text('{broken')
        with self.assertRaises(ValueError):self.registry.authenticate(self.token)

    def test_cross_matter_reply_and_cursor_are_refused(self):
        m=Matters(self.root)
        foreign=m.remember(self.b,'user','memory','隐私','不能引用')['note_id'];m.close()
        with self.assertRaises(ValueError):self.note(reply_to=[foreign])
        with self.assertRaises(ValueError):self.muse.call('discussion',{'matter':self.a,'before':foreign})

    def test_discussion_cursor_survives_new_arrivals(self):
        for i in range(6):self.note(topic='观点'+str(i))
        first=self.muse.call('discussion',{'matter':self.a,'limit':3})
        self.note(topic='新观点')
        second=self.peer.call('discussion',{'matter':self.a,'limit':3,'before':first['next_before']})
        self.assertEqual(len(second['contributions']),3)
        self.assertFalse({x['id'] for x in first['contributions']}&{x['id'] for x in second['contributions']})
        self.assertIsNone(second['next_before'])

    def test_search_includes_notes_from_another_agent(self):
        self.note(self.peer,topic='检索改进',body='独立提议：双层索引')
        result=self.muse.call('search_context',{'matter':self.a,'query':'双层索引'})
        self.assertEqual(result['notes'][0]['author'],'agent:peer')
        with self.assertRaises(ValueError):self.muse.call('search_context',{'query':'任意检索'})

    def test_evidence_must_belong_to_contribution_matter(self):
        store=Store(self.root)
        with store.db:
            store.ingest(envelope('codex','private','m1',{},'/private','user','私人证据',kind='text'),{})
        store.close();m=Matters(self.root);m.index()
        rid=m.db.execute('SELECT record FROM docs LIMIT 1').fetchone()[0]
        m.link(self.b,'record',str(rid),'仅私人事项');m.close()
        self.registry.change('muse',matters=[self.a,self.b])
        with self.assertRaises(ValueError):self.note(evidence=[rid])
        with self.assertRaises(ValueError):self.peer.call('read_record',{'record':rid})

    def test_stdio_reloads_policy_and_uses_fixed_author(self):
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'}]
        process=subprocess.run([sys.executable,'-m','context_hub.agent_client','--data-dir',str(self.root),
            '--agent','muse'],input=''.join(json.dumps(x)+'\n' for x in requests),capture_output=True,text=True,timeout=20)
        self.assertEqual(process.returncode,0,process.stderr)
        replies=[json.loads(x) for x in process.stdout.splitlines()]
        self.assertIn('agent:muse',replies[0]['result']['instructions'])
        tools={x['name'] for x in replies[1]['result']['tools']}
        self.assertIn('discussion',tools);self.assertNotIn('create_matter',tools)
        self.assertNotIn(self.token,process.stdout+process.stderr)

    def test_owner_cli_add_and_bundle_do_not_print_credentials(self):
        result=subprocess.run([sys.executable,'-m','context_hub','--data-dir',str(self.root),
            'agents','add','third','--matter',self.a,'--write'],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        metadata=json.loads(result.stdout)
        self.assertEqual(metadata['author'],'agent:third')
        self.assertFalse(metadata['connection_verified'])
        self.assertNotIn(self.read_token('third'),result.stdout+result.stderr)


class GatewayTests(unittest.TestCase):
    read_token=AgentTests.read_token
    def setUp(self):
        AgentTests.setUp(self)
        self.gateway=Gateway(self.root,0)
        self.thread=threading.Thread(target=self.gateway.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.gateway.shutdown();self.gateway.server_close();self.thread.join()
        AgentTests.tearDown(self)

    def request(self,payload=None,token=True,headers=None,path='/mcp',method='POST'):
        connection=http.client.HTTPConnection('127.0.0.1',self.gateway.server_port,timeout=10)
        h={'Content-Type':'application/json',**(headers or {})}
        if token:h['Authorization']='Bearer '+self.token
        connection.request(method,path,body=json.dumps(payload or {}).encode(),headers=h)
        response=connection.getresponse();raw=response.read()
        result=(response.status,json.loads(raw) if raw else None,dict(response.headers))
        connection.close();return result

    def test_http_auth_origin_host_and_stateless_protocol(self):
        init={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}}
        self.assertEqual(self.request(init,token=False)[0],401)
        self.assertEqual(self.request(init,headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request(init,headers={'Host':'evil.example'})[0],403)
        code,result,headers=self.request(init)
        self.assertEqual(code,200);self.assertEqual(result['result']['serverInfo']['name'],'context-hub-agent')
        self.assertNotIn('Mcp-Session-Id',headers)
        self.assertEqual(self.request({'jsonrpc':'2.0','method':'notifications/initialized'})[0],202)
        call={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'remember','arguments':
            {'matter':self.a,'kind':'digest','topic':'协作','body':'保留双方判断','request_id':'http-retry'}}}
        one=self.request(call)[1]['result'];two=self.request(call)[1]['result']
        self.assertFalse(one['isError']);self.assertFalse(two['isError'])
        self.assertEqual(json.loads(one['content'][0]['text'])['note_id'],json.loads(two['content'][0]['text'])['note_id'])
        self.registry.change('muse',revoke=True)
        self.assertEqual(self.request(call)[0],401)

    def test_http_has_no_owner_tools_and_limits_input(self):
        call={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'create_matter','arguments':{'name':'bad'}}}
        result=self.request(call)[1]
        self.assertTrue(result['result']['isError'])
        self.assertEqual(self.request(method='GET')[0],405)
        self.assertEqual(self.request(method='GET',path='/healthz')[0],200)
        self.assertEqual(self.request(headers={'MCP-Protocol-Version':'2099-01-01'})[0],400)
        self.assertEqual(self.request(headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request(payload=['batch'])[0],400)
        with self.assertRaises(ValueError):Gateway(self.root,0,'http://unsafe.example')


if __name__=='__main__':unittest.main()
