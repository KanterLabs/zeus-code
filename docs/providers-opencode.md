# OpenCode provider protocol

Zeus Code v1 targets the installed OpenCode `1.18.26` server API. Validation was
performed on September 7, 2026 against that binary's OpenAPI document at `/doc`,
plus the current official [server](https://opencode.ai/docs/server/),
[SDK](https://opencode.ai/docs/sdk/), and
[permission](https://opencode.ai/docs/permissions/) documentation. The live
validation covered Basic-authenticated health and model discovery plus one
isolated turn with the connected default `opencode/big-pickle` model. It returned
the requested `ZEUS_CODE_OK`, made no tool call, and changed no file. Resume,
approval, abort, and error behavior are covered by protocol fixtures based on the
installed OpenAPI schemas.

## Process and transport

Each Zeus provider run owns one `opencode serve` child started with
`--hostname 127.0.0.1 --port 0`. A random password is passed only through
`OPENCODE_SERVER_PASSWORD`, with a fixed private username through
`OPENCODE_SERVER_USERNAME`. Every request carries HTTP Basic authentication and
the selected checkout in the `directory` query parameter. The reported URL is
rejected unless it is plain HTTP on a loopback host. OpenCode documents both
the loopback default and the environment-based Basic auth mechanism in its
[server authentication contract](https://opencode.ai/docs/server/#authentication).

The adapter uses Python's asyncio streams for HTTP/1.1, JSON, chunked transfer,
and server-sent events, so no runtime package is required. Zeus launches the
server through its parent-death supervisor, which owns and cleans OpenCode's
separate process group if the daemon exits unexpectedly. The child process is
always stopped when the turn finishes or fails. Cancellation first calls the
session abort endpoint, then terminates the adapter-owned supervised process.

## Sessions and turns

For a new Zeus thread, `POST /session` creates an OpenCode session. The returned
ID is sent through the durable `provider_session` callback and awaited before
any prompt is submitted. For a resumed thread, `GET /session/:id` first verifies
the persisted ID, and that same callback is awaited before the new turn.

The event stream is connected at `GET /event` before the adapter sends
`POST /session/:id/prompt_async`. The asynchronous endpoint returns `204`; turn
completion then comes from `session.idle` or an idle `session.status` event.
`session.error` becomes an actionable provider error. The official
[server API table](https://opencode.ai/docs/server/#sessions) and
[SDK session methods](https://opencode.ai/docs/sdk/#sessions) document session
creation, messages, abort, and event streaming.

All received events are filtered by the exact provider session ID before Zeus
emits anything. A `message.part.delta` maps to `message_delta` only after that
part has been identified as text; reasoning deltas are not forwarded. Completed
text parts map to `message`, and tool parts map their
pending/running/completed/error states to Zeus tool events. Retry status text is
also forwarded. The final `message` and preceding deltas use the same part ID so
consumers reconcile the completed text instead of appending it twice. Text and
tool output within the provider protocol limit is forwarded intact; the daemon
splits it into bounded durable events.

## Permissions

OpenCode permission policy remains user-configured. Its defaults allow most
actions while asking for selected guards; `ask` requests offer `once`, `always`,
or `reject` outcomes as described in the official
[permission guide](https://opencode.ai/docs/permissions/#what-ask-does).

The adapter handles both `permission.asked` and the installed schema's
`permission.v2.asked` event. It preserves the full provider properties under
`details`, recovers the exact bash command from the related tool input when
available, and awaits the Zeus approval callback. Zeus `allow` maps to OpenCode
`once`, and Zeus `reject` maps to `reject`, sent to
`POST /permission/:requestID/reply`. An invalid or missing decision fails the
turn instead of granting access.

## Discovery and authentication

`check()` first resolves the executable, starts the same private server, reads
`GET /global/health`, and discovers `GET /provider`. It returns the installed
version plus models belonging only to provider IDs reported in `connected`.
This performs no model request. If the executable is missing, startup fails, or
no provider is connected, OpenCode is reported unavailable with an actionable
detail; this result is isolated to OpenCode and does not disable other provider
adapters. OpenCode documents credential setup through
[`opencode auth login`](https://opencode.ai/docs/cli/#auth).

## Model capabilities in v1.1

Connected model entries retain provider-reported variant names for the terminal
picker. Zeus does not synthesize a Codex-style reasoning list for OpenCode.
Automatic child-agent records are currently normalized from Codex collaboration
items; OpenCode tool events remain visible through the ordinary transcript.
