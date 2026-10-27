"""Keep native chat rows bounded while retaining large historical payloads in full.

ZCode reads an entire conversation into memory on resume. Raw tool results can
be megabytes each. References are explicit, hash-verified local artifacts;
they never imply that the full payload was loaded by the receiving model.
"""
import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from .store import atomic,safe_child

LIMIT=32768


def large(value,remaining=LIMIT):
    """Bounded aggregate estimate, including lists of many small text blocks."""
    def cost(item,budget):
        if isinstance(item,str):return len(item)+2
        if isinstance(item,(dict,list)):
            used=2
            for key,child in (item.items() if isinstance(item,dict) else enumerate(item)):
                used+=cost(child,budget-used)+1+(len(str(key))+3 if isinstance(item,dict) else 0)
                if used>budget:return used
            return used
        return 8
    return cost(value,remaining)>remaining


class PayloadBatch:
    """One immutable JSONL container, verified before native publication."""
    def __init__(self,root):
        name='batch-'+uuid.uuid4().hex+'.jsonl'
        self.path=safe_child(root,'native-payloads',name);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.temp=self.path.with_suffix('.pending');self.stream=self.temp.open('xb')
        self.hasher=hashlib.sha256();self.count=0;self.last=None;self.closed=False

    def add(self,raw):
        if not raw.endswith(b'\n'):raise ValueError('Native payload records must end at a JSONL boundary')
        digest=hashlib.sha256(raw).hexdigest()
        if self.last and self.last['record_sha256']==digest:return dict(self.last)
        offset=self.stream.tell();self.stream.write(raw);self.hasher.update(raw);self.count+=1
        self.last={'path':str(self.path),'record_offset':offset,'record_bytes':len(raw),
                   'record_sha256':digest,'complete':True,'loaded_in_chat':False}
        return dict(self.last)

    def finalize(self):
        self.stream.flush();os.fsync(self.stream.fileno());self.stream.close();self.closed=True
        with self.temp.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=self.hasher.hexdigest():raise ValueError('Native payload batch failed integrity check')
        os.replace(self.temp,self.path)
        return {'path':str(self.path),'sha256':digest,'records':self.count,'bytes':self.path.stat().st_size}

    def close(self):
        if not self.closed:self.stream.close();self.closed=True
        if self.temp.exists():self.temp.unlink()


def compact_record(root,row,raw,batch=None):
    """Preserve one full native wire once, with explicit previews for large fields.

    The native result's exact JSONL bytes replace repeated serializations of the
    same output as text, metadata and artifact JSON. Small results are unchanged.
    """
    payload=row.get('payload') or {};field=None
    if row.get('type')=='response_item' and payload.get('type') in ('function_call_output','custom_tool_call_output'):
        field='output'
    elif row.get('type')=='compacted':field='replacement_history'
    if not field or not large(payload.get(field)):return row
    ref=batch.add(raw) if batch else _save(root,raw,'.json')
    ref.update(json_selector=['payload',field],native_record=True)
    original=payload[field]
    if isinstance(original,str):preview=original[:1000]
    elif isinstance(original,list):preview=next((str(v.get('text',''))[:1000] for v in original if isinstance(v,dict) and v.get('text')),'')
    else:preview=''
    summary=preview+'\n\n[历史长记录已完整保存在本地原生记录附件中；这里只展示预览，可用 read_native_payload 补读原文。]\n'+json.dumps(ref,ensure_ascii=False)
    copied=dict(payload);copied[field]=summary if field=='output' else [{'context_hub_payload':ref}]
    copied['context_hub_payload']=ref
    return {**row,'payload':copied}


def read_payload(root,payload_ref,*,offset=0,length=12000):
    """Read a declared field of an immutable artifact, restricted to Hub storage."""
    if type(offset)!=int or offset<0 or not 1<=length<=12000:raise ValueError('Invalid native payload range')
    ref=json.loads(payload_ref) if isinstance(payload_ref,str) else payload_ref
    path=Path(ref['path']);folder=(Path(root)/'native-payloads').resolve()
    if not path.resolve().is_relative_to(folder) or path.is_symlink():raise ValueError('Native payload path is outside Hub storage')
    # Also check each parent before reading, not just the final leaf.
    current=path
    while current!=folder and current!=current.parent:
        if current.is_symlink():raise ValueError('Refusing symlinked native payload')
        current=current.parent
    if 'record_offset' in ref:
        start=ref['record_offset'];size=ref['record_bytes']
        if type(start)!=int or type(size)!=int or start<0 or size<1 or start+size>path.stat().st_size:
            raise ValueError('Native payload record is outside its container')
        with path.open('rb') as stream:stream.seek(start);raw=stream.readline(size+1)
        if len(raw)!=size:raise ValueError('Native payload range does not match one complete record')
        digest=ref['record_sha256']
    else:raw=path.read_bytes();digest=ref['sha256']
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Native payload integrity check failed')
    value=json.loads(raw)
    try:
        for field in ref.get('json_selector',[]):
            if type(field) not in (str,int):raise ValueError('Invalid native payload field selector')
            value=value[field]
    except (KeyError,IndexError,TypeError) as e:raise ValueError('Native payload field does not exist') from e
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
    if offset>len(text):raise ValueError('Native payload offset exceeds its full length')
    end=min(len(text),offset+length)
    return {'text':text[offset:end],'offset':offset,'total_chars':len(text),
            'next_offset':end if end<len(text) else None,'integrity_verified':True,'untrusted':True}


def _save(root,raw,suffix):
    digest=hashlib.sha256(raw).hexdigest()
    path=safe_child(root,'native-payloads',digest+suffix)
    if not path.exists():atomic(path,raw)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise ValueError('Historical payload artifact failed integrity check')
    return {'path':str(path),'sha256':digest,'bytes':len(raw),'complete':True,
            'loaded_in_chat':False}


def _pack(root,value,batch=None):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    if len(raw)<=LIMIT:return value,None
    ref=batch.add(raw+b'\n') if batch else _save(root,raw,'.json')
    if isinstance(value,str):
        return (value[:1000]+'\n\n[历史长记录已完整保存为本地附件；本段仅为预览，继续分析前请按需读取原文。]\n'+
                json.dumps(ref,ensure_ascii=False)),ref
    return {'context_hub_payload':ref},ref


def bound_part(root,data,batch=None):
    """Return a native part with bounded display fields and verified full artifacts."""
    refs=[]
    if data.get('type')=='file' and str(data.get('url','')).startswith('data:'):
        prefix,encoded=data['url'].split(',',1)
        if ';base64' not in prefix:raise ValueError('Unsupported inline historical image encoding')
        raw=base64.b64decode(encoded,validate=True)
        extension={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','image/gif':'.gif'}.get(data.get('mime'),'.bin')
        ref=_save(root,raw,extension);refs.append(ref)
        data['url']=Path(ref['path']).as_uri()
    def pack_field(container,key):
        if key not in container:return
        value,ref=_pack(root,container[key],batch=batch);container[key]=value
        if ref:refs.append(ref)
    pack_field(data,'text')
    state=data.get('state') or {}
    for key in ('input','output','error','raw'):pack_field(state,key)
    for key in list(state.get('metadata') or {}):pack_field(state['metadata'],key)
    marker=(data.get('metadata') or {}).get('context_hub_sync') or {}
    pack_field(marker,'native_wire')
    if refs:data.setdefault('metadata',{})['context_hub_payloads']=refs
    return data
