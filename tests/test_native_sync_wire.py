import json,sqlite3,unittest
from unittest.mock import patch
from context_hub import session_sync as ss
from context_hub.session_ir import native_sync
from context_hub.session_ir.live import encode_codex
from context_hub.session_ir.schema import UserText


class NativeWireTests(unittest.TestCase):
    def setUp(self):
        from test_sync_latency import SyncLatencyTests
        self.fixture=SyncLatencyTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.f=self.fixture.f;self.fixture.native_paginated()
        raw=self.f.path.read_bytes();header=json.loads(raw);header['payload']['multi_agent_version']='v2'
        self.f.path.write_text(json.dumps(header)+'\n',encoding='utf-8');raw=self.f.path.read_bytes()
        c=ss._db(self.f.root)
        try:c.execute('UPDATE pairs SET offset=?,prefix=?',(len(raw),ss._sha(raw)));c.commit()
        finally:c.close()
        c=self.history()
        try:c.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?',(len(raw),));c.commit()
        finally:c.close()

    def history(self):return sqlite3.connect(self.f.codex/'thread_history_1.sqlite')

    def test_v2_encoder_retains_one_model_message_and_native_item_identity(self):
        rows=native_sync.encode([UserText(ts='2026-10-25T10:00:00Z',text='精确同步文本')],
            pair='p',event='zcode:part1',cwd=str(self.f.root),thread_id='c1')
        model=[r for r in rows if r['type']=='response_item']
        self.assertEqual(len(model),1);self.assertEqual(model[0]['payload']['content'][0]['text'],'精确同步文本')
        started=rows[0]['payload'];completed=rows[-1]['payload'];native=next(r['payload'] for r in rows if r['payload'].get('type')=='item_completed')
        self.assertEqual(started['turn_id'],completed['turn_id']);self.assertEqual(native['turn_id'],started['turn_id'])
        self.assertEqual(native['thread_id'],'c1')
        self.assertEqual(model[0]['payload']['internal_chat_message_metadata_passthrough']['turn_id'],started['turn_id'])
        self.assertFalse(any(r.get('payload',{}).get('type')=='user_message' for r in rows))

    def test_v2_native_turn_items_are_projected_with_the_same_source_ids(self):
        self.f.add_zcode('incoming','user','新版正向显示测试')
        result=self.f.tick()[0];self.assertEqual(result['to_codex_parts'],1)
        rows=[json.loads(raw) for raw in self.f.path.read_bytes().splitlines()]
        completed=[r['payload'] for r in rows if r.get('payload',{}).get('type')=='item_completed']
        self.assertEqual(len(completed),1);native=completed[0]
        c=self.history()
        try:
            stored=json.loads(c.execute('SELECT item_json FROM thread_items WHERE item_id=?',(native['item']['id'],)).fetchone()[0])
            self.assertEqual(stored['content'][0]['text'],'新版正向显示测试')
            self.assertEqual(c.execute('SELECT turn_id FROM thread_items WHERE item_id=?',(native['item']['id'],)).fetchone()[0],native['turn_id'])
        finally:c.close()
        before=self.f.path.read_bytes();self.f.tick();self.assertEqual(self.f.path.read_bytes(),before)

    def test_already_acknowledged_legacy_text_gets_view_only_upgrade_once(self):
        self.f.add_zcode('legacy','user','旧格式已投递消息')
        def old_encoder(moments,**kwargs):kwargs.pop('thread_id');return encode_codex(moments,**kwargs)
        with patch.object(native_sync,'encode',side_effect=old_encoder):self.f.tick()
        before=self.f.path.read_bytes();result=self.f.tick()[0]
        self.assertEqual(result['to_codex_parts'],0)
        after=self.f.path.read_bytes();self.assertTrue(after.startswith(before))
        # The compatibility repair adds native presentation events, never a
        # second copy of the old user prompt to the model message sequence.
        extra=[json.loads(raw) for raw in after[len(before):].splitlines()]
        self.assertTrue(extra);self.assertTrue(all(r['type']=='event_msg' for r in extra))
        self.assertEqual(sum(r['payload'].get('type')=='item_completed' for r in extra),1)
        self.f.tick();self.assertEqual(self.f.path.read_bytes(),after)

    def test_crash_after_v2_append_recovers_without_duplicate_native_turn(self):
        from context_hub.session_ir import native_history
        self.f.add_zcode('retry','user','新版崩溃重试')
        with patch.object(native_history,'publish_synced_tail',side_effect=OSError('simulated crash')):
            self.assertIn('error',ss.sync_once(self.f.root)[0])
        before=self.f.path.read_bytes();self.f.tick();self.f.tick()
        self.assertEqual(self.f.path.read_bytes(),before)
        c=self.history()
        try:self.assertEqual(c.execute("SELECT count(*) FROM thread_items WHERE item_type='userMessage'").fetchone()[0],1)
        finally:c.close()


if __name__=='__main__':unittest.main()
