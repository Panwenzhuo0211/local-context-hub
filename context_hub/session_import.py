"""Validated Codex-to-ZCode import; compatibility entry point."""
from .native_import import import_codex_session
from .native_source import codex_sources


def _resolve_segments(config, codex_id):
    return [(s['started_at'], s['path'], s['header']['payload'])
            for s in codex_sources(config, codex_id)]
