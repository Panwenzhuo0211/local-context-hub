import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from context_hub.session_ir import emit_codex, manifest
from context_hub.session_ir.schema import Session, UserText


class MirrorSafetyTests(unittest.TestCase):
    def session(self):
        return Session(id='019ff000-0000-7000-8000-000000000001',source_harness='zcode',
            source_session_id='synthetic',cwd='/synthetic',started_at='2026-01-01T00:00:00Z',
            moments=[UserText(ts='2026-01-01T00:00:01Z',text='first')])

    def test_all_existing_content_is_preserved_and_scope_isolated(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);home=root/'target';hub=root/'hub'
            result=emit_codex.render(self.session(),target_dir=home,data_dir=hub,session_id=self.session().id)
            path=result.primary_path
            self.assertTrue(manifest.matches(path,result.session_id,hub))
            changed=path.read_bytes().replace(b'first',b'other')
            path.write_bytes(changed)
            self.assertFalse(manifest.matches(path,result.session_id,hub))
            with self.assertRaisesRegex(ValueError,'protected'):
                emit_codex.render(self.session(),target_dir=home,data_dir=hub,session_id=result.session_id)
            self.assertEqual(path.read_bytes(),changed)
            path.write_bytes(changed+b'new turns\n')
            with self.assertRaises(ValueError):
                emit_codex.render(self.session(),target_dir=home,data_dir=hub,session_id=result.session_id)
            self.assertTrue(path.read_bytes().endswith(b'new turns\n'))
            self.assertEqual(len(json.loads((hub/'codex-write-manifest.json').read_text())),1)

    def test_direct_explicit_target_uses_target_scoped_ledger(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            with patch('context_hub.cli.DEFAULT',root/'unrelated'):
                emit_codex.render(self.session(),target_dir=root/'target',session_id=self.session().id)
            self.assertFalse((root/'unrelated').exists())
            self.assertTrue((root/'target/.context-hub/codex-write-manifest.json').is_file())

    def test_corrupt_ledger_fails_without_clobbering_it(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);path=root/'codex-write-manifest.json';path.write_text('bad')
            with self.assertRaises(ValueError):manifest.get('id',root)
            self.assertEqual(path.read_text(),'bad')
