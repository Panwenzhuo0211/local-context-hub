"""Versioned, lossless source envelope; interpretation is a separate projection."""
import hashlib
import json
import os
from pathlib import Path

SCHEMA_VERSION = 1

def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()

def project_for(cwd, adapter, session, config):
    override = config.get('session_projects', {}).get(f'{adapter}:{session}')
    if override:
        return override
    path = str(Path(cwd).expanduser().resolve()) if cwd else ''
    matches = []
    for name, roots in config.get('projects', {}).items():
        for root in roots:
            root = str(Path(root).expanduser().resolve())
            if path == root or path.startswith(root + os.sep) or path.startswith(root + '/'):
                matches.append((len(root), name))
    if matches:
        return max(matches)[1]
    # Generic workspaces must be separated by session until explicitly mapped.
    if not path or path in {str(Path(r).expanduser().resolve()) for r in config.get('generic_roots', [])}:
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
