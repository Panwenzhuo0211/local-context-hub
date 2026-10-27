"""Two-phase background worker: ingest and projection hold separate locks.

Phase A (ingest lock) syncs sources into the state store and log segments;
phase B (projection lock) projects changed heads into the read model. A slow
projection or verification therefore never blocks ingestion, and vice versa.
Short-lived per tick; never holds a network socket.
"""
import json
import os
import sqlite3
import time
from pathlib import Path
from .cli import DEFAULT, validate, sync
from .model import dumps, digest
from .store import Store, exclusive, atomic, safe_child
from .matters import Matters

BACKUP_INTERVAL = 7*86400


def publish_status(root,state):
    path=safe_child(root,'background-status.json')
    atomic(path,dumps(state))
    try:
        from .report import render
        render(root)
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as e:
        state['report_error']=str(e)[:300]
        atomic(path,dumps(state))


def fingerprint(config):
    paths=[]
    for name,root in config['sources'].items():
        root=Path(root).expanduser().resolve()
        if name=='codex':
            paths += list((root/'sessions').rglob('*.jsonl'))+list((root/'archived_sessions').rglob('*.jsonl'))
            paths += [root/f for f in ('state_5.sqlite','state_5.sqlite-wal','thread_history_1.sqlite','thread_history_1.sqlite-wal')]
        if name=='zcode':
            paths += [root/f for f in ('cli/db/db.sqlite','cli/db/db.sqlite-wal','v2/tasks-index.sqlite','v2/tasks-index.sqlite-wal')]
        if name=='opencode':
            paths += [root/f for f in ('opencode.db','opencode.db-wal')]
    stats=[]
    for path in sorted(set(paths)):
        try:s=path.stat();stats.append((str(path),s.st_ino,s.st_size,s.st_mtime_ns))
        except FileNotFoundError:stats.append((str(path),'missing'))
    return digest(dumps([stats,config]))


def index_lag(root):
    """Seconds between now and the read model's last projection; None if unknown."""
    try:
        con=sqlite3.connect((Path(root)/'matters.sqlite').as_uri()+'?mode=ro',uri=True)
        try:row=con.execute("SELECT value FROM state WHERE key='indexed_at'").fetchone()
        finally:con.close()
        return round(time.time()-json.loads(row[0])['value'],1) if row else None
    except (sqlite3.Error,OSError,ValueError):return None


def weekly_backup(root):
    """RPO policy: unrebuildable notes must not depend on anyone running backup."""
    def last_at(con):
        row=con.execute("SELECT value FROM cursors WHERE key='auto-backup'").fetchone()
        return json.loads(row[0])['at'] if row else 0
    try:
        con=sqlite3.connect(Path(root)/'state.sqlite',timeout=15)
        stale=time.time()-last_at(con)>=BACKUP_INTERVAL
        con.close()
        if not stale:return None
        with exclusive(root):
            con=sqlite3.connect(Path(root)/'state.sqlite',timeout=15)
            try:
                if time.time()-last_at(con)<BACKUP_INTERVAL:return None
                store=Store(root)
                try:path=store.backup()
                finally:store.close()
                con.execute("INSERT OR REPLACE INTO cursors VALUES ('auto-backup',?)",
                            (dumps({'at':time.time(),'path':str(path)}),))
                con.commit()
                return path
            finally:con.close()
    except (sqlite3.Error,OSError,RuntimeError):return None


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
                publish_status(root,{**old,'status':'ok','last_checked':now})
                return {'status':'unchanged','last_success':old.get('last_success')}
            active=list(signatures) if force or now-old.get('last_success',0)>=86400 else [name for name in signatures if signatures[name]!=old.get('source_fingerprints',{}).get(name)]
            atomic(status_path,dumps({**old,'status':'running','started':now}))
            store=Store(root,migration_locked=True)
            try:
                result=sync(store,config,active_sources=active)
            finally:
                store.flush_log();store.close()
        with exclusive(root,'projection.lock'):
            index=Matters(root)
            try:index_result=index.index()
            finally:index.close()
            store=Store(root)
            try:
                store.clear_pending()
                pending=store.db.execute('SELECT count(*) FROM pending_index').fetchone()[0]
            finally:store.close()
            lag=index_lag(root)
        weekly_backup(root)
        # Adapter-level failures (source missing, schema drift) must be visible
        # in one glance, not buried among routine missing-log warnings.
        alarms=[w for w in result.get('warnings',[])
                if 'adapter skipped' in w or 'Unsupported source schema' in w]
        state={'status':'ok','last_success':time.time(),'last_checked':time.time(),'fingerprint':signature,
               'sync':result,'index':index_result,'source_fingerprints':signatures,
               'pending_heads':pending,'index_lag_seconds':lag,'alarms':alarms}
        publish_status(root,state)
        return state
    except RuntimeError as e:
        # An overlapping worker is routine; it must not overwrite the live status.
        if 'Another Context Hub' in str(e):return {'status':'busy'}
        raise
    except Exception as e:
        atomic(status_path,dumps({'status':'error','at':time.time(),'error':str(e)[:1000]}))
        raise


if __name__=='__main__':
    import argparse
    from .cli import setup_console
    os.umask(0o077)
    setup_console()
    p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,default=DEFAULT);p.add_argument('--force',action='store_true')
    a=p.parse_args()
    try:print(dumps(tick(a.data_dir,a.force)))
    except Exception as e:
        import sys
        print('Context Hub background failed: '+str(e),file=sys.stderr);raise SystemExit(1)
