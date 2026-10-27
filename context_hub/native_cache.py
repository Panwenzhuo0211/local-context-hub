"""A metadata-only native record index; source payloads remain in original files."""
import hashlib
import json
from pathlib import Path
from .native_source import codex_sources,checked_rollout,record_key,readonly,zcode_rows,zcode_watermark
from .store import atomic,safe_child,exclusive

VERSION=3


def pointer(root,session):
    return safe_child(root,'native-read-cache','session-'+hashlib.sha256(session.encode()).hexdigest()+'.json')


def load(root,session,watermark=None):
    if watermark is None:
        path=pointer(root,session)
        if not path.exists():return None
        watermark=json.loads(path.read_text(encoding='utf-8'))['watermark']
    import re
    if not re.fullmatch('[0-9a-f]{64}',watermark):raise ValueError('Invalid native source watermark')
    path=safe_child(root,'native-read-cache',watermark+'.json')
    if not path.exists():return None
    saved=json.loads(path.read_text(encoding='utf-8'))
    if saved.get('session')!=session:raise ValueError('Native cursor belongs to another conversation')
    return saved


def save(root,session,source,index):
    saved={'version':VERSION,'session':session,'source':source,'records':index,
           'refs':{r['record_ref']:r['ref'] for r in index}}
    with exclusive(root,'native-read-cache.lock'):
        atomic(safe_child(root,'native-read-cache',source['watermark']+'.json'),json.dumps(saved,ensure_ascii=False))
        atomic(pointer(root,session),json.dumps({'watermark':source['watermark']}))


def current(config,saved):
    adapter,sid=saved['session'].split(':',1)
    if adapter=='zcode':
        h,ms,ps,state=zcode_rows(config,sid,include_state=True)
        # The same ordering is used in native_context._walk.
        order={m['id']:i for i,m in enumerate(ms)}
        ps.sort(key=lambda p:(order[p['message_id']],p.get('sequence') is None,p.get('sequence') or 0,p['time_created']))
        return zcode_watermark(h,ms,ps,state)==saved['source']['watermark']
    actual=codex_sources(config,sid)
    segments=saved['source']['segments']
    return len(actual)==len(segments) and all(
        str(a['path'])==s['path'] and a['size']==s['bytes'] and a['mtime_ns']==s['mtime_ns']
        and ('inode' not in s or a['inode']==s['inode']) for a,s in zip(actual,segments))


def raw_record(config,saved,record_ref,ref):
    adapter,sid=saved['session'].split(':',1)
    if adapter=='codex':
        path,_=checked_rollout(config['sources']['codex'],ref['file'],sid)
        with path.open('rb') as f:f.seek(ref['offset']);raw=f.read(ref['length'])
        row=json.loads(raw);key,digest=record_key(row)
        if key!=record_ref or digest!=ref['record_sha256']:raise ValueError('Native record changed since snapshot')
    else:
        con=readonly(Path(config['sources']['zcode'])/'cli/db/db.sqlite')
        try:
            con.execute('BEGIN')
            header=con.execute('SELECT * FROM session WHERE id=?',(sid,)).fetchone()
            if not header or dict(header).get('time_updated')!=saved['source']['header_updated'] or dict(header).get('revert')!=saved['source']['branch']:
                raise ValueError('Native conversation branch or head changed; reread latest context')
            part=con.execute('SELECT * FROM part WHERE id=? AND session_id=?',(ref['part'],sid)).fetchone()
            message=con.execute('SELECT * FROM message WHERE id=? AND session_id=?',(ref['message'],sid)).fetchone()
            if not part or not message:raise ValueError('Native record removed since snapshot')
            row={'message':dict(message),'part':dict(part)}
        finally:con.close()
        if hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False).encode()).hexdigest()!=ref['record_sha256']:
            raise ValueError('Native record changed since snapshot')
    return row
