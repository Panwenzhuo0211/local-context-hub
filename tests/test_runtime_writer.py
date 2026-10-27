import hashlib,json,sqlite3,unittest
from unittest.mock import patch
from context_hub import collision_repair,session_sync as ss
from context_hub.session_ir.native_sync import encode
from context_hub.session_ir.schema import UserText
from context_hub.session_ir.live import MARKER,decode_codex

class RuntimeWriterTests(unittest.TestCase):
    def setUp(self):
        from test_native_sync_wire import NativeWireTests
        self.fixture=NativeWireTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups);self.f=self.fixture.f
        config=json.loads((self.f.root/'config.json').read_text());config['native_sync_runtime_writer']=True
        (self.f.root/'config.json').write_text(json.dumps(config))

    def test_two_source_turns_queue_without_changing_running_native_file(self):
        before=self.f.path.read_bytes()
        self.f.add_zcode('one','user','第一轮');self.f.tick()
        self.f.add_zcode('two','assistant','第二轮');self.f.tick();self.f.tick()
        self.assertEqual(self.f.path.read_bytes(),before)
        c=ss._db(self.f.root)
        try:
            self.assertEqual(c.execute('SELECT count(*) FROM runtime_messages').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM live_refresh').fetchone()[0],2)
            self.assertTrue(all(r[0] is None for r in c.execute('SELECT written_at FROM deliveries')))
        finally:c.close()

    def test_native_injection_is_not_echoed_back(self):
        row={'type':'response_item','payload':{'type':'message','id':'context-hub-sync:pair:item','role':'user','content':[{'type':'input_text','text':'原文'}]}}
        self.assertEqual(decode_codex(row),[])
        from context_hub.sync_tail import Tail,ready
        raw=(json.dumps(row)+'\n').encode()
        rows,active=ready(Tail(0,'',(),None,0,raw),False,MARKER)
        self.assertEqual(len(rows),1);self.assertFalse(active)

    def collision(self):
        self.f.add_zcode('collision','user','只来自ZCode的原文')
        c=ss._db(self.f.root)
        try:pair=c.execute('SELECT * FROM pairs').fetchone()
        finally:c.close()
        original=self.f.path.read_bytes();start=json.loads(original)['ordinal']+1
        copied=encode([UserText(ts='2026-10-25T14:00:00Z',text='只来自ZCode的原文')],pair=pair['id'],event='zcode:pcollision',cwd=str(self.f.root),thread_id='c1')
        native=encode([UserText(ts='2026-10-25T14:01:00Z',text='真正的原生你好')],pair='native',event='original',cwd=str(self.f.root),thread_id='c1')
        for i,r in enumerate(copied):r['ordinal']=start+i
        for i,r in enumerate(native):r['ordinal']=start+i;r.pop(MARKER,None)
        copied_bytes=b''.join((json.dumps(r,ensure_ascii=False)+'\n').encode() for r in copied)
        native_bytes=b''.join((json.dumps(r,ensure_ascii=False)+'\n').encode() for r in native)
        self.f.path.write_bytes(original+copied_bytes+native_bytes)
        c=ss._db(self.f.root)
        try:
            with c:c.execute('UPDATE pairs SET offset=?,prefix=?',(len(original+copied_bytes),hashlib.sha256(original+copied_bytes).hexdigest()))
        finally:c.close()
        return original,native_bytes

    def test_offline_repair_only_removes_bridge_rows_and_preserves_native_bytes(self):
        original,native=self.collision()
        with patch.object(collision_repair,'_closed'):
            result=collision_repair.repair(self.f.root)
        self.assertEqual(result['status'],'repaired')
        self.assertEqual(self.f.path.read_bytes(),original+native)
        c=ss._db(self.f.root)
        try:self.assertEqual(c.execute('SELECT offset FROM pairs').fetchone()[0],len(original))
        finally:c.close()
        before=self.f.path.read_bytes();self.f.tick();self.f.tick();self.assertEqual(self.f.path.read_bytes(),before)

    def test_repair_resumes_after_interruption_without_removing_native_rows(self):
        original,native=self.collision()
        with patch.object(collision_repair,'_closed'),patch('context_hub.session_ir.native_reconcile.reconcile',return_value=False):
            with self.assertRaises(ValueError):collision_repair.repair(self.f.root)
        self.assertEqual(self.f.path.read_bytes(),original+native)
        with patch.object(collision_repair,'_closed'):self.assertEqual(collision_repair.repair(self.f.root)['status'],'repaired')
        self.assertFalse((self.f.root/'native-repair-pending.json').exists())
