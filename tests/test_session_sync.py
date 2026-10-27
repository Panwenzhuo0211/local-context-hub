import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager


@contextmanager
def db(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()

from context_hub import session_sync as ss
from context_hub.session_ir.live import MARKER


class NativeSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root,self.codex,self.zcode = base/'hub',base/'codex',base/'zcode'
        self.root.mkdir();(self.codex/'sessions').mkdir(parents=True)
        (self.zcode/'cli/db').mkdir(parents=True)
        (self.root/'config.json').write_text(json.dumps({'schema_version':1,
            'native_sync_runtime_writer':False,  # offline append compatibility fixtures only
            'sources':{'codex':str(self.codex),'zcode':str(self.zcode)}}),encoding='utf-8')
        self.native = self.zcode/'cli/db/db.sqlite'
        with db(self.native) as con:
            con.executescript('''
                CREATE TABLE session(id TEXT PRIMARY KEY,directory TEXT,time_updated INTEGER);
                CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT REFERENCES session(id),
                    time_created INTEGER,time_updated INTEGER,data TEXT,sequence INTEGER);
                CREATE TABLE part(id TEXT PRIMARY KEY,message_id TEXT REFERENCES message(id),
                    session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT,sequence INTEGER);
            ''')
            con.execute('INSERT INTO session VALUES (?,?,?)',('z1',str(base),1000))
        self.path = self.codex/'sessions/rollout-c1.jsonl'
        self.path.write_text(json.dumps({'type':'session_meta','payload':{'id':'c1','cwd':str(base)}})+'\n',encoding='utf-8')
        self.add_zcode('baseline','user','原有历史',1000)
        self.baseline = self.native_rows()
        self.pair = ss.enroll(self.root,'z1','c1',self.path,replay_export=False)

    def native_rows(self):
        with db(self.native) as con:
            return {t:con.execute(f'SELECT * FROM {t} ORDER BY id').fetchall() for t in ('message','part')}

    def add_zcode(self,key,role,text,stamp=2000,completed=True,tool=None):
        info = {'role':role,'time':{'created':stamp}}
        if role=='assistant' and completed:info['time']['completed']=stamp+1
        data = tool or {'type':'text','text':text,'time':{'start':stamp,'end':stamp+1}}
        with db(self.native) as c:
            c.execute('INSERT INTO message VALUES (?,?,?,?,?,?)',
                      ('m'+key,'z1',stamp,stamp,json.dumps(info),stamp))
            c.execute('INSERT INTO part VALUES (?,?,?,?,?,?,?)',
                      ('p'+key,'m'+key,'z1',stamp,stamp,json.dumps(data),stamp))

    def append(self,*rows):
        with self.path.open('a',encoding='utf-8') as f:
            for row in rows:f.write(json.dumps(row,ensure_ascii=False)+'\n')

    def text(self,role,text,final=False):
        return {'type':'response_item','timestamp':'2026-10-25T02:00:00Z','payload':{
            'type':'message','role':role,'content':[{'type':'input_text' if role=='user' else 'output_text','text':text}],
            **({'phase':'final'} if final else {})}}

    def tick(self):
        result = ss.sync_once(self.root)
        self.assertFalse([x for x in result if x.get('error')],result)
        return result

    def test_bidirectional_exact_text_and_no_echo_after_restart(self):
        self.append(self.text('user','Codex 新指令：α 🚀'),self.text('assistant','已处理',True))
        self.add_zcode('new','user','ZCode 新指令：中文 🚀')
        result = self.tick()[0]
        self.assertEqual(result['to_zcode_parts'],2)
        self.assertEqual(result['to_codex_parts'],1)
        allrows = self.native_rows()
        imported = [json.loads(r[5]) for r in allrows['part'] if r[0].startswith('part_chsync_')]
        self.assertCountEqual([r['text'] for r in imported],['Codex 新指令：α 🚀','已处理'])
        lines = [json.loads(x) for x in self.path.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(r.get('payload',{}).get('message')=='ZCode 新指令：中文 🚀' for r in lines))
        before = self.path.read_bytes(),self.native_rows()
        for _ in range(3):self.tick()
        self.assertEqual((self.path.read_bytes(),self.native_rows()),before)
        for table,rows in self.baseline.items():
            self.assertTrue(all(r in allrows[table] for r in rows))
        self.assertTrue(Path(self.pair['backup']).exists())

    def test_wait_for_codex_turn_and_unicode_partial_tail(self):
        self.append({'type':'event_msg','payload':{'type':'task_started'}},self.text('user','尚未完成'))
        self.add_zcode('queued','user','等待投递')
        original = self.path.read_bytes()
        self.tick()
        self.assertEqual(self.path.read_bytes(),original)
        # Complete source messages are delivered before the model's turn ends.
        self.assertEqual(len(self.native_rows()['part']),3)
        self.assertEqual(ss.status(self.root)['pairs'][0]['status'],'waiting_codex_idle')
        consumed=ss.status(self.root)['pairs'][0]['offset']
        tail = (json.dumps(self.text('assistant','完成 🚀',True),ensure_ascii=False)+'\n').encode('utf-8')
        with self.path.open('ab') as f:f.write(tail[:-3])
        self.tick()
        self.assertEqual(ss.status(self.root)['pairs'][0]['offset'],consumed)
        with self.path.open('ab') as f:f.write(tail[-3:])
        self.tick()
        self.assertIn('等待投递',self.path.read_text(encoding='utf-8'))

    def test_wait_for_zcode_stream_and_pending_tool(self):
        self.add_zcode('stream','assistant','输出中',completed=False)
        self.add_zcode('tool','assistant','',tool={'type':'tool','tool':'Bash','callID':'zc-tool',
                                                 'state':{'status':'running','input':{'command':'echo ok'}}})
        before = self.path.read_bytes()
        self.tick();self.assertEqual(self.path.read_bytes(),before)
        with db(self.native) as c:
            info=json.loads(c.execute("SELECT data FROM message WHERE id='mstream'").fetchone()[0])
            info['time']['completed']=3000
            c.execute("UPDATE message SET data=? WHERE id='mstream'",(json.dumps(info),))
            data=json.loads(c.execute("SELECT data FROM part WHERE id='ptool'").fetchone()[0])
            data['state'].update(status='completed',output='ok')
            c.execute("UPDATE part SET data=? WHERE id='ptool'",(json.dumps(data),))
        self.tick()
        lines=[json.loads(x) for x in self.path.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(r.get('payload',{}).get('call_id')=='zc-tool' for r in lines))

    def test_tool_calls_and_results_are_native_history_not_executed(self):
        self.append(self.text('user','检查'),
            {'type':'response_item','timestamp':'2026-10-25T00:00:00Z','payload':{'type':'function_call','name':'exec_command',
                'call_id':'c-tool','arguments':json.dumps({'cmd':'NEVER EXECUTE THIS'})}},
            {'type':'response_item','timestamp':'2026-10-25T00:00:01Z','payload':{'type':'function_call_output','call_id':'c-tool','output':'历史结果'}},
            self.text('assistant','结束',True))
        self.tick()
        with db(self.native) as c:
            tools=[json.loads(r[0]) for r in c.execute("SELECT data FROM part WHERE json_extract(data,'$.type')='tool'")]
        self.assertEqual(tools[0]['state']['status'],'completed')
        self.assertEqual(tools[0]['state']['output'],'历史结果')
        self.assertEqual(tools[0]['tool'],'Bash')
        self.assertEqual(tools[0]['state']['time'],{'start':1792886400000,'end':1792886401000})

    def test_rewritten_prefix_stops_without_overwriting_history(self):
        self.path.write_text(self.path.read_text().replace('session_meta','different___'),encoding='utf-8')
        result=ss.sync_once(self.root)
        self.assertIn('error',result[0])
        self.assertEqual(self.native_rows(),self.baseline)

    def test_retry_after_native_commit_and_codex_append_crash_is_idempotent(self):
        self.append(self.text('user','只导入一次'),self.text('assistant','完成',True))
        self.add_zcode('retry','user','只回传一次')
        original=ss._append
        def crash(*args):
            original(*args)
            raise OSError('simulated crash after native append')
        with patch.object(ss,'_append',side_effect=crash):
            self.assertIn('error',ss.sync_once(self.root)[0])
        before=self.path.read_bytes(),self.native_rows()
        self.tick();self.tick()
        self.assertEqual((self.path.read_bytes(),self.native_rows()),before)

    def test_metadata_tail_consumed_and_pair_pause(self):
        self.append(self.text('assistant','完成',True),{'type':'event_msg','payload':{'type':'token_count'}})
        self.add_zcode('meta','user','投递')
        self.assertEqual(self.tick()[0]['to_codex_parts'],1)
        ss.main(['--data-dir',str(self.root),'pause','--pair',self.pair['id']])
        before=self.path.read_bytes()
        self.add_zcode('paused','user','暂停后不投递')
        self.assertEqual(ss.sync_once(self.root),[])
        self.assertEqual(self.path.read_bytes(),before)

    def test_enrollment_identity_and_unique_pairs(self):
        self.assertEqual(ss.enroll(self.root,'z1','c1',self.path)['id'],self.pair['id'])
        with self.assertRaisesRegex(ValueError,'different pair'):
            ss.enroll(self.root,'other','c1',self.path)
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            ss._rollout(ss._config(self.root),self.path,'not-c1')

    def test_system_runtime_content_does_not_become_user_instruction(self):
        self.append(self.text('developer','历史系统文本'),self.text('assistant','结束',True))
        self.tick()
        with db(self.native) as c:
            user_count=c.execute("SELECT count(*) FROM message WHERE json_extract(data,'$.role')='user'").fetchone()[0]
        self.assertEqual(user_count,1)

    def test_watch_single_tick_records_heartbeat(self):
        ss.watch(self.root,interval=1,iterations=1)
        self.assertEqual(ss.status(self.root)['worker']['status'],'running')

    def test_paginated_append_keeps_contiguous_ordinals(self):
        # Codex's newer desktop format requires an ordinal on every record.
        self.path.write_text(json.dumps({'type':'session_meta','ordinal':0,
            'payload':{'id':'c1','history_mode':'paginated','originator':'context-hub'}})+'\n',encoding='utf-8')
        state=ss._db(self.root)
        try:
            raw=self.path.read_bytes()
            with state:state.execute('UPDATE pairs SET offset=?,prefix=?',(len(raw),ss._sha(raw)))
        finally:state.close()
        self.add_zcode('ordinal','user','分页历史的新消息')
        result=ss.sync_once(self.root)
        self.assertIn('projection',result[0]['error'])
        rows=[json.loads(x) for x in self.path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['ordinal'] for r in rows],list(range(len(rows))))

    def test_reenable_pair_keeps_cursor_and_watch_start_is_idempotent(self):
        self.append(self.text('assistant','先完成',True));self.tick()
        state=ss._db(self.root)
        try:
            with state:state.execute('UPDATE pairs SET enabled=0')
        finally:state.close()
        old_offset=ss.status(self.root)['pairs'][0]['offset']
        resumed=ss.enroll(self.root,'z1','c1',self.path)
        self.assertTrue(resumed['enabled']);self.assertEqual(resumed['offset'],old_offset)
        with patch.object(ss.subprocess,'Popen') as spawn:
            from context_hub.store import atomic
            import time
            atomic(self.root/'session-sync-worker.json',json.dumps({'status':'running','heartbeat':time.time()}))
            self.assertEqual(ss.ensure_watch(self.root)['status'],'already_running')
            spawn.assert_not_called()

    def test_equal_timestamps_preserve_native_message_order_and_parent(self):
        self.add_zcode('zzz','user','先发',stamp=2000)
        self.add_zcode('aaa','user','后发',stamp=2000)
        self.append(self.text('assistant','接着回答',True))
        self.tick()
        rows=[json.loads(x) for x in self.path.read_text(encoding='utf-8').splitlines()]
        texts=[r['payload']['message'] for r in rows if r.get(MARKER) and
               r.get('payload',{}).get('type')=='user_message']
        self.assertEqual(texts,['先发','后发'])
        with db(self.native) as c:
            incoming=c.execute("SELECT data FROM message WHERE id LIKE 'msg_chsync_%'").fetchone()
        self.assertEqual(json.loads(incoming[0])['parentID'],'maaa')

    def test_sync_append_rebuilds_paginated_projection(self):
        # Codex desktop reads a mirror through its pagination projection;
        # synced appends must advance it or new messages stay invisible.
        history = self.codex/'thread_history_1.sqlite'
        with db(history) as c:
            c.executescript("""
                CREATE TABLE thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal,started_at_ms,completed_at_ms);
                CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at,completed_at,duration_ms,first_user_item_id,final_agent_item_id,rollout_byte_offset,rollout_end_ordinal,rollout_end_byte_offset,root_turn_id);
                CREATE TABLE thread_history_projection_state(thread_id PRIMARY KEY,next_rollout_byte_offset,next_rollout_ordinal);
            """)
        with db(self.codex/'state_5.sqlite') as c:
            c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, history_mode TEXT)')
        self.path.write_text(json.dumps({'type':'session_meta','ordinal':0,
            'payload':{'id':'c1','cwd':str(self.root),'originator':'context-hub',
                       'history_mode':'paginated'}})+'\n',encoding='utf-8')
        state=ss._db(self.root)
        try:
            raw=self.path.read_bytes()
            with state:state.execute('UPDATE pairs SET offset=?,prefix=?',(len(raw),ss._sha(raw)))
        finally:state.close()
        self.add_zcode('proj','user','分页镜像的新消息')
        self.tick()
        with db(history) as c:
            items=[json.loads(r[0]) for r in c.execute('SELECT item_json FROM thread_items')]
            offset,ordinal=c.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state').fetchone()
        texts=[i['content'][0]['text'] for i in items if i['type']=='userMessage']
        self.assertIn('分页镜像的新消息',texts)
        self.assertEqual(offset,self.path.stat().st_size)

    def test_import_creates_native_zcode_session(self):
        c2=self.codex/'sessions/rollout-c2.jsonl'
        rows=[{'type':'session_meta','payload':{'id':'c2','cwd':str(self.root)}},
              {'type':'response_item','timestamp':'2026-10-25T02:00:00Z','payload':{
                  'type':'message','role':'user','content':[{'type':'input_text','text':'把这场会话搬到 ZCode'}]}},
              {'type':'response_item','timestamp':'2026-10-25T02:00:01Z','payload':{
                  'type':'function_call','name':'exec_command','call_id':'fx1',
                  'arguments':json.dumps({'cmd':'ls'})}},
              {'type':'response_item','timestamp':'2026-10-25T02:00:02Z','payload':{
                  'type':'function_call_output','call_id':'fx1','output':'a.txt'}},
              {'type':'response_item','timestamp':'2026-10-25T02:00:03Z','payload':{
                  'type':'message','role':'assistant','content':[{'type':'output_text','text':'已完成'}]}},
              {'type':'response_item','timestamp':'2026-10-25T02:00:04Z','payload':{
                  'type':'message','role':'user','content':[{'type':'input_text','text':'同步回传的行'}]},
                  MARKER:{'pair':'p','event':'zcode:part_x'}}]
        c2.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        with db(self.native) as c:
            c.execute('ALTER TABLE session ADD COLUMN title TEXT')
        result=ss.import_codex_session(self.root,'c2',c2)
        sid=result['zcode_session_id']
        with db(self.native) as c:
            sess=c.execute('SELECT title FROM session WHERE id=?',(sid,)).fetchone()
            self.assertIn('把这场会话搬到 ZCode',sess[0])
            parts=[json.loads(r[0]) for r in c.execute('SELECT data FROM part WHERE session_id=?',(sid,))]
            texts=[p_['text'] for p_ in parts if p_.get('type')=='text']
            tools=[p_ for p_ in parts if p_.get('type')=='tool']
        self.assertIn('把这场会话搬到 ZCode',texts)
        self.assertIn('已完成',texts)  # real agent utterance: no historical label
        self.assertIn('同步回传的行',texts)  # sync echo imported once, marker stripped
        self.assertEqual(tools[0]['tool'],'Bash')
        self.assertEqual(tools[0]['state']['output'],'a.txt')
        markers=[(p_.get('metadata') or {}).get('context_hub_sync') for p_ in parts
                 if (p_.get('metadata') or {}).get('context_hub_sync')]
        self.assertTrue(markers and all(m.get('pair','').startswith('import_') for m in markers))
        with self.assertRaisesRegex(ValueError,'already exists'):
            ss.import_codex_session(self.root,'c2',c2)
        again=ss.import_codex_session(self.root,'c2',c2,replace=True)
        self.assertEqual(again['zcode_session_id'],sid)

    def test_enroll_replace_migrates_pair_to_new_mirror(self):
        c2=self.codex/'sessions/rollout-c2r.jsonl'
        c2.write_text(json.dumps({'type':'session_meta','payload':{'id':'c2r','cwd':str(self.root)}})+'\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'different pair'):
            ss.enroll(self.root,'z1','c2r',c2)
        migrated=ss.enroll(self.root,'z1','c2r',c2,replace=True)
        self.assertNotEqual(migrated['id'],self.pair['id'])
        state=ss._db(self.root)
        try:
            self.assertEqual([r['id'] for r in state.execute('SELECT id FROM pairs')],[migrated['id']])
            seeded=state.execute('SELECT count(*) FROM parts WHERE pair_id=?',(migrated['id'],)).fetchone()[0]
        finally:state.close()
        self.assertEqual(seeded,len(self.baseline['part']))


if __name__=='__main__':unittest.main()
