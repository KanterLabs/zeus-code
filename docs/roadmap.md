# Zeus Code roadmap

Updated September 9, 2026. Released baseline: **v1.1.1**. Tracking: Helm
project **ZC**; planning is recorded in **ZC-22**, and the complete implementation
is tracked by **ZC-27** for the **v1.1.0** UX release and **v1.1.1** remote-provider follow-up. Unrelated future
work remains in Backlog. Order below takes priority over older milestone labels.

## v1.2 daily workflow — ZC-28

Implement all eight accepted follow-ups plus the requested running-subagent UX as one verified release:

| Improvement | Acceptance |
| --- | --- |
| Workspace restore | Reopen the last server/project/thread with its draft and scroll position; no prompt replay on reconnect. |
| Focused sidebar | Pinned/recent ordering, collapsed inactive projects and project search keep large workspaces manageable. |
| Guided remote setup | SSH, runtime and readiness stages identify failures with a concrete recovery action. |
| Useful progress | Show current action, elapsed time and last activity, distinguishing disconnected and quiet providers. |
| Recoverable failures | Explicit retry/edit/check actions retain drafts and the identity of unconfirmed sends. |
| Change review | Surface changed files and open individual diffs in the current conversation. |
| Conversation management | First-prompt titles, rename, pin, archive, restore and search keep active work discoverable. |
| Connection health | Show connection/client/server versions with explicit release check and safe local update actions. |
| Live subagents | Surface running agents automatically on wide terminals, with compact narrow progress and selectable task/state/timing/result details preserving the parent conversation. |

The reported running-thread switch/stale issue is a release regression gate:
background execution must survive switching and reconnecting without replay.

Verification includes populated cache/database preservation and real terminal
walkthroughs at 48×16, 80×24 and 120×35. Existing physical two-machine and macOS
signoff remain separate gates.

## v1.1.0 UX series

All five slices shipped together in v1.1.0 with verified release assets and
Python 3.11/3.14 CI. The normal conversation view stays compact; detail is opened on demand.

| Order | Delivery | Cards | User-visible acceptance |
| --- | --- | --- | --- |
| 1 | Model indicator and searchable selector | ZC-23, ZC-13; capability prerequisite ZC-6 | A compact control beside the composer shows provider, configured model and supported reasoning setting. Click or F4 opens search across discovered models, including Provider default. |
| 2 | Automatic subagent activity | ZC-24 → ZC-25 | An expandable summary shows running/completed counts. Expand for each agent's task, state, elapsed time and result without leaving the parent conversation. |
| 3 | Readable work and results | ZC-10 | Tool output stays collapsed by default; the current action, failures and final answer are easy to distinguish. Raw event noise does not dominate the transcript. |
| 4 | Results that need attention | ZC-12 | Unread completions, failures and approval requests remain visible when working in another thread; reviewing them preserves the current draft. |
| 5 | Fewer commands and setup surprises | ZC-9, ZC-26 | A searchable command palette names its target. A compatible client update works while daemon work continues, with clear handling of deferred server upgrades. |

### Model controls

- Show the configured model on every selected conversation, including narrow
  terminal layouts. Label an unresolved provider choice **Provider default**;
  do not display a guessed actual model.
- Search the full discovered list instead of typing an undocumented identifier
  or squeezing five model names into a form title.
- Label cached/unavailable discovery. Only offer reasoning, agent or variant
  settings supported by that provider and model.
- Preserve explicit permissions and per-thread settings. Explain whether a
  change applies to the next message; never relabel an already-running request
  as if its model had changed.

### Automatic subagents first

Shane selected visibility into agents the provider launches automatically.
Manual worker launch and agent orchestration are outside this first delivery.

- Normalize provider-reported identities, parent relationships and lifecycle
  events before building the panel. Do not invent unsupported statuses or model
  information. Replay must deduplicate agent updates after SSH reconnect.
- Keep the panel collapsed unless opened. Show public task/result details;
  do not expose private reasoning. Selecting an agent must preserve parent
  draft, scroll and execution.
- Validate two concurrent agents, nested agents, completion, failure and stale
  connections with fixtures before any optional isolated live-provider test.

### Delivery gates

Each slice needs keyboard and narrow-terminal checks, preserved drafts and
populated state, and a real terminal walkthrough of its primary interaction.
Provider integration changes require contract fixtures first. Do not call a
feature complete merely because a button renders or a server connects.

## Recent delivery status

- **v1.1.0 / ZC-27:** all five UX slices shipped, including model controls,
  automatic agent details, transcript/themes, attention and safe client updates.
  The dev server upgrade retained all populated data after a verified backup.

- **v1.0.3 / ZC-16:** verified GitHub self-update, previous-binary retention and
  database backup. v1.1.0 replaces blanket running-daemon refusal
  with immutable client runtimes; server restart orchestration remains ZC-2.
- **ZC-5:** npm package exists, but automatic npm publication remains blocked by
  trusted-publisher configuration. GitHub Releases distribute current builds.
