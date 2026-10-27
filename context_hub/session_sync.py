"""Opt-in, append-only native session sync (separate from archive sync).

Inspired by opal-bridge's sync/watch/hooks. Only explicitly enrolled pairs
are touched. Native history is never re-rendered or overwritten. The source
adapters remain read-only; this separate writer imports new, attributed rows
into the enrolled ZCode session. No prompt is submitted and no model runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .cli import DEFAULT, setup_console, validate
from .store import atomic, exclusive
from . import live_refresh
from . import runtime_delivery
from .session_ir import manifest
from .session_ir.live import (MARKER, decode_codex, decode_zcode,
                              encode_codex, encode_zcode)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS pairs (
 id TEXT PRIMARY KEY, zcode_id TEXT UNIQUE NOT NULL, codex_id TEXT UNIQUE NOT NULL,
 path TEXT NOT NULL, offset INTEGER NOT NULL, prefix TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL,
 status TEXT NOT NULL DEFAULT 'ready', error TEXT, backup TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parts (
 pair_id TEXT NOT NULL, part_id TEXT NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(pair_id,part_id));
CREATE TABLE IF NOT EXISTS deliveries (
 pair_id TEXT NOT NULL, event TEXT NOT NULL, direction TEXT NOT NULL,
 delivered REAL NOT NULL, PRIMARY KEY(pair_id,event,direction));
CREATE TABLE IF NOT EXISTS projection_pending (
 pair_id TEXT PRIMARY KEY,path TEXT NOT NULL,created REAL NOT NULL);
'''


def _db(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / 'session-sync.sqlite', timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA + live_refresh.SCHEMA + runtime_delivery.SCHEMA)
    fields={r['name'] for r in con.execute('PRAGMA table_info(deliveries)')}
    for name in ('source_at','written_at','projected_at'):
        if name not in fields:con.execute('ALTER TABLE deliveries ADD COLUMN '+name+' REAL')
    fields={r['name'] for r in con.execute('PRAGMA table_info(pairs)')}
    if 'codex_active' not in fields:con.execute('ALTER TABLE pairs ADD COLUMN codex_active INTEGER')
    if 'repair_required' not in fields:con.execute('ALTER TABLE pairs ADD COLUMN repair_required INTEGER NOT NULL DEFAULT 0')
    for name in ('pending_to_codex','last_poll_ms'):
        if name not in fields:con.execute('ALTER TABLE pairs ADD COLUMN '+name+' REAL NOT NULL DEFAULT 0')
    con.commit()
    return con


def _config(root):
    config = json.loads((Path(root) / 'config.json').read_text(encoding='utf-8'))
    validate(root, config)
    if not {'zcode', 'codex'} <= set(config['sources']):
        raise ValueError('Native sync requires explicitly enrolled ZCode and Codex sources')
    return config


def _native(config, write=False):
    path = Path(config['sources']['zcode']).expanduser().resolve() / 'cli/db/db.sqlite'
    if not path.is_file() or path.is_symlink():
        raise ValueError('ZCode source database missing or symlinked')
    con = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                          uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    required = {'session': {'id', 'directory', 'time_updated'},
                'message': {'id', 'session_id', 'data', 'time_created', 'time_updated'},
                'part': {'id', 'session_id', 'message_id', 'data', 'time_created', 'time_updated'}}
    for table, columns in required.items():
        found = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
        if not columns <= found:
            con.close()
            raise ValueError('Unsupported native ZCode schema: ' + table)
    return con


def _rollout(config, path, expected_id):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing symlinked rollout')
    path = path.resolve()
    home = Path(config['sources']['codex']).expanduser().resolve()
    if not any(path.is_relative_to(home / d) for d in ('sessions', 'archived_sessions')):
        raise ValueError('Rollout must belong to the enrolled Codex source')
    if path.is_symlink() or not path.is_file():
        raise ValueError('Rollout missing or symlinked')
    with path.open('rb') as f:
        first = json.loads(f.readline())
    if first.get('type') != 'session_meta' or first.get('payload', {}).get('id') != expected_id:
        raise ValueError('Codex session identity mismatch')
    return path, {**first['payload'],'_sync_ordinal_base':first.get('ordinal',0)}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _part_digest(row):
    return _sha(row['data'].encode('utf-8'))


def enroll(root, zcode_id, codex_id, rollout=None, *, replay_export=True, replace=False):
    """Explicitly pair existing native sessions and back up the selected history."""
    zcode_id=zcode_id.removeprefix('zcode:');codex_id=codex_id.removeprefix('codex:')
    root = Path(root).resolve()
    config = _config(root)
    with exclusive(root, 'session-sync.lock'):
        state = _db(root)
        try:
            old = state.execute('SELECT * FROM pairs WHERE zcode_id=? OR codex_id=?',
                                (zcode_id, codex_id)).fetchone()
            if old:
                if old['zcode_id'] != zcode_id or old['codex_id'] != codex_id:
                    if not (replace and old['zcode_id'] == zcode_id):
                        raise ValueError('Session already enrolled in a different pair')
                    # Validate and back up the replacement before the atomic
                    # switch below. Any failure leaves the old pair untouched.
                else:
                    with state:
                        state.execute('UPDATE pairs SET enabled=1 WHERE id=?',(old['id'],))
                    return dict(state.execute('SELECT * FROM pairs WHERE id=?',(old['id'],)).fetchone())
            home = Path(config['sources']['codex']).expanduser().resolve()
            if rollout is None:
                from .native_source import current_codex_source
                rollout=current_codex_source(config,codex_id)
            path, meta = _rollout(config, rollout, codex_id)
            native = _native(config)
            try:
                native.execute('BEGIN')
                header = native.execute('SELECT * FROM session WHERE id=?', (zcode_id,)).fetchone()
                if not header:
                    raise ValueError('ZCode session not found')
                backup = {'session': dict(header)}
                for table in ('message', 'part'):
                    backup[table] = [dict(r) for r in native.execute(
                        f'SELECT * FROM {table} WHERE session_id=?', (zcode_id,))]
            finally:
                native.close()
            data = path.read_bytes()
            offset = len(data)
            if data and not data.endswith(b'\n'):
                raise ValueError('Codex is writing a partial record; retry enrollment')
            exported = manifest.get(codex_id, root) if replay_export else None
            baseline=backup['part']
            receipt_path=root/'native-imports'/(zcode_id+'.json')
            if replay_export and receipt_path.exists():
                receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
                if (receipt.get('version')!=2 or not receipt.get('complete') or
                    receipt.get('codex_session_id')!=codex_id or receipt.get('zcode_session_id')!=zcode_id):
                    raise ValueError('Native import receipt does not match the selected pair')
                segments=receipt.get('source',{}).get('segments',[])
                segment=next((s for s in segments if Path(s['path']).resolve()==path),None)
                if not segment or segment['bytes']>len(data) or _sha(data[:segment['bytes']])!=segment['sha256']:
                    raise ValueError('Native import baseline changed; revalidate before pairing')
                offset=segment['bytes']
                if offset and data[offset-1:offset]!=b'\n':raise ValueError('Invalid import baseline boundary')
                owner='import_'+codex_id+':'+zcode_id
                baseline=[p for p in backup['part'] if
                    (json.loads(p['data']).get('metadata',{}).get(MARKER) or {}).get('pair')==owner]
            if exported and Path(exported['path']).resolve() == path:
                offset = exported['size']
                if offset > len(data) or (offset and data[offset-1:offset] != b'\n'):
                    raise ValueError('Export watermark no longer matches rollout')
                if not exported.get('sha256') or _sha(data[:offset])!=exported['sha256']:
                    raise ValueError('Export baseline changed or lacks a verified hash; revalidate before pairing')
                from .session_ir import mirror_map
                entry=mirror_map.get_entry(root,zcode_id)
                if entry and entry.get('codex_id')==codex_id:
                    digests=entry.get('source_parts')
                    if digests is None:
                        from .native_source import zcode_rows,zcode_watermark
                        h,ms,ps,zstate=zcode_rows(config,zcode_id,include_state=True)
                        if zcode_watermark(h,ms,ps,zstate)!=entry.get('source_fingerprint'):
                            raise ValueError('Legacy export has no part baseline and source changed; revalidate before pairing')
                    else:
                        current_parts={p['id']:p for p in backup['part']}
                        if any(pid not in current_parts or _part_digest(current_parts[pid])!=digest for pid,digest in digests.items()):
                            raise ValueError('Exported ZCode history was edited or removed; revalidate before pairing')
                        baseline=[current_parts[pid] for pid in digests]
                else:raise ValueError('Export ownership does not match the selected ZCode session')
            pair_id = uuid.uuid5(uuid.NAMESPACE_URL, f'context-hub:{zcode_id}:{codex_id}').hex
            backup_path = root / 'backups/session-sync' / pair_id / 'before.json'
            backup['codex'] = {'id': codex_id, 'path': str(path), 'size': len(data),
                               'sha256': _sha(data)}
            atomic(backup_path, json.dumps(backup, ensure_ascii=False))
            from .sync_tail import activity
            active=activity(path,offset,MARKER)
            with state:
                if old:
                    state.execute('DELETE FROM pairs WHERE id=?',(old['id'],))
                    state.execute('DELETE FROM parts WHERE pair_id=?',(old['id'],))
                state.execute('INSERT INTO pairs(id,zcode_id,codex_id,path,offset,prefix,created,backup,codex_active,enabled) '
                              'VALUES (?,?,?,?,?,?,?,?,?,1)',
                              (pair_id,zcode_id,codex_id,str(path),offset,_sha(data[:offset]),time.time(),str(backup_path),int(active)))
                state.executemany('INSERT INTO parts VALUES (?,?,?)',
                                  [(pair_id, p['id'], _part_digest(p)) for p in baseline])
            return dict(state.execute('SELECT * FROM pairs WHERE id=?', (pair_id,)).fetchone())
        finally:
            state.close()


def _identify(pair, prefix, event):
    return prefix + '_chsync_' + uuid.uuid5(uuid.NAMESPACE_URL, pair + ':' + event).hex


def _put_row(con, table, row, pair):
    existing = con.execute(f'SELECT data FROM {table} WHERE id=?', (row['id'],)).fetchone()
    if existing:
        marker = json.loads(existing[0]).get('metadata', {}).get(MARKER, {})
        if marker.get('pair') != pair:
            raise ValueError('Native message identity collision; refusing replacement')
        return False
    columns = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
    if 'sequence' in columns:
        row['sequence'] = con.execute(f'SELECT coalesce(max(sequence),-1)+1 FROM {table} '
                                      'WHERE session_id=?', (row['session_id'],)).fetchone()[0]
    fields = list(row)
    con.execute(f'INSERT INTO {table}({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})',
                [row[k] for k in fields])
    return True


def _to_zcode(con, pair, rows, cwd, root, touched=None):
    from .native_import import _ms
    from .native_payloads import bound_part
    message_columns = {r['name'] for r in con.execute('PRAGMA table_info(message)')}
    latest = 'sequence IS NULL,sequence DESC,time_created DESC,rowid DESC' if 'sequence' in message_columns else 'time_created DESC,rowid DESC'
    parent_row = con.execute("SELECT id FROM message WHERE session_id=? AND json_extract(data,'$.role')='user' "
                             'ORDER BY '+latest+' LIMIT 1', (pair['zcode_id'],)).fetchone()
    parent = parent_row[0] if parent_row else None
    count = 0
    for offset, end, row in rows:
        moments = decode_codex(row)
        if not moments:
            continue
        event = f'codex:{offset}'
        stamp = _ms(row.get('timestamp'))
        messages, parts, results, parent = encode_zcode(
            moments, pair=pair['id'], event=event, sid=pair['zcode_id'], cwd=cwd,
            parent=parent, timestamp=stamp,
            identify=lambda prefix,key: _identify(pair['id'],prefix,key))
        for message in messages:
            _put_row(con, 'message', message, pair['id'])
            if touched is not None:touched.add(message['id'])
        for part in parts:
            part['data']=json.dumps(bound_part(root,json.loads(part['data'])),ensure_ascii=False)
            count += _put_row(con, 'part', part, pair['id'])
        for call_id, output, error, timestamp in results:
            matches = con.execute("SELECT id,data,message_id FROM part WHERE session_id=? AND "
                "(json_extract(data,'$.callID')=? OR substr(json_extract(data,'$.callID'),1,?)=?) "
                "AND json_extract(data,'$.metadata.context_hub_sync.pair')=?",
                (pair['zcode_id'],call_id,len(call_id)+2,call_id+'__',pair['id'])).fetchall()
            if not matches:
                # Keep orphan results visibly rather than silently losing evidence.
                from .session_ir.schema import Attachment
                m,p,_,parent = encode_zcode([Attachment(subtype='orphan_tool_result',data={'call_id':call_id,'output':output,'is_error':error})],
                    pair=pair['id'],event=event+':orphan',sid=pair['zcode_id'],cwd=cwd,
                    parent=parent,timestamp=stamp,identify=lambda prefix,key: _identify(pair['id'],prefix,key))
                for r in m:
                    _put_row(con,'message',r,pair['id'])
                    if touched is not None:touched.add(r['id'])
                for r in p:
                    r['data']=json.dumps(bound_part(root,json.loads(r['data'])),ensure_ascii=False)
                    count += _put_row(con,'part',r,pair['id'])
            for match in matches:
                if touched is not None:touched.add(match['message_id'])
                data = json.loads(match['data'])
                state = data['state']
                state.update(status='error' if error else 'completed',
                             time={'start':data.get('metadata',{}).get(MARKER,{}).get('source_started_ms',timestamp), 'end': timestamp},
                             title=state.get('title') or data.get('tool','Historical tool'),
                             metadata=state.get('metadata') or {})
                state['metadata']['native_result']=row.get('payload',{})
                state['error' if error else 'output'] = output
                con.execute('UPDATE part SET data=?,time_updated=? WHERE id=?',
                            (json.dumps(bound_part(root,data),ensure_ascii=False),timestamp,match['id']))
                count += 1
    if count:
        con.execute('UPDATE session SET time_updated=? WHERE id=?',
                    (int(time.time()*1000),pair['zcode_id']))
    return count


def _complete_rows(data, offset):
    """Only consume finished turns/own echoes; retain a partial tail for retry."""
    rows, position, boundary, active = [], offset, 0, False
    for raw in data[offset:].splitlines(keepends=True):
        if not raw.endswith(b'\n'):
            break
        end = position + len(raw)
        try:
            row = json.loads(raw)
        except (ValueError,UnicodeError) as e:
            raise ValueError(f'Malformed Codex record at byte {position}') from e
        rows.append((position,end,row))
        typ = row.get('payload',{}).get('type')
        if row.get('type') == 'response_item' and not row.get(MARKER):
            active = True
        if row.get('type') == 'event_msg' and typ in ('task_started','turn_started'):
            active = True
        if (row.get('type') == 'event_msg' and typ in ('task_complete','turn_complete','turn_aborted')) or (
            row.get('type') == 'response_item' and row.get('payload',{}).get('phase') in ('final','final_answer')):
            active = False
            boundary = len(rows)
        elif row.get(MARKER) and not active:
            boundary = len(rows)
        elif boundary and not active and row.get('type') != 'response_item':
            boundary = len(rows)
        position = end
    return rows[:boundary]


def _append(path, before, lines):
    from .sync_tail import Tail,token
    if isinstance(before,Tail):
        if token(path.stat()) != before.seal or (before.data and not before.data.endswith(b'\n')):
            raise ValueError('Codex changed while delivering; retry after the turn finishes')
        ordinal = before.ordinal+before.data.count(b'\n')
        with path.open('rb') as f:meta=json.loads(f.readline())
        paginated=meta.get('payload',{}).get('history_mode')=='paginated'
    else:
        current = path.read_bytes()
        if current != before or not current.endswith(b'\n'):
            raise ValueError('Codex changed while delivering; retry after the turn finishes')
        records = [json.loads(raw) for raw in current.splitlines()]
        paginated=records[0].get('payload',{}).get('history_mode') == 'paginated'
        if paginated and any(r.get('ordinal') != i for i,r in enumerate(records)):
            raise ValueError('Unsupported or inconsistent paginated rollout ordinals')
        ordinal=len(records)
    if isinstance(before,Tail) and token(path.stat()) != before.seal:
        raise ValueError('Codex changed while delivering; retry after the turn finishes')
    if paginated:
        for index,row in enumerate(lines,start=ordinal):row['ordinal'] = index
    payload = ''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in lines).encode('utf-8')
    fd = os.open(path,os.O_WRONLY|os.O_APPEND)
    try:
        if isinstance(before,Tail) and token(os.fstat(fd))!=before.seal:
            raise ValueError('Codex changed before append; retry after the turn finishes')
        written = os.write(fd,payload)
        if written != len(payload):
            raise OSError('Incomplete native append; recovery required')
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_pair(root, state, pair, config):
    path, meta = _rollout(config, pair['path'], pair['codex_id'])
    from .native_source import current_codex_source
    if current_codex_source(config,pair['codex_id'])!=path:
        raise ValueError('Native current rollout changed; pause and revalidate this pair before syncing')
    from . import sync_tail
    offset = pair['offset']
    paginated=meta.get('history_mode')=='paginated'
    runtime_writer=paginated and config.get('native_sync_runtime_writer',True)
    # Source reads use byte identity/hash and complete records. Ordinals are
    # native-writer state, never something an external writer may reserve.
    tail=sync_tail.read(path,offset,pair['prefix'],paginated=paginated and not runtime_writer,ordinal_base=meta['_sync_ordinal_base'])
    prior_active=pair['codex_active']
    if prior_active is None:prior_active=sync_tail.activity(path,offset,MARKER)
    codex_rows,active=sync_tail.ready(tail,bool(prior_active),MARKER,paginated=paginated and not runtime_writer)
    ordinal_collision=runtime_writer and (bool(pair['repair_required']) or any(row.get('ordinal')!=tail.ordinal+i for i,(_,_,row) in enumerate(codex_rows)))
    new_offset = codex_rows[-1][1] if codex_rows else offset
    target_busy=active or new_offset!=tail.seal[2]
    native_paginated=paginated and meta.get('originator')!='context-hub'
    native_v2=native_paginated and meta.get('multi_agent_version')=='v2'
    projection_blocked=False
    if native_paginated and not target_busy and not runtime_writer:
        from .session_ir import native_history
        home=Path(config['sources']['codex']).expanduser().resolve()
        projection_blocked=not native_history.ready_for_append(home,pair['codex_id'],tail)
        if projection_blocked and not state.execute('SELECT 1 FROM projection_pending WHERE pair_id=?',(pair['id'],)).fetchone():
            from .session_ir.native_reconcile import reconcile
            if reconcile(home,pair['codex_id'],path,tail):
                projection_blocked=not native_history.ready_for_append(home,pair['codex_id'],tail)
    writable=any(decode_codex(row) for start,end,row in codex_rows)
    native = _native(config, write=writable)
    try:
        native.execute('BEGIN IMMEDIATE' if writable else 'BEGIN')
        header = native.execute('SELECT * FROM session WHERE id=?', (pair['zcode_id'],)).fetchone()
        if not header or ('time_deleted' in header.keys() and header['time_deleted']):
            raise ValueError('Paired ZCode session no longer exists')
        if 'revert' in header.keys() and header['revert'] not in (None,'','null','{}'):
            raise ValueError('Paired ZCode session was rewound; pause and revalidate before syncing')
        touched=set()
        imported = _to_zcode(native,pair,codex_rows,header['directory'],root,touched)
        message_columns = {r['name'] for r in native.execute('PRAGMA table_info(message)')}
        part_columns = {r['name'] for r in native.execute('PRAGMA table_info(part)')}
        order = ('m.sequence IS NULL,m.sequence,' if 'sequence' in message_columns else '') + 'm.time_created,m.rowid,'
        order += ('p.sequence IS NULL,p.sequence,' if 'sequence' in part_columns else '') + 'p.time_created,p.rowid'
        # Filter acknowledged rows in SQLite, before reading/decoding large
        # historical payloads. The journal's composite primary key is indexed.
        native.execute('ATTACH DATABASE ? AS sync_journal',((Path(root)/'session-sync.sqlite').as_uri()+'?mode=ro',))
        parts = native.execute('SELECT p.*,m.data AS message_data FROM part p LEFT JOIN message m ON m.id=p.message_id '
            'WHERE p.session_id=? AND NOT EXISTS (SELECT 1 FROM sync_journal.parts s '
            'WHERE s.pair_id=? AND s.part_id=p.id) '
            "AND json_extract(p.data,'$.metadata.context_hub_sync') IS NULL ORDER BY "+order,
            (pair['zcode_id'],pair['id'])).fetchall()
        lines, delivered, ignored = [], [], []
        view_upgrade_records=0
        if native_v2 and not target_busy and not projection_blocked and not runtime_writer:
            from .session_ir.native_sync import legacy_views
            upgrades=legacy_views(path,pair['id'],tail)
            lines.extend(upgrades);view_upgrade_records=len(upgrades)
        # Crash recovery: an append may have succeeded before our journal commit.
        echoed = set()
        for start,end,row in codex_rows:
            if row.get(MARKER):
                echoed.add(row[MARKER]['event'])
        for part in parts:
            pid = part['id']
            pdata = json.loads(part['data'])
            if pdata.get('metadata',{}).get(MARKER):
                continue
            if pdata.get('type') == 'tool' and (pdata.get('state') or {}).get('status') not in ('completed','error'):
                continue
            if part['message_data'] is None:
                raise ValueError('ZCode part has no parent message')
            info = json.loads(part['message_data'])
            message={'data':part['message_data']}
            # Wait for an assistant message to finish. Streaming is never replayed.
            if info.get('role') == 'assistant' and not (info.get('time') or {}).get('completed'):
                continue
            event = 'zcode:' + pid
            moments = decode_zcode(message,part)
            if not moments:
                ignored.append((pid,_part_digest(part),event))
                continue
            if (target_busy or projection_blocked) and event not in echoed and not runtime_writer:
                # Already appended echoes may be acknowledged during recovery;
                # new records wait for the writer and its index to settle.
                continue
            if event not in echoed:
                if runtime_writer:
                    ids=runtime_delivery.queue(state,pair,event,moments,path)
                    live_refresh.enqueue(state,pair,'codex',ids,event)
                elif native_v2:
                    from .session_ir.native_sync import encode
                    lines.extend(encode(moments,pair=pair['id'],event=event,cwd=header['directory'],thread_id=pair['codex_id']))
                else:lines.extend(encode_codex(moments,pair=pair['id'],event=event,cwd=header['directory']))
            delivered.append((pid,_part_digest(part),event))
        # Commit the native transaction first. Deterministic imported IDs make a
        # crash before our journal commit safe to retry without duplicate messages.
        native.commit()
    except BaseException:
        native.rollback()
        raise
    finally:
        native.close()
    written_at=time.time()
    if lines:
        with state:
            state.execute('INSERT OR REPLACE INTO projection_pending VALUES (?,?,?)',(pair['id'],str(path),time.time()))
        _append(path,tail,lines)
        written_at=time.time()
    projected_at=None
    if paginated and not runtime_writer:
        from .session_ir import native_history
        home=Path(config['sources']['codex']).expanduser().resolve()
        dirty=state.execute('SELECT 1 FROM projection_pending WHERE pair_id=?',(pair['id'],)).fetchone()
        expected_ordinal=tail.ordinal+tail.data.count(b'\n')+len(lines)
        if native_paginated:
            if dirty and not native_history.publish_synced_tail(home,pair['codex_id'],path,pair['id'],expected_ordinal=expected_ordinal):
                raise ValueError('Native file persisted, but native projection is not ready; retry pending')
            if native_history.covers(home,pair['codex_id'],path,expected_ordinal=expected_ordinal):projected_at=time.time()
        elif dirty or not native_history.covers(home,pair['codex_id'],path):
            if not native_history.publish(home,pair['codex_id'],path,rebuild=True,incremental=True):
                raise ValueError('Native file persisted, but desktop projection is unavailable; retry pending')
            projected_at=time.time()
        else:projected_at=time.time()
    now = time.time()
    with state:
        live_refresh.enqueue(state,pair,'zcode',touched,new_offset)
        refresh_items=[r['payload']['item']['id'] for r in [*lines,*(row for _,_,row in codex_rows if row.get(MARKER,{}).get('pair')==pair['id'])]
            if r.get('type')=='event_msg' and r.get('payload',{}).get('type')=='item_completed']
        if paginated and not native_v2:
            from .session_ir.native_history import _id
            refresh_items.extend(_id(pair['codex_id'],r['ordinal'],'synced-item' if native_paginated else 'item')
                for r in [*lines,*(row for _,_,row in codex_rows if row.get(MARKER,{}).get('pair')==pair['id'])]
                if r.get('type')=='event_msg' and r.get('payload',{}).get('type') in ('user_message','agent_message'))
        live_refresh.enqueue(state,pair,'codex',refresh_items,tail.seal[2]+len(lines))
        pending=len(parts)-len(delivered)-len(ignored)
        phase=('needs_offline_collision_repair' if ordinal_collision else
               'waiting_codex_runtime' if runtime_writer and state.execute("SELECT 1 FROM runtime_messages WHERE pair_id=? AND status!='injected'",(pair['id'],)).fetchone() else
               'runtime_delivery_ready' if runtime_writer else
               'waiting_codex_idle' if target_busy and pending else
               'waiting_codex_projection' if projection_blocked and pending else
               'waiting_zcode_completion' if pending else
               'projection_ready' if projected_at else 'native_persisted')
        state.execute('UPDATE pairs SET offset=?,prefix=?,codex_active=?,updated=?,status=?,pending_to_codex=?,error=NULL WHERE id=?',
                      (new_offset,tail.digest_at(new_offset),int(active),now,phase,pending,pair['id']))
        if ordinal_collision:state.execute('UPDATE pairs SET repair_required=1 WHERE id=?',(pair['id'],))
        state.execute('DELETE FROM projection_pending WHERE pair_id=?',(pair['id'],))
        state.executemany('INSERT OR IGNORE INTO parts VALUES (?,?,?)',
                          [(pair['id'],pid,digest) for pid,digest,event in delivered+ignored])
        for pid,digest,event in delivered:
            source_at=next((p['time_created']/1000 for p in parts if p['id']==pid),None)
            state.execute('INSERT OR IGNORE INTO deliveries(pair_id,event,direction,delivered,source_at,written_at,projected_at) VALUES (?,?,?,?,?,?,?)',
                          (pair['id'],event,'zcode-to-codex',now,source_at,None if runtime_writer else written_at,projected_at))
        for start,end,row in codex_rows:
            if decode_codex(row):
                from .native_import import _ms
                state.execute('INSERT OR IGNORE INTO deliveries(pair_id,event,direction,delivered,source_at,written_at) VALUES (?,?,?,?,?,?)',
                              (pair['id'],'codex:'+str(start),'codex-to-zcode',now,_ms(row.get('timestamp'))/1000,written_at))
    sync_tail.advance(path,tail,new_offset,codex_rows)
    return {'pair':pair['id'],'to_zcode_parts':imported,'to_codex_parts':len(delivered),
            'phase':phase,'pending_to_codex_parts':pending,'filtered_parts':len(ignored),
            'codex_active':active,'codex_partial_record':new_offset!=tail.seal[2],
            'codex_projection_blocked':projection_blocked,
            'native_format':'native_inject_items' if runtime_writer else 'v2_completed_items' if native_v2 else 'legacy',
            'native_view_upgrade_records':view_upgrade_records,
            'delivery_mode':'native_runtime_writer' if runtime_writer else 'offline_append_compatibility',
            'desktop_visible':'UNVERIFIED','model_read':'UNVERIFIED'}


def import_codex_session(root, codex_id, rollout=None, *, replace=False,
                         title_prefix='[来自 Codex] ',dry_run=False,new_copy=False):
    """Delegate to session_import: Codex thread -> native ZCode session."""
    from .session_import import import_codex_session as _impl
    return _impl(root, codex_id, rollout, replace=replace, title_prefix=title_prefix,dry_run=dry_run,new_copy=new_copy)


def sync_once(root=DEFAULT):
    root = Path(root).resolve()
    config = _config(root)
    with exclusive(root,'session-sync.lock'):
        state = _db(root)
        try:
            results = []
            for pair in state.execute('SELECT * FROM pairs WHERE enabled=1').fetchall():
                started=time.monotonic()
                try:
                    result=_sync_pair(root,state,pair,config)
                    result['poll_ms']=round((time.monotonic()-started)*1000,3)
                    with state:state.execute('UPDATE pairs SET last_poll_ms=? WHERE id=?',(result['poll_ms'],pair['id']))
                    results.append(result)
                except (ValueError,OSError,sqlite3.Error) as e:
                    with state:
                        state.execute('UPDATE pairs SET status=?,error=?,updated=? WHERE id=?',
                                      ('error',str(e)[:1000],time.time(),pair['id']))
                    results.append({'pair':pair['id'],'error':str(e)[:1000]})
            refresh=live_refresh.dispatch(root,state,config)
            for result in results:
                result['runtime_refresh']=[r for r in refresh if r['pair']==result['pair']]
            return results
        finally:
            state.close()


def status(root=DEFAULT):
    root = Path(root)
    if not (root/'session-sync.sqlite').exists():
        return {'status':'not_configured','pairs':[]}
    state = sqlite3.connect((root/'session-sync.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
    state.row_factory=sqlite3.Row
    try:
        pairs = [dict(r) for r in state.execute('SELECT * FROM pairs')]
        worker_path = root/'session-sync-worker.json'
        worker = json.loads(worker_path.read_text(encoding='utf-8')) if worker_path.exists() else {}
        columns={r['name'] for r in state.execute('PRAGMA table_info(deliveries)')}
        times=','.join(k if k in columns else 'NULL AS '+k for k in ('source_at','written_at','projected_at'))
        return {'pairs':pairs,'worker':worker,
                'worker_live':bool(worker.get('status') in ('running','degraded') and time.time()-worker.get('heartbeat',0)<max(10,3*worker.get('effective_interval',2))),
                'delivery_counts':[dict(r) for r in state.execute(
                    'SELECT pair_id,direction,count(*) AS count FROM deliveries GROUP BY pair_id,direction')],
                'recent_deliveries':[dict(r) for r in state.execute(
                    'SELECT pair_id,direction,event,'+times+',delivered '
                    'FROM deliveries ORDER BY delivered DESC LIMIT 20')],
                'runtime_refresh':live_refresh.summary(state),
                'runtime_context':'refresh relay requires desktop launcher; desktop visibility and next-model read are UNVERIFIED'}
    finally:
        state.close()


def ensure_watch(root=DEFAULT):
    """Start the detached watcher if enrolled pairs lack a fresh heartbeat.

    Called by the existing scheduled archiver as a keep-alive, including its
    unchanged-source path. The lifetime lock prevents duplicate workers.
    """
    current = status(root)
    if not any(p['enabled'] for p in current['pairs']):
        return {'status':'not_configured'}
    if current.get('worker_live'):
        return {'status':'already_running'}
    options = {'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
    python = Path(sys.executable)
    if sys.platform == 'win32':
        windowless = python.with_name('pythonw.exe')
        if windowless.is_file(): python = windowless
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    else:
        options['start_new_session'] = True
    child = subprocess.Popen([str(python),'-m','context_hub.session_sync','--data-dir',str(Path(root).resolve()),
                              'watch','--interval','2'],cwd=str(Path(__file__).resolve().parent.parent),**options)
    return {'status':'starting','pid':child.pid}


def watch(root=DEFAULT, interval=2, *, iterations=None):
    if not 1 <= interval <= 60:
        raise ValueError('Poll interval must be between 1 and 60 seconds')
    root = Path(root).resolve()
    # A lifetime lock also makes Task Scheduler's keep-alive launch idempotent.
    with exclusive(root,'session-sync-watch.lock'):
        tick = 0;failures=0
        while iterations is None or tick < iterations:
            started = time.monotonic()
            try:
                result = sync_once(root)
                failures=failures+1 if any(r.get('error') for r in result) else 0
                worker = {'status':'running','pid':os.getpid(),'interval_seconds':interval,
                          'heartbeat':time.time(),'results':result}
                if failures:worker['status']='degraded'
                if not result:
                    worker['status'] = 'stopped'
            except (ValueError,OSError,sqlite3.Error,RuntimeError) as e:
                failures+=1
                worker = {'status':'degraded','pid':os.getpid(),'interval_seconds':interval,
                          'heartbeat':time.time(),'error':str(e)[:1000]}
            effective=min(30,interval*2**min(failures,4)) if failures else interval
            worker['effective_interval']=effective
            atomic(root/'session-sync-worker.json',json.dumps(worker,ensure_ascii=False))
            tick += 1
            if worker['status'] == 'stopped':
                break
            if iterations is None or tick < iterations:
                time.sleep(max(0,effective-(time.monotonic()-started)))


def main(argv=None):
    setup_console()
    p = argparse.ArgumentParser(description='Explicit native session pairing and append-only sync')
    p.add_argument('--data-dir',type=Path,default=DEFAULT)
    sub = p.add_subparsers(dest='command',required=True)
    add = sub.add_parser('pair')
    add.add_argument('--zcode',required=True)
    add.add_argument('--codex',required=True)
    add.add_argument('--rollout',type=Path)
    add.add_argument('--from-now',action='store_true')
    add.add_argument('--replace',action='store_true',help='Migrate the enrolled pair for this ZCode session to a new Codex mirror')
    imp = sub.add_parser('import',help='Create a native ZCode session from a Codex rollout')
    imp.add_argument('--codex',required=True)
    imp.add_argument('--rollout',type=Path)
    imp.add_argument('--title-prefix',default='[来自 Codex] ')
    imp.add_argument('--replace',action='store_true')
    imp.add_argument('--dry-run',action='store_true')
    imp.add_argument('--new-copy',action='store_true')
    sub.add_parser('once');sub.add_parser('status')
    pause = sub.add_parser('pause');pause.add_argument('--pair',required=True)
    w = sub.add_parser('watch');w.add_argument('--interval',type=float,default=2)
    w.add_argument('--iterations',type=int)
    args = p.parse_args(argv)
    if args.command == 'pair':
        result = enroll(args.data_dir,args.zcode,args.codex,args.rollout,
                        replay_export=not args.from_now,replace=args.replace)
    elif args.command == 'import':
        result = import_codex_session(args.data_dir,args.codex,args.rollout,
                                      replace=args.replace,title_prefix=args.title_prefix,dry_run=args.dry_run,new_copy=args.new_copy)
    elif args.command == 'once': result = sync_once(args.data_dir)
    elif args.command == 'status': result = status(args.data_dir)
    elif args.command == 'pause':
        with exclusive(args.data_dir,'session-sync.lock'):
            state = _db(args.data_dir)
            try:
                with state:
                    changed = state.execute('UPDATE pairs SET enabled=0 WHERE id=?',(args.pair,)).rowcount
                result = {'paused':bool(changed)}
            finally: state.close()
    else:
        watch(args.data_dir,args.interval,iterations=args.iterations)
        return 0
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
