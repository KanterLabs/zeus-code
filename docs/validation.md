# v1 validation

## v1.2.0 daily workflow candidate

September 9, 2026: the integrated suite passes **298 Python tests** and
**16 npm tests** under **ZC-28**. Source and packaged lifecycle checks, npm
packed installation and the packaged terminal walkthrough pass.
The running-thread regression uses an isolated real daemon and gated provider:
switching A → B → A and reconnecting preserve the original run, one provider
invocation, one user message and one terminal result. A continuously moving
event tail reproduced a client refresh timeout; catch-up now stops at the
snapshot sequence boundary and retains events beyond that boundary.

Recovery fixtures verify that unconfirmed sends remain tied to the original
server, late acknowledgements preserve newer drafts (including edits back to
the same text), and automatic diff previews use a separate transport from live
polling. The populated client cache keeps its existing schema and unknown fields.
Agent fixtures distinguish current-run activity from historical or offline
observations and never infer that a historical agent is still running.

The terminal walkthrough adds a 160×40 automatic agent panel, a 48×16 daily
workflow, and switching between two actually running fixture tasks. These tests
use local deterministic providers; they do not invoke paid model turns. The
packaged walkthrough produced 36 captures under `.work/ux-v1.2.0` and verified
that the health-check seam loaded the actual archive. Release checking stayed
idle until requested, and no real update ran inside the fixture.

Daemon regressions also cover rejecting a known unavailable provider without
creating a run or event, retaining an already accepted request after provider
availability changes, and adding projects during a frozen paginated snapshot.

## v1.1.1 remote discovery follow-up

The local suite passes **253 Python tests**. The packaged lifecycle and terminal
walkthrough pass. With a minimal noninteractive SSH PATH, the built application
finds the installed OpenCode 1.18.26 executable and discovers its 24 models.
A symlink fixture verifies discovery and execution use the same resolved binary.

The v1.1.1 tag passed Python 3.11/3.14 and npm package checks; GitHub published
all three assets. Downloaded checksums and bytes match the tested build. A managed
v1.1.0 launcher successfully ran `zeus-code update` against the public release,
reported v1.1.1 and retained its previous runtime. The idle dev daemon was then
upgraded with a verified SQLite backup and an unchanged full database fingerprint:
97 projects, 3 threads, 5 runs and 333 events. Both Codex (6 models) and OpenCode
(24 models) are available over SSH. npm publication still fails at the trusted
publishing step; this release is available through GitHub.

Process fixtures retain their bounded termination and stopped-write checks while
recognizing killed Linux zombies as terminated. The eight process tests pass in
Python 3.11 containers both with and without an init process. No supervisor
runtime behavior changed.

## v1.1.0 UX release

September 8, 2026: the integrated local suite passes **252 Python tests** and
**16 npm tests**. Model and agent integration fixtures use the installed
Codex 0.153.4/OpenCode 1.18.26 contracts without paid model turns. They cover
model capabilities, background discovery, persisted nested/concurrent child
activity, unread markers, provider defaults and retained settings.

`python3 scripts/e2e-ux.py` runs the actual curses client in a PTY against an
isolated daemon at **48×16**, **80×24** and **120×35**. It covers empty, running,
approval, failed and completed conversations; model/reasoning selection is
checked in persisted state. Palette, attention and agent controls preserve the
parent draft. ANSI captures are retained under `.work/ux-captures`; baseline
v1.0.9 captures are under `.work/ux-before`. CI runs the same walkthrough against
the built zipapp. The final packaged walkthrough and source/artifact lifecycle checks pass.
A real retained v1.0.9 daemon also keeps its PID, active row, draft and model
through a v1.1.0 client update, with a verified backup and working RPC diff.
The Python 3.13+ shutdown socket race has a deterministic regression.
The published GitHub archive matches the locally verified build byte for byte.
Release CI passed Python 3.11/3.14 and the npm packaging checks. The dev server
upgrade preserved all 97 projects, 3 threads, 5 runs and 333 events with a
verified pre-upgrade backup. npm registry publication remains blocked by trusted
publisher configuration; GitHub Releases carry this version. Tracking: **ZC-27**.

