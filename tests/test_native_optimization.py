import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from context_hub.native_context import read_session,read_record
from context_hub.mcp_server import Server


class NativeOptimizationTests(unittest.TestCase):
    def setUp(self):
        from test_native_repair import NativeRepairTests
        self.case=NativeRepairTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.f=self.case.f
        with sqlite3.connect(self.f.native) as c:
            c.execute('ALTER TABLE session ADD COLUMN title TEXT')
            c.execute("UPDATE session SET title='分页修复验收版' WHERE id='z1'")

    def add_session(self,sid,title,stamp=5000):
        with sqlite3.connect(self.f.native) as c:
            c.execute('INSERT INTO session(id,directory,time_updated,title) VALUES (?,?,?,?)',
                      (sid,'/another',stamp,title))

    def test_natural_language_title_load_is_native_and_read_only(self):
        before=self.f.native.read_bytes()
        for query in ('加载在zcode的分页修复验收版会话的原生上下文',
                      '帮我加载在ZCode里面的分页修复验收版会话的原生上下文',
                      '读取 ZCode 的《分页修复验收版》会话的原生上下文',
                      'zcode 的“分页修复验收版”'):
            result=Server(self.f.root).call('read_session_context',{'session':query,'limit':1})
            self.assertEqual(result['status'],'ready');self.assertEqual(result['session'],'zcode:z1')
            self.assertEqual(result['resolution']['match'],'exact_native_title')
            self.assertTrue(result['source']['read_only'])
        self.assertEqual(before,self.f.native.read_bytes())

    def test_same_title_never_selects_newest_or_combines_histories(self):
        self.add_session('z2','分页修复验收版',9000)
        result=read_session(self.f.root,'加载zcode的《分页修复验收版》会话的原生上下文')
        self.assertEqual(result['status'],'ambiguous');self.assertNotIn('records',result)
        self.assertEqual({c['session'] for c in result['candidates']},{'zcode:z1','zcode:z2'})

    def test_partial_title_returns_candidates_without_guessing(self):
        result=read_session(self.f.root,'加载zcode的《分页修复》会话的原生上下文')
        self.assertEqual(result['status'],'candidates');self.assertNotIn('source',result)

    def test_quoted_title_can_contain_the_other_client_name(self):
        for title in ('Codex 导入优化','ZCode'):
            with sqlite3.connect(self.f.native) as c:c.execute("UPDATE session SET title=? WHERE id='z1'",(title,))
            result=read_session(self.f.root,'加载在ZCode的《'+title+'》会话的原生上下文')
            self.assertEqual(result['session'],'zcode:z1')

    def test_source_error_does_not_fall_back_to_a_stale_search_excerpt(self):
        result=read_session(self.f.root,'加载Codex的《分页修复验收版》原生上下文')
        self.assertEqual(result['status'],'source_unavailable');self.assertNotIn('records',result)

    def test_previous_page_raw_reference_still_works_after_paging(self):
        p=self.case.write_source(rows=[self.case.text('早先'*2000),self.case.text('最新'*2000,'2026-10-25T01:00:00Z')])
        latest=read_session(self.f.root,'codex:source',limit=1,max_chars=1000)
        earlier=read_session(self.f.root,'codex:source',limit=1,cursor=latest['next_cursor'],max_chars=1000)
        raw=read_record(self.f.root,'codex:source',latest['pending_full_records'][0],watermark=latest['source']['watermark'])
        self.assertIn('最新',raw['text']);self.assertFalse(earlier['coverage']['full_history_in_this_response'])
        self.assertEqual(earlier['coverage']['remaining_after'],1)

    def test_unchanged_codex_pages_use_verified_index_instead_of_full_scan(self):
        self.case.write_source(rows=[self.case.text('早先'),self.case.text('最新','2026-10-25T01:00:00Z')])
        latest=read_session(self.f.root,'codex:source',limit=1)
        self.assertFalse(latest['coverage']['full_history_in_this_response'])
        with patch('context_hub.native_context._walk',side_effect=AssertionError('Full scan must not repeat')):
            earlier=read_session(self.f.root,'codex:source',limit=1,cursor=latest['next_cursor'])
            again=read_session(self.f.root,'codex:source',limit=1)
        self.assertTrue(earlier['cache_reused']);self.assertEqual(earlier['records'][0]['payload']['text'],'早先')
        self.assertTrue(again['cache_reused'])

    def test_cursor_cannot_be_reused_for_a_different_session_or_version(self):
        self.f.add_zcode('next','user','较新记录',stamp=3000)
        page=read_session(self.f.root,'zcode:z1',limit=1)
        self.add_session('z2','另一个会话')
        with self.assertRaisesRegex(ValueError,'conversation'):
            read_session(self.f.root,'zcode:z2',cursor=page['next_cursor'])
        cursor=json.loads(page['next_cursor']);cursor['version']=99
        with self.assertRaisesRegex(ValueError,'cursor'):
            read_session(self.f.root,'zcode:z1',cursor=json.dumps(cursor))

    def test_image_summary_is_explicitly_pending_not_fully_loaded(self):
        user=self.case.text('看图');user['payload']['content'].append({'type':'input_image','image_url':'data:image/png;base64,aGVsbG8='})
        self.case.write_source(rows=[user])
        page=read_session(self.f.root,'codex:source')
        self.assertTrue(page['pending_full_records']);self.assertFalse(page['selected_records_loaded'])
        self.assertFalse(page['coverage']['full_history_in_this_response'])

    def test_native_part_edit_without_header_update_invalidates_cache(self):
        page=read_session(self.f.root,'zcode:z1')
        with sqlite3.connect(self.f.native) as c:
            c.execute("UPDATE part SET data=? WHERE id='pbaseline'",(json.dumps({'type':'text','text':'原文已修改'}),))
        with self.assertRaisesRegex(ValueError,'source changed'):
            read_record(self.f.root,'zcode:z1',page['records'][0]['record_ref'],watermark=page['source']['watermark'])
        updated=read_session(self.f.root,'zcode:z1')
        self.assertEqual(updated['records'][0]['payload']['text'],'原文已修改')

    def test_stale_mirror_is_replaced_by_a_new_copy_only_on_explicit_import(self):
        import context_hub.session_ir.emit_codex as emit
        with patch.object(emit,'CODEX_HOME',self.f.codex):
            server=Server(self.f.root)
            first=server.call('prepare_resume',{'session':'zcode:z1'})
            old=Path(first['path']);before=old.read_bytes()
            self.f.add_zcode('later','user','后来新增的原话',stamp=3000)
            # Loading context never publishes a new native mirror.
            read=server.call('read_session_context',{'session':'加载ZCode的《分页修复验收版》原生上下文'})
            self.assertIn('后来新增的原话',json.dumps(read,ensure_ascii=False))
            second=server.call('prepare_resume',{'session':'zcode:z1'})
            self.assertNotEqual(first['codex_session_id'],second['codex_session_id'])
            self.assertEqual(second['previous_mirror_preserved'],first['codex_session_id'])
            self.assertEqual(before,old.read_bytes())
            self.assertIn('后来新增的原话',Path(second['path']).read_text(encoding='utf-8'))
            self.assertTrue(server.call('prepare_resume',{'session':'zcode:z1'})['mirror'])

    def test_same_size_mirror_edit_is_detected_by_hash_and_preserved(self):
        import context_hub.session_ir.emit_codex as emit
        with patch.object(emit,'CODEX_HOME',self.f.codex):
            server=Server(self.f.root);first=server.call('prepare_resume',{'session':'zcode:z1'})
            old=Path(first['path']);raw=old.read_bytes();changed=raw.replace('原有历史'.encode(),'用户修改'.encode())
            self.assertEqual(len(raw),len(changed));self.assertNotEqual(raw,changed);old.write_bytes(changed)
            second=server.call('prepare_resume',{'session':'zcode:z1'})
            self.assertNotEqual(first['codex_session_id'],second['codex_session_id']);self.assertEqual(old.read_bytes(),changed)


if __name__=='__main__':unittest.main()
