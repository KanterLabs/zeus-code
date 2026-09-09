"""Pure presentation data for automatic-agent activity in the terminal UI.

The provider summary is already normalized by :mod:`zeus_code.agents`.  This
module only orders and formats that public data.  It deliberately does not
inspect provider transcripts or expose fields other than the public task,
state, timing, model, and result supplied by the summary.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
import time
from typing import Any, Mapping, Sequence
import unicodedata


_TERMINAL_COLLAPSED_STATES = {"completed", "cancelled"}
_KNOWN_STATES = {"running", "completed", "failed", "cancelled", "unknown"}
_MAX_RENDER_AGENTS = 100


@dataclass(frozen=True)
class AgentCard:
    """One safe, display-ready automatic agent."""

    agent_id: str
    parent_id: str | None
    task: str
    state: str
    status: str
    elapsed_seconds: float | None
    elapsed: str | None
    model: str | None
    result: str | None
    updated_at: str | None
    run_id: str | None
    run_current: bool | None
    historical: bool
    run_unknown: bool
    depth: int = 0


@dataclass(frozen=True)
class PanelLine:
    """One terminal row plus the metadata needed for styling and mouse input."""

    text: str
    kind: str
    agent_id: str | None = None
    selectable_index: int | None = None
    depth: int = 0
    state: str = ""
    selected: bool = False


@dataclass(frozen=True)
class AgentPanelView:
    """A bounded snapshot suitable for either the live panel or detail view."""

    rows: tuple[PanelLine, ...]
    selectable: tuple[AgentCard, ...]
    selected_index: int
    selected_id: str | None
    running_count: int
    reported_running_count: int
    attention_count: int
    finished_count: int
    unknown_count: int
    other_count: int
    collapsed_count: int
    hidden_count: int
    stale: bool
    historical: bool

    @property
    def lines(self) -> tuple[str, ...]:
        """Return just the rendered text for simple inline integrations."""

        return tuple(row.text for row in self.rows)


def _safe_inline(value: Any) -> str:
    output: list[str] = []
    for character in str(value):
        code = ord(character)
        if character == "\t":
            output.append("    ")
        elif character in "\r\n":
            output.append("↵")
        elif code < 32 or code == 127:
            output.append("^" + chr((code ^ 64) & 127))
        else:
            output.append(character)
    return "".join(output).strip()


def _safe_multiline(value: Any) -> str:
    raw = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(_safe_inline(line) for line in raw.split("\n")).strip()


def _cell_width(character: str) -> int:
    if unicodedata.combining(character) or unicodedata.category(character) in {"Mn", "Me", "Cf"}:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _cells(text: str) -> int:
    return sum(_cell_width(character) for character in text)


def _clip(text: str, width: int) -> str:
    used = 0
    for index, character in enumerate(text):
        used += _cell_width(character)
        if used > width:
            return text[:index]
    return text


def _fit(text: str, width: int) -> str:
    width = max(1, width)
    if _cells(text) <= width:
        return text
    if width == 1:
        return "…"
    return _clip(text, width - 1) + "…"


def _take_line(text: str, width: int) -> tuple[str, str]:
    """Take one cell-bounded line, preferring a word boundary."""

    if _cells(text) <= width:
        return text.rstrip(), ""
    used = 0
    cutoff = 0
    for index, character in enumerate(text):
        character_cells = _cell_width(character)
        if used + character_cells > width:
            break
        used += character_cells
        cutoff = index + 1
    if cutoff == 0:
        return "…", text[1:].lstrip()
    boundary = text.rfind(" ", 0, cutoff + 1)
    if boundary > 0:
        return text[:boundary].rstrip(), text[boundary + 1 :].lstrip()
    return text[:cutoff].rstrip(), text[cutoff:].lstrip()


def _wrap_prefixed(prefix: str, value: str, width: int) -> list[str]:
    width = max(1, width)
    prefix = _clip(prefix, width)
    continuation = " " * min(_cells(prefix), max(0, width - 1))
    logical_lines = value.split("\n") or [""]
    output: list[str] = []
    first = True
    for logical in logical_lines:
        current_prefix = prefix if first else continuation
        remaining = logical.strip()
        if not remaining:
            output.append(_fit(current_prefix.rstrip(), width))
            first = False
            continue
        while remaining:
            available = max(1, width - _cells(current_prefix))
            part, remaining = _take_line(remaining, available)
            output.append(_fit(current_prefix + part, width))
            current_prefix = continuation
            first = False
    return output or [_fit(prefix.rstrip(), width)]


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3_600:
        return f"{total // 3_600}h {(total % 3_600) // 60:02d}m"
    if total >= 60:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total}s"


def _epoch_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 1_000 if math.isfinite(numeric) else None
    if not isinstance(value, str):
        return None
    try:
        numeric = float(value)
        return numeric / 1_000 if math.isfinite(numeric) else None
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric >= 0 and math.isfinite(numeric):
            return numeric
    return None


def _state_key(state: str) -> str:
    return state.casefold().strip() or "unknown"


def _status(state: str, *, stale: bool, historical: bool, run_unknown: bool) -> str:
    key = _state_key(state)
    if key == "running":
        if stale:
            status = "Last known running · offline"
            return status + (" · run unknown" if run_unknown else "")
        if run_unknown:
            return "Last reported running · run unknown"
        return "Last reported running" if historical else "Running"
    if key == "completed":
        return "Completed"
    if key == "failed":
        return "Failed · needs attention"
    if key == "cancelled":
        return "Cancelled"
    if key == "unknown":
        return "Status unknown"
    return state


def _elapsed(
    agent: Mapping[str, Any], *, state: str, now: float, frozen: bool,
) -> float | None:
    reported = _number(agent.get("elapsed"))
    if _state_key(state) != "running":
        return reported
    started_at = _epoch_seconds(agent.get("started_at"))
    if started_at is None:
        return reported
    endpoint = _epoch_seconds(agent.get("updated_at")) if frozen else now
    if endpoint is None or endpoint < started_at:
        return reported
    return endpoint - started_at


def _prepare(
    summary: Sequence[Mapping[str, Any]], *, now: float, stale: bool, historical: bool,
    current_run_id: str | None,
) -> list[AgentCard]:
    cards: list[AgentCard] = []
    seen: set[str] = set()
    for raw in summary[:_MAX_RENDER_AGENTS]:
        if not isinstance(raw, Mapping):
            continue
        agent_id = str(raw.get("id") or "").strip()
        if not agent_id or agent_id in seen:
            continue
        seen.add(agent_id)
        parent_value = raw.get("parent_id")
        parent_id = str(parent_value).strip() if parent_value is not None else None
        parent_id = parent_id or None
        state = _safe_inline(raw.get("state") or "unknown") or "unknown"
        task = _safe_inline(raw.get("label") or agent_id) or _safe_inline(agent_id) or "Agent"
        model = _safe_inline(raw["model"]) if raw.get("model") is not None else None
        model = model or None
        result = _safe_multiline(raw["result"]) if raw.get("result") is not None else None
        result = result or None
        updated_at = _safe_inline(raw["updated_at"]) if raw.get("updated_at") is not None else None
        updated_at = updated_at or None
        raw_run_id = raw.get("run_id")
        run_id = str(raw_run_id).strip() if raw_run_id is not None else None
        run_id = run_id or None
        run_current = None if current_run_id is None or run_id is None else run_id == current_run_id
        run_unknown = current_run_id is not None and run_id is None
        card_historical = historical or run_current is False
        elapsed_seconds = _elapsed(
            raw,
            state=state,
            now=now,
            frozen=stale or card_historical or run_unknown,
        )
        cards.append(AgentCard(
            agent_id=agent_id,
            parent_id=parent_id,
            task=task,
            state=state,
            status=_status(
                state,
                stale=stale,
                historical=card_historical,
                run_unknown=run_unknown,
            ),
            elapsed_seconds=elapsed_seconds,
            elapsed=_duration(elapsed_seconds) if elapsed_seconds is not None else None,
            model=model,
            result=result,
            updated_at=updated_at,
            run_id=run_id,
            run_current=run_current,
            historical=card_historical,
            run_unknown=run_unknown,
        ))
    return cards


def _ordered(cards: Sequence[AgentCard]) -> list[AgentCard]:
    by_id = {card.agent_id: card for card in cards}
    positions = {card.agent_id: index for index, card in enumerate(cards)}

    def valid_parent(card: AgentCard) -> str | None:
        parent_id = card.parent_id
        visited = {card.agent_id}
        while parent_id in by_id:
            if parent_id in visited:
                return None
            visited.add(parent_id)
            parent_id = by_id[parent_id].parent_id
        return card.parent_id if card.parent_id in by_id else None

    parents = {card.agent_id: valid_parent(card) for card in cards}
    children: dict[str | None, list[AgentCard]] = {None: []}
    for card in cards:
        children.setdefault(parents[card.agent_id], []).append(card)

    branch_running: dict[str, bool] = {}

    def has_running(card: AgentCard) -> bool:
        cached = branch_running.get(card.agent_id)
        if cached is not None:
            return cached
        value = _state_key(card.state) == "running" or any(has_running(child) for child in children.get(card.agent_id, ()))
        branch_running[card.agent_id] = value
        return value

    def rank(card: AgentCard) -> tuple[int, int]:
        if _state_key(card.state) == "running":
            priority = 0
        elif has_running(card):
            priority = 1
        else:
            priority = 2
        return priority, positions[card.agent_id]

    output: list[AgentCard] = []

    def visit(card: AgentCard, depth: int) -> None:
        output.append(replace(card, depth=depth))
        for child in sorted(children.get(card.agent_id, ()), key=rank):
            visit(child, depth + 1)

    for root in sorted(children[None], key=rank):
        visit(root, 0)
    return output


def _summary_line(
    *, width: int, stale: bool, running: int, reported_running: int, attention: int,
    finished: int, unknown: int, other: int, total: int,
) -> str:
    full_parts = []
    compact_parts = []
    tight_parts = []
    if running:
        full_parts.append(f"{running} running")
        compact_parts.append(f"{running} run")
        tight_parts.append(f"{running}r")
    if reported_running:
        full_parts.append(f"{reported_running} last reported running")
        compact_parts.append(f"{reported_running} last reported")
        tight_parts.append(f"{reported_running} reported")
    if attention:
        label = "needs attention" if attention == 1 else "need attention"
        full_parts.append(f"{attention} {label}")
        compact_parts.append(f"{attention} alert")
        tight_parts.append(f"{attention}!")
    if finished:
        full_parts.append(f"{finished} finished")
        compact_parts.append(f"{finished} done")
        tight_parts.append(f"{finished}✓")
    if unknown:
        full_parts.append(f"{unknown} unknown")
        compact_parts.append(f"{unknown} unknown")
        tight_parts.append(f"{unknown}?")
    if other:
        full_parts.append(f"{other} other status")
        compact_parts.append(f"{other} other")
        tight_parts.append(f"{other} other")
    if not full_parts:
        full_parts = compact_parts = tight_parts = [f"{total} reported"]

    availability = " · last known (offline)" if stale else ""
    compact_availability = " · last known/offline" if stale else ""
    tight_availability = " last known" if stale else ""
    variants = (
        "Agents" + availability + " · " + " · ".join(full_parts),
        "Agents" + compact_availability + " · " + " · ".join(compact_parts),
        "Agents" + tight_availability + " · " + " · ".join(tight_parts),
    )
    return next((variant for variant in variants if _cells(variant) <= width), _fit(variants[-1], width))


def _marker(card: AgentCard, *, stale: bool) -> str:
    key = _state_key(card.state)
    if key == "running":
        return "○" if stale or card.historical or card.run_unknown else "●"
    return {"completed": "✓", "failed": "!", "cancelled": "■", "unknown": "?"}.get(key, "?")


def _indent(depth: int) -> str:
    return "  " * max(0, min(depth, 8)) + ("↳ " if depth else "")


def _display_depth(card: AgentCard, visible_ids: set[str], by_id: Mapping[str, AgentCard]) -> int:
    depth = 0
    parent_id = card.parent_id
    visited = {card.agent_id}
    while parent_id in by_id and parent_id not in visited:
        visited.add(parent_id)
        if parent_id in visible_ids:
            depth += 1
        parent_id = by_id[parent_id].parent_id
    return depth


def _card_lines(
    card: AgentCard, *, index: int, selected: bool, width: int, depth: int, stale: bool,
) -> list[PanelLine]:
    indent = _indent(depth)
    focus = "› " if selected else "  "
    task = _fit(f"{focus}{indent}{_marker(card, stale=stale)} {card.task}", width)
    meta_prefix = "  " + indent + "  "
    meta = meta_prefix + card.status
    if card.elapsed:
        meta += " · " + card.elapsed
    if card.model:
        with_model = meta + " · " + card.model
        if _cells(with_model) <= width:
            meta = with_model
    common = {
        "agent_id": card.agent_id,
        "selectable_index": index,
        "depth": depth,
        "state": card.state,
        "selected": selected,
    }
    return [
        PanelLine(task, "task", **common),
        PanelLine(_fit(meta, width), "meta", **common),
    ]


def _detail_lines(
    card: AgentCard, *, index: int, width: int, depth: int,
) -> list[PanelLine]:
    indent = "  " + _indent(depth) + "  "
    fields = [
        ("Task", card.task, "detail"),
        ("State", card.status, "detail"),
        ("Elapsed", card.elapsed or "Not reported", "detail"),
        ("Model", card.model or "Not reported", "detail"),
    ]
    if card.updated_at:
        fields.append(("Last known update" if "offline" in card.status else "Last update", card.updated_at, "detail"))
    fields.append(("Result", card.result or "No public result reported.", "result"))
    output: list[PanelLine] = []
    for label, value, kind in fields:
        for text in _wrap_prefixed(indent + label + ": ", value, width):
            output.append(PanelLine(
                text=text,
                kind=kind,
                agent_id=card.agent_id,
                selectable_index=index,
                depth=depth,
                state=card.state,
                selected=True,
            ))
    return output


def build_agent_panel(
    summary: Sequence[Mapping[str, Any]], *, width: int, selected_index: int = 0,
    now: float | None = None, stale: bool = False, expanded: bool = False,
    max_agents: int = 4, historical: bool = False, current_run_id: str | None = None,
) -> AgentPanelView:
    """Build a safe, stable automatic-agent panel.

    The live view (``expanded=False``) shows running, failed, and uncertain
    agents while keeping completed/cancelled work behind the summary count.
    The expanded view shows the full tree and the selected agent's complete
    public result.  Offline and historical running timers stop at the last
    provider update.  Historical running state is kept as last reported; a
    parent run ending never fabricates a child-agent completion. When a current
    parent run ID is supplied, older or unassociated running observations stay
    frozen behind the live panel summary until opened.
    """

    try:
        width = max(1, int(width))
    except (TypeError, ValueError):
        width = 1
    try:
        clock = time.time() if now is None else float(now)
    except (TypeError, ValueError, OverflowError):
        clock = time.time()
    if not math.isfinite(clock):
        clock = time.time()
    normalized_run_id = str(current_run_id).strip() if current_run_id is not None else None
    normalized_run_id = normalized_run_id or None
    ordered = _ordered(_prepare(
        summary,
        now=clock,
        stale=stale,
        historical=historical,
        current_run_id=normalized_run_id,
    ))
    keys = [_state_key(card.state) for card in ordered]
    running_count = sum(
        key == "running" and not card.historical and not card.run_unknown
        for card, key in zip(ordered, keys)
    )
    reported_running_count = sum(key == "running" for key in keys) - running_count
    attention_count = sum(key == "failed" for key in keys)
    finished_count = sum(key in {"completed", "failed", "cancelled"} for key in keys)
    unknown_count = sum(key == "unknown" for key in keys)
    other_count = sum(key not in _KNOWN_STATES for key in keys)

    if not ordered:
        return AgentPanelView(
            rows=(),
            selectable=(),
            selected_index=0,
            selected_id=None,
            running_count=0,
            reported_running_count=0,
            attention_count=0,
            finished_count=0,
            unknown_count=0,
            other_count=0,
            collapsed_count=0,
            hidden_count=0,
            stale=bool(stale),
            historical=bool(historical),
        )

    priority = [
        card
        for card in ordered
        if _state_key(card.state) not in _TERMINAL_COLLAPSED_STATES
        and not (
            _state_key(card.state) == "running"
            and (card.historical or card.run_unknown)
        )
    ]
    collapsed_count = 0 if expanded else len(ordered) - len(priority)
    if expanded:
        visible = ordered
        hidden_count = 0
    else:
        try:
            limit = min(_MAX_RENDER_AGENTS, max(0, int(max_agents)))
        except (TypeError, ValueError):
            limit = 4
        visible = priority[:limit]
        hidden_count = len(priority) - len(visible)

    if visible:
        try:
            normalized_index = min(max(0, int(selected_index)), len(visible) - 1)
        except (TypeError, ValueError):
            normalized_index = 0
        selected_id = visible[normalized_index].agent_id
    else:
        normalized_index = 0
        selected_id = None

    rows = [PanelLine(
        _summary_line(
            width=width,
            stale=stale,
            running=running_count,
            reported_running=reported_running_count,
            attention=attention_count,
            finished=finished_count,
            unknown=unknown_count,
            other=other_count,
            total=len(ordered),
        ),
        "summary",
        state="offline" if stale else "",
    )]
    visible_ids = {card.agent_id for card in visible}
    by_id = {card.agent_id: card for card in ordered}
    for index, card in enumerate(visible):
        depth = card.depth if expanded else _display_depth(card, visible_ids, by_id)
        selected = index == normalized_index
        rows.extend(_card_lines(
            card,
            index=index,
            selected=selected,
            width=width,
            depth=depth,
            stale=stale,
        ))
        if expanded and selected:
            rows.extend(_detail_lines(card, index=index, width=width, depth=depth))
        rows.append(PanelLine(
            "",
            "spacer",
            agent_id=card.agent_id,
            selectable_index=index,
            depth=depth,
            state=card.state,
            selected=selected,
        ))
    if rows[-1].kind == "spacer":
        rows.pop()
    if hidden_count:
        noun = "agent" if hidden_count == 1 else "agents"
        rows.append(PanelLine(_fit(f"+{hidden_count} more {noun} · open details", width), "overflow"))

    return AgentPanelView(
        rows=tuple(rows),
        selectable=tuple(visible),
        selected_index=normalized_index,
        selected_id=selected_id,
        running_count=running_count,
        reported_running_count=reported_running_count,
        attention_count=attention_count,
        finished_count=finished_count,
        unknown_count=unknown_count,
        other_count=other_count,
        collapsed_count=collapsed_count,
        hidden_count=hidden_count,
        stale=bool(stale),
        historical=bool(historical),
    )


__all__ = ["AgentCard", "AgentPanelView", "PanelLine", "build_agent_panel"]
