"""Local release installation with private database snapshots and code rollback.

No downloads, credential handling, source-application writes or GitHub mutations.
The caller pauses the owned scheduler before installing and resumes it afterward.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from . import __version__
from .store import Store, atomic, exclusive


def snapshot(data, backup):
    """Consistent individual SQLite snapshots; caller holds the ingest lock."""
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    files=[]
    for path in data.iterdir():
        if not path.is_file() or path.is_symlink():continue
        target=backup/path.name
        if path.suffix=='.sqlite':
            src=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=30)
            dst=sqlite3.connect(target)
            try:src.backup(dst)
            finally:dst.close();src.close()
        elif path.suffix in ('.json','.txt') or path.name=='LOCATION':
            shutil.copy2(path,target)
        else:continue
        target.chmod(0o600);files.append(path.name)
    # Segments are append-only. Preserve them with hard links where available;
    # explicitly record that the backup is local, not a disaster-recovery copy.
    if (data/'log').is_dir():
        shutil.copytree(data/'log',backup/'log',copy_function=shutil.copy2)
    atomic(backup/'inventory.json',json.dumps({'files':files,'created':time.time()}))
    return files


def _stage(source, runtime, staged):
    if not (source/'context_hub/__init__.py').is_file():
        raise ValueError('Source is not a Context Hub release')
    if runtime.exists():
        shutil.copytree(runtime,staged,symlinks=True)
    else:staged.mkdir(mode=0o700)
    package=staged/'context_hub'
    if package.is_symlink():raise ValueError('Refusing symlink runtime package')
    if package.exists():shutil.rmtree(package)
    shutil.copytree(source/'context_hub',package,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for path in source.iterdir():
        if path.is_file() and not path.is_symlink() and (
                path.suffix in ('.py','.md','.command') or path.name in ('hub','hub.cmd','envelope.schema.json')):
            shutil.copy2(path,staged/path.name)
            if path.name=='hub' or path.suffix=='.command':(staged/path.name).chmod(0o700)
    result=subprocess.run([sys.executable,'-c',
        'from context_hub.mcp_server import Server; '
        'r=Server().handle({"jsonrpc":"2.0","id":1,"method":"initialize"}); '
        'assert r["result"]["serverInfo"]["name"]=="context-hub"'],
        cwd=staged,capture_output=True,text=True,timeout=30)
    if result.returncode:raise ValueError('Staged release could not initialize: '+result.stderr[-400:])


def _validate_data(data):
    store=Store(data,migration_locked=True)
    try:
        if store.db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('State integrity check failed')
        count=store.db.execute('SELECT count(*) FROM revisions').fetchone()[0]
        return {'revisions':count}
    finally:store.close()


def install_release(source, runtime, data, commit=None):
    source=Path(source).resolve();runtime=Path(runtime).absolute();data=Path(data).resolve()
    if runtime.is_symlink() or source==runtime.resolve():
        raise ValueError('Runtime must be a distinct, owned directory')
    if data==runtime.resolve() or data.is_relative_to(runtime.resolve()) or runtime.resolve().is_relative_to(data):
        raise ValueError('Runtime and data must not overlap')
    tag=str(time.time_ns())
    backup=data/'backups'/('release-'+tag)
    staged=runtime.with_name(runtime.name+'.stage-'+tag)
    backup.mkdir(parents=True,mode=0o700)
    original={p.name for p in data.iterdir()}
    code_swapped=False;had_runtime=runtime.exists()
    with exclusive(data,'upgrade.lock'),exclusive(data),exclusive(data,'projection.lock'):
        try:
            snapshot(data,backup/'data')
            _stage(source,runtime,staged)
            before=None
            legacy=data/'archive.sqlite'
            if legacy.exists():
                with sqlite3.connect(legacy.as_uri()+'?mode=ro',uri=True) as db:
                    before=db.execute('SELECT count(*) FROM revisions').fetchone()[0]
            validation=_validate_data(data)
            if 'state.sqlite' not in original and before is not None and validation['revisions']!=before:
                raise ValueError('Migration record count mismatch')
            metadata={'version':__version__,'commit':commit,'installed_at':time.time(),
                      'backup':str(backup),'validation':validation}
            atomic(staged/'deployment.json',json.dumps(metadata,ensure_ascii=False,indent=2))
            if had_runtime:os.replace(runtime,backup/'runtime')
            os.replace(staged,runtime);code_swapped=True
            # Verify actual installed code, not only the pre-swap stage.
            result=subprocess.run([sys.executable,'-m','context_hub','--version'],cwd=runtime,
                                  capture_output=True,text=True,timeout=30)
            if result.returncode or result.stdout.strip()!=__version__:
                raise ValueError('Installed release failed version check')
            atomic(backup/'release.json',json.dumps({**metadata,'status':'installed','runtime':str(runtime)}))
            return {**metadata,'status':'installed','runtime':str(runtime)}
        except Exception:
            # Code is recoverable, including a candidate that failed validation.
            if code_swapped and runtime.exists():os.replace(runtime,backup/'failed-runtime')
            if (backup/'runtime').exists() and not runtime.exists():os.replace(backup/'runtime',runtime)
            if staged.exists():os.replace(staged,backup/'failed-stage')
            # Keep original databases and notes. Quarantine only new migration
            # artifacts; do not restore an older notes snapshot over live writes.
            quarantine=backup/'failed-migration'
            for name in ('state.sqlite','state.sqlite-wal','state.sqlite-shm','log'):
                path=data/name
                if name not in original and path.exists():
                    quarantine.mkdir(exist_ok=True,mode=0o700)
                    os.replace(path,quarantine/name)
            atomic(backup/'release.json',json.dumps({'status':'rolled_back','runtime':str(runtime)}))
            raise


def rollback_code(runtime, backup):
    """Restore code only; never replace current notes with an old snapshot."""
    runtime=Path(runtime).absolute();backup=Path(backup).resolve()
    if not (backup/'runtime/context_hub/__init__.py').is_file():
        raise ValueError('No recoverable previous runtime')
    legacy=backup/'data/archive.sqlite'
    current=backup.parent.parent/'state.sqlite'
    if legacy.exists() and not (backup/'data/state.sqlite').exists() and current.exists():
        with sqlite3.connect(legacy.as_uri()+'?mode=ro',uri=True) as old, \
             sqlite3.connect(current.as_uri()+'?mode=ro',uri=True) as new:
            old_count=old.execute('SELECT count(*) FROM revisions').fetchone()[0]
            new_count=new.execute('SELECT count(*) FROM revisions').fetchone()[0]
            query='SELECT * FROM heads ORDER BY adapter,session,source_key'
            if old_count!=new_count or old.execute(query).fetchall()!=new.execute(query).fetchall():
                raise ValueError('New archive work exists; legacy downgrade needs a verified conversion. Code and current data preserved.')
    retained=backup/('replaced-runtime-'+str(time.time_ns()))
    if runtime.exists():os.replace(runtime,retained)
    try:os.replace(backup/'runtime',runtime)
    except Exception:
        if retained.exists():os.replace(retained,runtime)
        raise
    return {'status':'previous_code_restored','retained':str(retained),
            'data_note':'Current data preserved; unsafe legacy format downgrades are refused.'}


def main():
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path)
    p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--commit')
    p.add_argument('--rollback',type=Path)
    a=p.parse_args()
    if a.rollback:result=rollback_code(a.runtime,a.rollback)
    else:
        if not a.source:p.error('--source is required for installation')
        result=install_release(a.source,a.runtime,a.data_dir,a.commit)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
