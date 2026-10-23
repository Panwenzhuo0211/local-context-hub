# Contributing

Issues, bug reports and pull requests are welcome.

## Ground rules

- **Synthetic data only.** Never commit real conversation archives, personal documents,
  databases, credentials, local configuration or backups. Tests build their own fixtures
  in temporary directories; keep it that way.
- **Tests required.** Every behavior change ships with a test
  (`python -m unittest discover -s tests`). CI runs the same suite on macOS, Linux and
  Windows (Python 3.11/3.13) — platform-specific regressions are caught there, but note
  that local development on macOS cannot exercise Windows locking; rely on CI for that.
  On Windows, run tests with `PYTHONUTF8=1` (CI sets this) — fixtures are UTF-8.
- **No new dependencies.** The core is deliberately standard-library-only.
- **Read-only discipline.** Adapters must never write to agent data directories. Any new
  source integration needs the same guarantee plus schema validation
  (`adapters.require`) and a read-only connection.

## Accurate attribution

Disclose AI assistance in your contribution (tool, and what it did) in the PR
description, and credit other people for their actual contributions. Testing, bug
reports, implementation and technical design are distinct kinds of contribution;
commit authorship alone does not establish who conceived a mechanism.

## Reporting security issues

Open a private security advisory (GitHub → Security → Report a vulnerability) rather
than a public issue.
