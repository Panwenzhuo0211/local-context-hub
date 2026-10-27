import hashlib
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from context_hub.adapters import readonly
from context_hub.cli import sync, validate
from context_hub.model import project_for
from context_hub.store import Store, atomic, exclusive
from context_hub.views import render

class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.codex=self.base/'codex';self.zcode=self.base/'zcode';self.hub=self.base/'hub'
        (self.codex/'sessions').mkdir(parents=True);(self.zcode/'cli/db').mkdir(parents=True)
        self.project=str(self.base/'project')
        self.config={'schema_version':1,'sources':{'codex':str(self.codex),'zcode':str(self.zcode)},'projects':{'shared':[self.project]},'generic_roots':[]}
        self.log=self.codex/'sessions/one.jsonl'
        self.events=[{'type':'session_meta','ordinal':0,'payload':{'id':'c1','cwd':self.project}},
                     {'type':'response_item','ordinal':1,'payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'下一步验证'}]}}]
        self.write_log()
        c=sqlite3.connect(self.zcode/'cli/db/db.sqlite')
        c.executescript('''CREATE TABLE session(id TEXT,directory TEXT,time_updated INTEGER);
        CREATE TABLE message(id TEXT,session_id TEXT,data TEXT,time_created INTEGER);
        CREATE TABLE part(id TEXT,session_id TEXT,message_id TEXT,data TEXT,time_created INTEGER);
        CREATE TABLE session_entry(id TEXT,session_id TEXT,data TEXT,time_updated INTEGER);
        CREATE TABLE todo(session_id TEXT,position INTEGER,content TEXT);
        CREATE TABLE session_target(session_id TEXT,objective TEXT);''')
        c.execute('INSERT INTO session VALUES (?,?,?)',('z1',self.project,1))
        c.execute('INSERT INTO message VALUES (?,?,?,?)',('m1','z1',json.dumps({'role':'assistant'}),1))
        c.execute('INSERT INTO part VALUES (?,?,?,?,?)',('p1','z1','m1',json.dumps({'type':'text','text':'结论需核实'}),1))
        c.commit();c.close();self.store=Store(self.hub)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def write_log(self):self.log.write_text(''.join(json.dumps(e)+'\n' for e in self.events))
    def count(self):return self.store.db.execute('SELECT count(*) FROM revisions').fetchone()[0]
    def test_idempotence_and_source_unchanged(self):
        db=self.zcode/'cli/db/db.sqlite';before=[hashlib.sha256(p.read_bytes()).hexdigest() for p in (db,self.log)]
        sync(self.store,self.config);count=self.count();second=sync(self.store,self.config)
        self.assertGreater(count,0);self.assertEqual(count,self.count())
        self.assertTrue(all(x['new_revisions']==0 for x in second['adapters'].values()))
        self.assertEqual(before,[hashlib.sha256(p.read_bytes()).hexdigest() for p in (db,self.log)])
        self.assertEqual(0,second['adapters']['codex']['examined'])
    def test_partial_line_and_identical_repeated_messages(self):
        sync(self.store,self.config);count=self.count()
        event={'type':'response_item','ordinal':2,'payload':self.events[1]['payload']};data=json.dumps(event).encode()
        with self.log.open('ab') as f:f.write(data[:20])
        result=sync(self.store,self.config);self.assertEqual(count,self.count());self.assertTrue(result['warnings'])
        with self.log.open('ab') as f:f.write(data[20:]+b'\n')
        sync(self.store,self.config);self.assertEqual(count+1,self.count())
    def test_rewrite_and_database_revision_preserved(self):
        sync(self.store,self.config);count=self.count();self.events[1]['payload']['content'][0]['text']='different';self.write_log()
        c=sqlite3.connect(self.zcode/'cli/db/db.sqlite');c.execute('UPDATE part SET data=?',(json.dumps({'type':'text','text':'changed'}),));c.commit();c.close()
        result=sync(self.store,self.config);self.assertEqual(count+2,self.count());self.assertTrue(any('rewritten' in w for w in result['warnings']))
        self.assertEqual(2,self.store.db.execute("SELECT count(*) FROM revisions WHERE source_key='part:p1'").fetchone()[0])
    def test_failure_rolls_back_records_and_cursor(self):
        with self.log.open('ab') as f:f.write(b'{bad json}\n')
        with self.assertRaises(ValueError):sync(self.store,self.config)
        self.assertEqual(0,self.count());self.assertEqual(0,self.store.db.execute('SELECT count(*) FROM cursors').fetchone()[0])
        self.write_log();sync(self.store,self.config);self.assertGreater(self.count(),0)
    def test_readonly_wal(self):
        path=self.zcode/'cli/db/db.sqlite';c=sqlite3.connect(path);c.execute('PRAGMA journal_mode=WAL');c.execute('INSERT INTO session VALUES (?,?,?)',('z2',self.project,2));c.commit()
        with readonly(path) as r:
            self.assertEqual(2,r.execute('SELECT count(*) FROM session').fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):r.execute('DELETE FROM session')
        c.close()
    def test_mapping_updates_skipped_log_records(self):
        sync(self.store,self.config);self.config['session_projects']={'codex:c1':'other'};sync(self.store,self.config)
        self.assertEqual(2,len(list(self.store.rows('other'))))
        self.assertEqual('child',project_for(self.project+'/child/a','codex','s',{'projects':{'parent':[self.project],'child':[self.project+'/child']}}))
    def test_notes_opinions_handoff_and_manual_backup(self):
        sync(self.store,self.config)
        with self.store.db:
            for author,body in [('codex','方案可行'),('zcode','证据不足')]:self.store.note('shared',author,'opinion','新颖性',body,[1])
            self.store.note('shared','user','task','验证','核查证据',[1])
        folder=render(self.store,'shared','zcode')
        self.assertIn('方案可行',(folder/'DISAGREEMENTS.md').read_text());self.assertIn('证据不足',(folder/'DISAGREEMENTS.md').read_text())
        self.assertIn('Handoff to zcode',(folder/'HANDOFF.md').read_text());self.assertIn('结论需核实',(folder/'TRANSCRIPT.md').read_text())
        self.assertIn('下一步验证',(folder/'CANDIDATES.md').read_text());(folder/'MEMORY.md').write_text('manual change');render(self.store,'shared')
        self.assertTrue(any(p.read_text()=='manual change' for p in (folder/'history').iterdir()))
        self.assertEqual(self.count(),len(gzip.decompress((folder/'archive.jsonl.gz').read_bytes()).splitlines()))
    def test_overlap_symlink_and_lock(self):
        with self.assertRaises(ValueError):validate(self.codex/'data',self.config)
        with self.assertRaises(ValueError):validate(self.base,self.config)
        dest=self.hub/'evil';dest.symlink_to(self.log)
        with self.assertRaises(ValueError):atomic(dest,'oops')
        with exclusive(self.hub):
            with self.assertRaises(RuntimeError):
                with exclusive(self.hub):pass
    def test_backup_and_generic_separation(self):
        sync(self.store,self.config);path=self.store.backup();c=sqlite3.connect(path)
        self.assertEqual(self.count(),c.execute('SELECT count(*) FROM revisions').fetchone()[0]);c.close();cfg={'generic_roots':[self.project]}
        self.assertNotEqual(project_for(self.project,'zcode','1',cfg),project_for(self.project,'zcode','2',cfg))

    def test_backup_includes_unrebuildable_assets(self):
        from context_hub.matters import Matters
        sync(self.store,self.config)
        (self.hub/'config.json').write_text(json.dumps(self.config))
        m=Matters(self.hub);m.create('事项');m.close()
        path=self.store.backup();folder=Path(path).parent
        self.assertTrue(all((folder/n).is_file() for n in ('state.sqlite','matters.sqlite','config.json')))
        c=sqlite3.connect(folder/'matters.sqlite')
        self.assertEqual(1,c.execute('SELECT count(*) FROM matters').fetchone()[0]);c.close()

    def test_session_ir_restore_and_emit(self):
        import tempfile
        from context_hub.session_ir import restore_from_archive, emit_codex, mirror_map
        sync(self.store,self.config)
        # restore: moments rebuilt from archived envelopes
        ir=restore_from_archive(self.store,'z1')
        self.assertEqual(ir.source_harness,'zcode')
        self.assertEqual(ir.cwd,self.project)
        kinds={}
        for m in ir.moments: kinds[m.kind]=kinds.get(m.kind,0)+1
        self.assertNotIn('user_text',kinds)  # fixture assistant-only text
        self.assertGreaterEqual(kinds.get('assistant_text'),1)
        # emit: deterministic id -> native rollout jsonl in a sandbox dir
        with tempfile.TemporaryDirectory() as t:
            target=Path(t)/'codexhome'
            ir2=restore_from_archive(self.store,'z1')
            self.assertEqual(ir.id,ir2.id)  # idempotent across rebuilds
            result=emit_codex.render(ir2, session_id=ir2.id, target_dir=target)
            self.assertTrue(result.primary_path.exists())
            first=json.loads(result.primary_path.read_text(encoding='utf-8').splitlines()[0])
            self.assertEqual(first['payload']['id'],ir.id)
            self.assertEqual(first['payload']['originator'],'context-hub')
            self.assertEqual(first['payload']['source'],'cli')
            transcript=result.primary_path.read_text(encoding='utf-8')
            self.assertIn('结论需核实',transcript)
            mirror_map.record(self.hub, zcode_id='z1', codex_id=result.session_id)
            self.assertEqual(mirror_map.get_codex_for_zcode(self.hub,'z1'),result.session_id)

    def test_data_dir_relocation_pointer(self):
        import tempfile
        from context_hub.cli import resolve_data_dir
        with tempfile.TemporaryDirectory() as t:
            base=Path(t)/'fallback'; base.mkdir()
            self.assertEqual(resolve_data_dir(base),base)
            target=Path(t)/'real'; target.mkdir()
            (base/'LOCATION').write_text(str(target),encoding='utf-8')
            self.assertEqual(resolve_data_dir(base),target)
            (base/'LOCATION').write_text(str(Path(t)/'missing'),encoding='utf-8')
            self.assertEqual(resolve_data_dir(base),base)  # nonexistent target ignored

    def test_v1_archive_migrates_to_segments(self):
        import tempfile
        from context_hub.model import envelope, pack
        with tempfile.TemporaryDirectory() as t:
            hub=Path(t)
            c=sqlite3.connect(hub/'archive.sqlite')
            c.executescript('''CREATE TABLE revisions(id INTEGER PRIMARY KEY,adapter TEXT,session TEXT,source_key TEXT,
                revision TEXT,project TEXT,envelope BLOB,seen_at REAL,UNIQUE(adapter,session,source_key,revision));
                CREATE TABLE heads(adapter TEXT,session TEXT,source_key TEXT,revision_id INTEGER,PRIMARY KEY(adapter,session,source_key));
                CREATE TABLE cursors(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE pending_index(key TEXT PRIMARY KEY,revision_id INTEGER,project TEXT);''')
            env=envelope('zcode','z9','part:p9',{},'/m','assistant','迁移内容',kind='text')
            c.execute('INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?)',(1,'zcode','z9','part:p9','rev1','shared',pack(env),1.0))
            c.execute('INSERT INTO heads VALUES (?,?,?,?)',('zcode','z9','part:p9',1))
            c.execute('INSERT INTO cursors VALUES (?,?)',('k','{"v":1}'));c.commit();c.close()
            store=Store(hub)
            try:
                row=store.db.execute('SELECT * FROM revisions').fetchone()
                self.assertEqual(row['revision'],'rev1')
                from context_hub.model import unpack
                e=unpack(store.envelope_of(row));self.assertEqual(e['text'],'迁移内容')
                self.assertEqual(store.db.execute('SELECT value FROM cursors WHERE key=?',('k',)).fetchone()[0],'{"v":1}')
                self.assertTrue((hub/'archive.sqlite').exists())
                self.assertTrue((hub/'state.sqlite').exists())
            finally:store.close()
    def test_paginated_history_and_realtime(self):
        c=sqlite3.connect(self.codex/'thread_history_1.sqlite')
        c.executescript('''CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,item_json TEXT,created_at_ms INTEGER);
        CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,started_at INTEGER);
        CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,item_json TEXT,created_at_ms INTEGER);''')
        c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?)',('c1','t1','i1',json.dumps({'type':'agentMessage','text':'native message'}),1))
        c.execute('INSERT INTO thread_realtime_items VALUES (?,?,?,?)',('c1','rt1',json.dumps({'type':'transcript_segment','role':'user','text':'voice'}),2));c.commit();c.close()
        c=sqlite3.connect(self.codex/'state_5.sqlite');c.execute('CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT)');c.execute('INSERT INTO threads VALUES (?,?,?)',('c1',self.project,str(self.log)));c.commit();c.close()
        sync(self.store,self.config);folder=render(self.store,'shared');text=(folder/'TRANSCRIPT.md').read_text()
        self.assertIn('native message',text);self.assertIn('voice',text);self.assertGreater(len(list(self.store.rows('shared'))),4)

    def test_slim_stable_identity_and_compact(self):
        import tempfile
        from context_hub.model import envelope, slim
        from context_hub.store import read_blob
        with tempfile.TemporaryDirectory() as t:
            hub=Path(t);store=Store(hub)
            try:
                huge={'record':{'payload':'X'*400000},'raw_line':'{"payload":"'+('X'*400000)+'"}'}
                event=envelope('zcode','s1','part:a',huge,'/w','assistant','正文保持原样',kind='text')
                store.ingest(event,{});store.db.commit()
                # Same original event dedups against the slimmed copy: identity is pre-slim.
                store.ingest(event,{});store.db.commit()
                self.assertEqual(1,store.db.execute('SELECT count(*) FROM revisions').fetchone()[0])
                row=store.db.execute('SELECT * FROM revisions').fetchone()
                e=json.loads(__import__('zlib').decompress(read_blob(hub,row['seg'],row['off'],row['len'])))
                self.assertEqual(e['text'],'正文保持原样')
                self.assertTrue(e['raw'].get('_raw_truncated'))
                self.assertNotIn('raw_line',e['raw'])
                # A fat legacy blob gets compacted away without touching identity.
                fat=envelope('zcode','s1','part:b',{'record':{'blob':'Y'*300000}},'/w','assistant','旧胖载荷',kind='text')
                from context_hub.model import pack
                seg,off,length=store._append(pack(fat));store.flush_log()
                with store.db:
                    store.db.execute('INSERT INTO revisions(adapter,session,source_key,revision,project,seg,off,len,seen_at) VALUES (?,?,?,?,?,?,?,?,?)',
                                     ('zcode','s1','part:b','r-fat','w',seg,off,length,1.0))
                    store.db.execute('INSERT OR REPLACE INTO heads VALUES (?,?,?,?)',
                                     ('zcode','s1','part:b',store.db.execute('SELECT max(id) FROM revisions').fetchone()[0]))
                result=store.compact()
                self.assertLess(result['bytes_after'],result['bytes_before'])
                self.assertEqual(2,store.db.execute('SELECT count(*) FROM revisions').fetchone()[0])
                for row in store.db.execute('SELECT * FROM revisions'):
                    blob=read_blob(hub,row['seg'],row['off'],row['len'])
                    __import__('zlib').decompress(blob)  # every frame still readable
            finally:store.close()

    def test_opencode_adapter_incremental(self):
        oc=self.base/'opencode';oc.mkdir()
        c=sqlite3.connect(oc/'opencode.db')
        c.executescript('''CREATE TABLE session(id TEXT,directory TEXT,title TEXT,time_created INTEGER,time_updated INTEGER);
        CREATE TABLE message(id TEXT,session_id TEXT,data TEXT,time_created INTEGER,time_updated INTEGER);
        CREATE TABLE part(id TEXT,session_id TEXT,message_id TEXT,data TEXT,time_created INTEGER,time_updated INTEGER);
        CREATE TABLE todo(session_id TEXT,content TEXT,position INTEGER,time_updated INTEGER);''')
        c.execute('INSERT INTO session VALUES (?,?,?,?,?)',('o1',self.project,'OpenCode 会话',1000,1000))
        c.execute('INSERT INTO message VALUES (?,?,?,?,?)',('m1','o1',json.dumps({'role':'user'}),1000,1000))
        c.execute('INSERT INTO part VALUES (?,?,?,?,?,?)',('p1','o1','m1',json.dumps({'type':'text','text':'opencode 的结论'}),1000,1000))
        c.execute('INSERT INTO todo VALUES (?,?,?,?)',('o1','待办甲',1,1000))
        c.commit();c.close()
        config={**self.config,'sources':{**self.config['sources'],'opencode':str(oc)}}
        result=sync(self.store,config)
        self.assertGreater(result['adapters']['opencode']['new_revisions'],0)
        second=sync(self.store,config)
        self.assertEqual(0,second['adapters']['opencode']['new_revisions'])
        self.assertLessEqual(second['adapters']['opencode']['examined'],6)
        c=sqlite3.connect(oc/'opencode.db')
        c.execute('INSERT INTO part VALUES (?,?,?,?,?,?)',('p2','o1','m1',json.dumps({'type':'text','text':'新一条'}),2000,2000))
        c.commit();c.close()
        third=sync(self.store,config)
        self.assertEqual(1,third['adapters']['opencode']['new_revisions'])

    def test_incremental_watermark_skips_old_rows(self):
        sync(self.store,self.config);count=self.count()
        c=sqlite3.connect(self.zcode/'cli/db/db.sqlite')
        # A fresh row advances past the watermark; a backdated row does not.
        c.execute('INSERT INTO part VALUES (?,?,?,?,?)',('p2','z1','m1',json.dumps({'type':'text','text':'新增结论'}),2))
        c.execute('INSERT INTO part VALUES (?,?,?,?,?)',('p3','z1','m1',json.dumps({'type':'text','text':'旧行不该入档'}),0))
        c.commit();c.close()
        result=sync(self.store,self.config)
        self.assertEqual(count+1,self.count())
        self.assertLessEqual(result['adapters']['zcode']['examined'],10)
        self.assertFalse(self.store.db.execute("SELECT count(*) FROM revisions WHERE source_key='part:p3'").fetchone()[0])

    def test_incremental_catchup_and_full_reverify(self):
        sync(self.store,self.config)
        c=sqlite3.connect(self.zcode/'cli/db/db.sqlite')
        c.execute('INSERT INTO part VALUES (?,?,?,?,?)',('p2','z1','m1',json.dumps({'type':'text','text':'后续补充'}),5))
        c.commit();c.close()
        sync(self.store,self.config)
        self.assertEqual(1,self.store.db.execute("SELECT count(*) FROM revisions WHERE source_key='part:p2'").fetchone()[0])
        # Drifted timestamps older than the watermark are caught by the weekly full pass.
        self.store.db.execute("UPDATE cursors SET value=? WHERE key='zcode-sqlite-watermark'",
                              (json.dumps({'message':1,'part':1,'verified_at':0}),))
        self.store.db.commit()
        c=sqlite3.connect(self.zcode/'cli/db/db.sqlite')
        c.execute('INSERT INTO part VALUES (?,?,?,?,?)',('p3','z1','m1',json.dumps({'type':'text','text':'漂移行'}),0))
        c.commit();c.close()
        sync(self.store,self.config)
        self.assertEqual(1,self.store.db.execute("SELECT count(*) FROM revisions WHERE source_key='part:p3'").fetchone()[0])

    def test_moved_logs_truncation_and_deletion_retention(self):
        sync(self.store,self.config);count=self.count()
        archived=self.codex/'archived_sessions';archived.mkdir()
        self.log.rename(archived/self.log.name)
        sync(self.store,self.config);self.assertEqual(count,self.count())
        (archived/self.log.name).write_text(json.dumps(self.events[0])+'\n')
        sync(self.store,self.config);self.assertEqual(count,self.count())
        c=sqlite3.connect(self.zcode/'cli/db/db.sqlite');c.execute('DELETE FROM part');c.commit();c.close()
        sync(self.store,self.config);self.assertEqual(count,self.count())

    def test_completed_task_and_symlink_parent(self):
        sync(self.store,self.config)
        with self.store.db:
            self.store.note('shared','user','task','verify','still open',[])
            self.store.note('shared','user','task','verify','finished',[],'done')
        folder=render(self.store,'shared')
        self.assertNotIn('still open',(folder/'HANDOFF.md').read_text())
        self.assertIn('still open',(folder/'TASKS.md').read_text())
        other=Store(self.base/'other')
        (other.root/'projects').symlink_to(self.codex,target_is_directory=True)
        try:
            with self.assertRaises(ValueError):render(other,'shared')
        finally:other.close()

    def test_lightweight_handoff_does_not_repackage(self):
        sync(self.store,self.config)
        folder=render(self.store,'shared',include_archive=False)
        self.assertFalse((folder/'archive.jsonl.gz').exists())
        render(self.store,'shared')
        before=(folder/'archive.jsonl.gz').read_bytes()
        render(self.store,'shared','codex',include_archive=False)
        self.assertEqual(before,(folder/'archive.jsonl.gz').read_bytes())
        self.assertTrue((folder/'HANDOFF.md').read_text().startswith('# Handoff to codex'))

if __name__=='__main__':unittest.main()
