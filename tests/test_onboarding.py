import importlib.util
import io
import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from context_hub.agents import Registry
from context_hub.matters import Matters
from context_hub import integration


class OnboardingTests(unittest.TestCase):
    def test_fresh_owner_can_create_matter_and_enable_file_agent(self):
        path=Path(__file__).resolve().parent.parent/'agent-setup.py'
        spec=importlib.util.spec_from_file_location('owner_agent_setup',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw,patch('builtins.input',side_effect=['new-agent','合成共享事项','1','y','y']),redirect_stdout(io.StringIO()):
            root=Path(raw);module.main(root,reveal=False)
            registry=Registry(root);policy=registry.public('new-agent')
            self.assertTrue(policy['allow_remember']);self.assertEqual(len(policy['matters']),1)
            from context_hub.file_bridge import describe
            self.assertTrue(Path(describe(root,'new-agent')['instructions']).is_file())

    def test_installed_module_entries_install_and_uninstall_safely(self):
        with tempfile.TemporaryDirectory() as raw:
            home=Path(raw);(home/'.codex').mkdir();(home/'.codex/config.toml').write_text('model="keep"\n')
            # Simulate wheel layout: package parent exists, root wrappers do not.
            app=home/'site-packages';app.mkdir()
            with patch.object(integration,'__file__',str(app/'context_hub/integration.py')):
                integration.install(home,app=app,python='/python',activate=False)
                config=tomllib.loads((home/'.codex/config.toml').read_text())
                self.assertEqual(config['mcp_servers']['context-hub']['args'][:2],['-m','context_hub.mcp_server'])
                self.assertIn('context_hub.background',integration.task_definition('/python',app,None)[1])
                integration.uninstall(home,app=app,activate=False)
                self.assertEqual(tomllib.loads((home/'.codex/config.toml').read_text())['model'],'keep')

    def test_linux_activation_leaves_scheduling_to_owner(self):
        with tempfile.TemporaryDirectory() as raw,patch.object(integration,'WIN',False),patch.object(integration.sys,'platform','linux'),patch.object(integration.subprocess,'run') as command:
            home=Path(raw);(home/'.codex').mkdir();(home/'.codex/config.toml').write_text('model="keep"\n')
            result=integration.install(home,app=home/'app',python='/python',activate=True)
            self.assertEqual(result['service_kind'],'external-scheduler');command.assert_not_called()
            integration.uninstall(home,app=home/'app',activate=True);command.assert_not_called()
