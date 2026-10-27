import argparse
import json
import os
import sys
import time
from pathlib import Path
from .adapters import ADAPTERS
from .model import dumps, use_utf8_stdio
from .paths import default_data_dir
from .store import Store, exclusive, atomic, safe_child
from .views import render

_FALLBACK=default_data_dir()
if sys.platform not in ('darwin','win32') and not os.environ.get('CONTEXT_HUB_DATA_DIR'):
    # Keep earlier Linux installations discoverable instead of orphaning data.
    legacy=Path.home()/'Library/Application Support/ContextHub'
    if (legacy/'config.json').is_file() and not (_FALLBACK/'config.json').exists():_FALLBACK=legacy

def resolve_data_dir(fallback=_FALLBACK):
    """Follow a relocation pointer so bulk data can live on another drive.

    A one-line LOCATION file in the default directory redirects every entry
    point (CLI, MCP servers, worker, installer) without environment plumbing.
    """
    env_override = os.environ.get('CONTEXT_HUB_DATA_DIR')
    if env_override:
        return Path(env_override).expanduser()
    try:
        target=Path((Path(fallback)/'LOCATION').read_text(encoding='utf-8').strip())
        if target.is_absolute() and target!=Path(fallback) and target.exists():
            return target
    except OSError: pass
    return Path(fallback)

DEFAULT = resolve_data_dir()

