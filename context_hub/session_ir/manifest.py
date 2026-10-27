"""Explicitly scoped ownership ledger; never permits overwriting a session."""
import hashlib
import json
from pathlib import Path
from ..store import atomic, exclusive, safe_child


def _manifest_path(data_dir=None):
    if data_dir is None:
        from ..cli import DEFAULT
        data_dir = DEFAULT
    return safe_child(Path(data_dir), 'codex-write-manifest.json')


def _read(data_dir=None):
    path = _manifest_path(data_dir)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Invalid mirror ownership ledger')
    return data


def record_write(rollout_path, codex_id, data_dir=None):
    path = Path(rollout_path)
    root = _manifest_path(data_dir).parent
    with exclusive(root, 'mirror-ledger.lock'):
        data = _read(root)
        data[codex_id] = {'path': str(path.resolve()), 'size': path.stat().st_size,
                          'mtime': path.stat().st_mtime,
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        atomic(_manifest_path(root), json.dumps(data, ensure_ascii=False, indent=2))


def get(codex_id, data_dir=None):
    return _read(data_dir).get(codex_id)


def matches(rollout_path, codex_id, data_dir=None):
    path = Path(rollout_path)
    entry = get(codex_id, data_dir)
    if not entry or not path.is_file() or path.is_symlink():
        return False
    return (entry.get('path') == str(path.resolve()) and
            entry.get('sha256') == hashlib.sha256(path.read_bytes()).hexdigest())


def clear(data_dir=None):
    root = _manifest_path(data_dir).parent
    with exclusive(root, 'mirror-ledger.lock'):
        atomic(_manifest_path(root), '{}')
