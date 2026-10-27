"""Rebuild a Session IR from the archive's ZCode heads.

The archive already stores every message/part/tool/todo row verbatim in
envelopes; this module only re-forms them into the session IR shape so
emit_codex can write a native Codex session. Nothing is summarized or
dropped here: unknown part types ride along as metadata moments.

Adapted from opal-bridge's translator pipeline concept; the ZCode side is
new (opal only covered Claude Code <-> Codex).
"""
from __future__ import annotations

import json
import logging
import zlib
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

from ..store import read_blob
from ..model import digest
from .ids import deterministic_uuid7, fmt_iso_z
from .schema import (Session, UserText, AssistantText, Thinking, ToolCall,
                     ToolResult, PlanUpdate, Metadata, Attachment)

logger = logging.getLogger(__name__)


def _ms_to_iso(ms) -> str:
    if ms is None:
        return fmt_iso_z(datetime.now(timezone.utc))
    # timedelta keeps millisecond precision; float seconds lose the last ms.
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(ms))
    return fmt_iso_z(dt)


def _tool_to_canonical(name: str) -> str:
    from .tools_map import CC_TO_CANONICAL
    if not name:
        return 'shell'
    if name.startswith('mcp__'):
        return 'mcp_call'
    return CC_TO_CANONICAL.get(name, name)


def _translate_args(canonical_name: str, zc_input: dict) -> dict:
    from .tools_map import translate_args
    return translate_args(canonical_name, zc_input or {})


