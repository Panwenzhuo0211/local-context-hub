import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from context_hub import paths
from context_hub.integration import install, prepare
from context_hub.store import exclusive


class PlatformTests(unittest.TestCase):
    def test_default_data_dir_override(self):
        old=os.environ.get(paths.ENV_OVERRIDE)
        try:
            os.environ[paths.ENV_OVERRIDE]=str(Path(tempfile.gettempdir())/'ctxhub-override')
            self.assertEqual(paths.default_data_dir(), Path(tempfile.gettempdir())/'ctxhub-override')
        finally:
            if old is None:os.environ.pop(paths.ENV_OVERRIDE,None)
            else:os.environ[paths.ENV_OVERRIDE]=old

    def test_lock_excludes_second_writer(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            with exclusive(root):
                with self.assertRaises(RuntimeError):
                    with exclusive(root):pass

    def test_install_single_client(self):
        with tempfile.TemporaryDirectory() as t:
            home=Path(t)
            (home/'.codex').mkdir(parents=True)
            (home/'.codex/config.toml').write_text('model = "x"\n')
            result=install(home,app=home/'app',python='/python',activate=False)
            self.assertEqual(result['configured'],['codex'])
            self.assertFalse((home/'.zcode').exists())
            self.assertIn('context-hub',tomllib.loads((home/'.codex/config.toml').read_text())['mcp_servers'])
            self.assertIn('managed start',(home/'.codex/AGENTS.md').read_text())

    def test_install_requires_a_client(self):
        with tempfile.TemporaryDirectory() as t:
            home=Path(t);(home/'app').mkdir(parents=True)
            with self.assertRaises(ValueError):
                install(home,app=home/'app',python='/python',activate=False)

    def test_prepare_writes_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            home=Path(t)
            (home/'.codex').mkdir(parents=True)
            (home/'.codex/config.toml').write_text('model = "x"\n')
            before=(home/'.codex/config.toml').read_bytes()
            changes,configured=prepare(home,home/'app','/python')
            self.assertEqual(configured,['codex'])
            self.assertEqual(before,(home/'.codex/config.toml').read_bytes())
            self.assertIn(home/'.codex/AGENTS.md',changes)
            self.assertFalse((home/'.codex/AGENTS.md').exists())

    def test_install_without_rule_injection(self):
        with tempfile.TemporaryDirectory() as t:
            home=Path(t)
            (home/'.codex').mkdir(parents=True)
            (home/'.codex/config.toml').write_text('model = "x"\n')
            install(home,app=home/'app',python='/python',activate=False,write_rules=False)
            self.assertFalse((home/'.codex/AGENTS.md').exists())


if __name__=='__main__':unittest.main()
