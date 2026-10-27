# Context Hub

[![CI](https://github.com/Panwenzhuo0211/local-context-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/Panwenzhuo0211/local-context-hub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Local conversation archives and shared matters, with attributed multi-agent discussion and scoped retrieval.**

English · [简体中文](README.zh-CN.md)

If you work across multiple terminal AI agents (OpenAI Codex CLI, ZCode), each one keeps
its own private session history and knows nothing about the others. Handing context over
means copy-pasting summaries or re-explaining decisions. Context Hub fixes that locally:

1. **Read-only adapters** archive both agents' session databases and JSONL logs into one
   versioned local store (SQLite indexes and segmented logs) — sources are never modified.
2. **Matters** replace folder-based projects: one matter can span multiple directories,
   multiple sessions and multiple agents; sessions can belong to several matters.
3. **An MCP server** exposes bounded tools so any connected agent can recover a matter by
   name, read paginated evidence, and write back attributed notes — disagreements between
   agents are preserved side by side, never auto-merged.

Archiving and indexing run locally, with no model calls or telemetry. The default
MCP transport is stdio. An optional authenticated gateway binds only to loopback;
remote access requires an explicitly configured HTTPS proxy.

### Muse and future agents (0.6)

New agents can join shared matters through owner-managed profiles without a new
archive adapter. Each gets a fixed author, explicit matter grants, revocable
credentials and optional writeback. Discussions retain peer replies and differing
opinions; durable request IDs make retries safe. See [Agent enrollment](docs/AGENT-ENROLLMENT.md).
Muse's actual connection must be tested on your account; enrolling a profile does
not mean Muse is connected, and does not automatically archive all Muse chats.

| Agent capability | Connection |
|---|---|
| Local MCP or authorized local execution | Generated MCP config or `context-hub-agent` |
| Authorized local file writing, without execution | `context-hub-file-bridge`; query/reply JSON files |
| Cloud-only tools | Explicitly configured HTTPS gateway and supported authentication |

Enroll once with `context-hub agents add <id> --matter <name>`; add `--write` for
attributed contributions. Source checkouts also provide `python agent-setup.py`
and the macOS `接入新Agent.command` wizard, which can create the first matter.
File-channel clients need the broker running. See the [complete setup](docs/AGENT-ENROLLMENT.md).

## How it works

```
Codex CLI (SQLite + JSONL) ──┐
                             ├─► read-only adapters ──► versioned archive (SQLite)
ZCode (SQLite) ──────────────┘         (immutable revisions, log cursors with
                                        prefix-hash rewrite detection)
                                              │
                                              ▼
                              matters + local full-text index (FTS5, CJK bigrams)
                                              │
                                              ▼
                                   MCP stdio server ◄── your agents
                                              │
                                              ▼
                            attributed notes (author, evidence IDs, status history)
```

Key properties:

- **Versioned.** Every captured content change becomes a new immutable revision;
  deleted source records stay in the archive.
- **Rewrite-tolerant ingestion.** Log files are tracked with inode/size/mtime fingerprints
  and a SHA-256 prefix hash: truncation, rotation and rewrites are detected and re-read;
  incomplete trailing lines are deferred, never half-ingested. A same-size, same-mtime
  rewrite is caught by the daily re-verification pass.
- **Event identity ≠ content identity.** Repeated identical messages stay distinct events
  (position/ordinal-based keys); content hashes decide revisions.
- **Transactional.** Records and cursors commit together; a failed sync rolls back cleanly.
- **Untrusted by design.** Archived conversation content is treated as data, not
  instructions; tool outputs are bounded and paginated.

## Install

```sh
pip install git+https://github.com/Panwenzhuo0211/local-context-hub.git
# or: pipx install git+https://github.com/Panwenzhuo0211/local-context-hub.git
```

Python 3.11+ on macOS, Linux or Windows. No third-party dependencies.

Or run from source: `./hub <command>`.

## Quick start

```sh
context-hub init          # create the data directory and default config
context-hub sync          # archive both agents' current sessions (read-only)
context-hub projects      # list archived projects
context-hub context "my thesis revision"   # resolve a matter by natural language
```

Default data directory: `~/Library/Application Support/ContextHub` (macOS),
`%LOCALAPPDATA%\ContextHub` (Windows), `$XDG_DATA_HOME/context-hub` (Linux).
Override with `CONTEXT_HUB_DATA_DIR`.

### Connect an agent (MCP)

Codex CLI (`~/.codex/config.toml`):

```toml
[mcp_servers.context-hub]
command = "python3"            # or the absolute python you use
args = ["/path/to/context-hub/mcp-entry.py", "--client", "codex"]
startup_timeout_sec = 15
tool_timeout_sec = 60
```

ZCode (`~/.zcode/cli/config.json`): add under `mcp.servers`:

```json
"context-hub": {"type": "stdio", "command": "python3",
                "args": ["/path/to/context-hub/mcp-entry.py", "--client", "zcode"],
                "timeoutMs": 60000}
```

Or run the guided installer, which backs up and patches both client configs (reversibly)
and skips clients it cannot find — one client alone is fine:

```sh
context-hub-integrate --dry-run    # show what would change
context-hub-integrate              # install (add --no-rules to skip AGENTS.md rules)
context-hub-integrate --uninstall  # fully remove; archives are preserved
```

On macOS the installer also registers a launchd service (`dev.local.context-hub`) that
re-archives every ~180 s. On Linux/Windows, schedule the same job yourself:

```sh
# cron example (Linux): every 3 minutes
*/3 * * * * python3 -m context_hub.background
```

### Matter mapping (optional)

Archived sessions also retain legacy project groups by working directory. These
commands change that grouping; shared many-to-many matters use the owner MCP
`create_matter` and `link_context` tools instead:

```sh
context-hub map --project "my thesis revision" --root ~/Documents/thesis
context-hub map --project "my thesis revision" --session codex:<session-id>
```

## MCP tools

| Tool | Purpose |
|---|---|
| `continue_matter` | Resolve a matter by natural language; return its full context |
| `list_matters` | List matter names |
| `search_context` | Full-text search over archived conversations (optionally scoped) |
| `read_record` | Page through one archived record by evidence ID |
| `read_notes` | Paginated history of all notes/opinions/tasks for a matter |
| `discussion` | Attributed contributions and reply references with stable pagination |
| `remember` | Write an attributed note (memory/decision/task/opinion) with evidence IDs |
| `create_matter` | Create a matter the user explicitly asked for |
| `link_context` | Link/reject directory, session, record or project associations |
| `relate_matters` | Relate two matters without merging their materials |
| `register_memory_source` | Attach a local Markdown memory file to a matter |
| `context_status` | Last background run status and errors |
| `refresh_context` | Ask the background worker to run now |
| `handoff_session` | Continue one specific archived session across agents — returns its todos, title and recent messages |
| `prepare_resume` | Export an archived ZCode session as a native Codex session file; returns a ready-to-run `codex resume` command |

Notes default to `proposed`; they are never silently promoted to accepted decisions, and
a task reopened after `done` keeps its full history.

## Privacy and security posture

- Archiving has no model calls or telemetry. Default MCP uses stdio; optional file
  transport stays local. Optional authenticated HTTP binds to loopback; remote
  reachability requires explicit HTTPS setup.
- Archive adapters open sources read-only (`mode=ro` + `query_only`). Session resume
  creates a new Hub-owned mirror while protecting existing sessions. Overlapping
  or cloud-backed archive data directories are refused.
- Files and directories it creates are `0600`/`0700` on POSIX.
- Conversation content is **not encrypted at rest**, and there is **no secret redaction
  yet** — anything an agent typed into a session is archived verbatim on your disk.
  Treat the data directory as sensitive; do not point sources at machines you do not own.
- Content returned to an agent enters that model's processing pipeline; local storage is
  not a guarantee of offline model processing.
- The ChatGPT bridge (official Secure MCP Tunnel) is **experimental** and scoped to an
  explicit matter allowlist — see [CHATGPT-SETUP.md](CHATGPT-SETUP.md).

## Known limitations

- Retrieval is lexical (FTS5 with CJK bigrams), not semantic; add aliases for synonyms.
- Automatic title-based association can misfire; it is marked `auto`, gives a reason, and
  can be rejected (`link_context status="rejected"`), after which it is never re-applied.
- Indexed text is capped at 200,000 characters per record (full text stays readable via
  `read_record`).
- Forensic raw payloads above 256 KiB may retain a hash and prefix rather than every
  byte. Main conversation text is stored separately; full attachment/forensic raw
  preservation is not guaranteed. New agent enrollment is not full chat capture.
- No atomic snapshot across multiple source databases; content that never hit disk
  (e.g. unsent drafts) is out of scope.
- For JSONL logs without `ordinal` fields, identity falls back to byte offsets: inserting
  bytes at the front re-keys events (old revisions are preserved, stale index keys can
  linger). Current Codex logs always carry ordinals. See [docs/VERIFICATION.md](docs/VERIFICATION.md)
  for the counter-example suite that documents these behaviors.

## Development

```sh
python -m unittest discover -s tests
```

CI runs the suite on macOS, Linux and Windows across Python 3.11 and 3.14.
See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