- **v1.0.8 / ZC-20:** guided SSH server setup and remote project discovery.
- **v1.0.9 / ZC-21:** Codex discovery outside noninteractive SSH PATH, compact
  sidebar, searchable project picker, and draft-preserving provider failures.

The broader **v1.1** theme remains daily usability: install, open a project,
start a conversation and understand what is happening throughout a run.

## What already works

v1.0.2 includes thread creation with inline errors, independent provider runs,
per-thread drafts and view restoration, instant cached thread search, diff and
approval review, and live activity with elapsed/quiet timers. Codex defaults to
YOLO, while explicit thread permission settings are preserved. These behaviors
are the baseline for every milestone below.

See [validation evidence](validation.md) for the recorded 153-test baseline,
terminal checks and provider checks. The physical two-machine acceptance test
remains open; two daemons on one host do not complete that gate.

## v1.1 — Daily usability

Work through the three passes below. Each row is an implementation-sized Helm
Backlog card with its own scope and measurable acceptance criteria. Future work
is unclaimed; completing this roadmap does not complete any implementation card.

| Pass | Card | Improvement | Acceptance outcome |
| --- | --- | --- | --- |
| 1. Fix everyday failures | ZC-2 | Deterministic stop/restart and version visibility | Immediate stop/start waits for the observed daemon instance; a new restart command defers active runs by default and preserves populated state. |
| 1. Fix everyday failures | ZC-3 | Correct sidebar targets and archive recovery | With A open and B highlighted, rename/archive affect B; archived conversations can be restored. |
| 1. Fix everyday failures | ZC-4 | Visible focus on narrow terminals | No hidden Tab target; switching and resizing preserve drafts, selection and scroll position. |
| 2. Simplify setup | ZC-5 | Published standalone release and installation | A verified artifact installs without sudo; `zeus-code` works from home and unrelated directories with clear prerequisite/PATH help. |
| 2. Simplify setup | ZC-6 | Responsive provider discovery | A 20-second discovery delay does not hold up incoming events or cancellation dispatch beyond one second. |
| 2. Simplify setup | ZC-7 | One-command launch and local diagnostics | Launch starts or reuses one healthy daemon; doctor explains missing setup even before the daemon is running. |
| 2. Simplify setup | ZC-8 | Repository chooser for thread creation | Registered repositories ordered by recorded last-opened time and machine-aware path entry work from an empty workspace launched in the home directory. |
| 3. Polish daily work | ZC-9 | Searchable command palette | Visible actions and a non-function-key shortcut expose the main workflow, with clear targets and unavailable-action reasons. |
| 3. Polish daily work | ZC-10 | Readable conversation layout and terminal colors | Clear role/tool/error blocks, readable code and consistent spacing work at compact and wide sizes, with explicit theme choices and monochrome support. |
| 3. Polish daily work | ZC-11 | Multiline paste and expanded editing | Pasted newlines remain draft content; deliberate submission, Unicode editing and draft recovery work across resize and thread switches. |

Start with ZC-2, ZC-3 and ZC-4. Packaging and provider discovery can proceed
independently. Sequence changes to the shared TUI modules with one integrator;
avoid concurrent edits to the same input and layout paths.

The palette adds command search while Ctrl+P keeps its current thread-search
behavior. The transcript work uses the existing standard-library terminal
stack. The activity strip already exists: improve its presentation while
preserving accurate running, approval, terminal and offline states.

## v1.1 release acceptance

The implementation cards and the following checks must pass against the release
candidate. Record terminal captures and commands in [validation.md](validation.md),
separating deterministic fixtures from live provider calls.

- **Install and first use:** install on a clean supported account, launch from
  home, diagnose missing prerequisites, choose a Git repository, create a thread
  and send a first message. The user does not need to find a source checkout.
- **Navigation and input:** complete the workflow without function keys. Verify
  the open-A/highlighted-B action case, archive/restore, multiline paste and draft
  recovery at 48×16, 60×20, 80×24, 89 columns and 120×35, including live resize.
- **Presentation and progress:** capture welcome, running, quiet, approval,
  failure and completion states. Test dark/light terminals, limited color and
  monochrome; confirm provider discovery cannot freeze progress or cancellation.
- **Lifecycle and data:** exercise immediate stop/start, duplicate launch,
  version mismatch and populated-state reopening. Existing projects, events,
  sessions and drafts survive. Active work is not interrupted by routine launch
  or installation, and reconnect/restart never resubmits a prompt.
- **Two physical hosts:** complete ZC-19 below on the release candidate.
  Keep the gate open if either real-host execution or reconnect evidence is
  missing.
- **Supported environments:** run the required suite, build and lifecycle smoke
  checks on Python 3.11 and 3.14, and record actual Linux and macOS installation
  and terminal evidence. An untested platform remains an explicit release gap.

## Carryover: physical two-machine acceptance

**ZC-19 — Verify simultaneous local and remote work on two physical
machines.** Begin this check early, independently of the new remote-management
features. It is a carryover from v1 and a gate for v1.1 sign-off.

