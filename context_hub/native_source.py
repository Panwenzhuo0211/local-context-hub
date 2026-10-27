"""Validated, read-only snapshots of native conversation sources.

One resolver is shared by continuation, import and sync enrollment. A filename
is a discovery hint, not an identity or a claim of complete conversation history.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path


def readonly(path):
    con=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    con.row_factory=sqlite3.Row
    return con


def normalized(path):
    return Path(str(path).removeprefix('\\\\?\\')).expanduser()


def checked_rollout(home,path,sid):
    home=Path(home).expanduser().resolve();path=normalized(path)
    if path.is_symlink():raise ValueError('Refusing symlinked native source')
    resolved=path.resolve()
    if not any(resolved.is_relative_to(home/f) for f in ('sessions','archived_sessions')):
        raise ValueError('Native source is outside the enrolled Codex directory')
    current=path
    while current!=current.parent and current!=home:
        if current.is_symlink():raise ValueError('Refusing symlink inside native source path')
        current=current.parent
    if not resolved.is_file():raise ValueError('Native source missing: '+resolved.name)
    with resolved.open('rb') as f:
        try:header=json.loads(f.readline())
        except (ValueError,UnicodeError) as e:raise ValueError('Invalid native session header') from e
    if header.get('type')!='session_meta' or header.get('payload',{}).get('id')!=sid:
        raise ValueError('Native session identity mismatch')
    return resolved,header


def codex_sources(config,sid,rollout=None):
    home=Path(config['sources']['codex']).expanduser().resolve()
    candidates=[];authority=None
    if (home/'state_5.sqlite').is_file():
        con=readonly(home/'state_5.sqlite')
        try:
            columns={r['name'] for r in con.execute('PRAGMA table_info(threads)')}
            if 'rollout_path' in columns:
                row=con.execute('SELECT rollout_path FROM threads WHERE id=?',(sid,)).fetchone()
                if row and row[0]:authority=normalized(row[0]).resolve()
        finally:con.close()
    required={authority} if authority is not None else set()
    if rollout is not None:
        required.add(normalized(rollout).resolve());candidates.append(rollout)
    if authority is not None:candidates.append(authority)
    for folder in ('sessions','archived_sessions'):
        candidates.extend((home/folder).rglob('*'+sid+'*.jsonl'))
    seen=set();segments=[]
    for candidate in candidates:
        try:path,header=checked_rollout(home,candidate,sid)
        except ValueError as error:
            if normalized(candidate).resolve() not in required and 'identity mismatch' in str(error):continue
            raise
        if path in seen:continue
        seen.add(path)
        stamp=header.get('timestamp') or header['payload'].get('timestamp') or ''
        st=path.stat()
        segments.append({'path':path,'header':header,'started_at':stamp,
                         'size':st.st_size,'inode':st.st_ino,'mtime_ns':st.st_mtime_ns})
    if not segments:raise ValueError('Native Codex session not found: '+sid)
    if len(segments)>1 and any(not s['started_at'] for s in segments):
        raise ValueError('Multiple native segments without a verifiable ordering timestamp')
    segments.sort(key=lambda s:(s['started_at'],str(s['path'])))
    if rollout is None and authority is not None and authority not in seen:
        raise ValueError('Current authoritative native segment is missing')
    return segments


def record_key(row):
    payload=row.get('payload') or {}
    kind=str(row.get('type'))+':'+str(payload.get('type'))
    identity=payload.get('id') or payload.get('call_id')
    canonical=json.dumps({k:v for k,v in row.items() if k not in ('ordinal','context_hub_sync')},
                         ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    digest=hashlib.sha256(canonical).hexdigest()
    return (kind+':'+str(identity) if identity and row.get('type')=='response_item' else digest),digest


def current_codex_source(config,sid):
    home=Path(config['sources']['codex']).expanduser().resolve()
    if (home/'state_5.sqlite').exists():
        con=readonly(home/'state_5.sqlite')
        try:
            cols={r['name'] for r in con.execute('PRAGMA table_info(threads)')}
            if 'rollout_path' in cols:
                row=con.execute('SELECT rollout_path FROM threads WHERE id=?',(sid,)).fetchone()
                if row and row[0]:return checked_rollout(home,row[0],sid)[0]
        finally:con.close()
    sources=codex_sources(config,sid)
    if len(sources)!=1:raise ValueError('Multiple native segments require an authoritative current source')
    return sources[0]['path']


class CodexSnapshot:
    def __init__(self,config,sid,rollout=None):
        self.config=config;self.rollout=rollout
        self.session_id=sid;self.segments=codex_sources(config,sid,rollout)
        self.counts=Counter();self.duplicates=0;self.first_at=None;self.latest_at=None
        self.manifest=[];self.finished=False
        self.last_raw=None
        self.cwd=self.segments[0]['header']['payload'].get('cwd','')

    def records(self):
        seen={};previous_end=None
        for segment in self.segments:
            path=segment['path'];size=segment['size'];hasher=hashlib.sha256();position=0
            with path.open('rb') as f:
                while position<size:
                    start=position;raw=f.readline(size-position)
                    if not raw or not raw.endswith(b'\n'):
                        raise ValueError(f'Incomplete native JSONL at {path.name}:{start}; retry after source finishes')
                    position+=len(raw);hasher.update(raw)
                    try:row=json.loads(raw)
                    except (ValueError,UnicodeError) as e:
                        raise ValueError(f'Invalid native JSONL at {path.name}:{start}') from e
                    if not isinstance(row,dict):raise ValueError('Invalid native record object')
                    # Large event_msg/item_completed records duplicate the
                    # canonical response wire. Their exact bytes are already
                    # covered by the source-file hash; do not re-encode them.
                    semantic=row.get('type') in ('response_item','compacted') or (row.get('type')=='event_msg' and (row.get('payload') or {}).get('type')=='error')
                    if semantic:key,digest=record_key(row)
                    else:digest=hashlib.sha256(raw).hexdigest();key=digest
                    if key in seen:
                        if seen[key]!=digest:raise ValueError('Conflicting native event identity: '+key)
                        self.duplicates+=1;continue
                    seen[key]=digest
                    stamp=row.get('timestamp')
                    if stamp:
                        try:
                            dt=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                            if dt.tzinfo is None:raise ValueError('Native timestamp has no timezone')
                            stamp=dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
                        except (ValueError,TypeError,AttributeError) as e:raise ValueError('Invalid native source timestamp') from e
                    if row.get('type') in ('session_meta','turn_context'):
                        self.cwd=(row.get('payload') or {}).get('cwd') or self.cwd
                    meaningful=row.get('type') in ('response_item','compacted')
                    if meaningful and stamp:
                        if previous_end and stamp<previous_end and segment is not self.segments[0]:
                            raise ValueError('Overlapping native segments contain unverified events; refusing branch merge')
                        self.first_at=min(self.first_at or stamp,stamp)
                        self.latest_at=max(self.latest_at or stamp,stamp)
                    kind=str(row.get('type'))+':'+str((row.get('payload') or {}).get('type'))
                    self.counts[kind]+=1
                    self.last_raw=raw
                    yield row,{'file':str(path),'offset':start,'length':len(raw),'record_key':key,'cwd':self.cwd,'record_sha256':digest}
            if path.stat().st_ino!=segment['inode'] or path.stat().st_size!=size:
                raise ValueError('Native source changed while snapshotting; retry')
            # Verify the prefix again: size alone does not detect in-place edits.
            check=hashlib.sha256()
            with path.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):check.update(block)
            if check.digest()!=hasher.digest():raise ValueError('Native source was rewritten during snapshot')
            self.manifest.append({'path':str(path),'bytes':size,'sha256':hasher.hexdigest(),'mtime_ns':path.stat().st_mtime_ns,'inode':segment['inode']})
            previous_end=self.latest_at
        latest=codex_sources(self.config,self.session_id,self.rollout)
        if [(s['path'],s['size'],s['inode'],s['mtime_ns']) for s in latest]!=[(s['path'],s['size'],s['inode'],s['mtime_ns']) for s in self.segments]:
            raise ValueError('Native source head changed before snapshot verification completed')
        self.finished=True

    def receipt(self):
        if not self.finished:raise ValueError('Native snapshot has not been fully validated')
        watermark=hashlib.sha256(json.dumps(self.manifest,sort_keys=True).encode()).hexdigest()
        return {'session':self.session_id,'watermark':watermark,'segments':self.manifest,
                'first_at':self.first_at,'latest_at':self.latest_at,'source_types':dict(self.counts),
                'duplicate_records':self.duplicates,'cwd':self.cwd,'complete':True}


def zcode_rows(config,sid,*,include_state=False):
    path=Path(config['sources']['zcode']).expanduser().resolve()/'cli/db/db.sqlite'
    con=readonly(path)
    try:
        con.execute('BEGIN')
        row=con.execute('SELECT * FROM session WHERE id=?',(sid,)).fetchone()
        if not row:raise ValueError('Native ZCode session not found')
        header=dict(row)
        cols={r['name'] for r in con.execute('PRAGMA table_info(message)')}
        order=('sequence IS NULL,sequence,' if 'sequence' in cols else '')+'time_created,rowid'
        messages=[dict(r) for r in con.execute('SELECT * FROM message WHERE session_id=? ORDER BY '+order,(sid,))]
        revert=json.loads(header.get('revert') or '{}')
        target=revert.get('targetMessageID')
        if target:
            ids=[m['id'] for m in messages];keep=revert.get('keptMessageIDs')
            if keep is None:
                if target not in ids:raise ValueError('Native rewind target missing')
                keep=ids[:ids.index(target)]
            cut=revert.get('branchCutAfterMessageID') or revert.get('createdMessageID')
            if cut and cut not in ids:raise ValueError('Native rewind boundary missing')
            tail=ids[ids.index(cut)+(1 if revert.get('branchCutAfterMessageID') else 0):] if cut else []
            active=set(keep)|set(tail);messages=[m for m in messages if m['id'] in active]
        active={m['id'] for m in messages}
        cols={r['name'] for r in con.execute('PRAGMA table_info(part)')}
        order=('sequence IS NULL,sequence,' if 'sequence' in cols else '')+'time_created,rowid'
        parts=[dict(r) for r in con.execute('SELECT * FROM part WHERE session_id=? ORDER BY '+order,(sid,))
               if r['message_id'] in active]
        if include_state:
            tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            state={}
            for table in ('todo','session_target'):
                if table in tables:state[table]=[dict(r) for r in con.execute(f'SELECT * FROM {table} WHERE session_id=?',(sid,))]
            return header,messages,parts,state
        return header,messages,parts
    finally:con.close()


def zcode_watermark(header,messages,parts,state=None):
    h=hashlib.sha256(json.dumps([header,state],sort_keys=True,ensure_ascii=False).encode())
    for table in (messages,parts):
        for row in table:
            h.update(row['id'].encode());h.update(row['data'].encode())
    return h.hexdigest()
