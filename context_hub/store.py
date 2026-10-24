"""Private local archive: small state store + segmented append-only log.

Envelope payloads live in immutable segment files so the metadata store stays
tiny, cold segments back up incrementally, and history never has to be copied
whole again. A crash between a segment append and its commit leaves an
unreferenced frame: harmless garbage that sealing and compaction reclaim.
"""
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from .model import dumps, digest, project_for, pack, unpack, slim

if sys.platform == 'win32':
    import msvcrt

    def _lock_byte(f):
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('Another Context Hub process is writing; retry after it finishes')

    def _unlock_byte(f):
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_byte(f):
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another Context Hub process is writing; retry after it finishes')

    def _unlock_byte(f):
        fcntl.flock(f, fcntl.LOCK_UN)

@contextmanager
def exclusive(root, lockfile='hub.lock'):
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (root/lockfile).is_symlink(): raise ValueError('Refusing symlink lock')
    with (root/lockfile).open('a') as f:
        _lock_byte(f)
        try:
            yield
        finally:
            _unlock_byte(f)

def safe_child(root, *parts):
    root=Path(root).resolve()
    path=root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError('Output path escapes private data directory')
    current=root
    for part in path.relative_to(root).parts:
        current=current/part
        if current.is_symlink():raise ValueError('Refusing symlink inside data directory')
    return path

def atomic(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f'Refusing symlink output: {path}')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name+'.tmp')
    if temp.is_symlink():
        raise ValueError('Refusing symlink temporary file')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:
        f.write(text); f.flush(); os.fsync(f.fileno())
    os.replace(temp,path)

SEGMENT_LIMIT = 256*1024*1024

def segment_path(root,index):
    return Path(root)/'log'/f'seg-{index:06d}.log'

def read_blob(root,seg,offset,length):
    with segment_path(root,seg).open('rb') as f:
        f.seek(offset)
        frame=f.read(length)
    if len(frame)!=length or len(frame)<4:
        raise ValueError('Corrupt or truncated segment frame')
    return frame[4:]

STATE_SCHEMA='''
CREATE TABLE IF NOT EXISTS revisions (
  id INTEGER PRIMARY KEY, adapter TEXT, session TEXT, source_key TEXT,
  revision TEXT, project TEXT, seg INTEGER, off INTEGER, len INTEGER, seen_at REAL,
  UNIQUE(adapter,session,source_key,revision));
CREATE TABLE IF NOT EXISTS heads (
  adapter TEXT, session TEXT, source_key TEXT, revision_id INTEGER,
  PRIMARY KEY(adapter,session,source_key));
CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pending_index (
  key TEXT PRIMARY KEY, revision_id INTEGER, project TEXT);
CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY, project TEXT, author TEXT, kind TEXT, topic TEXT,
  body TEXT, evidence TEXT, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started REAL, result TEXT);
CREATE INDEX IF NOT EXISTS revision_project ON revisions(project);
CREATE INDEX IF NOT EXISTS head_revision ON heads(revision_id);
'''

