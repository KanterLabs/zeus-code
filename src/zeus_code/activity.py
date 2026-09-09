"""Honest, clock-driven activity summaries from persisted provider events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


ACTIVE_STATES = {"running", "awaiting_approval"}
TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
ACTIVITY_PHASES = {"starting", "thinking", "responding", "working"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"
    if total >= 60:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total}s"


def permission_label(thread: dict[str, Any]) -> str:
    if thread.get("provider") != "codex":
        return ""
    settings = thread.get("settings") or {}
    approval = settings.get("approval_policy", settings.get("approvalPolicy"))
    sandbox = settings.get("sandbox")
    if approval in (None, "never") and sandbox in (None, "danger-full-access", "dangerFullAccess"):
        return "YOLO"
    return "Custom permissions"


@dataclass(frozen=True)
class RunActivity:
    state: str
    label: str
    detail: str = ""
    elapsed: float | None = None
    quiet_for: float | None = None
    stale: bool = False

    @property
    def active(self) -> bool:
        return self.state == "running" and not self.stale

    def marker(self, clock: float) -> str:
        if self.stale:
            return "○"
        if self.active:
            return SPINNER[int(clock * 8) % len(SPINNER)]
        return {"completed": "✓", "failed": "×", "cancelled": "■",
                "interrupted": "!", "awaiting_approval": "!"}.get(self.state, "○")

    def headline(self, clock: float) -> str:
        text = f"{self.marker(clock)} {self.label}"
        if self.elapsed is not None:
            text += f" · {duration(self.elapsed)}"
        if self.stale:
            text += " · offline"
        return text

    @property
    def last_activity(self) -> str:
        if self.quiet_for is None or self.state not in ACTIVE_STATES:
            return ""
        return f"Last activity {duration(self.quiet_for)} ago"


def summarize_activity(
    thread: dict[str, Any], events: list[dict[str, Any]], run: dict[str, Any], *,
    now: float, stale: bool = False,
) -> RunActivity:
    """Use only the latest run; a local animation never asserts remote liveness."""
    run_id = run.get("id") or next((event.get("run_id") for event in reversed(events) if event.get("run_id")), None)
    current = [event for event in events if event.get("run_id") == run_id] if run_id else []
    state = str(run.get("state") or thread.get("state") or "idle")
    started = timestamp(run.get("created_at"))
    ended = timestamp(run.get("ended_at"))
    last_event = timestamp(run.get("last_event_at"))
    phase, detail = "starting", "Waiting for the provider to start"
    tools: dict[str, dict[str, Any]] = {}
    error = str(run.get("error") or "")
    for event in current:
        kind = event.get("kind")
        data = event.get("data") or {}
        event_time = timestamp(event.get("created_at"))
        if event_time is not None:
            last_event = max(last_event, event_time) if last_event is not None else event_time
        if kind == "run_state":
            if data.get("state") == "running" and started is None:
                started = event_time
            if data.get("state") in TERMINAL_STATES:
                ended = event_time if event_time is not None else ended
                error = str(data.get("error") or error)
        elif kind == "provider_session":
            phase, detail = "working", "Waiting for the next provider update"
        elif kind == "status":
            if data.get("phase") in ACTIVITY_PHASES:
                phase = data["phase"]
                detail = str(data.get("text") or "")
            elif data.get("text"):
                detail = str(data["text"])
        elif kind in {"message_delta", "message"} and data.get("role", "assistant") in {"assistant", "agent"}:
            phase = "responding" if kind == "message_delta" else "working"
            detail = "Streaming the response" if kind == "message_delta" else "Waiting for the next provider update"
        elif kind == "tool":
            item_id = str(data.get("item_id") or event.get("seq"))
            previous = tools.get(item_id, {})
            # Output deltas carry generic titles; retain the real command name.
            updated = {**previous, **data}
            if data.get("delta") and previous.get("title"):
                updated["title"] = previous["title"]
            tools.pop(item_id, None)
            tools[item_id] = updated
            phase, detail = "working", "Waiting for the next provider update"

    elapsed = None
    if started is not None:
        endpoint = ended if state in TERMINAL_STATES else (last_event if stale else now)
        if endpoint is not None:
            elapsed = max(0, endpoint - started)
    quiet_for = max(0, now - last_event) if last_event is not None else None
    if state in TERMINAL_STATES:
        label = {"completed": "Completed", "failed": "Failed", "cancelled": "Stopped", "interrupted": "Interrupted"}[state]
        detail = error or {"completed": "Ready for your next message", "cancelled": "Your draft and history are saved",
                           "interrupted": "Run ended; send a follow-up to continue", "failed": "See the error above; edit your prompt to retry"}[state]
    elif state == "awaiting_approval":
        label, detail = "Waiting for approval", "F6 opens the exact request"
    elif state == "running":
        active_tools = [tool for tool in tools.values() if tool.get("status") in {"running", "pending", "inProgress"}]
        if active_tools:
            tool = active_tools[-1]
            label = {"commandExecution": "Running command", "fileChange": "Editing files", "webSearch": "Searching the web",
                     "mcpToolCall": "Running tool", "collabAgentToolCall": "Working with agents"}.get(tool.get("tool_type"), "Running tool")
            detail = str(tool.get("title") or tool.get("name") or "Tool in progress")
            if len(active_tools) > 1:
                detail += f" (+{len(active_tools) - 1} more active)"
        else:
            label = {"starting": f"Starting {str(thread.get('provider') or 'agent').capitalize()}",
                     "thinking": "Thinking", "responding": "Writing response", "working": "Working"}[phase]
    else:
        label, detail = "Ready", "Send a message to start working"
    if stale:
        detail = (f"Last known: {label.lower()}. Reconnecting; work may continue on the machine."
                  if state in ACTIVE_STATES else f"Last known: {label.lower()}. {detail}")
    elif state == "running" and quiet_for is not None and quiet_for >= 120:
        # Silence alone cannot prove a provider is stuck. Preserve the actual
        # run state and describe the observation, without fabricating failure.
        detail = f"{label}: {detail}. No update for {duration(quiet_for)}; check connection or wait."
        label = "No recent activity"
    return RunActivity(state, label, detail, elapsed, quiet_for, stale)