Updater fixtures retain a live process and populated SQLite state while
switching a client launcher. They verify lazy imports from the old archive,
unchanged active rows, backup integrity, protocol/schema compatibility,
custom command names and safe deferral for a legacy archive still in use.
No user database is reset or restored.

## v1.0.3 self-update

September 8, 2026: the Linux candidate passed **178 tests**, including release
publication and updater fixtures, plus the standalone build and source/packaged
lifecycle smoke checks. The release workflow runs the suite on Python 3.11 and
3.14 before publishing a tagged release.

The updater checks the latest stable GitHub release, verifies artifact and
metadata checksums, and requires the embedded version/schema to agree. Tests
cover failed downloads, malformed metadata, downgrade prevention, renamed
bundles, source-checkout protection, explicit install directories, daemon-lock
refusal, populated-data backup integrity, retained binaries and incompatible
schemas. Updating does not run a migration or restore a database.

Release fixtures cover reproducible bytes despite source timestamp changes,
tag/source mismatch, incomplete-draft retries, conflicting assets and idempotent
publication. The three published assets are `zeus-code.pyz`, `release.json` and
`SHA256SUMS`. Final live release-download and CI evidence is tracked in Helm
**ZC-16**. The physical two-machine gate below remains open.

## v1.0.2 baseline

Validated on September 7, 2026, on Linux with Python 3.14.4. The implemented
application version is 1.0.2. The compatibility target is Python 3.11+ on Linux
and macOS; CI contains Python 3.11 and 3.14 jobs on `homelab`. macOS and the CI
runner executions are not claimed as locally verified.

## Automated evidence

The standard-library unittest suite covers:

- Codex and OpenCode protocol fixtures across real subprocess/HTTP boundaries:
  start, resume, streaming, approvals, cancellation, failures, discovery and
  resource limits. Reasoning events are not rendered as assistant replies.
- Two independent daemon runs, background approval rejection, cancellation
  isolation, client disconnection, request idempotence, event replay, pagination,
  state/draft/session restoration, duplicate startup and protocol mismatch.
- SQLite transactional state/events, populated-data reopening and migration,
  unsupported-newer-schema refusal, exact approval IDs, byte-bounded event pages,
  recovery without prompt replay, and verified online backup without overwrite.
- Real Git repositories: ordinary/unborn/detached branches, dedicated worktrees,
  staged/unstaged/untracked/binary/renamed files, unusual filenames, path safety,
  bounded large-file inspection, and external diff helper suppression.
- Portable process supervision, including actual parent SIGKILL, provider and
  descendant termination, stubborn-child escalation, pipe transparency and
  bounded startup failure cleanup.
- Atomic private client caches, preserved drafts and selected thread, separate
  connection/task state, sequence deduplication, frozen older history during
  continued output, changed server identity, send/switch races, Unicode input,
  and authoritative message/tool reconciliation.
- Transport negotiation, SSH destination validation, request serialization,
  bounded lines, snapshot assembly, source-checkout detached startup, CLI
  argument handling and actionable non-TTY errors.

**153 tests passed** in the 1.0.2 full-suite run. Source compilation, whitespace
checks, standalone packaging, and all three launcher smoke checks also passed.
The full suite, build, and bundle smoke also passed from an isolated clean
checkout with no pre-existing scratch directory.

```sh
mkdir -p .work/tmp
PYTHONPATH=src TMPDIR="$PWD/.work/tmp" python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests scripts
python3 scripts/build.py
python3 scripts/smoke.py
python3 scripts/smoke.py --executable dist/zeus-code.pyz
```

Local `/tmp` has an independent quota, so validation uses the ignored `.work`
directory. No user repositories or production databases were reset or restored.

## Real provider evidence

Installed provider versions: **codex-cli 0.153.4** and **OpenCode 1.18.26**.
Discovery detected an authenticated Codex account and connected OpenCode models.
Credential values were not copied into Zeus configuration or logs.

