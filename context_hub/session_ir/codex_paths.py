"""Codex sessions/YYYY/MM/DD/ path generation."""
# Adapted from opal-bridge (1va7/opal-bridge, MIT-style personal project by the
# same author). Renamed: canonical->session_ir, ingest/render->restore/emit,
# apply_patch->patch_io, pair_map->mirror_map. Field semantics unchanged.

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .ids import parse_cc_iso


CODEX_HOME = Path.home() / ".codex"


def codex_path(uuid: str, ts_iso: str, codex_home: Path | None = None) -> Path:
    """
    Build the canonical Codex rollout path:
      $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<YYYY>-<MM>-<DD>T<HH>-<MM>-<SS>-<UUID>.jsonl
    """
    home = codex_home or CODEX_HOME
    dt = parse_cc_iso(ts_iso)
    yyyy = f"{dt.year:04d}"
    mm = f"{dt.month:02d}"
    dd = f"{dt.day:02d}"
    fname_ts = dt.strftime("%Y-%m-%dT%H-%M-%S")
    return home / "sessions" / yyyy / mm / dd / f"rollout-{fname_ts}-{uuid}.jsonl"
