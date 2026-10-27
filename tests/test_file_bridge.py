import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from context_hub.agents import Registry
from context_hub.file_bridge import FileBroker,enable,MAX_REQUEST
from context_hub.matters import Matters


class FileBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        m=Matters(self.root);self.mid=m.create('公开测试事项')['id'];self.private=m.create('私人事项')['id'];m.close()
        self.registry=Registry(self.root);self.registry.enroll('muse',[self.mid],True)
        self.channel=enable(self.root,'muse')
        self.requests=Path(self.channel['requests']);self.responses=Path(self.channel['responses'])
        self.broker=FileBroker(self.root)

    def tearDown(self):self.temp.cleanup()

    def submit(self,rid='test-001',tool='continue_matter',args=None,**extra):
        path=self.requests/(rid+'.json')
        path.write_text(json.dumps({'request_id':rid,'tool':tool,
            'arguments':args if args is not None else {'query':'公开测试事项'},**extra},ensure_ascii=False))
        os.utime(path,(time.time()-3,)*2)
        return path

    def response(self,rid='test-001'):return json.loads((self.responses/(rid+'.json')).read_text())

    def remember(self,rid='write-001'):
        return self.submit(rid,'remember',{'matter':self.mid,'kind':'digest','topic':'文件验收','body':'合成测试结果'})

    def test_query_reply_and_independent_identity(self):
        self.submit();result=self.broker.tick();self.assertEqual(result['processed'],1)
        response=self.response();self.assertTrue(response['ok'])
        self.assertEqual(response['result']['matter']['id'],self.mid)
        self.assertEqual(response['receipt']['author'],'agent:muse')
        self.remember();self.broker.tick();note=self.response('write-001')['result']['note_id']
        self.submit('readback','discussion',{'matter':self.mid});self.broker.tick()
        self.assertEqual(self.response('readback')['result']['contributions'][0]['id'],note)
        self.assertEqual(self.response('readback')['result']['contributions'][0]['author'],'agent:muse')

    def test_crash_after_write_before_reply_does_not_duplicate(self):
        self.remember()
        from context_hub import file_bridge
        original=file_bridge.atomic
        def interrupted(path,text):
            if Path(path).name=='write-001.json':raise OSError('simulated response write failure')
            return original(path,text)
        with patch.object(file_bridge,'atomic',side_effect=interrupted):self.broker.tick()
        self.assertFalse((self.responses/'write-001.json').exists())
        FileBroker(self.root).tick();r=self.response('write-001')
        self.assertTrue(r['ok']);self.assertTrue(r['result']['replayed'])
        m=Matters(self.root)
        try:self.assertEqual(m.db.execute('SELECT count(*) FROM entries').fetchone()[0],1)
        finally:m.close()

    def test_id_collision_and_author_spoof_are_rejected(self):
        self.remember();self.broker.tick()
        self.submit('write-001','remember',{'matter':self.mid,'kind':'digest','topic':'文件验收','body':'改变内容'})
        self.broker.tick();self.assertFalse(self.response('write-001')['ok'])
        self.assertIn('reused',self.response('write-001')['error'])
        self.submit('spoof','remember',{'matter':self.mid,'kind':'memory','topic':'a','body':'b','author':'user'})
        self.broker.tick();self.assertFalse(self.response('spoof')['ok'])

    def test_scope_revocation_and_read_only_apply(self):
        self.submit('private','read_notes',{'matter':self.private});self.broker.tick()
        self.assertFalse(self.response('private')['ok'])
        self.registry.change('muse',allow_remember=False)
        self.remember();self.broker.tick();self.assertFalse(self.response('write-001')['ok'])
        self.registry.change('muse',revoke=True)
        self.submit();self.broker.tick();self.assertFalse(self.response()['ok'])

    def test_deleted_reply_is_not_regenerated_after_revocation(self):
        self.submit();self.broker.tick();self.assertTrue(self.response()['ok'])
        (self.responses/'test-001.json').unlink()
        self.registry.change('muse',revoke=True)
        FileBroker(self.root).tick();self.assertFalse(self.response()['ok'])

    def test_no_shell_arbitrary_output_or_retry_id_arguments(self):
        for rid,tool,args,extra in [
            ('shell','exec',{'cmd':'echo not executed'},{}),
            ('owner','create_matter',{'name':'no'},{}),
            ('path','list_matters',{}, {'output':'/tmp/forbidden'}),
            ('retry','remember',{'matter':self.mid,'kind':'digest','topic':'a','body':'b','request_id':'spoof'},{}),
        ]:
            self.submit(rid,tool,args,**extra)
        self.broker.tick()
        for rid in ('shell','owner','path','retry'):self.assertFalse(self.response(rid)['ok'])

    def test_symlinks_are_not_followed(self):
        target=self.root/'sentinel';target.write_text('unchanged')
        try:(self.requests/'link.json').symlink_to(target)
        except OSError:self.skipTest('symlinks unavailable')
        self.submit('output','list_matters',{})
        (self.responses/'output.json').symlink_to(target)
        result=self.broker.tick()
        self.assertEqual(target.read_text(),'unchanged')
        self.assertFalse((self.responses/'link.json').exists())
        self.assertEqual(result['errors'],2)

    def test_partial_json_is_recoverable_but_fresh_writes_wait(self):
        path=self.requests/'test-001.json';path.write_text('{partial')
        self.assertEqual(self.broker.tick()['processed'],0)
        os.utime(path,(time.time()-3,)*2);self.broker.tick();self.assertFalse(self.response()['ok'])
        self.submit();self.broker.tick();self.assertTrue(self.response()['ok'])

    def test_request_limit_and_id_filename_binding(self):
        path=self.requests/'large.json';path.write_bytes(b' '*(MAX_REQUEST+1));os.utime(path,(time.time()-3,)*2)
        self.submit('name',request_id='different')
        self.broker.tick()
        self.assertFalse(self.response('large')['ok']);self.assertFalse(self.response('name')['ok'])

    def test_channel_enable_is_explicit_and_instructions_hide_secrets(self):
        self.assertEqual(enable(self.root,'muse')['requests'],self.channel['requests'])
        token=json.loads(self.registry.credential_path('muse').read_text())['token']
        text=Path(self.channel['instructions']).read_text()
        self.assertNotIn(token,text)
        self.assertIn('写文件',text)
        enable(self.root,'muse',False);self.submit();self.assertEqual(self.broker.tick()['processed'],0)
        self.assertFalse((self.responses/'test-001.json').exists())

    def test_owner_can_disable_channel_after_revoking_profile(self):
        self.registry.change('muse',revoke=True)
        self.assertFalse(enable(self.root,'muse',False)['file_channel_enabled'])
        with self.assertRaises(ValueError):enable(self.root,'muse',True)


if __name__=='__main__':unittest.main()