Run the terminal client on Fedora with a local daemon and an outbound SSH
connection to the development host. Verify the configured development alias
(`dev` if still available) from Fedora before testing. This arrangement needs no
inbound SSH connection to the laptop. The design's `homelab` and `laptop` names
are examples, not required hostnames. Reachability has to be checked when the
test runs.

Use isolated repositories and run Codex on one physical machine and OpenCode on
the other. While both run, switch conversations, retain drafts and scroll
position, inspect a diff, resolve one background approval and cancel one run
while the other continues. Use an explicitly approval-enabled test thread for
that approval check; ordinary Codex conversations retain YOLO defaults.

Close/reopen the client and interrupt/recover SSH, then verify history, stale
labels, event ordering and no duplicated submissions. Record exact versions and
distinguish paid, explicitly opted-in real turns from provider fixtures. Keep
private addresses and credentials out of the public evidence.

## Additional workspace and remote work

Model controls and attention shipped in v1.1. The daily workflow release adds
staged diagnostics; connection removal/disable and its recovery rules remain
separate future work. These cards retain their original planning identities.

| Card | Improvement | Acceptance outcome |
| --- | --- | --- |
| ZC-12 | Unread results and an attention inbox | Completion, failure and approval needs remain discoverable across machines; server-scoped last-seen markers advance at live tail, while scrolled views preserve later unread output. |
| ZC-13 | Model and supported-settings picker | Search discovered model choices and provider-supported options; retain per-thread overrides and Codex YOLO defaults. |
| ZC-14 | Edit, disable and forget machine connections | Correct aliases/remote executable paths, pause reconnection and forget a connection with draft export/recovery and explicit handling of uncertain sends; remote work continues. |
| ZC-15 | Staged SSH diagnostics | Identify alias, authentication, executable, daemon, protocol and provider failures with concrete next actions and machine-readable results. |

## Future operations and history

| Card | Improvement | Acceptance outcome |
| --- | --- | --- |
| ZC-17 | Optional OS-managed daemon | Explicitly manage user-service startup after login on Linux/macOS; test each platform’s logout/reboot behavior while preserving state and preventing prompt replay. |
| ZC-18 | Full-history search and export | Search beyond the bounded client cache and export a scoped Markdown/JSON transcript with bounded resource use and explicit overwrite behavior. |

## Dependencies and implementation order

The following are prerequisite edges recorded in Helm. Other ordering within a
pass is a preference, so independent work can start without artificial blockers.

| Dependent card | Prerequisites |
| --- | --- |
| ZC-7 | ZC-5, ZC-2, ZC-6 |
| ZC-9 | ZC-3, ZC-4 |
| ZC-11 | ZC-4 |
| ZC-12 | ZC-9 |
| ZC-13 | ZC-6 |
| ZC-15 | ZC-14, ZC-7 |
| ZC-17 | ZC-5, ZC-2, ZC-16 |
| ZC-18 | ZC-10 |

Semantic transcript rendering and machine management can start independently of
the palette. Share one cache-compatibility plan for repository recency, composer
caret, unread markers and connection recovery: use additive defaults or an
explicit atomic migration, with one owner for overlapping cache edits. A version
bump must never discard existing drafts or state.

The physical-host gate has no feature prerequisites. Begin with the current
build and repeat it on the release candidate.

## Implementation boundaries

- UI navigation never owns provider execution. Persist accepted runs and events
  before acknowledging them; retain exact approval scope and request identities.
- Keep Python 3.11+ and standard-library runtime dependencies. Read the
  [Codex](providers-codex.md) and [OpenCode](providers-opencode.md) contracts and
  run provider fixture tests before integration changes.
- Any storage/cache format change must preserve populated data. Upgrade work
  requires transactional migration tests, a verified backup and compatibility
  checks for retained rollback binaries. Reset/recreate and automatic database
  restore are never normal upgrade steps; destructive restore needs explicit
  authorization for the exact backup.
- For implementation, run the unittest suite, `scripts/build.py` and
  `scripts/smoke.py`, plus focused terminal/provider checks for the changed
  behavior. Use `.work/tmp` for local test artifacts. Short CI jobs use `homelab`;
  future long integration/build jobs use `homelab-heavy`.
- Automatic agent orchestration, cross-provider conversation migration, a full
  code editor, automatic PR workflows and fleet-wide updating remain outside
  this roadmap's committed scope. Revisit after the daily workflow is dependable.

## Evidence behind the priorities

- [CLI and startup](../src/zeus_code/cli.py) and
  [daemon shutdown/discovery](../src/zeus_code/daemon.py): asynchronous shutdown
  acknowledgement, manual launch steps and discovery on the polling path.
- [TUI navigation/forms](../src/zeus_code/tui.py) and
  [workspace state](../src/zeus_code/workspace.py): sidebar action targeting,
  hidden narrow-layout focus, cwd defaults, add-only endpoints and cached search.
- [Transcript rendering](../src/zeus_code/transcript.py): flattened presentation
  that can gain semantic layout and consistent styling.
- [Product design](design.md), [architecture](implementation.md) and
  [validation record](validation.md): execution/data invariants, existing
  capabilities and the outstanding physical-host acceptance gap.