def setup_console():
    """Windows consoles default to a legacy code page; UTF-8 avoids losing output."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass

def default_config():
    # Opt-in sources only: an installed agent is not an enrolled one. Adding a
    # source enrolls its full history; removing it stops scanning immediately.
    return {'schema_version':1, 'sources':{'codex':str(Path.home()/'.codex'),'zcode':str(Path.home()/'.zcode')},
            'projects':{}, 'session_projects':{}, 'generic_roots':[str(Path.home()/'.zcode/workspace/default')]}

def validate(root,config):
    root = Path(root).expanduser().resolve()
    if config.get('schema_version')!=1:raise ValueError('Unsupported config schema version')
    if not config.get('sources'):raise ValueError('At least one source is required')
    for name,source in config['sources'].items():
        if name not in ADAPTERS:raise ValueError('Unknown adapter: '+name)
        src=Path(source).expanduser().resolve()
        if root==src or root.is_relative_to(src) or src.is_relative_to(root):
            raise ValueError('Data directory and source roots must not overlap')
    # Block recognizable cloud locations; local storage can still have OS backup.
    lowered=str(root).lower().replace('\\','/')
    if any(x in lowered for x in ('/mobile documents/','/cloudstorage/','/dropbox/','/onedrive/')):
        raise ValueError('Cloud-backed data directory refused; choose a local directory')

def source_paths(config):
    """Files whose mtimes reflect source freshness; cheap enough for query time."""
    paths=[]
    for name,root in config.get('sources',{}).items():
        root=Path(root).expanduser().resolve()
        if name=='codex':
            paths += [root/f for f in ('state_5.sqlite','state_5.sqlite-wal',
                                        'thread_history_1.sqlite','thread_history_1.sqlite-wal')]
            for folder in ('sessions','archived_sessions'):
                paths.append(root/folder)
        if name=='zcode':
            paths += [root/f for f in ('cli/db/db.sqlite','cli/db/db.sqlite-wal',
                                        'v2/tasks-index.sqlite','v2/tasks-index.sqlite-wal')]
        if name=='opencode':
            paths += [root/f for f in ('opencode.db','opencode.db-wal')]
    return paths

CHUNK_RECORDS = 20000

def sync(store,config,active_sources=None,progress=None):
    warnings=[];counts={};started=time.time()
    # Records and their cursors commit together per chunk. A crash replays from the
    # last committed cursor; digest dedup absorbs the overlap, so the previous
    # archive stays valid and a first import of a large source can resume.
    try:
        for name,root in config['sources'].items():
            if active_sources is not None and name not in active_sources:
                counts[name]={'examined':0,'new_revisions':0,'unchanged_source':True}
                continue
            total=added=0;pending=0
            for event in ADAPTERS[name](root).records(store,warnings):
                total+=1;added+=store.ingest(event,config);pending+=1
                if pending>=CHUNK_RECORDS:
                    store.db.commit();store.flush_log();pending=0
                    if progress:progress(name,total,added)
            if pending:store.db.commit();store.flush_log()
            counts[name]={'examined':total,'new_revisions':added}
        mapping_signature=__import__('hashlib').sha256(dumps({k:config.get(k) for k in ('projects','session_projects','generic_roots')}).encode()).hexdigest()
        if store.cursor('mapping_signature').get('value')!=mapping_signature:
            store.reclassify(config)
            store.set_cursor('mapping_signature',{'value':mapping_signature})
        shown=warnings[:50]
        result={'adapters':counts,'warnings':shown,'elapsed_seconds':round(time.time()-started,3)}
        if len(warnings)>len(shown):result['warnings_omitted']=len(warnings)-len(shown)
        store.db.execute('INSERT INTO runs(started,result) VALUES (?,?)',(started,dumps(result)))
        store.db.commit()
        return result
    except Exception:
        store.db.rollback()
        raise

def main(argv=None):
    os.umask(0o077)
    setup_console()
    p=argparse.ArgumentParser(description='Local read-only ZCode ↔ Codex context archive')
    from . import __version__
    p.add_argument('--version',action='version',version=__version__)
    p.add_argument('--data-dir',type=Path,default=DEFAULT)
    sub=p.add_subparsers(dest='command',required=True)
    agents=sub.add_parser('agents',help='Owner-only enrollment, grants and revocation')
    agents.add_argument('agent_args',nargs=argparse.REMAINDER)
    sub.add_parser('init');syncp=sub.add_parser('sync')
    syncp.add_argument('--progress',action='store_true',help='Report chunk progress to stderr during sync')
    sub.add_parser('projects');sub.add_parser('backup')
    sessions=sub.add_parser('sessions');sessions.add_argument('--project')
    ex=sub.add_parser('export');ex.add_argument('--project',required=True);ex.add_argument('--target',choices=['either','codex','zcode'],default='either')
    ho=sub.add_parser('handoff');ho.add_argument('--project',required=True);ho.add_argument('--target',choices=['codex','zcode'],required=True)
    note=sub.add_parser('note');note.add_argument('--project',required=True);note.add_argument('--author',choices=['user','codex','zcode','opencode'],required=True)
    note.add_argument('--kind',choices=['memory','decision','task','opinion'],required=True)
    note.add_argument('--topic',required=True);note.add_argument('--body',required=True)
    note.add_argument('--evidence',type=int,action='append',default=[])
    note.add_argument('--status',choices=['proposed','accepted','done','rejected'],default='proposed')
    mp=sub.add_parser('map');mp.add_argument('--project',required=True)
    group=mp.add_mutually_exclusive_group(required=True);group.add_argument('--root',type=Path);group.add_argument('--session',help='adapter:session_id')
    sub.add_parser('matters')
    sub.add_parser('report')
    sub.add_parser('compact')
    native=sub.add_parser('session-sync',help='Explicit append-only native session pairing/sync')
    native.add_argument('sync_args',nargs=argparse.REMAINDER)
    nctx=sub.add_parser('native-context',help='Read latest original context without native import/sync')
    nctx.add_argument('session');nctx.add_argument('--limit',type=int,default=20);nctx.add_argument('--cursor')
    nrec=sub.add_parser('native-record',help='Page a verified original context record')
    nrec.add_argument('session');nrec.add_argument('record_ref');nrec.add_argument('--offset',type=int,default=0)
    nrec.add_argument('--length',type=int,default=12000);nrec.add_argument('--watermark')
    payload=sub.add_parser('native-payload',help='Read a verified native history attachment')
    payload.add_argument('payload_ref');payload.add_argument('--offset',type=int,default=0)
    payload.add_argument('--length',type=int,default=12000)
    emit=sub.add_parser('emit',help='Emit an archived ZCode session as a native Codex session')
    emit.add_argument('--session',required=True,help='zcode:<session_id>')
    emit.add_argument('--target-dir',type=Path,default=None,help='Override Codex home (default: real ~/.codex)')
    emit.add_argument('--dry',action='store_true',help='Build the IR and report, write nothing')
    context=sub.add_parser('context');context.add_argument('query')
    search=sub.add_parser('search');search.add_argument('query');search.add_argument('--matter');search.add_argument('--limit',type=int,default=10)
    sub.add_parser('context-status')
    sub.add_parser('doctor',help='Read-only version, freshness and source diagnostics')
    args=p.parse_args(argv);root=args.data_dir.expanduser().resolve()
    if args.command=='agents':
        from .agents import main as agents_main
        return agents_main(['--data-dir',str(root),*args.agent_args])
    if args.command=='native-payload':
        from .native_payloads import read_payload
        print(dumps(read_payload(root,args.payload_ref,offset=args.offset,length=args.length)));return 0
    if args.command in ('native-context','native-record'):
        from . import native_context
        if args.command=='native-context':result=native_context.read_session(root,args.session,limit=args.limit,cursor=args.cursor)
        else:result=native_context.read_record(root,args.session,args.record_ref,offset=args.offset,length=args.length,watermark=args.watermark)
        print(dumps(result));return 0
    if args.command=='session-sync':
        from .session_sync import main as native_main
        return native_main(['--data-dir',str(root)]+args.sync_args)
    if args.command=='emit':
        from .session_ir import restore_from_archive
        from .session_ir import emit_codex, mirror_map
        try:
            if not args.session.startswith('zcode:'):
                raise ValueError('Expected --session zcode:<session_id>')
            store=Store(root)
            try:
                zid=args.session.split(':',1)[1]
                ir=restore_from_archive(store, zid)
                watermark=store.db.execute(
                    "SELECT max(id) FROM revisions WHERE adapter='zcode' AND session=?",
                    (zid,)).fetchone()[0]
            finally:
                store.close()
            if args.dry:
                kinds={}
                for m in ir.moments: kinds[m.kind]=kinds.get(m.kind,0)+1
                print(dumps({'ir_id':ir.id,'moments':len(ir.moments),'by_kind':kinds,
                             'cwd':ir.cwd,'dry':True}))
                return 0
            from .mcp_server import Server
            from .matters import Matters
            m=Matters(root)
            try:result=Server(root).prepare_resume(m,{'session':args.session},target_dir=args.target_dir)
            finally:m.close()
            print(dumps(result))
            return 0
        except (ValueError,OSError,__import__('sqlite3').Error) as e:
            print('Context Hub: '+str(e),file=sys.stderr);return 1
    if args.command=='report':
        from .report import render
        try:
            print(dumps({'report':str(render(root))}));return 0
        except (ValueError,OSError,__import__('sqlite3').Error) as e:
            print('Context Hub: '+str(e),file=sys.stderr);return 1
    if args.command in ('matters','context','search','context-status','doctor'):
        from .mcp_server import Server
        name={'matters':'list_matters','context':'continue_matter','search':'search_context','context-status':'context_status','doctor':'context_status'}[args.command]
        values={'query':args.query} if hasattr(args,'query') else {}
        if args.command=='search':
            values['limit']=args.limit
            if args.matter:values['matter']=args.matter
        try:print(json.dumps(Server(root).call(name,values),ensure_ascii=False,indent=2));return 0
        except (ValueError,OSError,__import__('sqlite3').Error) as e:
            print(str(e),file=sys.stderr);return 1
    try:
        config_path=root/'config.json'
        config=json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else default_config()
        validate(root,config)
        with exclusive(root):
            if args.command=='init':
                if not config_path.exists():atomic(config_path,json.dumps(config,indent=2,ensure_ascii=False)+'\n')
                print(dumps({'data_dir':str(root),'config':str(config_path)}));return 0
            if not config_path.exists():raise ValueError('Run init first')
            store=Store(root,migration_locked=True)
            try:
                if args.command=='sync':
                    progress=None
                    if args.progress or sys.stderr.isatty():
                        progress=lambda name,total,added: print(f'context-hub sync {name}: {total} examined, {added} new',
                                                                 file=sys.stderr,flush=True)
                    print(dumps(sync(store,config,progress=progress)))
                elif args.command=='compact':
                    print(dumps(store.compact()))
                elif args.command=='projects':print(json.dumps(store.projects(),indent=2,ensure_ascii=False))
                elif args.command=='sessions':
                    result=[]
                    query="SELECT r.* FROM revisions r JOIN heads h ON r.id=h.revision_id WHERE r.source_key LIKE 'thread:%' OR r.source_key LIKE 'session:%'"
                    for r in store.db.execute(query):
                        if args.project and r['project']!=args.project:continue
                        e=unpack(store.envelope_of(r))
                        result.append({'adapter':e['adapter'],'session':e['session_id'],'project':r['project'],'cwd':e['cwd'],'title':e['raw'].get('title','')})
                    print(json.dumps(result,ensure_ascii=False,indent=2))
                elif args.command=='backup':print(store.backup())
                elif args.command=='map':
                    backup=safe_child(root,'backups',f'config-{time.time_ns()}.json')
                    atomic(backup,config_path.read_text(encoding='utf-8'))
                    if args.root:
                        config.setdefault('projects',{}).setdefault(args.project,[])
                        value=str(args.root.expanduser().resolve())
                        if value not in config['projects'][args.project]:config['projects'][args.project].append(value)
                    else:
                        if ':' not in args.session or args.session.split(':',1)[0] not in ADAPTERS:raise ValueError('Expected adapter:session_id')
                        config.setdefault('session_projects',{})[args.session]=args.project
                    atomic(config_path,json.dumps(config,ensure_ascii=False,indent=2)+'\n')
                    with store.db:store.reclassify(config)
                    print(dumps({'project':args.project,'backup':str(backup),'next':'export this project; prior exports are historical snapshots'}))
                else:
                    known={r['project'] for r in store.projects()}
                    if args.project not in known:raise ValueError('Unknown project; sync and list projects first')
                    if args.command=='note':
                        for rid in args.evidence:
                            r=store.db.execute('SELECT project FROM revisions WHERE id=?',(rid,)).fetchone()
                            if not r or r[0]!=args.project:raise ValueError('Evidence must reference an existing record in this project')
                        with store.db:
                            nid=store.note(args.project,args.author,args.kind,args.topic,args.body,args.evidence,args.status)
                        print(dumps({'note_id':nid,'folder':str(render(store,args.project,include_archive=False))}))
                    else:print(render(store,args.project,args.target,include_archive=args.command=='export'))
            finally:store.close()
        return 0
    except (ValueError,RuntimeError,OSError,__import__('sqlite3').Error) as e:
        print('Context Hub: '+str(e),file=sys.stderr);return 1
