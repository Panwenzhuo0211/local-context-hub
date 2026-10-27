import json
import sqlite3
import unittest
from unittest.mock import patch
from context_hub import session_sync as ss, sync_tail
from context_hub.session_ir.live import MARKER


class SyncLatencyTests(unittest.TestCase):
    def setUp(self):
        from test_session_sync import NativeSyncTests
        self.f=NativeSyncTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_prompt_delivered_during_turn_and_reverse_wait_survives_restart(self):
        self.f.append({'type':'event_msg','payload':{'type':'task_started'}},self.f.text('user','立即送达'))
        self.f.add_zcode('reverse','user','等空闲再投递')
        result=self.f.tick()[0]
        self.assertEqual(result['to_zcode_parts'],1)
        self.assertEqual(result['phase'],'waiting_codex_idle')
        self.assertEqual(result['pending_to_codex_parts'],1)
        self.assertNotIn('等空闲再投递',self.f.path.read_text(encoding='utf-8'))
        sync_tail._CACHE.clear()
        self.assertEqual(self.f.tick()[0]['phase'],'waiting_codex_idle')
        self.f.append(self.f.text('assistant','完成',True))
        result=self.f.tick()[0];self.assertEqual(result['to_codex_parts'],1)
        self.assertEqual(result['pending_to_codex_parts'],0)
        self.assertIn('等空闲再投递',self.f.path.read_text(encoding='utf-8'))
        self.assertEqual(self.f.tick()[0]['to_codex_parts'],0)

    def test_unchanged_verified_prefix_needs_no_hash_or_body_read(self):
        self.f.tick()
        pair=ss.status(self.f.root)['pairs'][0]
        with patch.object(sync_tail.hashlib,'sha256',side_effect=AssertionError('full prefix scan')):
            result=sync_tail.read(self.f.path,pair['offset'],pair['prefix'])
        self.assertEqual(result.data,b'')

    def test_same_length_prefix_rewrite_is_rejected_even_with_new_tail(self):
        self.f.append(self.f.text('user','原始内容'),self.f.text('assistant','完成',True));self.f.tick()
        before=self.f.path.read_bytes();changed=before.replace('原始内容'.encode(),'被改内容'.encode())
        self.assertEqual(len(before),len(changed));self.f.path.write_bytes(changed)
        self.f.append(self.f.text('user','追加不能掩盖改写'))
        result=ss.sync_once(self.f.root)[0]
        self.assertIn('rewritten',result['error'])

    def source(self,sid='c2'):
        path=self.f.codex/'sessions'/('rollout-'+sid+'.jsonl')
        rows=[{'type':'session_meta','payload':{'id':sid,'cwd':str(self.f.root)}},
              self.f.text('user','导入前'),self.f.text('assistant','原回答',True)]
        path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        return path

    def test_delayed_enable_replays_both_sides_since_verified_import(self):
        path=self.source();report=ss.import_codex_session(self.f.root,'c2',path)
        sid=report['zcode_session_id']
        with path.open('a',encoding='utf-8') as out:
            for r in (self.f.text('user','导入后新指令'),self.f.text('assistant','新回答',True)):
                out.write(json.dumps(r,ensure_ascii=False)+'\n')
        con=sqlite3.connect(self.f.native)
        try:
            con.execute('INSERT INTO message VALUES (?,?,?,?,?,?)',('localm',sid,3000,3000,json.dumps({'role':'user'}),900))
            con.execute('INSERT INTO part VALUES (?,?,?,?,?,?,?)',('localp','localm',sid,3000,3000,json.dumps({'type':'text','text':'镜像里的新指令'}),900));con.commit()
        finally:con.close()
        pair=ss.enroll(self.f.root,'zcode:'+sid,'codex:c2',path)
        self.assertLess(pair['offset'],path.stat().st_size)
        results=ss.sync_once(self.f.root);self.assertFalse(any(r.get('error') for r in results),results)
        result=next(r for r in results if r['pair']==pair['id'])
        self.assertEqual((result['to_zcode_parts'],result['to_codex_parts']),(2,1))
        con=sqlite3.connect(self.f.native)
        try:
            texts=[json.loads(r[0]).get('text') for r in con.execute('SELECT data FROM part WHERE session_id=?',(sid,))]
            self.assertEqual(texts.count('导入前'),1);self.assertEqual(texts.count('导入后新指令'),1)
        finally:con.close()
        echoed=[json.loads(r) for r in path.read_text(encoding='utf-8').splitlines() if MARKER in r]
        self.assertEqual(sum(r.get('payload',{}).get('message')=='镜像里的新指令' for r in echoed),1)
        ss.sync_once(self.f.root)
        self.assertEqual(path.read_text(encoding='utf-8').count('镜像里的新指令'),2) # event + model history

    def test_receipt_for_another_source_cannot_bind_a_pair(self):
        path=self.source();report=ss.import_codex_session(self.f.root,'c2',path)
        other=self.source('c3')
        with self.assertRaisesRegex(ValueError,'receipt does not match'):
            ss.enroll(self.f.root,report['zcode_session_id'],'c3',other)
        self.assertEqual(len(ss.status(self.f.root)['pairs']),1)

    def export_receipt(self):
        from context_hub.session_ir import manifest,mirror_map
        path=self.source()
        manifest.record_write(path,'c2',self.f.root)
        baseline=self.f.baseline['part'][0]
        mirror_map.record(self.f.root,zcode_id='z1',codex_id='c2',
                          source_parts={baseline[0]:ss._sha(baseline[5].encode())})
        return path

    def test_delayed_enable_replays_zcode_changes_since_export_too(self):
        path=self.export_receipt();self.f.add_zcode('afterexport','user','ZCode 导出后新增')
        with path.open('a',encoding='utf-8') as out:
            for r in (self.f.text('user','Codex 副本新增'),self.f.text('assistant','完成',True)):
                out.write(json.dumps(r,ensure_ascii=False)+'\n')
        ss.enroll(self.f.root,'z1','c2',path,replace=True)
        result=self.f.tick()[0]
        self.assertEqual((result['to_zcode_parts'],result['to_codex_parts']),(2,1))
        before=path.read_bytes();self.f.tick();self.assertEqual(path.read_bytes(),before)
        self.assertIn('ZCode 导出后新增',path.read_text(encoding='utf-8'))

    def test_exported_source_edit_requires_revalidation(self):
        path=self.export_receipt();con=sqlite3.connect(self.f.native)
        try:con.execute("UPDATE part SET data=? WHERE id='pbaseline'",(json.dumps({'type':'text','text':'改写旧记录'}),));con.commit()
        finally:con.close()
        with self.assertRaisesRegex(ValueError,'edited or removed'):
            ss.enroll(self.f.root,'z1','c2',path,replace=True)
        self.assertEqual(ss.status(self.f.root)['pairs'][0]['codex_id'],'c1')

    def test_enrollment_in_middle_of_turn_does_not_allow_foreign_append(self):
        path=self.source()
        with path.open('a',encoding='utf-8') as out:out.write(json.dumps(self.f.text('user','正在生成'))+'\n')
        pair=ss.enroll(self.f.root,'z1','c2',path,replace=True,replay_export=False)
        self.assertTrue(pair['codex_active'])
        self.f.add_zcode('late','user','排队')
        result=self.f.tick()[0]
        self.assertEqual(result['phase'],'waiting_codex_idle');self.assertEqual(result['to_codex_parts'],0)

    def test_rewound_zcode_session_stops_without_delivering_abandoned_branch(self):
        con=sqlite3.connect(self.f.native)
        try:con.execute('ALTER TABLE session ADD COLUMN revert TEXT');con.execute('UPDATE session SET revert=?',('{"targetMessageID":"old"}',));con.commit()
        finally:con.close()
        before=self.f.path.read_bytes();result=ss.sync_once(self.f.root)[0]
        self.assertIn('rewound',result['error']);self.assertEqual(self.f.path.read_bytes(),before)

    def test_incremental_projection_keeps_earlier_items_untouched(self):
        from test_native_repair import NativeRepairTests
        from context_hub.session_ir import native_history
        fixture=NativeRepairTests();fixture.f=self.f;fixture.paginated_fixture()
        self.f.add_zcode('first','user','第一轮');self.f.tick()
        self.f.add_zcode('second','user','第二轮',stamp=3000);self.f.tick()
        con=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        try:before=con.execute('SELECT * FROM thread_items ORDER BY rollout_ordinal LIMIT 1').fetchone()
        finally:con.close()
        self.f.add_zcode('third','user','第三轮',stamp=4000);self.f.tick()
        con=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        try:
            self.assertEqual(con.execute('SELECT * FROM thread_items ORDER BY rollout_ordinal LIMIT 1').fetchone(),before)
            self.assertEqual(con.execute('SELECT count(*) FROM thread_items').fetchone()[0],3)
            self.assertEqual(con.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state').fetchone()[0],self.f.path.stat().st_size)
        finally:con.close()

    def native_paginated(self):
        from test_native_repair import NativeRepairTests
        fixture=NativeRepairTests();fixture.f=self.f;fixture.paginated_fixture()
        header={'type':'session_meta','ordinal':1000,'payload':{'id':'c1','originator':'Codex Desktop','history_mode':'paginated'}}
        self.f.path.write_text(json.dumps(header)+'\n',encoding='utf-8')
        raw=self.f.path.read_bytes();state=ss._db(self.f.root)
        try:
            state.execute('UPDATE pairs SET offset=?,prefix=?,codex_active=0',(len(raw),ss._sha(raw)));state.commit()
        finally:state.close()
        con=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        try:
            con.execute('INSERT INTO thread_history_projection_state VALUES (?,?,?)',('c1',len(raw),1001))
            con.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',('c1','original-native-turn',999,'completed',1,2,1000,'original-tool',None,0,1000,len(raw),'original-native-turn'))
            con.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?,?,?)',('c1','original-native-turn','original-tool',999,1,'{"type":"commandExecution","id":"original-tool"}','commandExecution',999,1,2));con.commit()
        finally:con.close()

    def test_original_native_pagination_global_ordinals_and_existing_items_are_preserved(self):
        self.native_paginated();self.f.add_zcode('incoming','user','从镜像接续原生')
        result=self.f.tick()[0];self.assertEqual(result['phase'],'projection_ready')
        rows=[json.loads(raw) for raw in self.f.path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['ordinal'] for r in rows],list(range(1000,1000+len(rows))))
        con=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        try:
            self.assertEqual(con.execute("SELECT turn_id FROM thread_items WHERE item_id='original-tool'").fetchone()[0],'original-native-turn')
            self.assertEqual(con.execute("SELECT count(*) FROM thread_turns WHERE turn_id='original-native-turn'").fetchone()[0],1)
            self.assertEqual(con.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state').fetchone(),(self.f.path.stat().st_size,1000+len(rows)))
            self.assertEqual(con.execute("SELECT count(*) FROM thread_items WHERE item_type='userMessage'").fetchone()[0],1)
        finally:con.close()

    def test_lagging_native_projection_queues_reverse_but_delivers_source(self):
        self.native_paginated()
        user=self.f.text('user','Codex 源消息');user['ordinal']=1001
        answer=self.f.text('assistant','回答',True);answer['ordinal']=1002
        self.f.append(user,answer);self.f.add_zcode('pending','user','等待索引追上')
        before=self.f.path.read_bytes();result=self.f.tick()[0]
        self.assertEqual(result['phase'],'waiting_codex_projection');self.assertEqual(result['to_zcode_parts'],2)
        self.assertEqual(self.f.path.read_bytes(),before)
        con=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        try:con.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=?',(len(before),1003));con.commit()
        finally:con.close()
        self.assertEqual(self.f.tick()[0]['to_codex_parts'],1)

    def test_native_projection_failure_recovers_without_second_append(self):
        from context_hub.session_ir import native_history
        self.native_paginated();self.f.add_zcode('retry','user','崩溃后仅一份')
        with patch.object(native_history,'publish_synced_tail',side_effect=OSError('simulated crash')):
            self.assertIn('error',ss.sync_once(self.f.root)[0])
        before=self.f.path.read_bytes();sync_tail._CACHE.clear()
        self.f.tick();self.f.tick()
        self.assertEqual(self.f.path.read_bytes(),before)
        con=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        try:self.assertEqual(con.execute("SELECT count(*) FROM thread_items WHERE item_type='userMessage'").fetchone()[0],1)
        finally:con.close()


if __name__=='__main__':unittest.main()