def restore_from_archive(store, session_id: str) -> Session:
    """Build a Session IR from ZCode heads currently in the archive."""
    heads = store.db.execute(
        '''SELECT h.source_key, r.seg, r.off, r.len FROM heads h
           JOIN revisions r ON r.id=h.revision_id
           WHERE h.adapter='zcode' AND h.session=?''', (session_id,)).fetchall()
    if not heads:
        raise ValueError('Session not found in archive heads: ' + session_id)

    envelopes = [json.loads(zlib.decompress(read_blob(store.root, r['seg'], r['off'], r['len'])))
                 for r in heads]
    # A selected-session source snapshot restores parts whose forensic raw was
    # capped in older archives. Never scan unrelated sessions or mutate the source.
    config_path = store.root / 'config.json'
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8'))
        source_root = config.get('sources', {}).get('zcode')
        source_db = Path(source_root) / 'cli/db/db.sqlite' if source_root else None
        if source_db and source_db.is_file():
            from ..adapters import readonly
            with readonly(source_db) as con:
                header = con.execute('SELECT * FROM session WHERE id=?', (session_id,)).fetchone()
                if header:
                    live = [{'source_key': 'session:' + session_id, 'raw': dict(header)}]
                    for table, prefix in (('message', 'message:'), ('part', 'part:')):
                        for row in con.execute(f'SELECT * FROM {table} WHERE session_id=?', (session_id,)):
                            live.append({'source_key': prefix + row['id'], 'raw': dict(row)})
                    try:
                        for row in con.execute('SELECT * FROM todo WHERE session_id=?', (session_id,)):
                            live.append({'source_key': 'todo:' + str(row['position']), 'raw': dict(row)})
                    except sqlite3.OperationalError:
                        pass
                    envelopes = live
    session_row = None
    roles = {}
    parts = []
    todos = []
    unavailable = []
    for envelope in envelopes:
        raw = envelope.get('raw', {})
        # Dispatch by source_key prefix: the envelope kind of a part row is its
        # content type ('text'/'tool'/...), not a stable structural tag.
        key = envelope.get('source_key') or ''
        if key.startswith('session:'):
            session_row = raw
        elif key.startswith('message:'):
            try:
                roles[raw['id']] = (json.loads(raw['data']).get('role'),
                                    raw.get('time_created'))
            except (ValueError, KeyError, TypeError):
                pass
        elif key.startswith('part:'):
            try:
                parts.append((raw.get('time_created') or 0, raw.get('id') or '',
                              raw.get('message_id') or '', json.loads(raw['data'])))
            except (ValueError, KeyError, TypeError):
                if envelope.get('text') and not raw.get('_raw_truncated'):
                    mid = 'fallback:' + key
                    roles[mid] = (envelope.get('role'), envelope.get('timestamp'))
                    parts.append((envelope.get('timestamp') or 0, key, mid,
                                  {'type': 'text', 'text': envelope['text']}))
                else:
                    unavailable.append(key)
        elif key.startswith('todo:'):
            todos.append(raw)

    if session_row is None:
        raise ValueError('Session header missing from archive: ' + session_id)

    started_ms = session_row.get('time_created')
    if started_ms is None:
        # Older sessions may lack a creation timestamp. The earliest observed
        # message/part time is the best anchor; with none at all, a digest of
        # the session id keeps the IR identity stable across rebuilds.
        candidates = [p[0] for p in parts if p[0]] +                      [r[1] for r in roles.values() if r and r[1]]
        if candidates:
            started_ms = min(candidates)
        else:
            started_ms = int(digest('context-hub:zcode:anchor:' + session_id)[:12], 16) % (2 ** 40)
    started_iso = _ms_to_iso(started_ms)
    ir_id = deterministic_uuid7('context-hub:zcode:' + session_id, started_iso)

    session = Session(
        id=ir_id,
        source_harness='zcode',
        source_session_id=session_id,
        cwd=session_row.get('directory') or '',
        started_at=started_iso,
        ended_at=_ms_to_iso(session_row.get('time_updated')) or None,
    )
    session.source_metadata['zcode'] = {
        'title': session_row.get('title') or '',
        'directory': session_row.get('directory') or '',
    }

    # A part's moment ordering: by its message time, then its own time.
    def _sort_key(item):
        part_ms, part_id, message_id, _data = item
        role, msg_ms = roles.get(message_id, (None, None))
        return ((msg_ms if msg_ms is not None else part_ms) or 0, part_ms or 0, part_id)

    moments = []
    for part_ms, part_id, message_id, data in sorted(parts, key=_sort_key):
        ptype = data.get('type')
        role = roles.get(message_id, (None, None))[0]
        ts = _ms_to_iso(part_ms)
        if ptype == 'text':
            text = data.get('text') or ''
            if not text:
                continue
            metadata = data.get('metadata') or {}
            runtime = metadata.get('runtimeMessage') or {}
            generated = (data.get('synthetic') is True or
                         metadata.get('visibility') == 'model-only' or
                         runtime.get('source') is not None)
            ref = {'part': part_id, 'message': message_id}
            if generated:
                moments.append(Attachment(ts=ts, subtype='historical_runtime',
                    data={'content': text, 'source': runtime.get('source') or
                          metadata.get('source') or 'synthetic'}, source_ref=ref))
            elif role == 'user':
                moments.append(UserText(ts=ts, text=text,
                                        source_ref={'part': part_id}))
            else:
                moments.append(AssistantText(ts=ts, text=text,
                                             source_ref={'part': part_id}))
        elif ptype == 'reasoning':
            text = data.get('text') or ''
            if text:
                moments.append(Thinking(ts=ts, text=text,
                                        source_ref={'part': part_id}))
        elif ptype == 'tool':
            state = data.get('state') or {}
            canonical = _tool_to_canonical(data.get('tool') or '')
            call_id = data.get('callID') or part_id
            moments.append(ToolCall(
                ts=ts, tool=canonical, call_id=call_id,
                args=_translate_args(canonical, state.get('input')
                                     if isinstance(state.get('input'), dict) else {}),
                wire_native={'zcode_tool': data.get('tool')},
                source_ref={'part': part_id}))
            status = state.get('status')
            if status in ('completed', 'error'):
                t = state.get('time') or {}
                duration = (int(t['end']) - int(t['start'])) \
                    if isinstance(t, dict) and t.get('start') and t.get('end') else None
                moments.append(ToolResult(
                    ts=_ms_to_iso((t or {}).get('end') if isinstance(t, dict) else None) or ts,
                    call_id=call_id,
                    output_text=state.get('output') or state.get('error') or '',
                    is_error=status == 'error',
                    duration_ms=duration,
                    source_ref={'part': part_id}))
        elif ptype in ('step-start', 'step-finish'):
            continue  # transport bookkeeping; token costs live in source_metadata
        else:
            moments.append(Metadata(ts=ts, subtype=ptype or 'unknown',
                                    data={'body': data},
                                    source_ref={'part': part_id}))

    for todo in todos:
        moments.append(PlanUpdate(
            ts=_ms_to_iso(todo.get('time_updated')),
            items=[{'content': todo.get('content'), 'status': todo.get('status'),
                    'priority': todo.get('priority')}],
            source_ref={'todo': str(todo.get('position'))}))

    session.moments = moments
    session.source_metadata['counts'] = {
        'parts': len(parts), 'moments': len(moments), 'todos': len(todos),
        'unavailable_parts': len(unavailable)}
    session.source_metadata['unavailable_parts'] = unavailable
    if unavailable:
        raise ValueError(f'{len(unavailable)} archived parts cannot be restored; refresh the selected session from its source before exporting')
    return session
