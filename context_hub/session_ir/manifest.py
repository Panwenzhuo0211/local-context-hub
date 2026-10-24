"""Track which Codex jsonls agent-bridge wrote, to distinguish them from
files that have been extended or renamed by the user via `codex resume`.

Used by `_list_codex_sessions` to skip agent-bridge-written files that
the user hasn't touched, preventing the 'echo' explosion where every CC
source produced both a codex translation AND a CC mirror of that
codex translation.

Two signals together imply 'user-touched, must translate back to CC':
  - file size differs from what we wrote (user appended turns), OR
  - user has set a thread_name in ~/.codex/session_index.jsonl (user
    renamed the session via codex's /name command)
"""
# Adapted from opal-bridge (1va7/opal-bridge, MIT-style personal project by the
# same author). Renamed: canonical->session_ir, ingest/render->restore/emit,
# apply_patch->patch_io, pair_map->mirror_map. Field semantics unchanged.

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _manifest_path():
    from ..cli import DEFAULT
    return Path(DEFAULT) / "codex-write-manifest.json"


def _read() -> dict:
    path=_manifest_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    try:
        path = _manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("failed to write %s: %s", MANIFEST_PATH, e)


def record_write(rollout_path: Path, codex_id: str) -> None:
    """Record that we just wrote this file. Stores size + mtime."""
    try:
        st = rollout_path.stat()
    except OSError:
        return
    data = _read()
    data[codex_id] = {
        "path": str(rollout_path),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }
    _write(data)


def get(codex_id: str) -> dict | None:
    return _read().get(codex_id)


def clear() -> None:
    path = _manifest_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
