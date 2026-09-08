# Zeus Code

A terminal workspace for independent **Codex** and **OpenCode** conversations
across local and SSH development machines. Named after Zeus, a black English lop
rabbit.

Start a task, switch projects, and come back to its conversation, unsent draft,
view position, and progress. Agents run in a persistent daemon on their machine;
closing the terminal client does not cancel them.

The [roadmap](docs/roadmap.md) prioritizes easier setup, reliable navigation and a
polished terminal UI for v1.1, followed by workspace and remote improvements.

## Start

Requires **Python 3.11+**, Git, and a terminal with curses support on Linux or
macOS. Use WSL on Windows. Install and authenticate Codex, OpenCode, or both on
each machine where tasks will execute. Zeus uses those existing credentials.
It has no third-party Python runtime dependencies, web dashboard, or database
service to configure.

With **Node.js 18+** installed, run from any directory without cloning:

```sh
npx github:KanterLabs/zeus-code
```

The launcher verifies the bundled application and starts the local daemon before
opening the workspace. Python 3.11+ with curses is still required; set
`ZEUS_CODE_PYTHON` to a Python executable if it is not named `python3` or `python`.
Runtime bundles live outside npm's temporary cache, so clearing that cache does
not interrupt the daemon. Existing conversations and settings are preserved.

The npm package name is **`@kanterlabs/zeus-code`**. After its first npm release:

```sh
npx @kanterlabs/zeus-code@latest
# Or install a persistent command:
npm install -g @kanterlabs/zeus-code
zeus-code
```

Use the scoped name: the unscoped `zeus-code` package belongs to another project.
`npx install zeus-code` is not an npm installation command.

From a checkout:

```sh
./zeus-code serve --background
./zeus-code
```

Press **Enter** on the welcome screen, **Ctrl+N**, or click **New thread**.
Choose a repository folder, name the conversation, and select `codex` or
`opencode` with the arrow keys. Press **Ctrl+S** to create it. The repository is
registered automatically and the new thread opens, ready for your first message.
Use **F4** to change its model. Each conversation keeps its provider for its lifetime.

Codex defaults to **YOLO**: full access and no approval prompts, equivalent to
`codex --yolo`. This applies to new and resumed conversations unless the thread
has explicit permission settings. The composer shows `YOLO` or
`Custom permissions`; OpenCode keeps its existing provider settings.

The activity strip above your message shows an animated indicator, elapsed run
time, and the current action: starting, thinking, running a command, editing
files, or writing a response. It shows the command name and time since the last
provider update even while you browse older messages. Completion, failure,
approval and cancellation stop the animation. **Ctrl+X** stops the selected run;
**F7** expands tool output. The sidebar also shows activity in other threads.
Offline machines show their last known state with a frozen timer.

Form defaults are selected: typing replaces them, and **Ctrl+U** clears a field.
**Tab / Shift+Tab** move between fields; **Enter** advances or submits the last
field. Errors stay inside the form so you can correct a path and retry.
You can also register repositories separately with **Ctrl+O** or **F3**.

For an installed command, build the standalone application:

```sh
python3 scripts/build.py
mkdir -p ~/.local/bin
install -m 755 dist/zeus-code.pyz ~/.local/bin/zeus-code
zeus-code serve --background
zeus-code
```

Ensure `~/.local/bin` is in your `PATH`. The built file requires only Python; it
can be copied to another supported machine. Alternatively, install the package
with `python3 -m pip install .` in your chosen Python environment.

## Update

For npm installations, use `npm install -g @kanterlabs/zeus-code@latest`, or run
`npx @kanterlabs/zeus-code@latest` each time. GitHub users can run
`npx github:KanterLabs/zeus-code` again. The npm launcher uses its own bundled
version. Running its `update` command installs a separate standalone command in
`~/.local/bin`; it does not replace files inside the npm package or runtime cache.

With v1.0.3 or newer installed:

```sh
zeus-code update
```

This installs the latest stable GitHub release after verifying its checksums
and version metadata. An installed standalone bundle updates in place. From a
source checkout or Python package installation, it installs the standalone
command into `~/.local/bin`; the checkout remains available for development.
Use `--install-dir DIR` to choose another directory, or `zeus-code update --check`
to check availability without changing files.

Stop the local daemon before installing an update. The updater refuses to
replace the application while the daemon is running. Choose when to stop active
work, then run:

