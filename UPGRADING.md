# Local reliability upgrade (0.5.0)

This release builds on the public 0.4.0 line and the session-history fix. It does
not publish changes, merge GitHub pull requests, or capture complete GPT chats.

## Daily use

Say the matter's name in a connected agent. Context includes a recovery receipt,
source record IDs and recent raw checkpoints. These are evidence, not accepted
decisions or an AI-generated summary. Agents still write substantive digests and
decisions through `remember`; model compliance cannot be forced by the archive.

`prepare_resume` safely reuses a matching source snapshot. Refreshing, source changes
or modifications to an existing mirror produce a fresh session and retain the
previous mirror ID in the local lineage ledger. No existing rollout is rewritten.

`context-hub doctor` reads code/deployment version, source availability, last successful
archive time, last check time, missing-log warnings and freshness. A successful archive
is distinct from a verified handoff. No model calls or network requests are made.

On Mac, `查看同步状态.command` generates and opens a private local report. The background
job refreshes this report; the page reloads every 30 seconds. Source notes are HTML-escaped.

## Installation and recovery

First run the complete test suite in an isolated checkout. Stop the owned background
scheduler before deployment and resume it afterward. Do not stop an unrelated app or
replace its configuration. The updater operates on the code runtime and Hub's own
data, never on ZCode/Codex source databases.

```sh
python3 -m context_hub.upgrade --source /path/to/tested/source \
  --runtime /path/to/ContextHubApp --data-dir /path/to/ContextHub --commit COMMIT
```

The updater snapshots individual SQLite databases and local metadata, stages code,
migrates the old archive to a temporary state file, checks integrity and record counts,
then swaps the runtime. It retains the original legacy archive. Stage or installed-code
failure restores the previous code and quarantines new migration artifacts. It never
restores an older notes snapshot over concurrent notes. Backups are local private files,
not off-device disaster recovery, and may include private local configuration.

```sh
python3 -m context_hub.upgrade --runtime /path/to/ContextHubApp \
  --data-dir /path/to/ContextHub --rollback /path/to/ContextHub/backups/release-ID
```

Manual rollback preserves current notes and archive data. If an old-format runtime
could no longer read newly captured revisions/heads, rollback refuses the unsafe
downgrade; use a verified backward conversion rather than discarding new work.
Keep the scheduler paused during manual recovery.

Existing MCP sessions can cache imported code and tool definitions. Reconnect their
Context Hub connection after upgrade (normally by opening a new client session).
Do not force-quit an agent that is in the middle of work. `context_status.code_version`
identifies the version serving the call; restarting the background job alone does
not reload a client's already-running MCP process.

## Acceptance checks

- Unit and migration failure tests; native Codex reader with synthetic data.
- Every previous mirror survives equal-size edits, appended turns and refreshes.
- Scoped ledgers never write into unrelated default Hub directories.
- Original archive counts and notes remain available after migration.
- Background completes, MCP initializes, matters and scoped GPT retrieval work.

Windows actual scheduling, desktop scrolling and complete GPT transcript capture
remain separate validation scopes. Missing historic source logs require coverage
analysis; do not erase archives to silence a warning.
