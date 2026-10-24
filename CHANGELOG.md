# Changelog

## 0.4.0 — 2026-10-24

- **Cross-agent session resume** (`session_ir/`): export an archived ZCode session as a
  native Codex session file and continue it with `codex resume` — the model gets the
  full first-person work record (message order, tool calls, todos, compact boundaries)
  instead of a text digest. Deterministic UUIDv7 exports; mirror ledger detects stale
  mirrors; user sessions and source stores are never touched.
- New MCP tools: `handoff_session`, `prepare_resume`; `remember` gains `kind=digest`.
- **Storage v3**: segmented immutable logs (256 MB) with content-addressed payloads,
  tiny state store, incremental backup four-piece set (state + matters + config +
  segments, weekly, keep 4); online archive compacted 1.4 GB → 237 MB.
- Windows: Scheduled Task auto-registration with full rollback, `hub.cmd` entry,
  console UTF-8; first-sync chunked and resumable (steady-state sync 586 s → 35 s).
- Retrieval: single-character/substring fallback; incremental auto-link with
  configurable stopwords; freshness gate marks stale archives; static HTML run report.
- In-session "继续" no longer triggers matter retrieval — only cross-session resume does.

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