class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.log_dir = self.root/'log'
        self.log_dir.mkdir(exist_ok=True)
        self._hot=None; self._hot_path=None
        self._hot_index=self._highest_segment()
        if (self.root/'archive.sqlite').exists() and not (self.root/'state.sqlite').exists():
            with exclusive(self.root):
                if (self.root/'archive.sqlite').exists() and not (self.root/'state.sqlite').exists():
                    self._migrate_v1()
        self.db = sqlite3.connect(self.root/'state.sqlite')
        try: os.chmod(self.root/'state.sqlite',0o600)
        except OSError: pass
        # WAL keeps the ingest and journal-purge writers from long-blocking each other.
        try: self.db.execute('PRAGMA journal_mode=WAL')
        except sqlite3.Error: pass
        self.db.row_factory = sqlite3.Row
        self.db.executescript(STATE_SCHEMA)

    def _highest_segment(self):
        best=-1
        for p in (self.root/'log').glob('seg-*.log'):
            try: best=max(best,int(p.stem.split('-')[1]))
            except (ValueError,IndexError): continue
        return max(best,0)

    def _append(self,blob):
        # Unbuffered: offsets recorded by ingest must be readable immediately;
        # durability is bounded by flush_log() at commit boundaries.
        frame=len(blob).to_bytes(4,'big')+blob
        while True:
            path=segment_path(self.root,self._hot_index)
            if self._hot is None or self._hot.closed or self._hot_path!=path:
                if self._hot and not self._hot.closed: self._hot.close()
                self._hot=open(path,'ab',buffering=0); self._hot_path=path
            size=os.fstat(self._hot.fileno()).st_size
            if size and size+len(frame)>SEGMENT_LIMIT:
                self._hot.close(); self._hot=None; self._hot_index+=1
                continue
            self._hot.write(frame)
            return self._hot_index,size,len(frame)

    def flush_log(self):
        if self._hot and not self._hot.closed:
            os.fsync(self._hot.fileno())

    def close(self):
        try:
            if self._hot and not self._hot.closed: self._hot.close()
        finally:
            self.db.close()

    def envelope_of(self,row):
        return read_blob(self.root,row['seg'],row['off'],row['len'])

    def cursor(self,key):
        row=self.db.execute('SELECT value FROM cursors WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_cursor(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)',(key,dumps(value)))

    def ingest(self,event,config):
        project=project_for(event['cwd'],event['adapter'],event['session_id'],config)
        # Identity digest over the ORIGINAL event; storage gets the slimmed copy.
        revision=digest(dumps(event))
        args=(event['adapter'],event['session_id'],event['source_key'])
        existing=self.db.execute('SELECT id FROM revisions WHERE adapter=? AND session=? AND source_key=? AND revision=?',(*args,revision)).fetchone()
        if existing:
            rid=existing[0];added=0
        else:
            seg,off,length=self._append(pack(slim(event)))
            result=self.db.execute('INSERT INTO revisions(adapter,session,source_key,revision,project,seg,off,len,seen_at) VALUES (?,?,?,?,?,?,?,?,?)',
                                   (*args,revision,project,seg,off,length,time.time()))
            rid=result.lastrowid;added=1
        head=self.db.execute('SELECT revision_id FROM heads WHERE adapter=? AND session=? AND source_key=?',args).fetchone()
        if not head or head[0]!=rid:
            self.db.execute('INSERT OR REPLACE INTO heads VALUES (?,?,?,?)',(*args,rid))
            self.db.execute('INSERT OR REPLACE INTO pending_index VALUES (?,?,?)',
                            (digest(dumps(list(args))),rid,project))
        return added

    def clear_pending(self):
        """Drop the consumed index journal; replay before this point is idempotent."""
        self.db.execute('DELETE FROM pending_index')
        self.db.commit()

    def reclassify(self,config):
        # Mapping changes also affect log entries skipped by byte cursors.
        for row in self.db.execute('SELECT id,seg,off,len,project FROM revisions'):
            e=unpack(read_blob(self.root,row['seg'],row['off'],row['len']))
            p=project_for(e['cwd'],e['adapter'],e['session_id'],config)
            if p!=row['project']:
                self.db.execute('UPDATE revisions SET project=? WHERE id=?',(p,row['id']))
        # Project labels changed; every head must be reprojected into the index.
        for row in self.db.execute('''SELECT h.adapter,h.session,h.source_key,h.revision_id,r.project
                                      FROM heads h JOIN revisions r ON r.id=h.revision_id'''):
            self.db.execute('INSERT OR REPLACE INTO pending_index VALUES (?,?,?)',
                            (digest(dumps([row['adapter'],row['session'],row['source_key']])),
                             row['revision_id'],row['project']))

    def rows(self,project=None,all_revisions=False):
        q='SELECT r.* FROM revisions r'
        if not all_revisions:q+=' JOIN heads h ON r.id=h.revision_id'
        args=()
        if project is not None:q+=' WHERE r.project=?';args=(project,)
        for r in self.db.execute(q+' ORDER BY r.id',args):
            d=dict(r); d['envelope']=read_blob(self.root,r['seg'],r['off'],r['len'])
            yield d

    def projects(self):
        return [dict(r) for r in self.db.execute('SELECT project, count(*) records, count(DISTINCT adapter || session) sessions FROM revisions GROUP BY project ORDER BY project')]

    def note(self,project,author,kind,topic,body,evidence,status='proposed'):
        if kind not in ('memory','decision','task','opinion'):raise ValueError('Invalid note kind')
        if author not in ('user','codex','zcode','opencode'):raise ValueError('Invalid note author')
        # A note is immutable. Updating a decision means a new attributed note.
        nid=digest(dumps([project,author,kind,topic,body,evidence,status]))[:24]
        self.db.execute('INSERT OR IGNORE INTO notes VALUES (?,?,?,?,?,?,?,?,?)',
                        (nid,project,author,kind,topic,body,dumps(evidence),status,time.time()))
        return nid

    def backup(self):
        """State/matters/config snapshots plus segment files, incremental by segment.

        Immutable segments already present in the previous backup are skipped;
        retention keeps the newest few folders. The archive payload itself can be
        rebuilt only from sources, so what must never be lost is small.
        """
        folder=safe_child(self.root,'backups',f'backup-{time.time_ns()}')
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        def snapshot(db_path,name):
            dest=sqlite3.connect(folder/name)
            try:
                source=sqlite3.connect(db_path)
                try:source.backup(dest)
                finally:source.close()
            finally:dest.close()
            os.chmod(folder/name,0o600)
        state=folder/'state.sqlite'
        dest=sqlite3.connect(state)
        try:self.db.backup(dest)
        finally:dest.close()
        os.chmod(state,0o600)
        matters=self.root/'matters.sqlite'
        if matters.is_file():snapshot(matters,'matters.sqlite')
        config=self.root/'config.json'
        if config.is_file():(folder/'config.json').write_bytes(config.read_bytes())
        files=[p.name for p in folder.iterdir()]
        previous=None
        candidates=sorted((p for p in (self.root/'backups').glob('backup-*') if p.is_dir()))
        for p in reversed(candidates[:-1] if len(candidates)>1 else []):
            previous=p;break
        copied=[]
        for seg in sorted((self.root/'log').glob('seg-*.log')):
            target=folder/seg.name
            if previous and (previous/seg.name).exists() and (previous/seg.name).stat().st_size==seg.stat().st_size:
                continue
            copied.append(seg.name)
            with seg.open('rb') as src, target.open('wb') as dst:
                dst.write(src.read())
        atomic(folder/'manifest.json',dumps({'files':files+copied,'previous':str(previous) if previous else None}))
        stale=candidates[:-4] if len(candidates)>4 else []
        for old in stale:
            for p in old.iterdir():
                p.unlink()
            old.rmdir()
        return state

    def compact(self):
        """Rewrite every segment with slimmed payloads, one segment at a time.

        Revision digests and ids never change -- this is storage compaction,
        not history rewrite. Peak extra disk is one segment's worth.
        """
        if self._hot and not self._hot.closed: self._hot.close()
        self._hot=None
        before_bytes=sum(p.stat().st_size for p in self.log_dir.glob('seg-*.log'))
        segments=sorted({r[0] for r in self.db.execute('SELECT DISTINCT seg FROM revisions')})
        next_index=self._highest_segment()+1
        for seg in segments:
            rows=self.db.execute('SELECT id,off,len FROM revisions WHERE seg=? ORDER BY off',(seg,)).fetchall()
            out_path=segment_path(self.root,next_index)
            out=open(out_path,'ab',buffering=0)
            moved=[]
            try:
                for row in rows:
                    blob=read_blob(self.root,seg,row['off'],row['len'])
                    event=unpack(blob)
                    slimmed=slim(event)
                    if slimmed is event:
                        frame_blob=blob
                    else:
                        frame_blob=pack(slimmed)
                    frame=len(frame_blob).to_bytes(4,'big')+frame_blob
                    off=os.fstat(out.fileno()).st_size
                    out.write(frame)
                    moved.append((next_index,off,len(frame),row['id']))
                os.fsync(out.fileno())
            finally:
                out.close()
            with self.db:
                self.db.executemany('UPDATE revisions SET seg=?,off=?,len=? WHERE id=?',moved)
            segment_path(self.root,seg).unlink()
            next_index+=1
        self._hot_index=self._highest_segment()
        after_bytes=sum(p.stat().st_size for p in self.log_dir.glob('seg-*.log'))
        return {'segments':len(segments),'bytes_before':before_bytes,'bytes_after':after_bytes}

    def _migrate_v1(self):
        """v1 archive.sqlite -> state store + segment log; the old file is kept."""
        old=self.root/'archive.sqlite'
        src=sqlite3.connect(old); src.row_factory=sqlite3.Row
        dest=sqlite3.connect(self.root/'state.sqlite')
        try:
            dest.executescript(STATE_SCHEMA)
            dest.execute('BEGIN')
            for row in src.execute('SELECT * FROM revisions ORDER BY id'):
                blob=row['envelope']
                seg,off,length=self._append(blob)
                dest.execute('INSERT OR REPLACE INTO revisions(id,adapter,session,source_key,revision,project,seg,off,len,seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (row['id'],row['adapter'],row['session'],row['source_key'],row['revision'],
                              row['project'],seg,off,length,row['seen_at']))
            for table in ('heads','cursors','pending_index','notes','runs'):
                try:rows=src.execute(f'SELECT * FROM {table}').fetchall()
                except sqlite3.Error:continue
                for r in rows:
                    dest.execute(f'INSERT OR REPLACE INTO {table} VALUES ({",".join("?"*len(r))})',tuple(r))
            dest.commit()
            self.flush_log()
        finally:
            dest.close(); src.close()
        old.rename(old.parent/(old.name+'.migrated-v1'))