```sh
zeus-code stop
zeus-code update
zeus-code serve --background
```

The previous executable is retained, and an existing Zeus database gets an
integrity-checked SQLite backup before replacement. The updater accepts releases
with the same database schema so the retained binary remains compatible. It
never resets or restores user data. Run the command separately on each machine;
`--host` is not supported for updates.

To get this command from an older source checkout, run these commands **inside
that checkout** once:

```sh
git pull --ff-only
./zeus-code update --install-dir "$HOME/.local/bin"
```

Ensure `~/.local/bin` is in your `PATH`, then use `zeus-code update` for future
releases. Tagged releases publish `zeus-code.pyz`, `release.json` and
`SHA256SUMS` after CI passes.

## Remote work

Install Zeus Code and an authenticated provider on each remote development
machine. On the remote machine, run:

```sh
zeus-code serve --background
```

On your laptop:

```sh
zeus-code connect homelab
```

`homelab` is an example of an existing OpenSSH alias. Zeus reuses SSH keys,
`ProxyJump`, and host verification. `ssh homelab zeus-code --version` should
succeed before connecting; the remote command must be on the noninteractive
SSH shell's `PATH`. Zeus uses batch-mode SSH, so unlock your key with `ssh-agent`
and complete first-time host verification with normal SSH beforehand.

**F2** opens the machine view; add more SSH aliases there. Local and remote
machines share one **machine → project → threads** tree. A repository on two
machines is two distinct project instances. Register remote paths on their
machine; repositories are never copied to the client for review.

Disconnected machines retain their last-known execution state with a **stale**
label. Reconnecting replays persisted sequence numbers and deduplicates events.
A disconnected client does not imply that the remote agent has stopped.

## Keyboard

| Key | Action |
| --- | --- |
| Ctrl+P | Search threads across all cached machines and projects |
| Ctrl+N | Choose a repository and create a named provider thread |
| Ctrl+O / Ctrl+G | Add repository / machines |
| Tab | Switch sidebar/composer focus, or advance a form |
| Enter | Send; activate selection or advance/submit a form |
| Alt+Enter / Ctrl+J | Insert a newline |
| Ctrl+D | Review changed files and unified diff |
| F2 / F3 / F4 | Machines / add project / model settings |
| Ctrl+R / Ctrl+A in sidebar | Rename / archive selected thread |
| F6 | Open pending approval |
| F7 | Expand/collapse tool output |
| F8 | Toggle Enter behavior; use Ctrl+S to send in multiline mode |
| Ctrl+X | Cancel only the selected thread's current run |
| Ctrl+Q | Exit the client; daemon work continues |
| F1 | Keyboard help |
| Esc | Close overlay |

Arrow/Page keys scroll in their focused view. The footer and overlays list
context-specific actions, including archive, history paging, and explicit retry
of an uncertain send. The sidebar collapses in narrow terminals; Ctrl+P remains
available. Terminal emulators may reserve a key; Enter behavior is configurable
and the multiline fallback is saved in the client cache.
The interface supports terminals from 48×16; the sidebar appears from 90 columns.
Mouse clicks work on buttons, workspace entries and form fields when supported
by your terminal.

To update a checkout, exit the client with **Ctrl+Q** and run `git pull --ff-only`.
Version 1.0.2 also changes the daemon's Codex adapter. After active tasks finish,
back up the database, run `./zeus-code stop`, then `./zeus-code serve --background`
and reopen `./zeus-code`. Restart each remote daemon you update as well. History,
provider session IDs and drafts are preserved; no database migration is needed.

## Approvals, diffs, and isolation

Approval views display the host, working directory, exact command or requested
permission, and stable request ID. **Allow once** and **reject** apply only to
the displayed request. Reviewing a diff never grants an approval or commits a
change. Unsupported provider requests fail explicitly instead of inventing an
answer or granting a broader permission.

Choose a dedicated Git worktree when creating a coding thread to isolate its
files. This requires a repository with at least one commit. Threads sharing a
checkout see checkout-wide changes and may affect each other's files; Zeus
labels that review as shared. Archiving a thread keeps its history and worktree.

Diffs include tracked, staged, unstaged, untracked, binary, and renamed files.
Use the arrow keys to select a file, Enter to load its patch, and Tab to switch
between files and patch. Approval requests support arrow and Page-key scrolling.
Large diffs are bounded and clearly marked. Large untracked files whose counts
cannot be inspected safely have unknown counts; select a specific file for its
bounded patch. Git's configured external diff/text-conversion programs are not
executed during review.

