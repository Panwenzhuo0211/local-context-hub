import json
import tempfile
import unittest
from pathlib import Path
from context_hub.matters import Matters
from context_hub.chatgpt_server import ChatGPTServer

class ChatGPTTests(unittest.TestCase):
    def test_scoped_tools_and_authorship(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);m=Matters(root);a=m.create('开放事项')['id'];m.create('私人事项');m.close()
            access=root/'access.json';access.write_text(json.dumps({'matters':[a],'allow_remember':True}))
            s=ChatGPTServer(root,access)
            self.assertEqual(len(s.call('list_matters',{})),1)
            status=s.call('context_status',{})
            self.assertTrue(status['local_sources_auto_archive'])
            self.assertFalse(status['chatgpt_full_transcript_auto_archive'])
            self.assertEqual(status['chatgpt_memory_mode'],'tool_assisted')
            with self.assertRaises(ValueError):s.call('read_notes',{'matter':'私人事项'})
            with self.assertRaises(ValueError):s.call('search_context',{'query':'私人'})
            with self.assertRaises(ValueError):s.call('read_record',{'record':1})
            with self.assertRaises(ValueError):s.call('register_memory_source',{})
            s.call('remember',{'matter':a,'kind':'opinion','topic':'技术判断','body':'独立观点'})
            self.assertEqual(s.call('read_notes',{'matter':a})['notes'][0]['author'],'chatgpt')
            self.assertNotIn('私人事项',json.dumps(s.call('continue_matter',{'query':'未知'}),ensure_ascii=False))
            access.write_text(json.dumps({'matters':[a],'allow_remember':False}))
            with self.assertRaises(ValueError):s.call('remember',{'matter':a,'kind':'memory','topic':'a','body':'b'})
            s.handle({'jsonrpc':'2.0','id':1,'method':'initialize'})
            listed=s.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})
            self.assertNotIn('link_context',[x['name'] for x in listed['result']['tools']])
