"""Read-only adapters. No source connection ever uses a writable URI."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from .model import envelope, text_content, digest

@contextmanager
def readonly(path):
    path = Path(path).expanduser().resolve()
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    con.execute('BEGIN')  # Consistent read transaction, including committed WAL rows.
    try:
        yield con
    finally:
        con.rollback()
        con.close()

def tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def require(con, table, columns):
    found = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    if not set(columns) <= found:
        raise ValueError(f'Unsupported source schema: {table}, missing {set(columns)-found}')

class ZCode:
    name = 'zcode'
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def records(self, store, warnings):
        path = self.root / 'cli/db/db.sqlite'
        if not path.exists():
            warnings.append('ZCode database missing; adapter skipped')
            return
        with readonly(path) as c:
            for table, cols in [('session', ['id','directory']), ('message',['id','session_id','data']),
                                ('part',['id','session_id','message_id','data'])]:
                require(c, table, cols)
            sessions = {r['id']: dict(r) for r in c.execute('SELECT * FROM session')}
            for sid, raw in sessions.items():
                yield envelope(self.name, sid, 'session:'+sid, raw, raw['directory'],
                               kind='session', timestamp=raw.get('time_updated'))
            roles = {}
            for row in c.execute('SELECT * FROM message ORDER BY time_created,id'):
                raw = dict(row); data = json.loads(raw['data']); sid = raw['session_id']
                roles[raw['id']] = data.get('role')
                yield envelope(self.name, sid, 'message:'+raw['id'], raw,
                               sessions.get(sid,{}).get('directory',''), data.get('role'),
                               timestamp=raw.get('time_created'), kind='message_metadata')
            for row in c.execute('SELECT * FROM part ORDER BY time_created,id'):
                raw = dict(row); data = json.loads(raw['data']); sid = raw['session_id']
                yield envelope(self.name, sid, 'part:'+raw['id'], raw,
                               sessions.get(sid,{}).get('directory',''), roles.get(raw['message_id']),
                               data.get('text','') if data.get('type') == 'text' else '',
                               raw.get('time_created'), data.get('type','unknown'))
            for table in ('session_entry','todo','session_target'):
                if table not in tables(c):
                    warnings.append(f'ZCode optional table absent: {table}')
                    continue
                for row in c.execute(f'SELECT * FROM {table}'):
                    raw = dict(row); sid = raw['session_id']
                    key = str(raw.get('id', raw.get('position', sid)))
                    yield envelope(self.name, sid, table+':'+sid+':'+key, raw,
                                   sessions.get(sid,{}).get('directory',''), kind=table,
                                   timestamp=raw.get('time_updated'))
        index = self.root / 'v2/tasks-index.sqlite'
        if index.exists():
            with readonly(index) as c:
                require(c, 'tasks', ['task_id','workspace_path','workspace_key'])
                for row in c.execute('SELECT * FROM tasks'):
                    raw = dict(row); sid = raw['task_id']
                    yield envelope(self.name, sid, 'task:'+raw['workspace_key']+':'+sid, raw,
                                   raw['workspace_path'], kind='task_metadata', timestamp=raw.get('updated_at'))
        else:
            warnings.append('ZCode task index absent')

class Codex:
    name = 'codex'
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def records(self, store, warnings):
        sessions = {}; paths = set()
        state = self.root / 'state_5.sqlite'
        if state.exists():
            with readonly(state) as c:
                require(c, 'threads', ['id','cwd','rollout_path'])
                for row in c.execute('SELECT * FROM threads'):
                    raw = dict(row); sid = raw['id']; sessions[sid] = raw
                    if raw['rollout_path']:
                        paths.add(Path(raw['rollout_path']).expanduser().resolve())
                    yield envelope(self.name, sid, 'thread:'+sid, raw, raw['cwd'],
                                   kind='session', timestamp=raw.get('updated_at_ms'))
        else:
            warnings.append('Codex state_5.sqlite missing; using log metadata')
        for folder in ('sessions', 'archived_sessions'):
            paths.update(p.resolve() for p in (self.root/folder).rglob('*.jsonl'))
        for path in sorted(paths):
            # Do not follow index entries or symlinks outside configured source root.
            if not path.is_relative_to(self.root):
                warnings.append(f'Codex log outside allowed root skipped: {path.name}')
                continue
            if not path.exists():
                warnings.append(f'Codex indexed log missing: {path.name}')
                continue
            cursor_key = 'codex-log:'+str(path)
            cursor = store.cursor(cursor_key)
            st = path.stat()
            signature = [st.st_ino, st.st_size, st.st_mtime_ns]
            if cursor.get('stat_signature') == signature and time.time()-cursor.get('verified_at',0)<86400:
                continue
            with path.open('rb') as f:
                first = f.readline()
                try:
                    meta = json.loads(first).get('payload', {})
                except (ValueError, AttributeError):
                    warnings.append(f'Invalid first log record: {path.name}')
                    continue
                sid = meta.get('id') or meta.get('session_id')
                if not sid:
                    warnings.append(f'No session id: {path.name}')
                    continue
                cwd = sessions.get(sid, {}).get('cwd', meta.get('cwd',''))
                cursor_key = 'codex-log:'+str(path)
                cursor = store.cursor(cursor_key)
                offset = cursor.get('offset', 0)
                f.seek(0)
                # Validate entire consumed prefix; catches truncation AND same-size rewrites.
                hasher = __import__('hashlib').sha256()
                remaining = offset
                while remaining:
                    chunk = f.read(min(1024*1024,remaining))
                    if not chunk: break
                    hasher.update(chunk); remaining -= len(chunk)
                if remaining or (offset and hasher.hexdigest() != cursor.get('prefix_hash')):
                    warnings.append(f'Log rewritten/truncated; rereading: {path.name}')
                    offset = 0
                f.seek(offset)
                if not offset:
                    hasher = __import__('hashlib').sha256()
                while True:
                    start = f.tell(); line = f.readline()
                    if not line: break
                    if not line.endswith(b'\n'):
                        warnings.append(f'Incomplete final log line deferred: {path.name}')
                        break
                    try:
                        data = json.loads(line)
                    except ValueError:
                        # Preserve corrupt bytes, but do not silently advance past them.
                        raise ValueError(f'Malformed JSONL at {path.name}:{start}')
                    payload = data.get('payload',{}); typ = data.get('type','unknown')
                    role = payload.get('role') if typ=='response_item' else None
                    text = text_content(payload.get('content')) if typ=='response_item' and payload.get('type')=='message' else ''
                    # Position, not text hash: repeated identical messages remain distinct.
                    key = 'rollout:'+str(data.get('ordinal', start))
                    yield envelope(self.name, sid, key, {'record':data, 'raw_line':line.decode('utf-8')},
                                   cwd, role, text, data.get('timestamp'), typ)
                    hasher.update(line); offset = f.tell()
                store.set_cursor(cursor_key, {'offset':offset,'prefix_hash':hasher.hexdigest(),'stat_signature':signature,'verified_at':time.time()})
        history = self.root/'thread_history_1.sqlite'
        if history.exists():
            with readonly(history) as c:
                for table in ('thread_items','thread_turns','thread_realtime_items'):
                    if table not in tables(c):
                        warnings.append(f'Codex history table missing: {table}'); continue
                    for row in c.execute(f'SELECT * FROM {table}'):
                        raw = dict(row); sid = raw['thread_id']
                        data = json.loads(raw['item_json']) if 'item_json' in raw else {}
                        typ = data.get('type', table)
                        role = {'userMessage':'user','agentMessage':'assistant'}.get(typ, data.get('role'))
                        text = data.get('text','') or text_content(data.get('content'))
                        key = table+':'+str(raw.get('turn_id',''))+':'+str(raw.get('item_id',''))
                        yield envelope(self.name, sid, key, raw, sessions.get(sid,{}).get('cwd',''),
                                       role, text, raw.get('created_at_ms',raw.get('started_at')), typ)
        else:
            warnings.append('Codex paginated history absent; coverage limited to logs')

ADAPTERS = {'codex': Codex, 'zcode': ZCode}
