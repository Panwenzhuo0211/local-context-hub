import hashlib
import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from context_hub.native_payloads import PayloadBatch,compact_record,large,read_payload


class NativePayloadBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_large_native_outputs_are_stored_exactly_once_and_can_be_paged(self):
        output=[{'type':'text','text':'原始工具输出😀'*10000}]
        row={'type':'response_item','timestamp':'2026-10-25T00:00:00Z',
             'payload':{'type':'custom_tool_call_output','call_id':'a','output':output}}
        raw=(json.dumps(row,ensure_ascii=False)+'\n').encode()
        batch=PayloadBatch(self.root)
        try:
            compact=compact_record(self.root,row,raw,batch)
            ref=compact['payload']['context_hub_payload']
            again=batch.add(raw);self.assertEqual(again['record_offset'],ref['record_offset'])
            summary=batch.finalize()
        finally:batch.close()
        self.assertEqual(summary['records'],1);self.assertEqual(summary['bytes'],len(raw))
        self.assertEqual(Path(ref['path']).read_bytes(),raw)
        self.assertNotEqual(compact['payload']['output'],row['payload']['output'])
        self.assertEqual(row['payload']['output'],output)
        text='';offset=0
        while True:
            page=read_payload(self.root,json.dumps(ref),offset=offset)
            text+=page['text']
            if page['next_offset'] is None:break
            offset=page['next_offset']
        self.assertEqual(json.loads(text),output)

    def test_many_small_blocks_trigger_batch_without_losing_any_blocks(self):
        blocks=[{'type':'text','text':'x'*1000} for _ in range(50)]
        self.assertTrue(large(blocks))
        self.assertFalse(large([{'text':'small'}]))

    def test_batch_failure_cleans_unpublished_file(self):
        batch=PayloadBatch(self.root);pending=batch.temp
        batch.add(b'{"a":1}\n');batch.close()
        self.assertFalse(pending.exists());self.assertFalse(batch.path.exists())

    def test_payload_tampering_and_outside_paths_are_rejected(self):
        batch=PayloadBatch(self.root)
        try:ref=batch.add(b'{"payload":{"output":"original"}}\n');batch.finalize()
        finally:batch.close()
        Path(ref['path']).write_bytes(b'{"payload":{"output":"modified"}}\n')
        with self.assertRaisesRegex(ValueError,'integrity'):read_payload(self.root,ref)
        outside=self.root/'outside.json';outside.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'outside Hub'):
            read_payload(self.root,{'path':str(outside),'sha256':hashlib.sha256(b'{}').hexdigest()})

    def test_every_large_field_can_share_the_same_verified_batch(self):
        from context_hub.native_payloads import bound_part
        output='完整输出'*20000
        batch=PayloadBatch(self.root)
        try:
            data=bound_part(self.root,{'type':'tool','state':{'input':{},'output':output}},batch=batch)
            summary=batch.finalize()
        finally:batch.close()
        ref=data['metadata']['context_hub_payloads'][0]
        self.assertEqual(summary['records'],1)
        page=read_payload(self.root,ref,length=12000)
        self.assertEqual(page['text'],output[:12000]);self.assertTrue(page['integrity_verified'])

    def test_multiple_records_cannot_be_misread_as_one_payload(self):
        batch=PayloadBatch(self.root)
        try:
            first=batch.add(b'{"a":1}\n');batch.add(b'{"b":2}\n');summary=batch.finalize()
        finally:batch.close()
        forged={**first,'record_bytes':summary['bytes'],'record_sha256':summary['sha256']}
        with self.assertRaisesRegex(ValueError,'one complete record'):read_payload(self.root,forged)
        with self.assertRaisesRegex(ValueError,'field does not exist'):
            read_payload(self.root,{**first,'json_selector':['missing']})

    def test_cli_can_read_batch_before_client_tool_catalogue_is_reloaded(self):
        batch=PayloadBatch(self.root)
        try:ref=batch.add('{"text":"完整记录"}\n'.encode());batch.finalize()
        finally:batch.close()
        ref['json_selector']=['text']
        result=subprocess.run([sys.executable,'-X','utf8','-m','context_hub','--data-dir',str(self.root),
                               'native-payload',json.dumps(ref)],capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['text'],'完整记录')


if __name__=='__main__':unittest.main()
