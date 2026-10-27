import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from context_hub.session_ir.native_history import publish

class NativeHistoryTests(unittest.TestCase):
    def test_many_turns_have_exact_text_offsets_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            c=sqlite3.connect(home/'state_5.sqlite')
            c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, history_mode TEXT)')
            c.execute("INSERT INTO threads VALUES ('s','legacy')");c.commit();c.close()
            c=sqlite3.connect(home/'thread_history_1.sqlite')
            c.executescript('''CREATE TABLE thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal,started_at_ms,completed_at_ms);
            CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at,completed_at,duration_ms,first_user_item_id,final_agent_item_id,rollout_byte_offset,rollout_end_ordinal,rollout_end_byte_offset,root_turn_id);
            CREATE TABLE thread_history_projection_state(thread_id PRIMARY KEY,next_rollout_byte_offset,next_rollout_ordinal);''');c.close()
            lines=[{'type':'session_meta','payload':{'originator':'context-hub'}}]
            for i in range(120):
                for typ,text in [('user_message',f'问题{i}'),('agent_message',f'回答{i}')]:
                    lines.append({'type':'event_msg','timestamp':'2026-01-01T00:00:00Z','payload':{'type':typ,'message':text}})
            for ordinal, line in enumerate(lines): line['ordinal']=ordinal
            path=home/'mirror.jsonl';path.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in lines),encoding='utf-8')
            self.assertTrue(publish(home,'s',path))
            c=sqlite3.connect(home/'thread_history_1.sqlite')
            self.assertEqual(c.execute('SELECT count(*) FROM thread_turns').fetchone()[0],120)
            self.assertEqual(c.execute('SELECT count(*) FROM thread_items').fetchone()[0],240)
            offset,ordinal=c.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state').fetchone()
            self.assertEqual((offset,ordinal),(path.stat().st_size,len(lines)))
            raw=path.read_bytes()
            for off in c.execute('SELECT rollout_byte_offset FROM thread_turns'):
                self.assertEqual(json.loads(raw[off[0]:].splitlines()[0])['payload']['type'],'user_message')
            first=json.loads(c.execute('SELECT item_json FROM thread_items ORDER BY rollout_ordinal LIMIT 1').fetchone()[0])
            self.assertEqual(first['content'][0]['text'],'问题0');c.close()
            with self.assertRaises(ValueError):publish(home,'s',path)
