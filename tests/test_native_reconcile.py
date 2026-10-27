import json,sqlite3,unittest
from context_hub import session_sync as ss


class NativeReconcileTests(unittest.TestCase):
    def setUp(self):
        from test_sync_latency import SyncLatencyTests
        self.fixture=SyncLatencyTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.f=self.fixture.f;self.fixture.native_paginated()

    def source(self,*,complete=True,item_type='AgentMessage',thread='c1'):
        stamp='2026-10-25T10:00:00Z'
        events=[{'type':'session_meta','payload':{'id':'c1','originator':'Codex Desktop','history_mode':'paginated'}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'native-t','root_turn_id':'native-t','started_at':1792922400}},
            {'type':'event_msg','payload':{'type':'item_completed','thread_id':thread,'turn_id':'native-t',
                'item':{'type':'UserMessage','id':'native-u','client_id':'client-u','content':[{'type':'text','text':'源用户消息','text_elements':[]}]}}},
            {'type':'event_msg','payload':{'type':'item_completed','thread_id':thread,'turn_id':'native-t',
                'item':{'type':item_type,'id':'native-a','content':[{'type':'Text','text':'源回答'}],'phase':'final_answer'}}},
            self.f.text('assistant','源回答',True)]
        if complete:events.append({'type':'event_msg','payload':{'type':'task_complete','turn_id':'native-t','root_turn_id':'native-t','completed_at':1792922401,'duration_ms':1000}})
        for n,row in enumerate(events,1000):row['ordinal']=n;row['timestamp']=stamp
        self.f.path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in events),encoding='utf-8')
        raw=self.f.path.read_bytes();state=ss._db(self.f.root)
        try:state.execute('UPDATE pairs SET offset=?,prefix=?,codex_active=0',(len(raw),ss._sha(raw)));state.commit()
        finally:state.close()
        con=self.history()
        try:con.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=7,next_rollout_ordinal=1001');con.commit()
        finally:con.close()
        self.f.add_zcode('new','user','反向应当到达')

    def history(self):return sqlite3.connect(self.f.codex/'thread_history_1.sqlite')

    def test_stale_segment_checkpoint_is_reconciled_from_native_ids(self):
        self.source();result=self.f.tick()[0]
        self.assertEqual(result['to_codex_parts'],1);self.assertEqual(result['phase'],'projection_ready')
        con=self.history()
        try:
            self.assertEqual(con.execute("SELECT turn_id FROM thread_items WHERE item_id='original-tool'").fetchone()[0],'original-native-turn')
            self.assertEqual(con.execute("SELECT turn_id FROM thread_items WHERE item_id='native-u'").fetchone()[0],'native-t')
            item=json.loads(con.execute("SELECT item_json FROM thread_items WHERE item_id='native-a'").fetchone()[0]);self.assertEqual(item['text'],'源回答')
            self.assertEqual(con.execute("SELECT status FROM thread_turns WHERE turn_id='native-t'").fetchone()[0],'completed')
            self.assertEqual(con.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state').fetchone()[0],self.f.path.stat().st_size)
        finally:con.close()
        before=self.f.path.read_bytes();self.f.tick();self.assertEqual(self.f.path.read_bytes(),before)

    def test_unknown_unprojected_native_item_fails_without_writing(self):
        self.source(item_type='FutureToolWidget');before=self.f.path.read_bytes()
        result=ss.sync_once(self.f.root)[0];self.assertIn('Unsupported unprojected',result['error'])
        self.assertEqual(self.f.path.read_bytes(),before)
        con=self.history()
        try:self.assertEqual(con.execute('SELECT count(*) FROM thread_items').fetchone()[0],1)
        finally:con.close()

    def test_existing_native_tool_is_preserved_exactly(self):
        self.source(item_type='FutureToolWidget');con=self.history();original='{"type":"commandExecution","id":"native-a","native_extra":17}'
        try:
            con.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?,?,?)',('c1','native-t','native-a',1003,1,original,'commandExecution',1003,1,2));con.commit()
        finally:con.close()
        self.assertEqual(self.f.tick()[0]['to_codex_parts'],1)
        con=self.history()
        try:self.assertEqual(con.execute("SELECT item_json FROM thread_items WHERE item_id='native-a'").fetchone()[0],original)
        finally:con.close()

    def test_missing_native_completion_remains_queued(self):
        self.source(complete=False);before=self.f.path.read_bytes();result=self.f.tick()[0]
        self.assertEqual(result['phase'],'waiting_codex_projection');self.assertEqual(result['to_codex_parts'],0)
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_other_thread_attribution_is_rejected(self):
        self.source(thread='other-thread');before=self.f.path.read_bytes()
        result=ss.sync_once(self.f.root)[0];self.assertIn('another thread',result['error']);self.assertEqual(self.f.path.read_bytes(),before)

    def test_hidden_zcode_runtime_is_acknowledged_without_delivery_or_pending(self):
        self.source(complete=False)
        con=sqlite3.connect(self.f.native)
        try:
            con.execute("UPDATE part SET data=? WHERE id='pnew'",(json.dumps({'type':'text','text':'Never show this English runtime note','synthetic':True}),));con.commit()
        finally:con.close()
        result=self.f.tick()[0]
        self.assertEqual(result['filtered_parts'],1);self.assertEqual(result['pending_to_codex_parts'],0)
        self.assertEqual(result['to_codex_parts'],0)
        con=ss._db(self.f.root)
        try:self.assertEqual(con.execute("SELECT count(*) FROM deliveries WHERE direction='zcode-to-codex'").fetchone()[0],0)
        finally:con.close()


if __name__=='__main__':unittest.main()