The 1.0.2 Codex fixtures verify YOLO defaults (`never` approvals and
`danger-full-access` sandbox) for both new and resumed threads, retained explicit
permission overrides, and generic activity phases without reasoning content.
The installed app-server also accepted fresh threads with the default YOLO
settings and with an explicit `on-request`/`workspace-write` override. These
configuration checks started no model turns. Resume remains covered by protocol
fixtures and the earlier live conversation checks below.

Individual adapter smoke checks returned an exact verification token without
tools or file changes. A later integrated check ran **Codex gpt-5.6-sol** and
**OpenCode opencode/big-pickle** concurrently through two independent Zeus
daemons and two separate Git repositories. Both client connections closed after
submission, reconnected, and retried the original request IDs. Each returned the
same run ID, completed normally, and then resumed the same provider session for
a second turn. Both fresh and resumed turns returned the expected token.

Reproduce deliberately (four live turns, consumes configured provider allowance):

```sh
python3 scripts/live_check.py --live --codex-model gpt-5.6-sol --opencode-model opencode/big-pickle
```

Approval and cancellation isolation are validated with deterministic structured
provider fixtures. These are distinct from the live no-tools model checks.

## Terminal and packaging evidence

The 1.0.2 activity strip and sidebar are driven by accepted runs and provider
events. Tests cover elapsed and quiet timers, concurrent tools, exact command
titles during output streaming, terminal outcomes, pending approvals, stale
machines, and activity while browsing older history. Run timestamps survive the
500-event client cache limit and reopening. Delayed snapshots and old retry
acknowledgements cannot replace a newer run or regress terminal state; retrying
an unconfirmed request preserves a subsequently edited draft.

A real tmux terminal, real Unix-socket daemon and deterministic provider fixture
verified thinking, command execution, response streaming, completion, failure,
approval, selected-run cancellation, and offline last-known state. Animation and
elapsed time advanced during quiet periods and stopped on completion. The exact
Unicode multiline draft survived 120×32, 80×24, 60×20, 48×16 and 90×16 sizes.
The compact sidebar keeps the selected thread and activity visible at minimum
height. Captures of the activity strip were inspected visually. These terminal
checks did not invoke a paid provider.

The 1.0.1 interface adds a complete first-use creation form, automatic project
selection, immediate display of successful creation, inline errors and editable
defaults. Regression tests drive keyboard input through real Unix sockets and
Git repositories, including the first provider reply, invalid-path correction,
duplicate-submit prevention and Unicode/resize handling. The layout uses explicit
dark backgrounds, readable thread names, visible actions and a framed composer.

A real tmux pseudoterminal used the clickable New thread action, corrected an
invalid path, created both provider threads, and switched between them. The
exact Unicode multiline draft survived 120×32, 80×24, 60×20 and 48×16 terminal
sizes, and Ctrl+Q exited from a modal. Screen captures were inspected for welcome,
form and composer layout. The following earlier v1 checks continue to cover
execution and storage.
Additional terminal checks verified the actual caret column and preserved draft
for CJK, emoji and combining characters in both the form and composer. The
standalone bundle also opened the welcome screen and creation form and exited
cleanly.

A real 120×35 pseudoterminal session opened both provider threads, entered an
unsent Unicode draft with a newline, switched to the other thread and typed a
second draft, then returned. Both drafts were recovered exactly. Ctrl+J inserted
a newline without submitting. The global switcher, keyboard-help overlay, diff
review and Ctrl+Q exit worked without a traceback.

A second pseudoterminal check selected a changed file, loaded its scoped patch,
returned to the file list, resized from 120×35 to 80×24, and exited cleanly.

The source launcher, standalone `.pyz`, and the same bundle renamed to
`zeus-code` passed process-level smoke checks: detached startup, duplicate-start
protection, project/thread registration, untracked diff, verified backup,
graceful stop, and restart preserving server and thread IDs. Build output is
`dist/zeus-code.pyz` plus `dist/SHA256SUMS`.
Missing-server errors returned a nonzero exit status through every launcher.

## Outstanding physical two-machine gate

The design's final two-machine scenario is **not yet verified on the actual
homelab and laptop**. Both SSH attempts failed at name resolution in this
environment. The live concurrent test above used two daemon instances on one
physical machine. SSH argument construction and the byte relay are implemented
and tested separately; this is not a claim of a successful real remote run.