## Lifecycle and command line

`zeus-code serve` runs in the foreground and prints readiness and state location.
`serve --background` detaches, redirects logs, and survives the launching terminal.
Repeated startup detects the existing daemon. `status` shows state; `stop`
gracefully cancels this daemon's active runs and preserves history.

```sh
zeus-code status
zeus-code doctor
zeus-code project add /path/to/repo --name my-project
zeus-code thread create PROJECT_ID 'Fix the tests' --provider codex --worktree
zeus-code send THREAD_ID 'Investigate the test failure'
zeus-code events --thread THREAD_ID --after 0
zeus-code diff THREAD_ID --path src/example.py
zeus-code approve REQUEST_ID reject
zeus-code cancel THREAD_ID
zeus-code stop
```

RPC commands return JSON. Add `--host homelab` to target a remote daemon. A send
accepts `--request-id UUID`; use the **same ID and prompt** when retrying an
uncertain submission. The CLI prints its request ID on a transport failure.
An accepted request is durable before acknowledgement, so retrying cannot create
a duplicate run. Never substitute a new ID to recover an uncertain result.

## State, backups, and recovery

By default, server state lives in `$XDG_DATA_HOME/zeus-code` (usually
`~/.local/share/zeus-code`) and the client cache in
`$XDG_STATE_HOME/zeus-code/client.json` (usually `~/.local/state`). Use
`--data-dir PATH` or `ZEUS_CODE_DATA_DIR` for an alternate local daemon, and
`ZEUS_CODE_CLIENT_STATE` for an alternate client cache. State directories and
files are restricted to the current user. Remote access uses SSH to relay the
local Unix socket; Zeus opens no externally accessible TCP listener.

The daemon uses SQLite WAL with transactional state/event writes. Back up a live
or stopped server using:

```sh
zeus-code backup /safe/location/zeus-backup.sqlite3
```

The backup uses SQLite's consistent backup API, checks integrity, and refuses to
overwrite an existing target. Back up before upgrades and retain the old binary.
Migrations are transactional; a binary refuses an unsupported newer schema.
There is no automatic reset or restore. Provider-owned sessions remain in the
providers' standard data directories, so include those directories in your own
machine backups if you need provider resume after machine loss.

Normal client/SSH disconnects preserve active jobs. A supervisor stops provider
process groups if their owning daemon dies. A daemon crash or machine
reboot is different: previously active runs become explicitly interrupted,
pending approvals become stale, and persisted history and session IDs survive.
Zeus never resubmits their prompts automatically. Send an explicit follow-up to
resume the provider session after reviewing the interrupted result. A foreground
daemon ends when its controlling terminal sends it a shutdown signal; use
background mode for persistence. Automatic startup after reboot through an OS
service is optional future work.

Resource defaults: eight concurrent runs per daemon, 64 client connections,
1 MiB RPC/provider event limits, approximately 512 KiB event pages, 256 KiB diff
views, and bounded client history windows with older-page loading. A 1 GiB server
history budget refuses further output/runs with an actionable error while keeping
existing data; set `ZEUS_CODE_MAX_STORAGE_MB` before startup to change it. No
automatic history deletion occurs.

## Development and verification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/build.py
python3 scripts/smoke.py
python3 scripts/smoke.py --executable dist/zeus-code.pyz
```

The deterministic suite uses real sockets, SQLite databases, Git repositories,
subprocess fixtures, and fake provider conversations. It needs no provider
credentials or paid model calls. The separate live provider check is opt-in.
GitHub Actions uses the organization's `homelab` runners.

```sh
python3 scripts/live_check.py --live --codex-model gpt-5.6-sol
```

This runs four tiny real turns (one fresh and one resumed per provider) through
two local daemons. It consumes the configured providers' allowances and does not
substitute for checking SSH connectivity on your actual machines.

See [validation evidence](docs/validation.md), the [architecture and
protocol](docs/implementation.md), and the validated integration contracts for
[Codex](docs/providers-codex.md) and [OpenCode](docs/providers-opencode.md).
The v1 scope excludes automatic agent orchestration, cross-provider conversation
migration, a full code editor, and automatic PR workflows.
