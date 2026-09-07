# Zeus Code — product design



Zeus Code

Product goals, interaction design, and MVP scope

Shane Kanterman  |  September 7, 2026  |  Draft 0.3

Product goal

Combine the terminal coding experience the user enjoys in OpenCode with the effortless project and conversation switching they value in T3 Code. One keyboard-driven workspace manages independent Codex and OpenCode conversations across local and remote development machines.

The problem

Changing projects currently interrupts the workflow: navigating terminals, changing directories, locating sessions, and checking which agent needs attention. The product should make switching conversations a view change while work continues independently.

Core promise

Start a task on one project, switch to another project or machine, and return with the conversation, draft, scroll position, and agent progress intact.

Day-one requirements

Codex and OpenCode support; a persistent project/thread sidebar; concurrent agent execution; SSH-connected dev boxes; approvals and diff review; saved sessions and reconnect recovery.

Identity and design status

Product: Zeus Code. Repository: https://github.com/KanterLabs/zeus-code. Named after Zeus, a black English lop rabbit. Mascot direction: a black rabbit with long floppy ears, paired with charcoal and cyan. The mockups use Zeus Code branding and only homelab and laptop. Written requirements resolve image inconsistencies. Provider protocols and implementation technology remain to be validated.



Interaction model

One navigation hierarchy

Machine → project → threads. Local is a machine entry alongside SSH hosts. Each thread appears once beneath its project. The same repository on two machines is represented as two machine-scoped project instances.

Thread identity

A thread owns its provider, provider session ID, machine, working directory or worktree, branch context, conversation history, draft, and view position. The provider is selected at creation. Moving an existing conversation between providers is outside the MVP.

Independent execution

Each conversation is managed by an independent async task. Async tasks do not require one dedicated operating-system thread each. Changing selection never cancels, pauses, or restarts an agent. Blocking commands use subprocesses or worker execution.

State and attention

Execution states: idle, running, awaiting approval, completed, failed, and cancelled. Connectivity is tracked separately: connected, reconnecting, or disconnected. After disconnection, show last-known task state with a stale indicator until the host confirms current state.

Keyboard and layout proposal

Ctrl+P opens the global thread switcher; Ctrl+N creates a thread; Tab changes focus; Ctrl+D opens diff review; Esc closes an overlay. Enter sends and Alt+Enter inserts a newline where supported; provide a configurable fallback. These are proposed bindings, to be checked across terminal environments.

The sidebar collapses on narrow terminals. Tool calls collapse by default, long output is paginated or virtualized, and new output does not pull users away from older messages they are reading. Status always includes a text label, not color alone.



Mockup A — Main workspace



Figure 1. Main conversation, streamed tool activity, persistent draft, and background attention indicators.

What this screen must accomplish

Keep the selected conversation readable while making other work visible. The header identifies the machine, project, thread, provider, and branch. The composer belongs to this thread and keeps its draft when the user leaves.

Refinements for implementation

Replace the mockup’s duplicated Projects and Conversations sections with the single machine → project → threads tree. Use one consistent status summary. Opening another thread must restore its view immediately from locally available state; remote synchronization follows without blocking navigation.

Acceptance scenario

While Codex is working on Hostlet, type an unsent follow-up. Switch to an OpenCode conversation in Pulse, then return. Codex has continued, the draft is unchanged, and the previous scroll position is restored.



Mockup B — Cross-machine switching



Figure 2. Global thread search with project, host, provider, and execution state.

What this screen must accomplish

Search conversation titles across every registered project and machine. Each result provides enough context to distinguish similarly named threads. Show recent conversations before typing, then filter results without waiting for a network round trip.

Remote development behavior

Reuse SSH host aliases, keys, and jump-host configuration. Register a remote repository by its path. Agents execute on that host using its tools and authentication. A lightweight daemon on each development machine owns its sessions independently of the TUI connection.

Refinements for implementation

Move the detailed Dev Boxes list into a dedicated machine view; retain host and connection indicators in the switcher. Standardize shortcuts to the written proposal. A disconnected host must never imply that its agent has stopped.

Acceptance scenario

