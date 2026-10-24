"""Versioned, lossless source envelope; interpretation is a separate projection."""
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

SCHEMA_VERSION = 1

@lru_cache(maxsize=4096)
def _resolved(cwd):
    # resolve() touches the filesystem per path component; on Windows that
    # dominates per-record cost, and cwds repeat heavily within a session.
    return str(Path(cwd).expanduser().resolve()) if cwd else ''

@lru_cache(maxsize=16)
def _generic_set(roots):
    return frozenset(str(Path(r).expanduser().resolve()) for r in roots)

def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()

def under_root(path, root):
    """True when path equals or lives under root, on either separator style."""
    return path == root or path.startswith(root + '/') or path.startswith(root + os.sep)

def project_for(cwd, adapter, session, config):
    override = config.get('session_projects', {}).get(f'{adapter}:{session}')
    if override:
        return override
    path = _resolved(cwd)
    matches = []
    for name, roots in config.get('projects', {}).items():
        for root in roots:
            root = _resolved(root)
            if under_root(path, root):
                matches.append((len(root), name))
    if matches:
        return max(matches)[1]
    # Generic workspaces must be separated by session until explicitly mapped.
    if not path or path in _generic_set(tuple(config.get('generic_roots', []))):
        return f'unassigned-{adapter}-{digest(session)[:12]}'
    return f'{Path(path).name or "root"}-{digest(path)[:12]}'

def envelope(adapter, session, key, raw, cwd='', role=None, text='', timestamp=None, kind='event'):
    return dict(schema_version=SCHEMA_VERSION, adapter=adapter, session_id=session,
                source_key=key, cwd=cwd, role=role, text=text, timestamp=timestamp,
                kind=kind, raw=raw)

def text_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(x.get('text', '') for x in content if isinstance(x, dict) and isinstance(x.get('text'), str))


def pack(value):
    import zlib
    return zlib.compress(dumps(value).encode(), 6)

def unpack(value):
    import zlib
    return json.loads(zlib.decompress(value) if isinstance(value, bytes) else value)

def use_utf8_stdio():
    """MCP/CLI JSON is UTF-8 regardless of the platform console codec (cp1252 etc.)."""
    import sys
    for stream in (sys.stdout, sys.stdin):
        try: stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError, OSError): pass

RAW_LIMIT = 256*1024
RAW_PREFIX = 8192

def slim(event, limit=RAW_LIMIT, prefix=RAW_PREFIX):
    """Storage-level payload reduction that never touches text/role/metadata.

    Identity (the revision digest) is computed over the ORIGINAL event, so
    slimming changes storage cost, not deduplication or history. Two cuts:
    duplicate raw_line copies next to their parsed record, and forensic raw
    blobs above the limit become a SHA-256 plus a short prefix.
    """
    raw = event.get('raw')
    if not isinstance(raw, dict):
        return event
    working = dict(event)
    if 'raw_line' in raw and 'record' in raw:
        raw = {k: v for k, v in raw.items() if k != 'raw_line'}
        raw['_raw_line_bytes'] = len(event['raw']['raw_line'])
        working['raw'] = raw
    serialized = dumps(raw)
    if len(serialized) > limit:
        working['raw'] = {'_raw_truncated': True, '_raw_sha256': digest(serialized),
                          '_raw_prefix': serialized[:prefix], '_raw_bytes': len(serialized)}
    return working if working != event else event
