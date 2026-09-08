# Codex provider

Zeus Code integrates Codex through `codex app-server --stdio`. The app-server
protocol is Codex's structured interface for authentication, conversation
history, approvals, and streamed events. It uses JSON-RPC-style messages without
the `jsonrpc` field and newline-delimited JSON on stdio. See the official
[Codex app-server documentation](https://developers.openai.com/codex/app-server).

## Validated baseline

This adapter was implemented and tested on September 7, 2026 against the locally
installed `codex-cli 0.153.4`. Its `app-server --help` accepts `--stdio`, and its
generated stable JSON schemas match the methods used here. OpenAI describes the
app-server command as experimental, so a future Codex CLI release may require an
adapter update even though Zeus stays on the stable, non-experimental protocol
surface.

The validation covered two levels:

- Protocol-fixture tests exercise start, resume, streaming, command and file
  approvals, permission grants, failures, cancellation, discovery, pagination,
  and unsupported server requests through a real subprocess boundary.
- A local authenticated smoke test started a fresh `gpt-5.6-luna` thread in an
  isolated `.work` directory. It returned `ZEUS_CODE_OK`, streamed message
  deltas, emitted the persistent provider thread ID, requested no approvals, and
  completed normally. No tool or file-change action was requested.

The authoritative protocol sequence is documented under [Getting
started](https://developers.openai.com/codex/app-server#protocol): initialize the
connection, start or resume a thread, start a turn, and consume notifications
until `turn/completed`. The same reference documents
[`turn/interrupt`](https://developers.openai.com/codex/app-server#interrupt-a-turn)
and the server-initiated [approval
requests](https://developers.openai.com/codex/app-server#approvals).

## Lifecycle and persistence

Every Zeus turn starts its own app-server subprocess and performs this handshake:

1. Send `initialize`, then the `initialized` notification.
2. Call `thread/start` for a new Zeus conversation or `thread/resume` for a saved
   one.
3. Persist and emit the returned `thread.id` before calling `turn/start`.
4. Translate notifications until the matching `turn/completed` event.
5. Close and reap the owned app-server process.

Zeus emits a structured activity phase while this work happens: `starting` while
starting or resuming app-server, `thinking` while Codex reasons, `working` for
tool-item activity, and `responding` when Codex writes its answer. These events
contain short display text and item IDs where Codex supplies them; reasoning
content remains private.

The app-server runs behind Zeus's supervised subprocess wrapper. If the daemon
dies without running normal cleanup, the wrapper detects its missing parent and
terminates the provider's complete process group before exiting.

Zeus deliberately persists `thread.id`, even though the generic database field
is named `session_id`. Codex requires the thread ID for `thread/resume`.
`thread.sessionId` identifies the root shared by a fork tree and is not always a
resumable thread ID. OpenAI documents that distinction in [Start or resume a
thread](https://developers.openai.com/codex/app-server#start-or-resume-a-thread).

On task cancellation, Zeus sends `turn/interrupt` with the exact Codex thread and
turn IDs, waits briefly for its response, then closes the app-server and, if
needed, terminates only the process group created for that run. Other Zeus runs
have separate processes and are unaffected.

## Settings

`RunContext.model` selects the Codex model. The adapter recognizes these optional
`RunContext.settings` entries:

| Zeus setting | App-server field | Notes |
| --- | --- | --- |
| `approval_policy` | `approvalPolicy` | `onRequest`/`on_request` map to `on-request`; `unlessTrusted` maps to `untrusted` |
| `sandbox` | `sandbox` | Camel-case aliases map to `read-only`, `workspace-write`, or `danger-full-access` |
| `personality` | `personality` | Passed at thread start or resume |
| `service_tier` | `serviceTier` | Passed at thread start or resume |
| `reasoning_effort` | `effort` | Passed to `turn/start` |
| `reasoning_summary` | `summary` | Passed to `turn/start` |

Camel-case protocol names are accepted for the approval policy and service tier,
and `effort`/`summary` are accepted as aliases. Unknown settings remain daemon
data and are not forwarded into Codex configuration.

When `approval_policy` and `sandbox` are absent or null, Zeus sends
`approvalPolicy: "never"` and `sandbox: "danger-full-access"` on both
`thread/start` and `thread/resume`. This is the app-server protocol equivalent of
running interactive Codex with
[`codex --yolo`](https://learn.chatgpt.com/docs/developer-commands?surface=cli):
Codex does not ask for approvals and runs without a sandbox. Explicit settings
still win, so a thread configured for `on-request`, `untrusted`, `read-only`, or
`workspace-write` keeps those restrictions. Zeus does not modify the user's
global Codex configuration.

## Events and approvals

Agent-message deltas become `message_delta`; the authoritative completed agent
item becomes `message`. Command output, file-patch updates, MCP progress, and item
lifecycle notifications become `tool` events with a protocol-derived
`tool_type`. Warnings, plan updates, and the generic `starting`, `thinking`,
`working`, and `responding` phases become `status` events. Raw reasoning is not
exposed; reasoning lifecycle items produce only a `Thinking` status and item ID.

The adapter handles all binary approval requests represented by the Zeus v1
provider contract:

- `item/commandExecution/requestApproval`: `allow` becomes `accept`; `reject`
  becomes `decline`.
- `item/fileChange/requestApproval`: the same `accept`/`decline` mapping.
- `item/permissions/requestApproval`: `allow` returns exactly the requested
  permission subset with turn scope; `reject` returns an empty permission set.
- Legacy `execCommandApproval` and `applyPatchApproval`: `allow` becomes
  `approved`; `reject` returns the legacy structured denial.

The approval callback receives the original app-server request ID, command or
permission data, working directory, and a copy of the complete request params.
Zeus v1 supports allow-once and reject. It does not expose session-wide approval,
exec-policy amendments, or persistent network-policy decisions.

App-server can also ask open-ended questions, request MCP elicitation, refresh
host-owned tokens, or invoke client-owned dynamic tools. Those requests cannot be
represented by Zeus v1's binary approval callback. The adapter replies with a
protocol error and fails the run explicitly, so the provider never waits forever
or invents an answer.

## Discovery and limits

`check()` performs no paid model turn. It finds the executable, reads
`codex --version`, initializes app-server, calls `account/read` without refreshing
tokens, and paginates visible models through `model/list`. The official reference
documents both [model discovery](https://developers.openai.com/codex/app-server#list-models-modellist)
and [authentication state](https://developers.openai.com/codex/app-server#1-check-auth-state).
Returned health details contain only the authentication mode, never account
email, tokens, or credentials.

For resource safety, one JSONL message is limited to 1 MiB, retained stderr to
the last 32 KiB, events queued before a request response to 4 MiB and 4,096
messages, and discovery to ten pages of 100 visible models. Message and tool text
passes to the daemon without adapter truncation; the daemon persists long text in
ordered 8,192-character continuation chunks. Display titles and diagnostic status
summaries are bounded to keep them usable. An oversized or malformed message,
premature process exit, timeout, unsupported request, or failed turn raises an
actionable `ProviderError`. Complete conversation history and provider session
IDs remain the daemon's persistence responsibility.

## Executable discovery over SSH

Readiness checks and runs use the same executable resolution: the configured
executable on PATH first, then common user-local npm/Volta and Homebrew locations
for the default `codex` command. Explicit executable paths are never replaced.
This handles noninteractive SSH sessions that omit `~/.local/bin` without editing
shell startup files. A known unavailable provider leaves the draft intact and
reports setup failure before accepting a run.

## v1.1 model and child-agent visibility

Model discovery retains `supportedReasoningEfforts`, `defaultReasoningEffort`
and `isDefault` from the installed app-server's `model/list` response. The client
uses these capabilities for its searchable picker. An unset model remains
**Provider default**; discovery does not establish the actual model of a run.

`collabAgentToolCall` item notifications carry normalized `data.agents`
snapshots. Receiver thread IDs and `agentsStates` keys establish child identity;
a spawn's sender establishes its parent. Spawn prompts provide task labels and
provider state messages provide public results. Unknown states remain visible.
Observed lifecycle timestamps use epoch milliseconds. No private reasoning or
synthetic resolved model is included. Tests cover concurrent children, nesting,
completion, interruption and later reuse, with persisted replay after restart.
