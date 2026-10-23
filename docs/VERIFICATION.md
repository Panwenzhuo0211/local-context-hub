# Verification

How this project is tested, and what the log-ingestion mechanism actually does under
adversarial conditions.

## Automated suite

`python -m unittest discover -s tests` — 37 tests, under one second, standard library
only. CI runs the same suite on macOS, Linux and Windows (Python 3.11/3.13).

Coverage highlights:

- **Archive invariants**: idempotent sync, source files provably unchanged (hash-compared
  before/after), failure rolls back records *and* cursors, partial trailing lines deferred
  and later ingested exactly once, rewritten/truncated logs re-read with old revisions
  preserved, identical repeated messages kept as distinct events.
- **Read-only guarantees**: WAL-mode source databases opened via `mode=ro` reject writes;
  symlink and path-escape refusals for outputs.
- **Matters layer**: natural-language resolution and ambiguity, many-to-many links,
  rejected auto-associations never overwritten, revision/reversion indexing, paginated
  notes and records, prompt-injection content handled as data.
- **MCP protocol**: initialize/tools handshake, argument validation, per-client author
  binding, real stdio subprocess round-trip, scoped ChatGPT allowlist enforcement.
- **Integration**: backups taken before any write, unrelated client config preserved,
  idempotent re-install, conflicting servers refused, uninstall preserves later unrelated
  config, single-client installs, `--dry-run` writes nothing, rule injection optional.

## Counter-example suite for log ingestion

`docs/` experiment (see repository history for the script and raw output) drives the real
adapter + sync pipeline against five adversarial mutations of synthetic JSONL logs, with
a naive offset-only collector as baseline:

| Scenario | Hub behavior | Naive offset baseline |
|---|---|---|
| Insert into consumed region (logs with `ordinal`) | Detects rewrite, full re-read; existing event identity unchanged; exactly one new revision | Misaligned parses |
| Insert into consumed region (no ordinals) | Re-keys events at new byte offsets; old revisions preserved; one stale index key lingers | Misaligned parses |
| Rotation/replacement at same path | Old session retained, new session ingested | Silently skips everything (offset past EOF) |
| Identical content at two positions | Two distinct events | n/a |
| Incomplete trailing line | Deferred; ingested exactly once after completion | Parse error; event lost forever |
| Same-size, same-mtime rewrite (in-place) | Invisible to stat fingerprint on the day; caught by the daily prefix-hash re-verification | Never detected |
| Concurrent append during sync | No crash, no duplicates | n/a |

Real-format check (2026-10): 41 genuine Codex log files — 10,487 `response_item` lines,
100% carry `ordinal`; 41/41 start with a `session_meta` line. So the byte-offset fallback
and the first-line assumption are latent behaviors behind a format change, not defects in
the current format.

These experiments document behavior; they do not by themselves establish novelty or
inventive merit of anything.

## Known gaps

- Windows-specific paths (locking via `msvcrt`, `%LOCALAPPDATA%`) are validated by CI,
  not by daily local use — treat early Windows releases as beta.
- No secret redaction or encryption at rest (documented in the README).
- The ChatGPT tunnel bridge is experimental and not exercised by CI.
