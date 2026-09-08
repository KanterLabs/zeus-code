"""Pure, bounded rendering of persisted Zeus conversation events."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any

from .activity import ACTIVITY_PHASES


MAX_RENDER_LINES = 10_000
MAX_RENDER_EVENTS = 20_000
MAX_ENTRY_CHARS = 512 * 1024
OMITTED_MARKER = "[… earlier output omitted …]"


class TranscriptLine(str):
    """String-compatible line carrying its event role for terminal styling."""

    def __new__(cls, text: str, kind: str):
        line = super().__new__(cls, text)
        line.kind = kind
        return line


@dataclass
class _Entry:
    kind: str
    run_id: str | None
    data: dict[str, Any]
    text_parts: list[str] = field(default_factory=list)
    final_message: bool = False


def render_transcript(
    events: list[dict], width: int, expanded_tools: bool = False
) -> list[str]:
    """Render events in chronological order into terminal-safe display lines.

    Stable message, tool, and approval items retain the position of their first
    event while later events update their displayed state.  Work is bounded to
    the newest event window and newest output lines.
    """
    width = max(1, int(width))
    source = events[-MAX_RENDER_EVENTS:]
    input_omitted = len(events) > len(source)

    entries: list[_Entry] = []
    positions: dict[tuple[str, str | None, str], int] = {}

    for raw_event in source:
        if not isinstance(raw_event, dict):
            continue
        kind = str(raw_event.get("kind", "event"))
        lowered = kind.casefold()
        if kind == "provider_session" or "reasoning" in lowered or "thought" in lowered:
            continue
        raw_data = raw_event.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        # Machine phases belong in the live activity display. Keep ordinary
        # provider status messages (warnings, plans, retries) in the transcript.
        if kind == "status" and data.get("phase") in ACTIVITY_PHASES:
            continue
        run_value = raw_event.get("run_id")
        run_id = None if run_value is None else str(run_value)

        if kind in {"message", "message_delta"}:
            _record_message(entries, positions, kind, run_id, data)
        elif kind == "tool" and _stable_id(data.get("item_id")) is not None:
            _record_latest(entries, positions, "tool", run_id, data, "item_id")
        elif kind == "approval" and _stable_id(data.get("id")) is not None:
            _record_approval(entries, positions, run_id, data)
        else:
            entries.append(_Entry(kind, run_id, dict(data)))

    # Build from the tail so both work and retained output stay bounded.  A
    # single very large recent entry cannot force all earlier entries to wrap.
    retained: deque[str] = deque()
    output_omitted = input_omitted
    line_budget = MAX_RENDER_LINES - 1
    for entry in reversed(entries):
        available = line_budget - len(retained)
        if available <= 0:
            output_omitted = True
            break
        lines, truncated = _entry_lines(entry, width, expanded_tools, available)
        if truncated:
            output_omitted = True
        role = str(entry.data.get("role", "assistant")).casefold()
        if role == "agent":
            role = "assistant"
        semantic = role if entry.kind in {"message", "message_delta"} else entry.kind
        if entry.kind == "run_state" and entry.data.get("state") == "failed":
            semantic = "error"
        retained.extendleft(reversed([TranscriptLine(line, semantic) for line in lines]))
        if truncated:
            break

    result = list(retained)
    if output_omitted:
        result.insert(0, _fit_marker(width))
    return result[:MAX_RENDER_LINES]


def _record_message(
    entries: list[_Entry],
    positions: dict[tuple[str, str | None, str], int],
    kind: str,
    run_id: str | None,
    data: dict[str, Any],
) -> None:
    item_id = _stable_id(data.get("item_id"))
    if item_id is None:
        entry = _Entry(kind, run_id, dict(data))
        entry.text_parts.append(_text(data.get("text", "")))
        entries.append(entry)
        return

    key = ("message", run_id, item_id)
    index = positions.get(key)
    if index is None:
        index = len(entries)
        positions[key] = index
        entries.append(_Entry("message", run_id, dict(data)))
    entry = entries[index]
    incoming = _text(data.get("text", ""))

    if kind == "message_delta":
        if not entry.final_message:
            entry.text_parts.append(incoming)
        return

    entry.kind = "message"
    entry.data = dict(data)
    if data.get("continuation") and entry.final_message:
        entry.text_parts.append(incoming)
    else:
        # The provider's completed item contains the authoritative full text.
        entry.text_parts = [incoming]
    entry.final_message = True


def _record_latest(
    entries: list[_Entry],
    positions: dict[tuple[str, str | None, str], int],
    kind: str,
    run_id: str | None,
    data: dict[str, Any],
    id_field: str,
) -> None:
    stable_id = _stable_id(data.get(id_field))
    assert stable_id is not None
    key = (kind, run_id, stable_id)
    index = positions.get(key)
    if index is None:
        positions[key] = len(entries)
        entry = _Entry(kind, run_id, dict(data))
        entry.text_parts = [_text(data.get("text", ""))]
        entries.append(entry)
        return

    entry = entries[index]
    incoming = _text(data.get("text", ""))
    if data.get("delta"):
        entry.text_parts.append(incoming)
        # Stream chunks carry a generic title (for example, "Command").  Keep
        # the title from item/started while accepting live status changes.
        for field_name, value in data.items():
            if field_name not in {"title", "name", "text", "delta", "continuation"}:
                entry.data[field_name] = value
    elif data.get("continuation"):
        entry.text_parts.append(incoming)
        entry.data.update(data)
    else:
        entry.data = dict(data)
        entry.text_parts = [incoming]


def _record_approval(
    entries: list[_Entry],
    positions: dict[tuple[str, str | None, str], int],
    run_id: str | None,
    data: dict[str, Any],
) -> None:
    approval_id = _stable_id(data.get("id"))
    assert approval_id is not None
    key = ("approval", run_id, approval_id)
    index = positions.get(key)
    if index is None:
        positions[key] = len(entries)
        entries.append(_Entry("approval", run_id, dict(data)))
        return
    entry = entries[index]
    payload = entry.data.get("payload")
    entry.data.update(data)
    if "payload" not in data and payload is not None:
        entry.data["payload"] = payload


def _entry_lines(
    entry: _Entry, width: int, expanded_tools: bool, limit: int
) -> tuple[list[str], bool]:
    data = entry.data
    kind = entry.kind

    if kind in {"message", "message_delta"}:
        role = str(data.get("role", "assistant")).casefold()
        if role not in {"user", "assistant", "agent"}:
            return [], False
        prefix = "you: " if role == "user" else "agent: "
        text, clipped = _joined_text(entry.text_parts)
        return _message_wrapped(prefix, text, width, limit, clipped)

    if kind == "tool":
        title = _safe_text(data.get("title") or data.get("name") or "tool")
        status = _safe_text(data.get("status") or "")
        header = f"▸ {title}  {status}".rstrip()
        if not expanded_tools and sum(_cell_width(c) for c in header) > width * 2:
            header = f"▸ {status} · {title}" if status else f"▸ {title}"
            chunks = iter(_wrapped_line_chunks(header.replace("\n", " ↵ "), width))
            lines = [next(chunks, "") for _ in range(min(2, limit))]
            if lines:
                tail = lines[-1]
                while tail and sum(_cell_width(c) for c in tail) >= width:
                    tail = tail[:-1]
                lines[-1] = tail + "…"
            return lines, False
        lines, clipped = _wrapped(header, width, limit, False)
        if expanded_tools and len(lines) < limit:
            detail, detail_clipped = _joined_text(entry.text_parts)
            if detail:
                detail_lines, detail_truncated = _wrapped(
                    "  " + detail, width, limit - len(lines), detail_clipped
                )
                lines.extend(detail_lines)
                clipped = clipped or detail_truncated
        return lines, clipped

    if kind == "approval":
        state = _safe_text(data.get("approval_state") or data.get("state") or "pending")
        decision = data.get("decision")
        if decision:
            state = {"allow": "allowed", "reject": "rejected"}.get(
                decision, _safe_text(decision)
            )
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        command = payload.get("command")
        suffix = f": {_safe_text(command)}" if command else ""
        return _wrapped(f"! approval {state}{suffix}", width, limit, False)

    if kind == "run_state":
        state = _safe_text(data.get("state") or "state unknown")
        error = data.get("error")
        if state == "failed" and error:
            return _wrapped(f"× failed: {_safe_text(error)}", width, limit, False)
        if state in {"running", "starting"}:
            return [], False  # The persistent activity strip owns live phases.
        glyph = {"completed": "✓", "cancelled": "■"}.get(state, "—")
        return _wrapped(f"{glyph} {state}", width, limit, False)

    if kind == "error":
        detail = data.get("message") or data.get("text") or "Unknown error"
        return _wrapped(f"error: {_safe_text(detail)}", width, limit, False)

    if kind == "status":
        return _wrapped(f"— {_safe_text(data.get('text') or 'status')}", width, limit, False)

    summary = data.get("text") or data.get("message") or kind.replace("_", " ")
    return _wrapped(_safe_text(summary), width, limit, False)


def _message_wrapped(prefix: str, text: str, width: int, limit: int, clipped: bool) -> tuple[list[str], bool]:
    """Preserve literal code while giving Markdown lists hanging continuation lines."""
    lines: deque[str] = deque(maxlen=max(0, limit))
    produced = 0
    fenced = False
    for index, logical in enumerate(text.split("\n")):
        expanded = logical.expandtabs(4)
        is_fence = bool(re.match(r"^\s*(```|~~~)", expanded))
        if is_fence:
            fenced = not fenced
        lead = prefix if index == 0 else ""
        marker = None if fenced or is_fence else re.match(r"^(\s*(?:[-*+] |\d+[.)] |#{1,6} |[>] ))", expanded)
        chunks = iter(_wrapped_line_chunks(lead + expanded, width))
        first = next(chunks, "")
        lines.append(first)
        produced += 1
        indent = min(len(lead) + len(marker.group(1)), max(0, width // 2)) if marker else 0
        if marker:
            remainder = (lead + expanded)[len(first):]
            chunks = _wrapped_line_chunks(remainder, max(1, width - indent)) if remainder else iter(())
        for chunk in chunks:
            lines.append(" " * indent + chunk)
            produced += 1
    return list(lines), clipped or produced > limit


def _joined_text(parts: list[str]) -> tuple[str, bool]:
    total = sum(len(part) for part in parts)
    if total <= MAX_ENTRY_CHARS:
        return _safe_text("".join(parts)), False

    remaining = MAX_ENTRY_CHARS
    kept: list[str] = []
    for part in reversed(parts):
        if remaining <= 0:
            break
        if len(part) <= remaining:
            kept.append(part)
            remaining -= len(part)
        else:
            kept.append(part[-remaining:])
            remaining = 0
    kept.reverse()
    return _safe_text("".join(kept)), True


def _wrapped(
    text: str, width: int, limit: int, already_truncated: bool
) -> tuple[list[str], bool]:
    lines: deque[str] = deque(maxlen=max(0, limit))
    truncated = already_truncated
    produced = 0
    for logical in text.split("\n"):
        logical = logical.expandtabs(4)
        for line in _wrapped_line_chunks(logical, width):
            produced += 1
            if len(lines) == limit:
                truncated = True
            lines.append(line)
    if produced > limit:
        truncated = True
    return list(lines), truncated


def _wrapped_line_chunks(text: str, width: int):
    if not text:
        yield ""
        return
    chunk: list[str] = []
    cells = 0
    for character in text:
        cell_width = _cell_width(character)
        if chunk and cell_width and cells + cell_width > width:
            yield "".join(chunk)
            chunk = []
            cells = 0
        chunk.append(character)
        cells += cell_width
    if chunk:
        yield "".join(chunk)


def _cell_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _safe_text(value: Any) -> str:
    text = _text(value)
    if len(text) > MAX_ENTRY_CHARS:
        text = "[… earlier text omitted …]" + text[-MAX_ENTRY_CHARS:]
    safe: list[str] = []
    for character in text:
        if character in {"\n", "\t"}:
            safe.append(character)
            continue
        codepoint = ord(character)
        category = unicodedata.category(character)
        if codepoint < 32:
            safe.append(chr(0x2400 + codepoint))
        elif codepoint == 127:
            safe.append("␡")
        elif category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            safe.append(f"<U+{codepoint:04X}>")
        else:
            safe.append(character)
    return "".join(safe)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _stable_id(value: Any) -> str | None:
    return str(value) if isinstance(value, (str, int)) else None


def _fit_marker(width: int) -> str:
    if len(OMITTED_MARKER) <= width:
        return OMITTED_MARKER
    compact = "[omitted]"
    return compact[:width]
