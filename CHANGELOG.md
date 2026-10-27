# Changelog

## 0.6.1 — 2026-10-27

- Optional local file request/reply transport for agents with authorized file writing
  but no local execution. Uses scoped tools and fixed authors, bounded JSON,
  persistent request hashes and crash-safe write receipts; no public endpoint.
- File-channel enable/disable, private instructions and heartbeat. Owner shutdown
  remains possible after profile revocation.
- Muse query, attributed writeback and cross-client readback verified on a local Mac;
  this does not imply full automatic Muse chat capture or support on every account.
- Guided onboarding can create a first matter and opt into a file channel. Wheel
  integration uses packaged modules; Linux scheduling stays explicit.
- Extend installation checks to all agent console scripts and enforce UTF-8 stdin.
- Explicitly close upgrade, rollback and mirror-inspection SQLite handles, including
  failure paths, so Windows can release the database files promptly.

## 0.6.0 — 2026-10-27

- Owner-managed profiles: explicit matters, fixed authors, optional writeback,
  expiring credentials, rotation and immediate revocation.
- Reusable scoped MCP stdio and authenticated loopback HTTP entry points. No raw
  transcript adapter is needed just to query matters and contribute notes.
- Shared discussion with reply references, stable pagination, independent viewpoints
  and transactional request IDs. Existing clients remain compatible.

## 0.5.0 — 2026-10-27 (local reliability upgrade)

- Protect every existing mirror from overwrite. Reuse verifies scoped path + SHA256;
  refreshes and changed sources branch to new mirrors with preserved parent history.
- Explicit, locked, atomic ownership ledgers; direct target exports stay target-scoped.
- Transactional v1 migration publishes only completed state and retains the original archive.
- Private release snapshots, staged validation, automatic code rollback and migration quarantine.
- Read-only `doctor`, code/deployment version and freshness/coverage diagnostics; local status entry.
- Automatic source checkpoints and recovery receipts distinguish raw capture from semantic notes.
- Escape untrusted HTML in local reports; repair existing-note FTS initialization.

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
