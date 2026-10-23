"""A short-lived local worker suitable for launchd; never holds a network socket."""
import json
import os
import sqlite3
import time
from pathlib import Path
from .cli import DEFAULT, validate, sync
from .model import dumps, digest
from .store import Store, exclusive, atomic, safe_child
from .matters import Matters


def fingerprint(config):
    paths=[]
    for name,root in config['sources'].items():
        root=Path(root).expanduser().resolve()
        if name=='codex':
            paths += list((root/'sessions').rglob('*.jsonl'))+list((root/'archived_sessions').rglob('*.jsonl'))
            paths += [root/f for f in ('state_5.sqlite','state_5.sqlite-wal','thread_history_1.sqlite','thread_history_1.sqlite-wal')]
        if name=='zcode':
            paths += [root/f for f in ('cli/db/db.sqlite','cli/db/db.sqlite-wal','v2/tasks-index.sqlite','v2/tasks-index.sqlite-wal')]
    stats=[]
    for path in sorted(set(paths)):
        try:s=path.stat();stats.append((str(path),s.st_ino,s.st_size,s.st_mtime_ns))
        except FileNotFoundError:stats.append((str(path),'missing'))
    return digest(dumps([stats,config]))


def tick(root=DEFAULT, force=False):
    root=Path(root).expanduser().resolve()
    config=json.loads((root/'config.json').read_text(encoding='utf-8'));validate(root,config)
    status_path=safe_child(root,'background-status.json')
    try:
        with exclusive(root):
            old=json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else {}
            signatures={name:fingerprint({**config,'sources':{name:source}}) for name,source in config['sources'].items()}
            signature=digest(dumps(signatures));now=time.time()
            if not force and old.get('fingerprint')==signature and now-old.get('last_success',0)<86400:
                return {'status':'unchanged','last_success':old.get('last_success')}
            active=list(signatures) if force or now-old.get('last_success',0)>=86400 else [name for name in signatures if signatures[name]!=old.get('source_fingerprints',{}).get(name)]
            atomic(status_path,dumps({**old,'status':'running','started':now}))
            store=Store(root)
            try:result=sync(store,config,active_sources=active)
            finally:store.close()
            index=Matters(root)
            try:index_result=index.index()
            finally:index.close()
            state={'status':'ok','last_success':time.time(),'fingerprint':signature,
                   'sync':result,'index':index_result,'source_fingerprints':signatures}
            atomic(status_path,dumps(state));return state
    except RuntimeError as e:
        # An overlapping worker is routine; it must not overwrite the live status.
        if 'Another Context Hub' in str(e):return {'status':'busy'}
        raise
    except Exception as e:
        atomic(status_path,dumps({'status':'error','at':time.time(),'error':str(e)[:1000]}))
        raise


if __name__=='__main__':
    import argparse
    os.umask(0o077)
    from .model import use_utf8_stdio;use_utf8_stdio()
    p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,default=DEFAULT);p.add_argument('--force',action='store_true')
    a=p.parse_args()
    try:print(dumps(tick(a.data_dir,a.force)))
    except Exception as e:
        import sys
        print('Context Hub background failed: '+str(e),file=sys.stderr);raise SystemExit(1)