Run Codex on homelab and OpenCode on laptop. Disconnect the TUI, reconnect, and recover history and current state without creating duplicate runs. A powered-off host is shown as unavailable, with its last-known state clearly marked.



Mockup C — Review and approvals



Figure 3. Changed files, unified diff, and a pending command approval in the selected remote thread.

What this screen must accomplish

Review changes from the thread’s working directory without copying the repository locally. Show file paths and readable additions/deletions. Approvals identify the exact command, host, and working directory, with clear allow-once and reject actions.

Isolation and control

Offer a dedicated Git worktree when creating a coding thread. Independent async tasks do not isolate files. If threads share a checkout, disclose that fact and avoid presenting checkout-wide changes as belonging exclusively to one conversation.

Refinements for implementation

Remove the speculative Code / Plan / Execute / Review top bar. Approval actions apply only to the displayed request and remain separate from diff acceptance or committing. Cancelling a run preserves its history and leaves other conversations running.

Acceptance scenario

An inactive thread requests approval. Its sidebar label and the global attention count update. Opening it reveals the request; rejecting it is delivered to that session only. Review remains available afterward.



MVP delivery and validation

Required user outcomes

Navigate: add machines and projects, create named provider-specific threads, search globally, rename and archive threads, and restore drafts and history.

Execute: run both supported providers concurrently, stream messages and tool events, expose supported model settings, respond to approvals, cancel individual runs, and resume supported sessions.

Work remotely: detect missing agent setup, run on the selected host, preserve jobs through TUI disconnects, synchronize missed events, and distinguish connection failures from execution failures.

Review: inspect changed files and diffs, create optional worktrees, and show accurate repository and branch context.

Architecture direction

The TUI handles input and renders cached state. A daemon on each machine owns provider sessions, persistent event history, pending approvals, and process lifecycle. Provider adapters translate supported structured APIs into shared events and commands. SSH transports communication to remote daemons.

Use stable thread/run/request identifiers and event sequence numbers for reconnect replay and deduplication. Retain output durably while bounding in-memory buffers. Daemon crashes and host reboots require explicit recovery handling; survival of an SSH disconnect does not guarantee survival of a reboot.

Release gate

Demonstrate two projects on two machines, with one Codex and one OpenCode task running concurrently. Switch with a saved draft, handle a background approval, review a diff, cancel one run without affecting the other, and reconnect without missing persisted events or duplicating work.

Deferred and unresolved

Defer automatic multi-agent orchestration, cross-provider context migration, full code editing, and automated PR workflows. Before implementation, validate both providers’ supported integration contracts, authentication behavior, cancellation and resume semantics; choose the TUI stack and persistence format; define daemon installation, version negotiation, and resource limits.

This document consolidates the product discussion and its three generated mockups. It specifies intended behavior rather than claiming an implemented or benchmarked integration.



Simple server operation

One command to serve

Proposed CLI: zeus-code serve. Start a Zeus Code server on homelab or laptop without a configuration file, container stack, database setup, or web dashboard. The server owns agent sessions and stores its state in a standard user data directory.

Connect from the laptop

Proposed CLI: zeus-code connect homelab. Reuse the existing SSH host alias and credentials to reach the server. The local TUI should discover a local server automatically. The laptop can also serve its own local projects; both machines appear in the same navigation tree.

Clear lifecycle

The default serve command runs in the foreground and prints readiness, state location, and the connection command. zeus-code serve --background is the proposed detached mode for work that must survive closing its launching terminal. Provide status and graceful stop commands. Host reboot recovery is a separate optional service-install capability.

Useful defaults

Use SSH transport for remote access and a local-only endpoint by default. Automatically create persistent storage and detect Codex/OpenCode on the serving machine. Report missing provider setup without preventing use of the other provider. Duplicate startup should report the existing server rather than create competing instances.

Acceptance criteria

On a supported machine with Zeus Code and one authenticated provider installed, start the server with one command, connect from the laptop, register a repository, and launch a thread. Close and reopen the client: the task continues and history returns. A detached server survives its launching terminal closing. Failed startup and connection errors provide a concrete next action.

These commands specify the intended product interface; they are not available commands in an implemented release yet.
