"""Private local archive. Transactional ingestion, immutable content revisions."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from .model import dumps, digest, project_for, pack, unpack

BUSY = 'Another Context Hub process is writing; retry after it finishes'


def _lock_file(f):
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(f):
    if os.name == 'nt':
        import msvcrt
        try: msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError: pass
    else:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def exclusive(root):
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (root/'hub.lock').is_symlink(): raise ValueError('Refusing symlink lock')
    with (root/'hub.lock').open('a') as f:
        try:
            _lock_file(f)
        except BlockingIOError:
            raise RuntimeError(BUSY)
        except OSError:
            if os.name != 'nt':
                raise
            raise RuntimeError(BUSY)
        try:
            yield
        finally:
            _unlock_file(f)

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

class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        db = self.root/'archive.sqlite'
        if db.is_symlink(): raise ValueError('Refusing symlink archive')
        self.db = sqlite3.connect(db)
        os.chmod(db,0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS revisions (
          id INTEGER PRIMARY KEY, adapter TEXT, session TEXT, source_key TEXT,
          revision TEXT, project TEXT, envelope TEXT, seen_at REAL,
          UNIQUE(adapter,session,source_key,revision));
        CREATE TABLE IF NOT EXISTS heads (
          adapter TEXT, session TEXT, source_key TEXT, revision_id INTEGER,
          PRIMARY KEY(adapter,session,source_key));
        CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS notes (
          id TEXT PRIMARY KEY, project TEXT, author TEXT, kind TEXT, topic TEXT,
          body TEXT, evidence TEXT, status TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started REAL, result TEXT);
        CREATE INDEX IF NOT EXISTS revision_project ON revisions(project);
        CREATE INDEX IF NOT EXISTS head_revision ON heads(revision_id);
        ''')

    def close(self): self.db.close()
    def cursor(self,key):
        row=self.db.execute('SELECT value FROM cursors WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else {}
    def set_cursor(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)',(key,dumps(value)))

    def ingest(self,event,config):
        project=project_for(event['cwd'],event['adapter'],event['session_id'],config)
        revision=digest(dumps(event))
        args=(event['adapter'],event['session_id'],event['source_key'])
        existing=self.db.execute('SELECT id FROM revisions WHERE adapter=? AND session=? AND source_key=? AND revision=?',(*args,revision)).fetchone()
        if existing:
            rid=existing[0];added=0
        else:
            result=self.db.execute('INSERT INTO revisions(adapter,session,source_key,revision,project,envelope,seen_at) VALUES (?,?,?,?,?,?,?)',
                                   (*args,revision,project,pack(event),time.time()))
            rid=result.lastrowid;added=1
        head=self.db.execute('SELECT revision_id FROM heads WHERE adapter=? AND session=? AND source_key=?',args).fetchone()
        if not head or head[0]!=rid:
            self.db.execute('INSERT OR REPLACE INTO heads VALUES (?,?,?,?)',(*args,rid))
        return added

    def reclassify(self,config):
        # Mapping changes also affect log entries skipped by byte cursors.
        for row in self.db.execute('SELECT id,envelope,project FROM revisions'):
            e=unpack(row['envelope']);p=project_for(e['cwd'],e['adapter'],e['session_id'],config)
            if p!=row['project']:
                self.db.execute('UPDATE revisions SET project=? WHERE id=?',(p,row['id']))

    def rows(self,project=None,all_revisions=False):
        q='SELECT r.* FROM revisions r'
        if not all_revisions:q+=' JOIN heads h ON r.id=h.revision_id'
        args=()
        if project is not None:q+=' WHERE r.project=?';args=(project,)
        return self.db.execute(q+' ORDER BY r.id',args)

    def projects(self):
        return [dict(r) for r in self.db.execute('SELECT project, count(*) records, count(DISTINCT adapter || session) sessions FROM revisions GROUP BY project ORDER BY project')]

    def note(self,project,author,kind,topic,body,evidence,status='proposed'):
        if kind not in ('memory','decision','task','opinion'):raise ValueError('Invalid note kind')
        if author not in ('user','codex','zcode'):raise ValueError('Invalid note author')
        # A note is immutable. Updating a decision means a new attributed note.
        nid=digest(dumps([project,author,kind,topic,body,evidence,status]))[:24]
        self.db.execute('INSERT OR IGNORE INTO notes VALUES (?,?,?,?,?,?,?,?,?)',
                        (nid,project,author,kind,topic,body,dumps(evidence),status,time.time()))
        return nid

    def backup(self):
        folder=safe_child(self.root,'backups');folder.mkdir(exist_ok=True,mode=0o700)
        path=folder/f'archive-{time.time_ns()}.sqlite'
        dest=sqlite3.connect(path)
        try:self.db.backup(dest)
        finally:dest.close()
        os.chmod(path,0o600)
        return path
