"""Mirror ledger: which session we exported where, both directions.

Port of opal-bridge pair_map.py. The ledger lives in the context-hub data
directory (not a global cache) so uninstalling the app removes it together
with everything else it owns.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _ledger_path(data_dir: Path) -> Path:
    return Path(data_dir) / 'mirror-map.json'


def _read(data_dir: Path) -> dict:
    path = _ledger_path(data_dir)
    if not path.exists():
        return {'zcode_to_codex': {}, 'codex_to_zcode': {}}
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
        d.setdefault('zcode_to_codex', {})
        d.setdefault('codex_to_zcode', {})
        return d
    except (json.JSONDecodeError, OSError):
        return {'zcode_to_codex': {}, 'codex_to_zcode': {}}


def _write(data_dir: Path, data: dict) -> None:
    from ..store import atomic
    atomic(_ledger_path(data_dir), json.dumps(data, indent=2, ensure_ascii=False))


def record(data_dir: Path, *, zcode_id: str, codex_id: str,
           source_watermark: int | None = None, path=None, parent_id=None,
           source_parts=None, source_fingerprint=None) -> None:
    """Record a mirror pair with the source's revision watermark at export time."""
    from ..store import exclusive
    with exclusive(data_dir, 'mirror-map.lock'):
        d = _read(data_dir)
        d['zcode_to_codex'][zcode_id] = {'codex_id': codex_id,
             'source_watermark': source_watermark, 'path':str(path) if path else None,
             'parent_id':parent_id, 'source_parts': source_parts, 'source_fingerprint': source_fingerprint}
        d['codex_to_zcode'][codex_id] = zcode_id
        d.setdefault('history', {})[codex_id] = {'zcode_id':zcode_id, 'parent_id':parent_id,
                                               'source_watermark':source_watermark}
        _write(data_dir, d)


def get_codex_for_zcode(data_dir: Path, zcode_id: str) -> str | None:
    entry = _read(data_dir)['zcode_to_codex'].get(zcode_id)
    if entry is None:
        return None
    return entry['codex_id'] if isinstance(entry, dict) else entry


def get_entry(data_dir: Path, zcode_id: str) -> dict | None:
    """Full ledger entry; legacy string entries return {'codex_id': str}."""
    entry = _read(data_dir)['zcode_to_codex'].get(zcode_id)
    if entry is None:
        return None
    return entry if isinstance(entry, dict) else {'codex_id': entry}


def get_zcode_for_codex(data_dir: Path, codex_id: str) -> str | None:
    return _read(data_dir)['codex_to_zcode'].get(codex_id)
