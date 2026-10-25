import json
import tempfile
import unittest
from pathlib import Path
from context_hub.store import Store
from context_hub.model import envelope
from context_hub.session_ir.restore_zcode import restore_from_archive
from context_hub.session_ir.emit_codex import _render_moment

class RestoreRegressionTests(unittest.TestCase):
    def test_runtime_reminder_is_not_user_message_and_history_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            try:
                with store.db:
                    store.ingest(envelope('zcode','s','session:s',
                        {'id':'s','directory':'/example','time_created':1000},kind='session'),{})
                    for i in range(120):
                        mid=f'm{i}'
                        store.ingest(envelope('zcode','s','message:'+mid,
                            {'id':mid,'data':json.dumps({'role':'user'}),'time_created':1000+i},
                            kind='message_metadata'),{})
                        data={'type':'text','text':f'真实问题 {i}'}
                        if i==60:
                            data.update(text='The TodoWrite tool has not been used recently',synthetic=True,
                                metadata={'runtimeMessage':{'source':'todo_reminder'},'visibility':'model-only'})
                        store.ingest(envelope('zcode','s',f'part:p{i}',
                            {'id':f'p{i}','message_id':mid,'time_created':1000+i,'data':json.dumps(data)},
                            kind='text'),{})
                ir=restore_from_archive(store,'s')
                self.assertEqual(len(ir.moments),120)
                self.assertEqual(sum(m.kind=='user_text' for m in ir.moments),119)
                lines=[line for moment in ir.moments for line in _render_moment(moment,ir)]
                users=[l['payload']['message'] for l in lines if l['type']=='event_msg' and l['payload']['type']=='user_message']
                self.assertEqual(users[0],'真实问题 0')
                self.assertEqual(users[-1],'真实问题 119')
                self.assertFalse(any('TodoWrite' in x for x in users))
            finally:
                store.close()

    def test_truncated_part_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp))
            try:
                with store.db:
                    store.ingest(envelope('zcode','s','session:s',{'id':'s','directory':'/example','time_created':1},kind='session'),{})
                    store.ingest(envelope('zcode','s','part:p',{'_raw_truncated':True},kind='tool'),{})
                with self.assertRaisesRegex(ValueError,'cannot be restored'):
                    restore_from_archive(store,'s')
            finally:store.close()
