import json
import plistlib
import tempfile
import tomllib
import unittest
from pathlib import Path
from context_hub.integration import prepare, install, uninstall, BEGIN

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)
        for p in (self.home/'.codex',self.home/'.zcode/cli'):p.mkdir(parents=True)
        (self.home/'.codex/config.toml').write_text('model = "existing"\n[mcp_servers.other]\ncommand = "other"\n')
        (self.home/'.zcode/cli/config.json').write_text(json.dumps({'mcp':{'servers':{'other':{'command':'keep'}}},'plugins':['keep']}))
        for p in (self.home/'.codex/AGENTS.md',self.home/'.zcode/AGENTS.md'):p.write_text('已有用户规则\n')
    def tearDown(self):self.temp.cleanup()
    def test_preserve_backup_idempotence(self):
        first=install(self.home,app=self.home/'app',python='/python',activate=False)
        manifest=json.loads(Path(first['backup_manifest']).read_text())
        self.assertTrue(all(Path(x['backup']).exists() for x in manifest if x['backup']))
        codex=tomllib.loads((self.home/'.codex/config.toml').read_text())
        self.assertEqual(codex['model'],'existing');self.assertIn('other',codex['mcp_servers'])
        zd=json.loads((self.home/'.zcode/cli/config.json').read_text());self.assertEqual(zd['plugins'],['keep'])
        install(self.home,app=self.home/'app',python='/python',activate=False)
        self.assertEqual((self.home/'.codex/AGENTS.md').read_text().count(BEGIN),1)
        self.assertTrue((self.home/'.codex/AGENTS.md').read_text().startswith('已有用户规则'))
    def test_opencode_wiring_conditional(self):
        oc=self.home/'.config/opencode';oc.mkdir(parents=True)
        (oc/'opencode.json').write_text(json.dumps({'provider':{'keep':True}}))
        install(self.home,app=self.home/'app',python='/python',activate=False)
        cfg=json.loads((oc/'opencode.json').read_text(encoding='utf-8'))
        self.assertEqual(cfg['provider'],{'keep':True})
        self.assertIn('context-hub',cfg['mcp'])
        self.assertTrue((oc/'AGENTS.md').exists())
        uninstall(self.home,app=self.home/'app',activate=False)
        cfg=json.loads((oc/'opencode.json').read_text(encoding='utf-8'))
        self.assertNotIn('mcp',cfg)

    def test_conflicting_server_refused_before_write(self):
        p=self.home/'.codex/config.toml';p.write_text('[mcp_servers.context-hub]\ncommand="unrelated"\n')
        before=p.read_bytes()
        with self.assertRaises(ValueError):install(self.home,app=self.home/'app',python='/python',activate=False)
        self.assertEqual(before,p.read_bytes())

    def test_uninstall_preserves_later_unrelated_config(self):
        install(self.home,app=self.home/'app',python='/python',activate=False)
        p=self.home/'.codex/config.toml'
        p.write_text(p.read_text()+'\n[mcp_servers.newer]\ncommand="keep-new"\n')
        uninstall(self.home,app=self.home/'app',activate=False)
        result=tomllib.loads(p.read_text())
        self.assertIn('newer',result['mcp_servers']);self.assertNotIn('context-hub',result['mcp_servers'])
        self.assertNotIn(BEGIN,(self.home/'.codex/AGENTS.md').read_text())

if __name__=='__main__':unittest.main()
