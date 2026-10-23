# Changelog

## 0.3.0 — 2026-10-24

- Cross-platform core: per-OS default data directory (macOS / Windows / Linux) with
  `CONTEXT_HUB_DATA_DIR` override; portable file locking (`msvcrt` on Windows, `flock`
  elsewhere).
- Integration supports single-client installs (skips missing Codex/ZCode configs),
  works on Linux and Windows (background scheduling left to the system scheduler;
  launchd on macOS), and gains `--dry-run` / `--no-rules` flags.
- Installable package with console scripts: `context-hub`, `context-hub-mcp`,
  `context-hub-integrate`.
- Public documentation (English + 简体中文), MIT license, CI matrix
  (macOS/Linux/Windows × Python 3.11/3.13), counter-example verification suite for the
  log-ingestion mechanism (`docs/VERIFICATION.md`).

## 0.2.0 — 2026-10-17

- Shared matters: many-to-many directory/session/record links, natural-language matter
  resolution, rejection of wrong auto-associations.
- MCP tool surface (12 tools) for Codex and ZCode, plus a scoped ChatGPT bridge over the
  official Secure MCP Tunnel (experimental).
- Attributed notes with evidence IDs and status history; Markdown project-memory sources.
- Background archive service (launchd) with change-fingerprint skipping.

## 0.1.0 — 2026-10-16

- Read-only, versioned archive of Codex and ZCode session data with incremental log
  cursors, prefix-hash rewrite detection, project mapping and deterministic handoff
  views.