Once SSH aliases are available:

1. Install the same build on each host and verify `ssh HOST zeus-code --version`.
2. Start `zeus-code serve --background` on each host; connect and register one
   project on each machine.
3. Launch a Codex task on one machine and an OpenCode task on the other.
4. Save a draft and scroll position, switch threads, then return while both run.
5. Answer a background approval, inspect a changed-file diff, and cancel one
   task while the other continues.
6. Close and reopen the client. Confirm preserved history, current state and
   no duplicate runs. Make one host unavailable and confirm its stale label.

The packaged implementation is ready for this final environment acceptance;
this document deliberately leaves that gate open.

## Progress services

The required Roadmap updater failed. The installed tracker was still queried,
but no Zeus Code project exists, so work was not put in an unrelated project.
Hark publishing timed out. Repository checkpoints and this evidence file are the
durable record for this work.

## v1.0.4 npm installation (September 8, 2026)

Local validation of the scoped `@kanterlabs/zeus-code` package:

- Python suite: 178 tests passed; standalone build and process lifecycle smoke passed.
- `python3 scripts/smoke-npm.py`: packed archive contains only the launcher,
  package metadata, README and three release artifacts. Installation into an
  isolated prefix works from an unrelated directory.
- A populated daemon survives deletion of both the npm cache and npm install
  prefix. Its stable runtime bundle remains available; the thread survives a
  verified SQLite backup and daemon restart.
- Bare launcher in a real terminal starts the isolated daemon, reaches connected
  UI state and exits cleanly with Ctrl+Q. No provider execution was requested.
- Launcher unit tests: 11 passed, covering prerequisite selection, verified
  cache behavior, argument/cwd forwarding and startup/update rules.
- After pushing commit `816228e`, `npx --yes github:KanterLabs/zeus-code --version`
  fetched and prepared the GitHub package from an unrelated directory and printed
  `zeus-code 1.0.4`. Bare GitHub npx launch also reached the connected UI and
  exited cleanly; GitHub CI run `34247028837` passed for that commit.

Registry publication remains a separate release gate pending npm credentials.
All 16 npm tests pass, including publication, exact-byte retry, conflicting-version
refusal, tag mismatch and registry failures. A real registry lookup and pack with
no publishing credential reaches the documented `NPM_TOKEN` handoff error.

The v1.0.4 release gate caught npm 10 preparation output mixed into JSON metadata;
neither registry nor GitHub release was published from that tag. v1.0.5 keeps
build diagnostics on stderr and prepares before machine-readable packing. The
full packed-install/state smoke and publisher credential handoff pass with
npm 10.8.2 as well as the local npm 9 baseline.

v1.0.5 at `f199235` passed all main and tagged test jobs. GitHub Release v1.0.5
published its three verified assets; the npm step in run `34248494657` stopped
with the explicit missing `NPM_TOKEN` error. npm remains unpublished until that
secret is configured and the failed job is rerun.

Fresh-cache GitHub installation reports v1.0.5 with npm 10.8.2 `npx` and npm 9
`npm exec --yes --package=github:KanterLabs/zeus-code -- zeus-code --version`.
This machine's npm 9 `npx` misroutes Git preparation to `npm exec install` on a
fresh cache; the README gives the working `npm exec` fallback. Cached npm 9 npx
success alone does not establish fresh-install compatibility.

## Download and open E2E (September 8, 2026)

`scripts/e2e-npx.py` exercises npx in a real pseudo-terminal with an empty npm
cache, isolated home/application state and a working directory outside the
checkout. It waits for the welcome screen and connected indicator, presses
Ctrl+Q, verifies the daemon remains running without creating any threads, then
reopens and verifies the same daemon PID and workspace identity. It stops its
own daemon and removes only its temporary files. Startup and exit have deadlines.

Both the local packed-checkout test and the live
`--package @kanterlabs/zeus-code@latest` test passed against npm v1.0.6. No model
was invoked. The local test runs before publication; a separate homelab job tests
the exact version from the npm registry after each successful trusted publish.
