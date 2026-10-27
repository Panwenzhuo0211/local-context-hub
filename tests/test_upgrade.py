import json
import sqlite3
import tempfile
import subprocess
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from context_hub import __version__
from context_hub.store import Store
from context_hub.model import envelope
from context_hub.upgrade import install_release, rollback_code
from context_hub.health import inspect


class UpgradeTests(unittest.TestCase):
    def test_install_and_recover_previous_code_preserves_data(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);data=base/'data';runtime=base/'app';runtime.mkdir()
            (runtime/'old-marker').write_text('previous code')
            (runtime/'context_hub').mkdir();(runtime/'context_hub/__init__.py').write_text('old')
            store=Store(data)
            with store.db:store.ingest(envelope('codex','s','k',{},text='synthetic'),{})
            store.close();(data/'config.json').write_text('{"sources":{}}')
            result=install_release(Path(__file__).parents[1],runtime,data,commit='test')
            self.assertEqual(result['status'],'installed')
            self.assertEqual(json.loads((runtime/'deployment.json').read_text())['version'],__version__)
            with closing(sqlite3.connect(data/'state.sqlite')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM revisions').fetchone()[0],1)
            rollback_code(runtime,Path(result['backup']))
            self.assertEqual((runtime/'context_hub/__init__.py').read_text(),'old')
            self.assertTrue((data/'state.sqlite').exists())

    def test_failure_restores_code_and_quarantines_new_state(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);data=base/'data';data.mkdir();runtime=base/'app';runtime.mkdir()
            (runtime/'old-marker').write_text('original')
            (data/'config.json').write_text('{"sources":{}}')
            def fail(root):
                (root/'state.sqlite').write_text('partial migration')
                raise ValueError('simulated migration failure')
            with patch('context_hub.upgrade._validate_data',side_effect=fail):
                with self.assertRaisesRegex(ValueError,'simulated'):
                    install_release(Path(__file__).parents[1],runtime,data)
            self.assertEqual((runtime/'old-marker').read_text(),'original')
            self.assertFalse((data/'state.sqlite').exists())
            backup=next((data/'backups').glob('release-*'))
            self.assertTrue((backup/'failed-migration/state.sqlite').is_file())
            self.assertEqual(json.loads((backup/'release.json').read_text())['status'],'rolled_back')

    def test_health_distinguishes_old_warning_and_failed_sources(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            (root/'config.json').write_text(json.dumps({'sources':{'zcode':str(root/'missing')}}))
            (root/'background-status.json').write_text(json.dumps({'status':'ok','last_success':100,
                'sync':{'warnings':['Codex indexed log missing: a'],'warnings_omitted':2}}))
            state=inspect(root,now=150)
            self.assertEqual(state['health'],'error')
            self.assertEqual(state['warnings_count'],3)
            self.assertFalse(state['sources']['zcode']['available'])
            self.assertTrue(inspect(root,now=1000)['stale'])
            self.assertFalse(state['handoff_verified'])

    def test_post_swap_failure_recovers_previous_runtime(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);data=base/'data';runtime=base/'app';runtime.mkdir()
            (runtime/'old-marker').write_text('original')
            store=Store(data);store.close()
            original_run=subprocess.run
            def run(args,**kw):
                if '-m' in args:
                    return subprocess.CompletedProcess(args,1,'','simulated installed failure')
                return original_run(args,**kw)
            with patch('context_hub.upgrade.subprocess.run',side_effect=run):
                with self.assertRaisesRegex(ValueError,'Installed release'):
                    install_release(Path(__file__).parents[1],runtime,data)
            self.assertEqual((runtime/'old-marker').read_text(),'original')
            backup=next((data/'backups').glob('release-*'))
            self.assertTrue((backup/'failed-runtime/deployment.json').is_file())
            self.assertTrue((data/'state.sqlite').exists())

    def test_doctor_never_creates_a_data_directory(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'missing'
            self.assertEqual(inspect(root)['status'],'not_started')
            self.assertFalse(root.exists())

    def test_refused_legacy_rollback_closes_both_read_handles(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);backup=root/'backups/release-test'
            (backup/'runtime/context_hub').mkdir(parents=True)
            (backup/'runtime/context_hub/__init__.py').write_text('old')
            (backup/'data').mkdir()
            for path,count in ((backup/'data/archive.sqlite',1),(root/'state.sqlite',2)):
                with closing(sqlite3.connect(path)) as db:
                    db.executescript('CREATE TABLE revisions(id); CREATE TABLE heads(adapter,session,source_key);')
                    db.executemany('INSERT INTO revisions VALUES (?)',[(i,) for i in range(count)]);db.commit()
            opened=[];closed=[];connect=sqlite3.connect
            class Tracked(sqlite3.Connection):
                def close(self):closed.append(self);super().close()
            def tracked(*args,**kwargs):
                value=connect(*args,**kwargs,factory=Tracked);opened.append(value);return value
            with patch('context_hub.upgrade.sqlite3.connect',side_effect=tracked):
                with self.assertRaisesRegex(ValueError,'New archive work exists'):
                    rollback_code(root/'app',backup)
            self.assertEqual(len(opened),2);self.assertEqual(closed,opened[::-1])
