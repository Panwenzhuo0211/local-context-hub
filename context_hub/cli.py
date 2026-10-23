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

DEFAULT = default_data_dir()

def default_config():
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
    if any(x in str(root).lower() for x in ('/mobile documents/','/cloudstorage/','/dropbox/')):
        raise ValueError('Cloud-backed data directory refused; choose a local directory')

def sync(store,config,active_sources=None):
    warnings=[];counts={};started=time.time()
    # A failure rolls back records AND cursors together; previous archive remains valid.
    with store.db:
        for name,root in config['sources'].items():
            if active_sources is not None and name not in active_sources:
                counts[name]={'examined':0,'new_revisions':0,'unchanged_source':True}
                continue
            total=added=0
            for event in ADAPTERS[name](root).records(store,warnings):
                total+=1;added+=store.ingest(event,config)
            counts[name]={'examined':total,'new_revisions':added}
        mapping_signature=__import__('hashlib').sha256(dumps({k:config.get(k) for k in ('projects','session_projects','generic_roots')}).encode()).hexdigest()
        if store.cursor('mapping_signature').get('value')!=mapping_signature:
            store.reclassify(config)
            store.set_cursor('mapping_signature',{'value':mapping_signature})
        result={'adapters':counts,'warnings':warnings,'elapsed_seconds':round(time.time()-started,3)}
        store.db.execute('INSERT INTO runs(started,result) VALUES (?,?)',(started,dumps(result)))
    return result

def main(argv=None):
    os.umask(0o077);use_utf8_stdio()
    p=argparse.ArgumentParser(description='Local read-only ZCode ↔ Codex context archive')
    p.add_argument('--data-dir',type=Path,default=DEFAULT)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('init');sub.add_parser('sync');sub.add_parser('projects');sub.add_parser('backup')
    sessions=sub.add_parser('sessions');sessions.add_argument('--project')
    ex=sub.add_parser('export');ex.add_argument('--project',required=True);ex.add_argument('--target',choices=['either','codex','zcode'],default='either')
    ho=sub.add_parser('handoff');ho.add_argument('--project',required=True);ho.add_argument('--target',choices=['codex','zcode'],required=True)
    note=sub.add_parser('note');note.add_argument('--project',required=True);note.add_argument('--author',choices=['user','codex','zcode'],required=True)
    note.add_argument('--kind',choices=['memory','decision','task','opinion'],required=True)
    note.add_argument('--topic',required=True);note.add_argument('--body',required=True)
    note.add_argument('--evidence',type=int,action='append',default=[])
    note.add_argument('--status',choices=['proposed','accepted','done','rejected'],default='proposed')
    mp=sub.add_parser('map');mp.add_argument('--project',required=True)
    group=mp.add_mutually_exclusive_group(required=True);group.add_argument('--root',type=Path);group.add_argument('--session',help='adapter:session_id')
    sub.add_parser('matters')
    context=sub.add_parser('context');context.add_argument('query')
    search=sub.add_parser('search');search.add_argument('query');search.add_argument('--matter');search.add_argument('--limit',type=int,default=10)
    sub.add_parser('context-status')
    args=p.parse_args(argv);root=args.data_dir.expanduser().resolve()
    if args.command in ('matters','context','search','context-status'):
        from .mcp_server import Server
        name={'matters':'list_matters','context':'continue_matter','search':'search_context','context-status':'context_status'}[args.command]
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
            store=Store(root)
            try:
                if args.command=='sync':print(dumps(sync(store,config)))
                elif args.command=='projects':print(json.dumps(store.projects(),indent=2,ensure_ascii=False))
                elif args.command=='sessions':
                    from .model import unpack
                    result=[]
                    query="SELECT r.* FROM revisions r JOIN heads h ON r.id=h.revision_id WHERE r.source_key LIKE 'thread:%' OR r.source_key LIKE 'session:%'"
                    for r in store.db.execute(query):
                        if args.project and r['project']!=args.project:continue
                        e=unpack(r['envelope'])
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
