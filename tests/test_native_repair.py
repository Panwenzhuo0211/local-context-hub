import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from context_hub import session_sync as ss
from context_hub.native_source import CodexSnapshot
from context_hub.native_context import read_session,read_record
from context_hub.native_import import import_codex_session
from context_hub.session_ir.live import decode_zcode,encode_codex,encode_zcode
from context_hub.session_ir.restore_codex import _translate_line
from context_hub.session_ir.emit_codex import _render_moment
from context_hub.session_ir.schema import Session
from context_hub.mcp_server import Server


class NativeRepairTests(unittest.TestCase):
    def setUp(self):
        from test_session_sync import NativeSyncTests
        self.f=NativeSyncTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def write_source(self,sid='source',rows=None,name=None):
        path=self.f.codex/'sessions'/(name or ('rollout-'+sid+'.jsonl'))
        header={'type':'session_meta','timestamp':'2026-10-09T00:00:00Z',
                'payload':{'id':sid,'cwd':str(self.f.root)}}
        path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in [header,*(rows or [])]),encoding='utf-8')
        return path

    def text(self,text,stamp='2026-10-25T00:00:00Z',role='user'):
        row=self.f.text(role,text,role=='assistant');row['timestamp']=stamp;return row

    def native_connection(self):
        c=sqlite3.connect(self.f.native);self.addCleanup(c.close);return c

    def paginated_fixture(self):
        c=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        try:
            c.executescript('''CREATE TABLE thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal,started_at_ms,completed_at_ms);
                CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at,completed_at,duration_ms,first_user_item_id,final_agent_item_id,rollout_byte_offset,rollout_end_ordinal,rollout_end_byte_offset,root_turn_id);
                CREATE TABLE thread_history_projection_state(thread_id PRIMARY KEY,next_rollout_byte_offset,next_rollout_ordinal);''')
        finally:c.close()
        c=sqlite3.connect(self.f.codex/'state_5.sqlite')
        try:c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,history_mode TEXT)');c.commit()
        finally:c.close()
        self.f.path.write_text(json.dumps({'type':'session_meta','ordinal':0,
            'payload':{'id':'c1','originator':'context-hub','history_mode':'paginated'}})+'\n',encoding='utf-8')
        state=ss._db(self.f.root)
        try:
            raw=self.f.path.read_bytes()
            with state:state.execute('UPDATE pairs SET offset=?,prefix=?',(len(raw),ss._sha(raw)))
        finally:state.close()

    def test_hidden_runtime_is_omitted_in_both_render_paths(self):
        part={'id':'auto','time_created':1000,'data':json.dumps({'type':'text',
              'text':'The TodoWrite tool has not been used recently.','synthetic':True,
              'metadata':{'visibility':'model-only','runtimeMessage':{'source':'todo_reminder'}}})}
        moments=decode_zcode({'data':json.dumps({'role':'user'})},part)
        self.assertEqual(encode_codex(moments,pair='p',event='e',cwd=''),[])
        from context_hub.session_ir.schema import Attachment
        auto=Attachment(subtype='historical_runtime',data={'content':'hidden reminder'})
        self.assertEqual(_render_moment(auto,Session()),[])

    def test_message_level_synthetic_visibility_is_respected(self):
        part={'id':'auto','time_created':1000,'data':json.dumps({'type':'text','text':'internal'})}
        info={'role':'user','semantics':{'uiVisibility':'hidden','origin':'agent_runtime'}}
        self.assertEqual(decode_zcode({'data':json.dumps(info)},part),[])

    def test_actual_english_and_user_quotations_are_preserved(self):
        for text in ('I wrote this sentence.','请解释 <environment_context> 的含义。'):
            p=self.write_source(rows=[self.text(text)])
            result=read_session(self.f.root,'codex:source')
            self.assertEqual(result['records'][0]['payload']['text'],text)

    def test_native_exec_input_and_subagent_authorship_survive(self):
        raw="const result = await tools.exec_command({cmd:'echo 历史'}); text(result);"
        rows=[{'type':'response_item','timestamp':'2026-10-25T00:00:01Z','payload':{
            'type':'custom_tool_call','name':'exec','call_id':'call_exec','input':raw}},
              {'type':'response_item','timestamp':'2026-10-25T00:00:02Z','payload':{
                  'type':'custom_tool_call_output','call_id':'call_exec','output':'exact result'}},
              {'type':'response_item','timestamp':'2026-10-25T00:00:03Z','payload':{
                  'type':'agent_message','id':'agent-m1','author':'agent-a','recipient':'agent-b',
                  'content':[{'type':'text','text':'子任务的原始通信'}]}}]
        path=self.write_source(rows=rows)
        result=import_codex_session(self.f.root,'source',path)
        c=self.native_connection()
        parts=[json.loads(r[0]) for r in c.execute('SELECT data FROM part WHERE session_id=?',(result['zcode_session_id'],))]
        tool=next(p for p in parts if p['type']=='tool')
        self.assertEqual(tool['tool'],'exec');self.assertEqual(tool['state']['input']['historical_input'],raw)
        self.assertEqual(tool['state']['output'],'exact result')
        self.assertEqual(tool['state']['time'],{'start':1792886401000,'end':1792886402000})
        structure=next(p['text'] for p in parts if p['type']=='text')
        self.assertIn('agent-a',structure);self.assertIn('agent-b',structure)
        self.assertNotIn('unknown custom_tool_call',structure)
        info=json.loads(c.execute("SELECT data FROM message WHERE session_id=? AND json_extract(data,'$.role')='assistant' ORDER BY sequence DESC LIMIT 1",(result['zcode_session_id'],)).fetchone()[0])
        self.assertEqual(info['semantics']['uiVisibility'],'hidden')

    def test_compaction_replacement_and_images_are_not_dropped(self):
        image='data:image/png;base64,aGVsbG8='
        user=self.text('原图');user['payload']['content'].append({'type':'input_image','image_url':image})
        compact={'type':'compacted','timestamp':'2026-10-25T00:00:01Z',
                 'payload':{'message':'','replacement_history':[{'type':'message','role':'user','content':'压缩后的真实摘要'}]}}
        result=import_codex_session(self.f.root,'source',self.write_source(rows=[user,compact]))
        c=self.native_connection();parts=[json.loads(r[0]) for r in c.execute('SELECT data FROM part WHERE session_id=?',(result['zcode_session_id'],))]
        file=next(p for p in parts if p['type']=='file')
        self.assertTrue(file['url'].startswith('file:'))
        ref=file['metadata']['context_hub_payloads'][0]
        self.assertEqual(Path(ref['path']).read_bytes(),b'hello')
        self.assertEqual(hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest(),ref['sha256'])
        self.assertTrue(any('压缩后的真实摘要' in p.get('text','') for p in parts))
        self.assertEqual(result['verified_image_blocks'],1)

    def test_large_tool_output_is_bounded_and_complete_artifact_is_verified(self):
        from context_hub.native_payloads import bound_part,LIMIT
        text='完整输出😀'*20000
        part={'type':'tool','state':{'input':{},'output':text,'metadata':{'native_result':{'output':text}}}}
        data=bound_part(self.f.root,part)
        self.assertLess(len(data['state']['output']),LIMIT)
        refs=data['metadata']['context_hub_payloads']
        self.assertTrue(all(r['complete'] and not r['loaded_in_chat'] for r in refs))
        self.assertEqual(json.loads(Path(refs[0]['path']).read_text(encoding='utf-8')),text)
        self.assertEqual(hashlib.sha256(Path(refs[0]['path']).read_bytes()).hexdigest(),refs[0]['sha256'])
        Path(refs[0]['path']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'integrity'):
            bound_part(self.f.root,{'type':'text','text':text})

    def test_opaque_unknown_response_is_preserved_with_a_type(self):
        row={'type':'response_item','timestamp':'2026-10-25T00:00:00Z',
             'payload':{'type':'future_native_event','new_field':{'value':'must survive'}}}
        moment=_translate_line(row,'fixture','')[0]
        self.assertEqual(moment.subtype,'native_record')
        self.assertEqual(moment.data['record'],row['payload'])

    def test_authoritative_compound_segment_and_overlap_dedup(self):
        old=self.text('旧段','2026-10-18T00:00:00Z')
        self.write_source(rows=[old])
        latest=self.f.codex/'sessions/rollout-source_suffix.jsonl'
        header={'type':'session_meta','timestamp':'2026-10-18T01:00:00Z','payload':{'id':'source','cwd':str(self.f.root)}}
        latest.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in [header,old,self.text('最新原文','2026-10-25T00:00:00Z')]),encoding='utf-8')
        con=sqlite3.connect(self.f.codex/'state_5.sqlite')
        try:
            con.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT)');con.execute('INSERT INTO threads VALUES (?,?)',('source',str(latest)));con.commit()
        finally:con.close()
        result=read_session(self.f.root,'codex:source')
        self.assertEqual([r['payload']['text'] for r in result['records']],['旧段','最新原文'])
        self.assertEqual(len(result['source']['segments']),2)
        self.assertEqual(result['source']['duplicate_records'],1)
        self.assertTrue(result['source']['latest_at'].startswith('2026-10-25'))

    def test_native_read_full_text_and_explicit_large_record_paging(self):
        text='原始内容'*4000;self.write_source(rows=[self.text(text)])
        page=read_session(self.f.root,'codex:source',max_chars=1000)
        self.assertFalse(page['selected_records_loaded']);ref=page['pending_full_records'][0]
        a=read_record(self.f.root,'codex:source',ref,length=12000,watermark=page['source']['watermark'])
        b=read_record(self.f.root,'codex:source',ref,offset=a['next_offset'],length=12000,watermark=page['source']['watermark'])
        self.assertEqual(json.loads(a['text']+b['text'])['payload']['content'][0]['text'],text)

    def test_cursor_refuses_to_mix_source_revisions(self):
        p=self.write_source(rows=[self.text('早先'),self.text('最新','2026-10-25T01:00:00Z')])
        first=read_session(self.f.root,'codex:source',limit=1)
        with p.open('a',encoding='utf-8') as out:out.write(json.dumps(self.text('又更新','2026-10-25T02:00:00Z'))+'\n')
        with self.assertRaisesRegex(ValueError,'source changed'):
            read_session(self.f.root,'codex:source',cursor=first['next_cursor'])

    def test_missing_current_segment_and_outside_path_fail_closed(self):
        self.write_source(rows=[self.text('旧文件')])
        con=sqlite3.connect(self.f.codex/'state_5.sqlite')
        try:
            con.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT)');con.execute('INSERT INTO threads VALUES (?,?)',('source',str(self.f.codex/'sessions/missing.jsonl')));con.commit()
        finally:con.close()
        with self.assertRaisesRegex(ValueError,'missing'):read_session(self.f.root,'codex:source')

    def test_invalid_line_never_replaces_existing_import(self):
        p=self.write_source(rows=[self.text('完整原文')]);r=import_codex_session(self.f.root,'source',p)
        c=self.native_connection();before=c.execute('SELECT data FROM part WHERE session_id=?',(r['zcode_session_id'],)).fetchall()
        with p.open('ab') as out:out.write(b'{incomplete')
        with self.assertRaisesRegex(ValueError,'Incomplete'):import_codex_session(self.f.root,'source',p,replace=True)
        self.assertEqual(c.execute('SELECT data FROM part WHERE session_id=?',(r['zcode_session_id'],)).fetchall(),before)

    def test_replace_protects_local_continuation_and_new_copy_is_separate(self):
        p=self.write_source(rows=[self.text('来源')]);r=import_codex_session(self.f.root,'source',p)
        c=self.native_connection()
        c.execute('UPDATE part SET data=? WHERE session_id=?',(json.dumps({'type':'text','text':'用户本地修改'}),r['zcode_session_id']));c.commit()
        with self.assertRaisesRegex(ValueError,'local changes'):import_codex_session(self.f.root,'source',p,replace=True)
        copy=import_codex_session(self.f.root,'source',p,new_copy=True)
        self.assertNotEqual(copy['zcode_session_id'],r['zcode_session_id'])
        self.assertEqual('用户本地修改',json.loads(c.execute('SELECT data FROM part WHERE session_id=?',(r['zcode_session_id'],)).fetchone()[0])['text'])

    def test_tool_result_never_updates_another_session_or_wildcard_match(self):
        c=self.native_connection();c.execute('INSERT INTO session VALUES (?,?,?)',('other','/other',1))
        info=json.dumps({'role':'assistant'});data=json.dumps({'type':'tool','callID':'call_x__0','state':{'output':'original'}})
        c.execute('INSERT INTO message VALUES (?,?,?,?,?,?)',('foreign-m','other',1,1,info,0))
        c.execute('INSERT INTO part VALUES (?,?,?,?,?,?,?)',('foreign-p','foreign-m','other',1,1,data,0));c.commit()
        rows=[{'type':'response_item','timestamp':'2026-10-25T00:00:00Z','payload':{'type':'function_call','call_id':'call_x','name':'exec_command','arguments':'{"cmd":"echo historical"}'}},
              {'type':'response_item','timestamp':'2026-10-25T00:00:01Z','payload':{'type':'function_call_output','call_id':'call_x','output':'new'}}]
        import_codex_session(self.f.root,'source',self.write_source(rows=rows))
        self.assertEqual(c.execute("SELECT data FROM part WHERE id='foreign-p'").fetchone()[0],data)

    def test_bad_pair_migration_preserves_old_pair(self):
        with self.assertRaises(ValueError):ss.enroll(self.f.root,'z1','missing',self.f.codex/'sessions/missing.jsonl',replace=True)
        state=ss._db(self.f.root)
        try:self.assertEqual(state.execute('SELECT id FROM pairs').fetchone()[0],self.f.pair['id'])
        finally:state.close()

    def test_import_and_native_read_do_not_enable_sync(self):
        p=self.write_source(rows=[self.text('只读或单次导入')]);state=ss._db(self.f.root)
        try:
            with state:state.execute('UPDATE pairs SET enabled=0')
        finally:state.close()
        read_session(self.f.root,'codex:source')
        result=import_codex_session(self.f.root,'source',p)
        self.assertFalse(result['sync_enabled']);self.assertFalse(ss.status(self.f.root)['pairs'][0]['enabled'])
        self.assertEqual(ss.ensure_watch(self.f.root)['status'],'not_configured')

    def test_status_is_read_only_even_with_a_legacy_schema(self):
        path=self.f.root/'session-sync.sqlite';before=path.read_bytes()
        ss.status(self.f.root)
        self.assertEqual(before,path.read_bytes())

    def test_explicit_boolean_validation(self):
        with self.assertRaisesRegex(ValueError,'dry_run'):
            Server(self.f.root).call('import_native_session',{'codex_session':'source','dry_run':'yes'})

    def test_projection_failure_retries_even_when_no_new_lines_remain(self):
        from context_hub.session_ir import native_history
        self.paginated_fixture();self.f.add_zcode('projection-retry','user','重试后应可见')
        with patch.object(native_history,'publish',side_effect=ValueError('simulated projection failure')):
            self.assertIn('error',ss.sync_once(self.f.root)[0])
        appended=self.f.path.read_bytes()
        result=ss.sync_once(self.f.root)
        self.assertNotIn('error',result[0]);self.assertEqual(self.f.path.read_bytes(),appended)
        c=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        try:
            texts=[json.loads(r[0]) for r in c.execute('SELECT item_json FROM thread_items')]
            self.assertEqual(len(texts),1);self.assertEqual(texts[0]['content'][0]['text'],'重试后应可见')
        finally:c.close()
        state=ss._db(self.f.root)
        try:self.assertEqual(state.execute('SELECT count(*) FROM projection_pending').fetchone()[0],0)
        finally:state.close()

    def test_projection_rebuild_keeps_native_nontext_items(self):
        self.paginated_fixture();self.f.add_zcode('first','user','第一条');self.f.tick()
        c=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        original=json.dumps({'type':'commandExecution','id':'native-tool','command':'historical only'})
        try:
            tid,ordinal=c.execute('SELECT turn_id,rollout_ordinal FROM thread_turns LIMIT 1').fetchone()
            c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('c1',tid,'native-tool',ordinal,1,original,'commandExecution',ordinal,1,1));c.commit()
        finally:c.close()
        self.f.add_zcode('second','user','第二条',stamp=3000);self.f.tick()
        c=sqlite3.connect(self.f.codex/'thread_history_1.sqlite')
        try:self.assertEqual(c.execute("SELECT item_json FROM thread_items WHERE item_id='native-tool'").fetchone()[0],original)
        finally:c.close()

    def test_native_zcode_active_branch_not_abandoned_messages(self):
        self.f.add_zcode('abandoned','user','已回退的分支',stamp=2000)
        self.f.add_zcode('live','user','当前分支',stamp=3000)
        c=self.native_connection();c.execute('ALTER TABLE session ADD COLUMN revert TEXT')
        c.execute('UPDATE session SET revert=? WHERE id=?',(json.dumps({'targetMessageID':'mabandoned',
            'keptMessageIDs':['mbaseline'],'branchCutAfterMessageID':'mabandoned'}),'z1'));c.commit()
        result=read_session(self.f.root,'zcode:z1')
        texts=[r['payload'].get('text') for r in result['records']]
        self.assertIn('当前分支',texts);self.assertNotIn('已回退的分支',texts)

    def test_timezone_in_source_timestamps_has_no_false_overlap(self):
        p=self.write_source(rows=[self.text('甲','2026-10-25T08:00:00+08:00'),self.text('乙','2026-10-25T00:00:01Z')])
        result=read_session(self.f.root,'codex:source')
        self.assertEqual(result['source']['latest_at'],'2026-10-25T00:00:01.000000Z')

    def test_raw_record_cache_detects_later_source_change(self):
        p=self.write_source(rows=[self.text('原始内容'*1000)])
        page=read_session(self.f.root,'codex:source',max_chars=1000)
        with p.open('a',encoding='utf-8') as out:out.write(json.dumps(self.text('新内容','2026-10-25T01:00:00Z'))+'\n')
        with self.assertRaisesRegex(ValueError,'source changed'):
            read_record(self.f.root,'codex:source',page['pending_full_records'][0],watermark=page['source']['watermark'])

    def test_rich_tool_result_original_blocks_preserved(self):
        output=[{'type':'text','text':'精确结果'},{'type':'image','url':'data:image/png;base64,aGVsbG8='}]
        rows=[{'type':'response_item','timestamp':'2026-10-25T00:00:00Z','payload':{'type':'custom_tool_call','name':'exec','call_id':'rich','input':'text(1)'}},
              {'type':'response_item','timestamp':'2026-10-25T00:00:01Z','payload':{'type':'custom_tool_call_output','call_id':'rich','output':output}}]
        result=import_codex_session(self.f.root,'source',self.write_source(rows=rows))
        c=self.native_connection();part=json.loads(c.execute('SELECT data FROM part WHERE session_id=?',(result['zcode_session_id'],)).fetchone()[0])
        self.assertEqual(part['state']['metadata']['native_result']['output'],output)

    def test_legacy_json_fingerprint_receipt_can_still_be_safely_updated(self):
        from context_hub.native_import import _fingerprint
        source=self.write_source(rows=[self.text('原始记录')]);result=import_codex_session(self.f.root,'source',source)
        c=self.native_connection();receipt=Path(result['receipt']);saved=json.loads(receipt.read_text(encoding='utf-8'))
        saved['target_fingerprint']=_fingerprint(c,result['zcode_session_id'],'json-v1')
        saved.pop('fingerprint_algorithm',None);receipt.write_text(json.dumps(saved),encoding='utf-8')
        with source.open('a',encoding='utf-8') as out:out.write(json.dumps(self.text('新增记录','2026-10-25T01:00:00Z'))+'\n')
        updated=import_codex_session(self.f.root,'source',source,replace=True)
        self.assertEqual(updated['fingerprint_algorithm'],'framed-v1');self.assertEqual(updated['messages'],2)

    def test_framed_fingerprint_detects_same_length_native_edits(self):
        source=self.write_source(rows=[self.text('原始记录')]);result=import_codex_session(self.f.root,'source',source)
        c=self.native_connection();data=c.execute('SELECT data FROM part WHERE session_id=?',(result['zcode_session_id'],)).fetchone()[0]
        changed=data.replace('原始记录','后改记录');self.assertEqual(len(changed),len(data));self.assertNotEqual(changed,data)
        c.execute('UPDATE part SET data=? WHERE session_id=?',(changed,result['zcode_session_id']));c.commit()
        with self.assertRaisesRegex(ValueError,'local changes'):import_codex_session(self.f.root,'source',source,replace=True)


if __name__=='__main__':unittest.main()
