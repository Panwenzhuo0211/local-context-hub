"""Snapshot -> isolated native staging -> verified short publication transaction.

Import is an explicit operation. It never enrolls a sync pair or opens a window.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from .cli import validate
from .native_source import CodexSnapshot,readonly
from .session_ir.restore_codex import _translate_line
from .session_ir.policy import hidden_moment
from .session_ir.live import encode_zcode,MARKER
from .session_ir.schema import Attachment
from .store import atomic,exclusive
from .native_payloads import bound_part,compact_record,PayloadBatch


def _ms(stamp):
    if not isinstance(stamp,str) or not stamp:
        raise ValueError('Native event timestamp is missing; refusing fabricated import dates')
    try:return int(datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()*1000)
    except ValueError as e:raise ValueError('Invalid native event timestamp') from e


def _fingerprint(con,sid,algorithm='json-v1'):
    if algorithm not in ('json-v1','framed-v1'):raise ValueError('Unknown native fingerprint algorithm')
    h=hashlib.sha256()
    for table in ('message','part'):
        columns={r[1] for r in con.execute(f'PRAGMA table_info({table})')}
        fields=[k for k in ('id','message_id','data','time_created','time_updated','sequence') if k in columns]
        if algorithm=='framed-v1':h.update(json.dumps([table,fields],separators=(',',':')).encode())
        for row in con.execute(f'SELECT {",".join(fields)} FROM {table} WHERE session_id=? ORDER BY id',(sid,)):
            if algorithm=='json-v1':h.update(json.dumps(tuple(row),ensure_ascii=False,separators=(',',':')).encode())
            else:
                # Exact typed values with explicit byte lengths are unambiguous;
                # avoid escaping every already-serialized JSON payload again.
                for value in row:
                    raw=b'n' if value is None else (b's'+value.encode('utf-8') if isinstance(value,str) else b'j'+json.dumps(value).encode())
                    h.update(len(raw).to_bytes(8,'big'));h.update(raw)
    return h.hexdigest()


def import_codex_session(root,codex_id,rollout=None,*,replace=False,
                         title_prefix='[来自 Codex] ',dry_run=False,new_copy=False):
    root=Path(root).resolve();config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    validate(root,config)
    if not {'codex','zcode'}<=set(config['sources']):raise ValueError('Both native sources must be enrolled')
    snapshot=CodexSnapshot(config,codex_id,rollout)
    cwd=snapshot.segments[0]['header']['payload'].get('cwd') or ''
    sid='session_'+(uuid.uuid4().hex if new_copy else uuid.uuid5(uuid.NAMESPACE_URL,'context-hub:import:'+codex_id).hex)
    target=Path(config['sources']['zcode']).expanduser().resolve()/'cli/db/db.sqlite'
    probe=readonly(target)
    try:
        columns={r['name'] for r in probe.execute('PRAGMA table_info(session)')}
        msgcols={r['name'] for r in probe.execute('PRAGMA table_info(message)')}
        partcols={r['name'] for r in probe.execute('PRAGMA table_info(part)')}
        same=probe.execute('SELECT * FROM session WHERE directory=? ORDER BY time_updated DESC LIMIT 1',(cwd,)).fetchone()
        template=dict(same) if same else {}
    finally:probe.close()
    if not {'id','session_id','data','time_created','time_updated'}<=msgcols or not {'id','message_id','session_id','data','time_created','time_updated'}<=partcols:
        raise ValueError('Unsupported target native schema')
    title=None;home=Path(config['sources']['codex']).expanduser().resolve()
    if (home/'state_5.sqlite').exists():
        con=readonly(home/'state_5.sqlite')
        try:
            available={r['name'] for r in con.execute('PRAGMA table_info(threads)')}
            if 'title' in available:
                row=con.execute('SELECT title FROM threads WHERE id=?',(codex_id,)).fetchone()
                if row:title=row[0]
        finally:con.close()
    staging_root=root/'import-staging';staging_root.mkdir(parents=True,exist_ok=True)
    with exclusive(root,'native-import.lock'), tempfile.TemporaryDirectory(dir=staging_root) as folder:
        stagepath=Path(folder)/'native.sqlite';stage=sqlite3.connect(stagepath)
        stage.row_factory=sqlite3.Row;batch=PayloadBatch(root)
        try:
            # This database is disposable staging, never the native authority.
            # Its rows are fingerprinted before a separate durable publication.
            stage.execute('PRAGMA journal_mode=OFF');stage.execute('PRAGMA synchronous=OFF')
            stage.executescript('''
                CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT,sequence INTEGER);
                CREATE TABLE part(id TEXT PRIMARY KEY,session_id TEXT,message_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT,sequence INTEGER);
                CREATE TABLE calls(call_id TEXT PRIMARY KEY,part_id TEXT);
            ''')
            counts=Counter();filtered=Counter();first=None;last=None;parent=None;event=0
            verified_inputs=0;verified_images=0
            part_seq=0;msg_seq=0;first_user=None;unmatched=0
            pair='import_'+codex_id+':'+sid
            def identify(prefix,key):return prefix+'_chsync_'+uuid.uuid5(uuid.NAMESPACE_URL,pair+':'+key).hex
            def put(messages,parts):
                nonlocal msg_seq,part_seq
                for table,rows in (('message',messages),('part',parts)):
                    for item in rows:
                        row=dict(item);row['sequence']=msg_seq if table=='message' else part_seq
                        if table=='message':msg_seq+=1
                        else:
                            part_seq+=1
                            row['data']=json.dumps(bound_part(root,json.loads(row['data']),batch=batch),ensure_ascii=False)
                        fields=list(row)
                        stage.execute(f'INSERT INTO {table}({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})',[row[k] for k in fields])
                        if table=='part':
                            data=json.loads(row['data'])
                            if data.get('type')=='tool':stage.execute('INSERT INTO calls VALUES (?,?)',(data['callID'],row['id']))
            for row,ref in snapshot.records():
                wire=str(row.get('type'))+':'+str((row.get('payload') or {}).get('type'))
                translated=compact_record(root,row,snapshot.last_raw,batch=batch)
                for moment in _translate_line(translated,src_file=ref['file'],cwd=ref['cwd']):
                    if hidden_moment(moment) or moment.kind=='thinking':filtered[wire]+=1;continue
                    moment.source_ref=ref;stamp=_ms(moment.ts)
                    first=min(first if first is not None else stamp,stamp);last=max(last if last is not None else stamp,stamp)
                    if moment.kind=='user_text' and first_user is None:first_user=moment.text
                    event+=1;counts[moment.kind]+=1
                    messages,parts,results,parent=encode_zcode([moment],pair=pair,event=ref['record_key']+':'+str(event),
                        sid=sid,cwd=ref['cwd'],parent=parent,timestamp=stamp,identify=identify)
                    if moment.kind=='tool_call' and moment.wire_native:
                        encoded=json.loads(parts[0]['data'])['metadata'][MARKER]['native_wire']
                        if encoded!=moment.wire_native:raise ValueError('Native tool wire changed during encoding')
                        verified_inputs+=1
                    if moment.kind=='user_text':
                        images=[a for a in moment.attachments if a.get('type')=='input_image']
                        files=[json.loads(p['data']) for p in parts if json.loads(p['data']).get('type')=='file']
                        if len(images)!=len(files):raise ValueError('Image attachment disappeared during encoding')
                        verified_images+=len(files)
                    put(messages,parts)
                    for call_id,output,error,end in results:
                        matched=stage.execute('SELECT part_id FROM calls WHERE call_id=?',(call_id,)).fetchone()
                        if not matched:
                            unmatched+=1
                            orphan=Attachment(ts=moment.ts,subtype='orphan_tool_result',data=moment.to_dict(),source_ref=ref)
                            m,p,_,parent=encode_zcode([orphan],pair=pair,event=ref['record_key']+':orphan',sid=sid,
                                cwd=cwd,parent=parent,timestamp=stamp,identify=identify);put(m,p)
                            continue
                        part=stage.execute('SELECT data FROM part WHERE id=?',(matched[0],)).fetchone()
                        data=json.loads(part[0]);state=data['state'];start=data['metadata'][MARKER]['source_started_ms']
                        state.update(status='error' if error else 'completed',time={'start':start,'end':end},
                                     title=data['tool'],metadata={})
                        if moment.extra.get('native_record'):
                            state['metadata']['native_result']=moment.extra['native_record']
                        state['error' if error else 'output']=output
                        stage.execute('UPDATE part SET data=?,time_updated=? WHERE id=?',
                            (json.dumps(bound_part(root,data,batch=batch),ensure_ascii=False),end,matched[0]))
                    if event%400==0:stage.commit()
            stage.commit();source=snapshot.receipt();payload_batch=batch.finalize()
            if first is None:raise ValueError('Source contains no importable native history')
            if source['cwd']!=cwd:
                cwd=source['cwd'];probe=readonly(target)
                try:
                    same=probe.execute('SELECT * FROM session WHERE directory=? ORDER BY time_updated DESC LIMIT 1',(cwd,)).fetchone()
                    template=dict(same) if same else {}
                finally:probe.close()
            title=title_prefix+str(title or first_user or codex_id).splitlines()[0][:160]
            session={'id':sid,'directory':cwd,'path':cwd,'title':title,'time_created':first,'time_updated':last,
                     'project_id':template.get('project_id') or 'proj_context_hub_'+hashlib.sha256(cwd.encode()).hexdigest()[:16],
                     'version':template.get('version') or '0.17.0','slug':'chsync-'+sid.removeprefix('session_')}
            session={k:v for k,v in session.items() if k in columns}
            # Stage fingerprint uses precisely the columns copied to the target.
            for table,available in (('message',msgcols),('part',partcols)):
                if 'sequence' not in available:stage.execute(f'ALTER TABLE {table} DROP COLUMN sequence')
            stage.commit();fingerprint=_fingerprint(stage,sid,'framed-v1')
            report={'zcode_session_id':sid,'codex_session_id':codex_id,'source':source,'cwd':cwd,
                    'messages':msg_seq,'parts':part_seq,'segments':len(source['segments']),
                    'semantic_counts':dict(counts),'filtered_runtime':dict(filtered),
                    'unmatched_tool_results_preserved':unmatched,'first_source_ms':first,'last_source_ms':last,
                    'verified_native_tool_wires':verified_inputs,'verified_image_blocks':verified_images,
                    'target_fingerprint':fingerprint,'fingerprint_algorithm':'framed-v1','title':title,'version':2,'imported_at':time.time(),
                    'dry_run':dry_run,'sync_enabled':False,'complete':True,'payload_batch':payload_batch,
                    'large_payloads':'完整内容保存在校验过的本地附件中；原生会话显示明确预览和路径，模型需按需补读。'}
            if dry_run:return report
            receipt_path=root/'native-imports'/(sid+'.json');pending=receipt_path.with_suffix('.pending.json')
            native=sqlite3.connect(target.as_uri()+'?mode=rw',uri=True,timeout=5);native.row_factory=sqlite3.Row
            try:
                native.execute('PRAGMA foreign_keys=ON');native.execute('BEGIN IMMEDIATE')
                existing=native.execute('SELECT * FROM session WHERE id=?',(sid,)).fetchone()
                if existing:
                    if not replace:raise ValueError('Imported session already exists; use a reviewed replace or new_copy')
                    previous=json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else None
                    if not previous or previous.get('version')!=2:
                        raise ValueError('Existing import has no verified ownership receipt; use new_copy')
                    if _fingerprint(native,sid,previous.get('fingerprint_algorithm','json-v1'))!=previous['target_fingerprint'] or ('title' in columns and existing['title']!=previous['title']):
                        raise ValueError('Target contains local changes; refusing replacement, use new_copy')
                    if last<previous['last_source_ms'] or msg_seq<previous['messages']:
                        raise ValueError('Replacement source is shorter than the verified import; use new_copy')
                    backup=root/'backups/native-imports'/(sid+'-'+str(time.time_ns())+'.sqlite')
                    backup.parent.mkdir(parents=True,exist_ok=True)
                    # Back up only the selected session in a separate private database.
                    saved=sqlite3.connect(backup)
                    try:
                        for table in ('session','message','part'):
                            sql=native.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
                            saved.execute(sql)
                            rows=native.execute(f'SELECT * FROM {table} WHERE '+('id=?' if table=='session' else 'session_id=?'),(sid,))
                            for old in rows:saved.execute(f'INSERT INTO {table} VALUES ({",".join("?" for _ in old)})',tuple(old))
                        saved.commit()
                    finally:saved.close()
                    for table in ('part','message'):native.execute(f'DELETE FROM {table} WHERE session_id=?',(sid,))
                    native.execute('UPDATE session SET '+','.join(k+'=?' for k in session if k!='id')+' WHERE id=?',
                                   [v for k,v in session.items() if k!='id']+[sid])
                else:
                    fields=list(session);native.execute(f'INSERT INTO session({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})',[session[k] for k in fields])
                native.execute('ATTACH DATABASE ? AS staging',(stagepath.as_uri()+'?mode=ro',))
                for table,available in (('message',msgcols),('part',partcols)):
                    fields=[r['name'] for r in stage.execute(f'PRAGMA table_info({table})') if r['name'] in available]
                    cols=','.join(fields)
                    native.execute(f'INSERT INTO {table}({cols}) SELECT {cols} FROM staging.{table}')
                if _fingerprint(native,sid,'framed-v1')!=fingerprint:raise ValueError('Published native rows failed fingerprint verification')
                atomic(pending,json.dumps(report,ensure_ascii=False,indent=2))
                native.commit()
            except BaseException:
                native.rollback();raise
            finally:native.close()
            atomic(receipt_path,json.dumps(report,ensure_ascii=False,indent=2))
            report['receipt']=str(receipt_path)
            report['note']='已校验原生导入；未自动开启同步或打开窗口。'
            return report
        finally:batch.close();stage.close()
