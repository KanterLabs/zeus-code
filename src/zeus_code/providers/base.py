"""Provider contract. Adapters never own application persistence."""
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass
class RunContext:
    thread_id: str
    run_id: str
    cwd: str
    session_id: str | None
    model: str | None
    settings: dict[str, Any]
    emit: Callable[[str, dict[str, Any]], Awaitable[None]]
    approve: Callable[[dict[str, Any]], Awaitable[str]]


class ProviderError(RuntimeError):
    """An actionable provider failure safe to show in the conversation."""


class Provider(Protocol):
    async def run(self, context: RunContext, prompt: str) -> None:
        """Run one turn; cancellation must abort only this run and clean up children.

        emit kinds: provider_session {session_id}, message_delta {item_id, text},
        message {role, text, item_id?}, tool {item_id, title, status, text?},
        status {text}. Never emit app state transitions; daemon owns these.
        approve receives {provider_request_id, kind, command, cwd, details?},
        blocks until decision, and returns 'allow' or 'reject'. Approval kind
        may be command/file_change/permission. Preserve exact command/details.
        Resume context.session_id; emit provider_session BEFORE any new turn.
        Return on success; raise ProviderError on failure, CancelledError on cancel.
        """
        ...

    async def check(self) -> dict[str, Any]:
        """Return {available, version?, detail, models?: [{id, name}]}.

        Missing authentication should be explained; never print credentials.
        Do not run a paid model turn. Discovery errors must not disable peers.
        """
        ...
