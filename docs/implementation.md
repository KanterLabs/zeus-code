# Zeus Code v1 implementation and validation

Source of product requirements: [design.md](design.md), supplied design draft 0.3.

## Architecture contract

Python 3.11+ on Linux and macOS. No third-party runtime dependencies. `curses`
TUI, asyncio Unix-domain socket daemon, SQLite WAL event journal. SSH invokes
`zeus-code bridge` on the selected alias; bridge only relays local socket bytes.
The daemon owns provider subprocesses, so leaving the TUI or SSH connection does
not cancel work. Each active run has a separate asynchronous task and adapter.
Provider process groups are supervised so unexpected daemon exits stop owned
provider processes. Daemon/host failure marks interrupted runs failed, preserves events and provider
session IDs, and requires an explicit subsequent send to resume. Never replay a
prompt automatically after a server restart.

Every RPC is a JSON line: `{id, version: 1, method, params}`. Reply is
`{id, result}` or `{id, error: {code, message}}`. Line limit 1 MiB, no TCP listener.
The user-only socket and state directory are mode 0600/0700. Requests dispatch
sequentially per connection; the UI uses a background async polling task per
machine and renders locally cached state immediately.

### RPC methods

- `hello` → `{protocol_version, version, server_id, hostname, pid}`.
- `snapshot {offset?: int}` → hello fields plus `projects`, `threads`,
  `approvals`, `last_seq`, `next_offset`. Pages are limited to about 512 KiB;
  the standard client transparently assembles them while retaining the first
  page's event sequence for subsequent replay.
- `providers` → cached `{codex: {available, detail, version?, models?, status?,
  checked_at?, cached?, refreshing?}, opencode: ...}`. Capability discovery runs
  in the background so provider checks do not block event replay or cancellation.
  Model entries include `id`, `name`, and provider-reported optional
  `reasoning_efforts`, `variants`, `is_default`, `default_reasoning_effort`.
  Missing capability fields are unavailable, not guessed defaults.
- `refresh_providers` requests background capability refresh without waiting for
  provider subprocesses.
- `add_project {path, name?}` → project `{id, name, path, branch, ...}`.
- `create_thread {project_id, title, provider, model?, settings?, worktree?: bool}`
  → thread `{id, project_id, title, provider, cwd, branch, isolated, session_id,
  state, archived, draft, scroll, model, settings, created_at, updated_at}`.
- `update_thread {thread_id, title?, archived?, draft?, scroll?, model?, settings?}`
  → updated thread. Provider and cwd never change after thread creation.
- `send {thread_id, prompt, request_id}` → durable run `{id, thread_id, state, ...}`.
  request_id is unique/idempotent; retry it after an uncertain connection result.
- `cancel {thread_id, run_id?}` → `{cancelled: bool}`. Only that current run.
- `approve {request_id, decision: "allow"|"reject"}` → approval record. IDs are
  globally stable and replies are idempotent. Stale or other decisions fail.
- `events {after: int, limit?: int, thread_id?}` → `{events, last_seq}` in ascending
  sequence order. Events `{seq, thread_id, run_id, kind, data, created_at}`.
- `history {thread_id, before?: int, limit?: int}` → `{events, has_more}` latest
  chronological page (before is exclusive); render at most a bounded page.
- `diff {thread_id, path?}` → `{branch, shared, files, diff, truncated}`.
  files entries `{path, status, additions, deletions}`; include untracked files.
  Counts may be null with `stats_unavailable` for very large/unreadable untracked
  files; `diff_omitted` flags patches omitted from the aggregate view. A selected
  file may still be reviewed with bounded output.
- `stop` → `{stopping: true}`, gracefully cancel own runs and stop daemon.

Execution states: idle, running, awaiting_approval, completed, failed, cancelled.
Connection state belongs to client machine cache, never overwrites task state.
Provider contract: `src/zeus_code/providers/base.py`. Events and approvals are
persisted before sending responses. Output events are bounded; disk quota refuses
new execution, with an actionable error, rather than removing history.

## Checkpoints

- [x] Validate both provider protocols, cancellation, approvals, resume and auth.
- [x] Implement durable storage, migrations, event replay and data preservation.
- [x] Implement independent daemon runs, lifecycle commands, and SSH transport.
- [x] Build keyboard workspace, machine/project/thread navigation, drafts and review.
- [x] Verify concurrent providers, isolated approvals/cancellation, reconnect and recovery.
- [x] Package v1, document installation and operations, and run local release checks.
- [ ] Complete acceptance on the actual homelab and laptop after SSH aliases resolve.

Detailed evidence and the outstanding live two-host gate are in [validation.md](validation.md).

No existing production database is being migrated or deployed by this task.
Populated-database reopening/migration and backup integrity are release checks.

### Automatic agents and local attention

Provider tool events may carry `data.agents` snapshots, with a stable child `id`,
optional `parent_id`, `label`, `state`, public `result`, and provider-reported
`model` or lifecycle times. These are persisted through the existing event
journal. The client merges bounded snapshots by agent ID and sequence, including
resumed children. Private reasoning is not rendered as a result.

Read markers are additive client-cache data, scoped by machine, server identity,
thread and event sequence. A result becomes read only when its selected
conversation is visibly at the live tail. Overlays and history browsing do not
advance the marker. Pending approvals remain actionable independently of unread
counts. No database migration is required.

Release metadata includes `protocol_version` alongside `schema_version`.
Compatible updates switch a managed launcher to a new immutable runtime while
retaining running-daemon archives and the previous executable. Legacy archives
in use are retained until a later explicit update can replace their entrypoint.

## Daily workflow state (v1.2)

Client cache version 1 gains additive project/thread preferences for pins,
recency and collapse, plus recovery evidence and last successful connection time.
Selection changes remain local view operations. They must never cancel a daemon
run or automatically resend an unconfirmed prompt. Offline snapshots are last
known data; connection checks and explicit retries are separate actions.

The first accepted run assigns a short, whitespace-normalized title only to an
untouched default-named conversation. The title change and run acceptance use
one SQLite transaction; duplicate request IDs and later prompts do not rename
conversations. Database schema and RPC protocol remain at version 1.

Client health checks reuse stable release validation. An explicit update runs
the existing updater in a child process with captured output, preserving the
terminal and using the same immutable-runtime, compatibility and backup rules
as `zeus-code update`. It does not restart a local or remote daemon.

Workspace refresh drains through the snapshot's fixed event high-water mark,
processing the whole final page and reconciling any later state transitions.
It does not wait for an empty event page: a continuously busy provider must not
prevent publishing the refreshed snapshot or connection state. Later events
remain available from the retained cursor on the next poll.

Automatic-agent summaries retain the parent run ID of the latest reporting
event. The live panel counts current-run observations; older or unscoped
running states remain available as frozen, last-reported history rather than
becoming live again when a new parent turn starts.

Snapshot pages on one RPC connection traverse a frozen record set and retain
its original sequence boundary. Adding a project between pages cannot shift
thread offsets. The daemon releases the remaining records after the final page
or when the connection closes; protocol-1 clients keep using `next_offset`.
New sends are rejected before acceptance when cached provider discovery
explicitly reports unavailable. Previously accepted request IDs still return
the original run, including after provider availability changes.
