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
