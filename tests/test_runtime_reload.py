import hashlib
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from context_hub.runtime_reload import Runtime


class RuntimeReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=Path(self.tmp.name)/'app';(self.app/'context_hub').mkdir(parents=True)
        self.file=self.app/'context_hub/module.py';self.file.write_text('value = 1\n',encoding='utf-8')
        self.server=SimpleNamespace(initialized=True,root=Path(self.tmp.name)/'hub',client='codex')
        self.runtime=Runtime(self.server,self.app)

    def publish(self,digest=None):
        marker={'version':'test','files':{'context_hub/module.py':digest or hashlib.sha256(self.file.read_bytes()).hexdigest()}}
        (self.app/'runtime-generation.json').write_text(json.dumps(marker),encoding='utf-8')

    def test_incomplete_deployment_keeps_current_connection(self):
        self.publish('0'*64)
        with patch.object(self.runtime,'_load_server') as load:self.assertFalse(self.runtime.refresh());load.assert_not_called()
        self.assertIs(self.runtime.server,self.server);self.assertIn('incomplete',self.runtime.error)

    def test_verified_deployment_switches_only_between_calls(self):
        newer=SimpleNamespace(initialized=True)
        self.publish()
        with patch.object(self.runtime,'_load_server',return_value=newer) as load:
            self.assertTrue(self.runtime.refresh());self.assertFalse(self.runtime.refresh());load.assert_called_once()
        self.assertIs(self.runtime.server,newer);self.assertIsNone(self.runtime.error)

    def test_failed_reload_retains_old_server(self):
        self.publish()
        with patch.object(self.runtime,'_load_server',side_effect=SyntaxError('bad update')):
            self.assertFalse(self.runtime.refresh())
        self.assertIs(self.runtime.server,self.server);self.assertEqual(self.runtime.error,'bad update')

    def test_real_stdio_connection_reloads_and_announces_tools_without_restart(self):
        package=Path(__file__).resolve().parents[1]/'context_hub'
        shutil.copytree(package,self.app/'context_hub',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
        self.server.root.mkdir()
        process=subprocess.Popen([sys.executable,'-X','utf8','-m','context_hub.mcp_server','--data-dir',str(self.server.root)],
            cwd=self.app,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        events=queue.Queue();notifications=[]
        def collect():
            for line in process.stdout:events.put(json.loads(line))
        threading.Thread(target=collect,daemon=True).start()
        def request(i,method,params):
            process.stdin.write(json.dumps({'jsonrpc':'2.0','id':i,'method':method,'params':params})+'\n');process.stdin.flush()
            while True:
                obj=events.get(timeout=15)
                if 'id' not in obj:notifications.append(obj);continue
                self.assertEqual(obj['id'],i);return obj
        try:
            initial=request(1,'initialize',{'protocolVersion':'2025-06-18'})
            self.assertTrue(initial['result']['capabilities']['tools']['listChanged'])
            before=request(2,'tools/call',{'name':'context_status','arguments':{}})
            old_version=json.loads(before['result']['content'][0]['text'])['native_context_repair']
            new_version=old_version[:-1]+('X' if old_version[-1]!='X' else 'Y')
            source=self.app/'context_hub/mcp_server.py'
            # Same-size version edit deliberately exercises stale .pyc handling.
            source.write_text(source.read_text(encoding='utf-8').replace(old_version,new_version),encoding='utf-8')
            marker={'version':new_version,'files':{'context_hub/mcp_server.py':hashlib.sha256(source.read_bytes()).hexdigest()}}
            (self.app/'runtime-generation.json').write_text(json.dumps(marker),encoding='utf-8')
            reply=request(3,'tools/call',{'name':'context_status','arguments':{}})
            self.assertFalse(reply['result']['isError'])
            status=json.loads(reply['result']['content'][0]['text'])
            self.assertEqual(status['native_context_repair'],new_version)
            self.assertTrue(status['runtime_reload']['supported']);self.assertIsNone(status['runtime_reload']['error'])
            self.assertTrue(any(n['method']=='notifications/tools/list_changed' for n in notifications))
            self.assertIsNone(process.poll())
        finally:
            process.stdin.close()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            process.stdout.close();process.stderr.close()


if __name__=='__main__':unittest.main()
