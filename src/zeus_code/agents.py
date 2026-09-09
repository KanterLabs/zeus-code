"""Bounded summaries of automatic subagents from persisted provider events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


MAX_AGENT_EVENTS = 500
MAX_AGENTS = 100
MAX_AGENT_LABEL = 240
MAX_AGENT_RESULT = 2_000

_STATE_ALIASES = {
    "pendinginit": "running",
    "pending": "running",
    "starting": "running",
    "inprogress": "running",
    "working": "running",
    "running": "running",
    "complete": "completed",
    "completed": "completed",
    "succeeded": "completed",
    "errored": "failed",
    "error": "failed",
    "failed": "failed",
    "notfound": "failed",
    "interrupted": "cancelled",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "shutdown": "cancelled",
}


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _state(value: Any) -> str:
    text = _text(value, 80) or "unknown"
    compact = "".join(character for character in text.casefold() if character.isalnum())
    return _STATE_ALIASES.get(compact, text)


def _event_sequence(event: dict[str, Any], position: int) -> tuple[int, int]:
    try:
        return int(event.get("seq", 0)), position
    except (TypeError, ValueError):
        return 0, position


def _epoch_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # The provider event schema defines numeric lifecycle values as epoch
        # milliseconds, including deliberately tiny timestamps in fixtures.
        return float(value) / 1_000
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _agent_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    if len(value) <= MAX_AGENTS:
        return [entry for entry in value if isinstance(entry, dict)]
    # Preserve both established parents and the newest children when a
    # malformed or unusually large provider snapshot exceeds the UI bound.
    first = MAX_AGENTS // 2
    bounded = value[:first] + value[-(MAX_AGENTS - first) :]
    return [entry for entry in bounded if isinstance(entry, dict)]


def summarize_agents(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge provider agent snapshots into a stable, bounded public summary.

    Providers attach an ``agents`` list to tool-event data. Each stable agent
    ID is merged independently, so concurrent siblings and nested children do
    not overwrite one another. Sequence ordering ignores delayed older events,
    while a newer event may legitimately resume an existing child. Observed
    ``started_at`` values are retained (numeric values are epoch milliseconds),
    completed ``elapsed`` values are expressed in seconds, and ``run_id`` names
    the parent run of the latest snapshot that reported each agent. Missing
    source run IDs remain explicit ``None`` rather than being inferred.
    """
    source = [event for event in events[-MAX_AGENT_EVENTS:] if isinstance(event, dict)]
    source = [event for _, event in sorted(enumerate(source), key=lambda pair: _event_sequence(pair[1], pair[0]))]
    merged: dict[str, dict[str, Any]] = {}
    first_seen = 0
    update_order = 0
    seen_sequences: set[int] = set()

    for event in source:
        try:
            sequence = int(event.get("seq", 0))
        except (TypeError, ValueError):
            sequence = 0
        if sequence > 0:
            if sequence in seen_sequences:
                continue
            seen_sequences.add(sequence)
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        event_time = _text(event.get("created_at"), 80)
        for raw in _agent_entries(data.get("agents")):
            agent_id = _text(raw.get("id"), 512)
            if agent_id is None:
                continue
            record = merged.get(agent_id)
            is_new = record is None
            if record is None:
                record = {
                    "id": agent_id,
                    "parent_id": None,
                    "label": agent_id,
                    "state": "unknown",
                    "result": None,
                    "run_id": None,
                    "_first_seen": first_seen,
                    "_updated_order": update_order,
                    "_known_run_id": None,
                    "_started_at": None,
                    "_finished_at": None,
                }
                first_seen += 1
                merged[agent_id] = record

            event_run_id = _text(event.get("run_id"), 512)
            previous_run_id = record["_known_run_id"]
            run_changed = (
                previous_run_id is not None
                and event_run_id is not None
                and previous_run_id != event_run_id
            )
            # A provider may reuse a stable child ID in a later parent run.
            # Known cross-run updates start a fresh visible lifecycle; carrying
            # the old timer or result forward would claim facts not reported
            # for the new run.
            if run_changed:
                record["state"] = "unknown"
                record["result"] = None
                record.pop("elapsed", None)
                record.pop("started_at", None)
                record["_started_at"] = None
                record["_finished_at"] = None
            record["run_id"] = event_run_id
            if event_run_id is not None:
                record["_known_run_id"] = event_run_id

            if "parent_id" in raw:
                record["parent_id"] = _text(raw.get("parent_id"), 512)
            label = _text(raw.get("label"), MAX_AGENT_LABEL)
            if label is not None:
                record["label"] = label

            previous_state = record["state"]
            if "state" in raw:
                incoming_state = _state(raw.get("state"))
                if incoming_state != "unknown" or is_new:
                    record["state"] = incoming_state
                if record["state"] == "running" and previous_state != "running":
                    # Codex may reuse a child through sendInput/resumeAgent.
                    # A newer event starts a fresh visible lifecycle.
                    record["result"] = None
                    record.pop("elapsed", None)
                    record.pop("started_at", None)
                    record["_started_at"] = None
                    record["_finished_at"] = None

            if "result" in raw or "message" in raw:
                record["result"] = _text(raw.get("result", raw.get("message")), MAX_AGENT_RESULT)

            model = _text(raw.get("model"), 160)
            if model is not None:
                record["model"] = model
            updated_at = _text(raw.get("updated_at"), 80) or event_time
            if updated_at is not None:
                record["updated_at"] = updated_at

            started_value = raw["started_at"] if "started_at" in raw else raw.get("startedAtMs")
            started_at = _epoch_seconds(started_value)
            finished_at = _epoch_seconds(raw.get("finished_at", raw.get("completedAtMs")))
            if started_at is not None:
                record["_started_at"] = started_at
                record["started_at"] = (
                    started_value
                    if isinstance(started_value, (int, float)) and not isinstance(started_value, bool)
                    else _text(started_value, 80)
                )
            if finished_at is not None:
                record["_finished_at"] = finished_at

            elapsed = raw.get("elapsed")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed >= 0:
                record["elapsed"] = elapsed
            elif (
                record["_started_at"] is not None
                and record["_finished_at"] is not None
                and record["_finished_at"] >= record["_started_at"]
            ):
                record["elapsed"] = record["_finished_at"] - record["_started_at"]
            update_order += 1
            record["_updated_order"] = update_order

    records = list(merged.values())
    if len(records) > MAX_AGENTS:
        records = sorted(
            records,
            key=lambda record: (
                record["state"] != "running",
                -record["_updated_order"],
            ),
        )[:MAX_AGENTS]
    records.sort(key=lambda record: record["_first_seen"])
    for record in records:
        for internal in ("_first_seen", "_updated_order", "_known_run_id", "_started_at", "_finished_at"):
            record.pop(internal, None)
    return records


__all__ = ["MAX_AGENT_EVENTS", "MAX_AGENTS", "summarize_agents"]
