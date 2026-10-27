# Native context repair — 2026-10-25

## Scope and observed causes

- The live encoder converted hidden ZCode runtime reminders into assistant
  text, bypassing the one-off export filter. Both now share one visibility policy.
- The same Codex session had an older rollout ending October 18 and a current
  continuation file ending October 25. Suffix-based discovery selected the old
  file. Source resolution now validates identity and the native database's
  current path, and checks all nonconflicting continuation segments.
- Native parsing lost custom tool inputs, subagent authorship, image blocks and
  compaction replacement history. These are retained as typed historical data.
- A successful file append followed by a failed pagination projection could
  falsely report success on retry. A durable pending projection now recovers it.
- Directly embedding hundreds of megabytes of raw tool data caused the real
  ZCode reader to exhaust its default Node heap. Bounded native previews now
  reference complete, hash-verified local artifacts rather than duplicating
  those payloads in chat rows.

## Executed validation

- Windows, Python 3.14, explicit UTF-8: **92 tests passed**. Tests cover hidden
  reminders, real English, continuation sources, strict parsing, source dates,
  active rewinds, full-record paging, raw tool input/output, image artifacts,
  target change protection, migration failure and projection crash recovery.
- The full real source was scanned from two files, approximately 774 MB total.
  Validation reached **October 25, 10:34 Hong Kong time**, rather than October 18.
  It produced 4,892 messages / 4,925 native parts, verified 3,257 original tool
  call wires and retained 33 image blocks. No model or historical tool ran.
- Import publication used an isolated database with the installed ZCode schema.
  Production conversation rows were not replaced. Native sync pairs remain paused.
- The installed ZCode 0.17.0 app-server resumed that isolated imported session
  using an isolated home/profile. Its last 20 messages matched the native
  sequence exactly and reached the verified October 25 head, including 16 tool
  parts. No `session/send` request was made. The initial verification process
  used a data-directory override that did not isolate native session storage;
  that result was discarded and the check was repeated with an isolated home.

The private source manifests, import receipt, native-reader receipt and
deployment receipt live under the configured Hub data directory in `validation/`.
They contain source paths and identifiers and are not committed to this repository.

## Boundaries

| Claim | Result |
| --- | --- |
| Resolve complete, current source instead of an older same-ID file | PASS: full real source |
| Preserve original semantic data and source dates | PASS: tests and isolated import |
| Actual ZCode reader can resume and return the current imported head | PASS: isolated profile, installed app-server |
| Prevent unsolicited native import/sync | PASS: separate explicit entry points; pairs paused |
| Retry failed pagination publication without new messages | PASS: fault-injection test |
| Repair existing contaminated or outdated chats in place | Not performed; local work preserved |
| Desktop hot refresh or active model reading external appends | UNVERIFIED |

A complete source receipt means source validation completed. It does not mean
all history fits in one model context. Pages, omitted large payloads and local
artifact references are explicit, and must be read as needed before reasoning
from their full contents.
