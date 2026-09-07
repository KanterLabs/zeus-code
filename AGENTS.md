# Zeus Code

Python 3.11+ terminal client and per-machine asyncio daemon. Runtime dependencies
are limited to the standard library. Linux and macOS are supported; Windows
users need WSL. Product scope: `docs/design.md`; protocol: `docs/implementation.md`.

- Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and
  `python3 scripts/build.py`; run `python3 scripts/smoke.py` for process lifecycle.
- In this development environment `/tmp` has a quota. Use `.work/tmp` for local
  test artifacts (`TMPDIR="$PWD/.work/tmp"`) and preserve unrelated files.
- UI selection must never own provider execution. Persist accepted runs and
  events before acknowledging. Never replay a prompt automatically on reconnect.
- Approval IDs are globally unique Zeus IDs, scoped to exactly one provider
  request. Never broaden approval scope or conflate review with approval.
- Migrations must preserve populated data and be transactional. Backups use the
  SQLite backup API. Never restore or delete user state as an upgrade action.
- Read provider contract docs before changing integrations; run fixture tests
  first. Live tests may cost money and must use isolated test repositories.
- GitHub Actions jobs use `homelab` for this short suite. Future long browser,
  build or container jobs use `homelab-heavy` per organization policy.
- Shane requested Sol workers for work on 2026-09-07. Use `gpt-5.6-sol` workers
  for that workstream, each with explicit module ownership; do not use Luna.
