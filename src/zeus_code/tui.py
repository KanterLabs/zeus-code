"""The production curses interface for Zeus Code."""

from __future__ import annotations

import asyncio
import curses
import json
import os
import sys
import textwrap
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .activity import ACTIVE_STATES, RunActivity, duration, permission_label, summarize_activity, timestamp
from .agents import summarize_agents
from .themes import THEMES, color_pairs
from .transcript import render_transcript
from .ui_forms import Form
from .workspace import Workspace


RABBIT = "ZEUS CODE"
STATE_GLYPHS = {
    "idle": "· idle",
    "running": "▶ running",
    "awaiting_approval": "! approval",
    "completed": "✓ completed",
    "failed": "× failed",
    "cancelled": "■ cancelled",
}
PROVIDER_NAMES = {"codex": "Codex", "opencode": "OpenCode"}


@dataclass(frozen=True)
class CommandAction:
    """One command-palette row with its resolved target and availability."""

    key: str
    label: str
    target: str
    run: Callable[[], None]
    shortcut: str = ""
    enabled: bool = True
    reason: str = ""
    keywords: str = ""


def command_palette_results(actions: list[CommandAction], query: str = "") -> list[CommandAction]:
    """Filter actions without hiding unavailable commands or their reasons."""
    words = query.casefold().split()
    if not words:
        return actions
    return [
        action for action in actions
        if all(
            word in " ".join((action.label, action.target, action.shortcut, action.reason, action.keywords)).casefold()
            for word in words
        )
    ]


def model_picker_results(
    workspace: Workspace,
    query: str = "",
    *,
    machine_id: str | None = None,
    provider_name: str | None = None,
    current_model: str | None = None,
) -> list[dict[str, Any]]:
    """Return Provider default plus the full discovered model list.

    A configured custom model remains selectable even when discovery does not
    know it.  Discovery state is presentation metadata only; no model is ever
    inferred for the provider-default row.
    """
    machine_id = machine_id or workspace.selected_machine_id
    machine = workspace.machines[machine_id]
    thread = workspace.selected_thread if machine_id == workspace.selected_machine_id else None
    provider_name = str(provider_name or (thread or {}).get("provider") or "codex")
    provider = machine.get("providers", {}).get(provider_name, {})
    available = provider.get("available") is not False
    cached = bool(provider.get("cached"))
    provider_status = str(provider.get("status") or "")
    unavailable = provider_status == "unavailable" or (not available and provider_status != "checking")
    state_labels = (["cached"] if cached else []) + (["checking"] if provider_status == "checking" else []) + (["provider unavailable"] if unavailable else [])
    suffix = "  ·  " + " · ".join(state_labels) if state_labels else ""
    rows: list[dict[str, Any]] = [{
        "id": None,
        "name": "Provider default",
        "label": "Provider default" + suffix,
        "provider": provider_name,
        "reasoning_efforts": [],
        "variants": [],
        "custom": False,
        "cached": cached,
        "available": available,
    }]
    seen: set[str] = set()
    for raw in provider.get("models") or []:
        model = raw if isinstance(raw, dict) else {"id": str(raw), "name": str(raw)}
        model_id = str(model.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        name = str(model.get("name") or model_id)
        capability_labels: list[str] = []
        reasoning = [str(value) for value in model.get("reasoning_efforts", []) if str(value)]
        variants = [str(value) for value in model.get("variants", []) if str(value)]
        if reasoning:
            capability_labels.append("reasoning " + "/".join(reasoning))
        if variants:
            capability_labels.append("variants " + "/".join(variants))
        identity = name if name == model_id else f"{name}  ·  {model_id}"
        capability = "  ·  " + " · ".join(capability_labels) if capability_labels else ""
        rows.append({
            "id": model_id,
            "name": name,
            "label": identity + capability + suffix,
            "provider": provider_name,
            "reasoning_efforts": reasoning,
            "variants": variants,
            "custom": False,
            "cached": cached,
            "available": available,
            "is_default": bool(model.get("is_default")),
            "default_reasoning_effort": model.get("default_reasoning_effort"),
        })
    current_model = str(current_model or "").strip() or None
    if current_model and current_model not in seen:
        rows.insert(1, {
            "id": current_model,
            "name": current_model,
            "label": f"{current_model}  ·  current custom model" + suffix,
            "provider": provider_name,
            "reasoning_efforts": [],
            "variants": [],
            "custom": True,
            "cached": cached,
            "available": available,
        })
    words = query.casefold().split()
    if words:
        rows = [row for row in rows if all(word in str(row["label"]).casefold() for word in words)]
    return rows


def model_indicator(
    thread: dict[str, Any], provider: dict[str, Any] | None = None, *, compact: bool = False,
) -> str:
    """Describe configured model settings without resolving provider default."""
    provider_name = str(thread.get("provider") or "provider")
    configured = str(thread.get("model") or "").strip()
    model_label = configured or "Provider default"
    if configured and provider and not compact:
        for raw in provider.get("models") or []:
            if isinstance(raw, dict) and str(raw.get("id") or "") == configured:
                name = str(raw.get("name") or configured)
                model_label = name if name == configured else f"{name} ({configured})"
                break
    settings = thread.get("settings") if isinstance(thread.get("settings"), dict) else {}
    qualifiers = [str(settings[key]) for key in ("reasoning_effort", "variant") if settings.get(key)]
    return " · ".join([PROVIDER_NAMES.get(provider_name.casefold(), provider_name), model_label, *qualifiers])


def _thread_attention(workspace: Workspace, machine_id: str, thread_id: str) -> int:
    unread = getattr(workspace, "unread_count", None)
    count = int(unread(machine_id, thread_id)) if callable(unread) else 0
    approvals = sum(1 for approval in workspace.approvals(machine_id) if approval.get("thread_id") == thread_id)
    return max(count, approvals)


def execution_label(state: str | None, *, stale: bool = False) -> str:
    label = STATE_GLYPHS.get(state or "idle", f"· {state or 'idle'}")
    return f"{label} (stale)" if stale else label


def wrap_text(text: str, width: int) -> list[str]:
    width = max(1, width)
    lines: list[str] = []
    for logical in str(text).splitlines() or [""]:
        lines.extend(textwrap.wrap(logical, width=width, replace_whitespace=False, drop_whitespace=False) or [""])
    return lines


def event_lines(event: dict[str, Any], width: int, *, expanded_tools: bool = False) -> list[str]:
    """Convert one shared event to compact readable lines (tool details collapsed)."""
    kind = str(event.get("kind", "event"))
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if kind == "message":
        prefix = "you" if data.get("role") == "user" else "agent"
        text = data.get("text", "")
        return wrap_text(f"{prefix}: {text}", width)
    if kind == "message_delta":
        return wrap_text(str(data.get("text", "")), width)
    if kind == "tool":
        title = data.get("title") or data.get("name") or "tool"
        status = data.get("status", "")
        marker = "▾" if expanded_tools else "▸"
        lines = [f"{marker} {title}  {status}".rstrip()[:width]]
        if expanded_tools and data.get("text"):
            lines.extend(wrap_text(str(data["text"]), max(1, width - 2)))
        return lines
    if kind in {"run_started", "run_finished", "run_state", "state", "status"}:
        text = data.get("text") or data.get("state") or kind.replace("_", " ")
        return [f"— {text}"[:width]]
    if kind == "error":
        return wrap_text(f"error: {data.get('message') or data.get('text') or data}", width)
    summary = data.get("text") or data.get("message") or kind.replace("_", " ")
    return wrap_text(str(summary), width)


def conversation_lines(events: list[dict[str, Any]], width: int, *, expanded_tools: bool = False) -> list[str]:
    """Render reconciled, continuation-aware provider output."""
    return render_transcript(events, width, expanded_tools)


def visible_conversation_lines(
    events: list[dict[str, Any]], width: int, height: int, view: dict[str, Any], *, expanded_tools: bool = False,
) -> list[str]:
    """Return a stable viewport, pinned to an event high-water while browsing."""
    scroll = max(0, int(view.get("scroll", 0)))
    anchor = view.get("anchor_seq") if scroll else None
    if anchor is not None:
        events = [event for event in events if int(event.get("seq", 0)) <= int(anchor)]
    lines = conversation_lines(events, width, expanded_tools=expanded_tools)
    end = max(0, len(lines) - scroll)
    return lines[max(0, end - max(1, height)):end]


def safe_terminal_text(value: Any) -> str:
    """Make arbitrary daemon/user text safe for one curses output row."""
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
    return "".join(output)


def character_width(character: str) -> int:
    if unicodedata.combining(character) or unicodedata.category(character) in {"Mn", "Me", "Cf"}:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def clip_cells(text: str, columns: int) -> str:
    used = 0
    for index, character in enumerate(text):
        used += character_width(character)
        if used > columns:
            return text[:index]
    return text


def text_cells(text: str) -> int:
    return sum(character_width(character) for character in text)


def ellipsize_cells(text: str, columns: int) -> str:
    """Fit text in terminal cells and make truncation explicit."""
    if columns <= 0:
        return ""
    if text_cells(text) <= columns:
        return text
    if columns == 1:
        return "…"
    return clip_cells(text, columns - 1) + "…"


def text_view(text: str, cursor: int, columns: int) -> tuple[str, int]:
    """Keep the insertion caret visible using terminal cells, not code points."""
    cursor = min(max(0, cursor), len(text))
    column = sum(character_width(character) for character in text[:cursor])
    start = 0
    while column >= max(1, columns) and start < cursor:
        column -= character_width(text[start])
        start += 1
    return clip_cells(text[start:], columns), column


@dataclass(frozen=True)
class TreeRow:
    kind: str
    machine_id: str
    project_id: str | None
    thread_id: str | None
    text: str
    state: str | None = None
    stale: bool = False
    attention: int = 0


def build_tree_rows(workspace: Workspace) -> list[TreeRow]:
    rows: list[TreeRow] = []
    for machine_id, machine in workspace.machines.items():
        alias = str(machine.get("alias", machine_id))
        connection = machine.get("connection", "disconnected")
        threads = workspace.threads(machine_id)
        machine_attention = sum(
            _thread_attention(workspace, machine_id, str(thread.get("id")))
            for thread in threads
        )
        rows.append(TreeRow("machine", machine_id, None, None, f"{alias} [{connection}]", attention=machine_attention))
        by_project: dict[str, list[dict[str, Any]]] = {}
        for thread in threads:
            by_project.setdefault(str(thread.get("project_id")), []).append(thread)
        for project in workspace.projects(machine_id):
            project_id = str(project.get("id"))
            selected = (
                machine_id == workspace.selected_machine_id
                and project_id == workspace.state.get("selected_project")
            )
            if not selected and not by_project.get(project_id):
                continue
            rows.append(TreeRow("project", machine_id, project_id, None, f"  {project.get('name', project_id)}"))
            for thread in sorted(by_project.get(project_id, []), key=lambda item: str(item.get("updated_at", "")), reverse=True):
                thread_id = str(thread.get("id"))
                attention = _thread_attention(workspace, machine_id, thread_id)
                rows.append(
                    TreeRow(
                        "thread", machine_id, project_id, thread_id,
                        f"    {thread.get('title', thread_id)}", str(thread.get("state", "idle")),
                        bool(machine.get("stale")), attention,
                    )
                )
    return rows


def project_picker_results(
    workspace: Workspace, query: str = "", *, machine_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return every registered project on one machine, filtered by name or path."""
    machine_id = machine_id or workspace.selected_machine_id
    words = query.casefold().split()
    selected_id = workspace.state.get("selected_project") if machine_id == workspace.selected_machine_id else None
    results: list[dict[str, Any]] = []
    for project in workspace.projects(machine_id):
        name = str(project.get("name") or project.get("id") or "Project")
        path = str(project.get("path") or "")
        if words and not all(word in f"{name} {path}".casefold() for word in words):
            continue
        results.append({
            "machine_id": machine_id,
            "project": project,
            "label": f"{name}  ·  {path}" if path else name,
        })
    return sorted(
        results,
        key=lambda result: (
            0 if result["project"].get("id") == selected_id else 1,
            str(result["project"].get("name") or "").casefold(),
            str(result["project"].get("path") or "").casefold(),
        ),
    )


class TUIApplication:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.running = True
        self.focus = "composer"
        self.tree_index = 0
        self.composer = workspace.thread_view().get("draft", "")
        self.cursor = len(self.composer)
        self.overlay: str | None = None
        self.form: Form | None = None
        self.search_query = ""
        self.search_index = 0
        self.diff: dict[str, Any] | None = None
        self.diff_files: list[dict[str, Any]] = []
        self.diff_focus = "files"
        self.diff_file_index = 0
        self.diff_file_scroll = 0
        self.diff_scroll = 0
        self.diff_machine_id: str | None = None
        self.diff_thread_id: str | None = None
        self.approval_choice: tuple[str, dict[str, Any]] | None = None
        self.approval_scroll = 0
        self.approval_line_count = 0
        self.expanded_tools = False
        self.expanded_agents: set[tuple[str, str]] = set()
        self.agent_scroll = 0
        self._screen_size = (24, 80)
        self.model_context: dict[str, Any] | None = None
        self.model_focus = "models"
        self.model_selected: dict[str, Any] | None = None
        self.model_setting_index = 0
        self._new_thread_model_settings: dict[str, Any] = {}
        self._color_enabled = False
        self.status = "Starting connections…"
        self.tasks: set[asyncio.Task[Any]] = set()
        self._last_escape = False
        self._form_machine_id: str | None = None
        self._form_selection: tuple[Any, ...] | None = None
        self._mouse_targets: list[tuple[int, int, int, int, Callable[[], None]]] = []
        self._cursor_position: tuple[int, int] | None = None
        self._sending: set[tuple[str, str]] = set()

    async def run(self, screen: Any) -> None:
        curses.raw()
        curses.nonl()
        screen.keypad(True)
        screen.timeout(40)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self._init_colors()
        try:
            curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)
            curses.mouseinterval(0)
            curses.set_escdelay(25)
        except (curses.error, AttributeError):
            pass
        self.workspace.start_polling()
        try:
            while self.running:
                self.workspace.cache.save_if_due(self.workspace.state)
                self._refresh_startup_status()
                self.draw(screen)
                try:
                    key = screen.get_wch()
                except curses.error:
                    key = None
                if key is not None:
                    self.handle_key(key)
                await asyncio.sleep(0)
        finally:
            for task in self.tasks:
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.workspace.close()

    def _refresh_startup_status(self) -> None:
        if self.status != "Starting connections…":
            return
        machines = list(self.workspace.machines.values())
        if any(machine.get("connection") == "connected" for machine in machines):
            self.status = "Ready"
        elif machines and all(machine.get("last_error") for machine in machines):
            selected = self.workspace.selected_machine
            if selected.get("host") is None:
                self.status = "Local server unavailable · run zeus-code serve --background"
            else:
                self.status = f"{selected.get('alias')} unavailable · check SSH and zeus-code bridge"

    def _init_colors(self) -> None:
        theme = str(self.workspace.state.get("settings", {}).get("theme") or "dark")
        self._color_enabled = False
        if theme == "monochrome" or not curses.has_colors():
            return
        curses.start_color()
        selected_theme = theme
        if theme == "terminal":
            try:
                curses.use_default_colors()
            except curses.error:
                selected_theme = "dark"
        pairs = color_pairs(selected_theme, curses.COLORS)
        for pair, (foreground_color, background_color) in pairs.items():
            curses.init_pair(pair, foreground_color, background_color)
        self._color_enabled = bool(pairs)

    def color(self, pair: int) -> int:
        return curses.color_pair(pair) if self._color_enabled else 0

    @staticmethod
    def put(screen: Any, y: int, x: int, text: str, attr: int = 0, width: int | None = None) -> None:
        height, columns = screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= columns:
            return
        maximum = max(0, columns - x - 1)
        if width is not None:
            maximum = min(maximum, max(0, width))
        try:
            screen.addstr(y, x, clip_cells(safe_terminal_text(text), maximum), attr)
        except curses.error:
            pass

    def _fill(self, screen: Any, y: int, x: int, width: int, height: int, attr: int) -> None:
        for row in range(y, y + height):
            self.put(screen, row, x, " " * max(0, width), attr, width)

    def _button(self, screen: Any, y: int, x: int, label: str, action: Callable[[], None], *, primary: bool = False) -> None:
        text = f"  {label}  "
        self.put(screen, y, x, text, self.color(2 if primary else 7) | curses.A_BOLD)
        self._mouse_targets.append((y, x, 1, len(text), action))

    def _selection(self) -> tuple[Any, ...]:
        return (self.workspace.selected_machine_id, self.workspace.state.get("selected_project"), self.workspace.state.get("selected_thread"))

    def _attention_items(self) -> list[dict[str, Any]]:
        items = getattr(self.workspace, "attention_items", None)
        if callable(items):
            return list(items())
        fallback: list[dict[str, Any]] = []
        for machine_id, machine in self.workspace.machines.items():
            projects = {str(project.get("id")): project for project in self.workspace.projects(machine_id)}
            threads = {str(thread.get("id")): thread for thread in self.workspace.threads(machine_id)}
            for approval in self.workspace.approvals(machine_id):
                thread_id = str(approval.get("thread_id") or "")
                thread = threads.get(thread_id, {})
                project = projects.get(str(thread.get("project_id")), {})
                fallback.append({
                    "machine_id": machine_id,
                    "thread_id": thread_id,
                    "title": thread.get("title") or thread_id,
                    "project_name": project.get("name") or "Project",
                    "machine_name": machine.get("alias") or machine_id,
                    "state": "awaiting_approval",
                    "unread_count": 1,
                    "needs_approval": True,
                    "stale": bool(machine.get("stale")),
                })
        return fallback

    def _attention_count(self) -> int:
        return sum(max(1, int(item.get("unread_count") or 0)) for item in self._attention_items())

    def draw(self, screen: Any) -> None:
        screen.bkgd(" ", self.color(6))
        screen.erase()
        self._mouse_targets = []
        self._cursor_position = None
        height, width = screen.getmaxyx()
        self._screen_size = (height, width)
        if height < 16 or width < 48:
            self.put(screen, 1, 2, "ZEUS CODE", self.color(1) | curses.A_BOLD)
            self.put(screen, 3, 2, "Resize to at least 48 columns × 16 rows.", self.color(10))
            self.put(screen, 5, 2, "Ctrl+Q exits; your daemon keeps running.", self.color(10))
            screen.refresh()
            return
        sidebar_width = min(32, max(25, width // 4)) if width >= 90 else 0
        self._draw_header(screen, width)
        if sidebar_width:
            self._draw_sidebar(screen, 4, sidebar_width, height - 7)
        self._draw_conversation(screen, 4, sidebar_width, width, height)
        self._draw_footer(screen, height - 1, width)
        if self.overlay:
            for row in range(height):
                try:
                    screen.chgat(row, 0, width - 1, self.color(9))
                except curses.error:
                    pass
            self._draw_overlay(screen, height, width)
        try:
            curses.curs_set(1 if self._cursor_position else 0)
            if self._cursor_position:
                screen.move(*self._cursor_position)
        except curses.error:
            pass
        screen.refresh()

    def _draw_header(self, screen: Any, width: int) -> None:
        machine = self.workspace.selected_machine
        project = self.workspace.selected_project or {}
        thread = self.workspace.selected_thread or {}
        self.put(screen, 1, 2, "(/)  " + RABBIT, self.color(1) | curses.A_BOLD)
        self.put(screen, 2, 2, "A little space to build.", self.color(10))
        if width >= 90:
            context = str(thread.get("title") or project.get("name") or "Your workspace")
            self.put(screen, 1, 34, context, curses.A_BOLD, width - 61)
            details = [str(machine.get("alias", "local"))]
            if project:
                details.append(str(project.get("name")))
            if thread:
                details.extend([str(thread.get("provider")), str(thread.get("branch") or ""), str(thread.get("state", "idle"))])
                details.append(permission_label(thread))
            self.put(screen, 2, 34, " / ".join(filter(None, details)), self.color(10), width - 40)
        elif project or thread:
            details = [str(machine.get("alias", "local")), str(project.get("name") or ""), str(thread.get("title") or "")]
            if thread:
                details.extend([str(thread.get("provider") or ""), str(thread.get("branch") or ""), str(thread.get("state", "idle"))])
                details.append(permission_label(thread))
            self.put(screen, 2, 2, " " * (width - 4), self.color(6), width - 4)
            self.put(screen, 2, 2, " / ".join(filter(None, details)), self.color(10), width - 4)
        connection = str(machine.get("connection", "disconnected"))
        connection_x = max(24, width - len(connection) - 4)
        self.put(screen, 1, connection_x, "● " + connection, self.color(5 if connection == "connected" else 3))
        attention = self._attention_count()
        if attention:
            label = f"! {attention} attention"
            attention_x = connection_x - len(label) - 3
            if attention_x >= 20:
                self.put(screen, 1, attention_x, label, self.color(3) | curses.A_BOLD, len(label))
                self._mouse_targets.append((1, attention_x, 1, len(label), self._open_attention))
        self.put(screen, 3, 1, "─" * (width - 3), self.color(9))

    def _draw_sidebar(self, screen: Any, top: int, width: int, height: int) -> None:
        self._fill(screen, top, 1, width - 1, height, self.color(11))
        compact = height < 13
        self.put(screen, top if compact else top + 1, 3, "WORKSPACE", self.color(8) | curses.A_BOLD)
        self._button(screen, top + (2 if compact else 3), 3, "+ New thread", self._new_thread_form, primary=True)
        rows = build_tree_rows(self.workspace)
        if self.focus != "sidebar":
            self.tree_index = next((index for index, row in enumerate(rows)
                                    if row.thread_id == self.workspace.state.get("selected_thread") and row.thread_id
                                    and row.machine_id == self.workspace.selected_machine_id), self.tree_index)
        self.tree_index = max(0, min(self.tree_index, len(rows) - 1))
        # A thread gets its own second line, so titles never compete with status.
        y = top + (4 if compact else 6)
        end = top + height - (2 if compact else 3)
        capacity = max(1, end - y)
        start = 0
        while start < self.tree_index and sum(2 if row.kind == "thread" else 1 for row in rows[start:self.tree_index + 1]) > capacity:
            start += 1
        for index, row in enumerate(rows[start:], start):
            size = 2 if row.kind == "thread" else 1
            if y + size > end:
                break
            selected = row.thread_id and row.thread_id == self.workspace.state.get("selected_thread") and row.machine_id == self.workspace.selected_machine_id
            active = selected or (self.focus == "sidebar" and index == self.tree_index)
            attr = self.color(13 if active else 11)
            self._fill(screen, y, 2, width - 3, size, attr)
            if row.kind == "machine":
                machine = self.workspace.machines[row.machine_id]
                mark = "●" if machine.get("connection") == "connected" else "○"
                badge = f"  !{row.attention}" if row.attention else ""
                self.put(screen, y, 3, f"{mark} {machine.get('alias', 'local')}{badge}", self.color(14 if active else 12) | curses.A_BOLD, width - 5)
            elif row.kind == "project":
                self.put(screen, y, 4, "▾ " + row.text.strip(), attr | curses.A_BOLD, width - 6)
            else:
                thread = next((item for item in self.workspace.threads(row.machine_id) if item.get("id") == row.thread_id), {})
                self.put(screen, y, 5, row.text.strip(), attr | (curses.A_BOLD if active else 0), width - 7)
                activity = self._thread_activity(thread, row.machine_id)
                phase = {"Running command": "command", "Running tool": "tool", "Editing files": "editing",
                         "Searching the web": "search", "Working with agents": "agents", "Writing response": "replying",
                         "Waiting for approval": "approval", "Starting Codex": "starting", "Starting Opencode": "starting",
                         "Completed": "done", "Interrupted": "paused"}.get(activity.label, activity.label.lower())
                attention = f" · !{row.attention}" if row.attention else ""
                label = activity.marker(time.monotonic()) + " " + str(thread.get("provider", "")) + " · " + ("offline" if activity.stale else phase) + attention
                self.put(screen, y + 1, 5, label, self.color(14 if active else (12 if row.state in ACTIVE_STATES else 8)), width - 7)
            self._mouse_targets.append((y, 2, size, width - 3, lambda i=index: self._open_tree_row(i)))
            y += size
        if not self.workspace.projects():
            self.put(screen, min(y + 1, end), 4, "No repositories yet", self.color(8), width - 6)
        self.put(screen, top + height - 2, 3, "Open project   Ctrl+O", self.color(12), width - 5)
        self._mouse_targets.append((top + height - 2, 2, 1, width - 3, self._open_project_picker))
        self.put(screen, top + height - 1, 3, "Machines       Ctrl+G", self.color(8), width - 5)
        self._mouse_targets.append((top + height - 1, 2, 1, width - 3, self._show_machines))

    def _draw_conversation(self, screen: Any, top: int, left: int, width: int, height: int) -> None:
        content_left = left + 3
        content_width = max(10, width - content_left - 3)
        thread = self.workspace.selected_thread
        if thread is None:
            self._draw_welcome(screen, top, content_left, content_width, height)
            return
        composer_y = height - 8
        # Stack activity directly above the composer so a 48×16 terminal still
        # has one transcript row when the collapsed agent summary is present.
        activity_y = composer_y - 2
        agent_lines = self._agent_lines(content_width, wrap_results=True)
        max_agent_height = max(1, activity_y - top - 1)
        if (
            self._agent_key() in self.expanded_agents
            and self._agent_details_overflow()
            and self.overlay is None
        ):
            self.agent_scroll = 0
            self.overlay = "agents"
        if len(agent_lines) > max_agent_height:
            if max_agent_height == 1:
                agent_lines = agent_lines[:1]
            else:
                hidden = len(agent_lines) - max_agent_height + 1
                agent_lines = agent_lines[: max_agent_height - 1] + [f"  … {hidden} more agent detail line{'s' if hidden != 1 else ''}"]
        agent_y = activity_y - len(agent_lines)
        transcript_height = max(0, agent_y - top)
        events = self.workspace.view_events()
        if not events:
            if transcript_height:
                self.put(screen, top, content_left + 1, "What would you like to build?", curses.A_BOLD, content_width - 2)
            if transcript_height >= 5:
                self.put(screen, top + 2, content_left + 1, "Ask a question, describe a change, or paste an error.", self.color(10), content_width - 2)
                self.put(screen, top + 3, content_left + 1, "Your draft stays here when you switch conversations.", self.color(10), content_width - 2)
        elif transcript_height:
            visible = visible_conversation_lines(events, content_width, transcript_height, self.workspace.thread_view(), expanded_tools=self.expanded_tools)
            for offset, line in enumerate(visible):
                attr = self.color(6)
                kind = getattr(line, "kind", "")
                if kind == "user":
                    attr = self.color(1) | curses.A_BOLD
                elif kind == "tool":
                    attr = self.color(10)
                elif kind == "error":
                    attr = self.color(4) | curses.A_BOLD
                elif kind == "approval":
                    attr = self.color(3) | curses.A_BOLD
                elif kind in {"status", "run_state"}:
                    attr = self.color(10)
                self.put(screen, top + offset, content_left, line, attr, content_width)
        if agent_lines:
            self._fill(screen, agent_y, content_left, content_width, len(agent_lines), self.color(7))
            for offset, line in enumerate(agent_lines):
                attr = self.color(12) | curses.A_BOLD if offset == 0 else self.color(8)
                if " · failed" in line:
                    attr = self.color(4)
                self.put(screen, agent_y + offset, content_left + 1, line, attr, content_width - 2)
            self._mouse_targets.append((agent_y, content_left, len(agent_lines), content_width, self._toggle_agents))
        self._draw_activity(screen, activity_y, content_left, content_width, thread)
        self._fill(screen, composer_y, content_left, content_width, 5, self.color(7))
        provider_warning = self._provider_warning(thread)
        provider_checking = bool(
            provider_warning
            and self.workspace.selected_machine.get("providers", {}).get(str(thread.get("provider") or ""), {}).get("status") == "checking"
        )
        border = self.color(3 if provider_checking else 4 if provider_warning else 12 if self.focus == "composer" else 15)
        self.put(screen, composer_y, content_left, "╭" + "─" * (content_width - 2) + "╮", border, content_width)
        mode = permission_label(thread)
        provider = self.workspace.selected_machine.get("providers", {}).get(str(thread.get("provider") or ""), {})
        indicator = model_indicator(thread, provider, compact=content_width < 60)
        title_parts = ([provider_warning[0]] if provider_warning else []) + [indicator] + ([mode] if mode else [])
        title = " " + " · ".join(title_parts) + " "
        title_width = max(1, content_width - 4)
        self.put(screen, composer_y, content_left + 2, title, border | curses.A_BOLD, title_width)
        self._mouse_targets.append((composer_y, content_left + 2, 1, min(title_width, len(title)), self._model_form))
        for row in range(1, 4):
            self.put(screen, composer_y + row, content_left, "│", border)
            self.put(screen, composer_y + row, content_left + content_width - 1, "│", border)
        self.put(screen, composer_y + 4, content_left, "╰" + "─" * (content_width - 2) + "╯", border, content_width)
        text_width = max(1, content_width - 5)
        logical = self.composer.split("\n")
        caret_line = self.composer[:self.cursor].count("\n")
        caret_column = len(self.composer[:self.cursor].rsplit("\n", 1)[-1])
        first_line = max(0, caret_line - 2)
        active_text, caret_column = text_view(logical[caret_line], caret_column, text_width)
        for row, line in enumerate(logical[first_line:first_line + 3]):
            rendered = active_text if row + first_line == caret_line else line
            self.put(screen, composer_y + row + 1, content_left + 2, rendered, self.color(7), text_width)
        if not self.composer:
            self.put(screen, composer_y + 1, content_left + 2, "Describe your next step…", self.color(8), text_width)
        if self.focus == "composer":
            self._cursor_position = (composer_y + 1 + caret_line - first_line, content_left + 2 + caret_column)
        enter_action = "Enter send" if self.workspace.state["settings"].get("enter_sends", True) else "Ctrl+S send"
        controls = (
            provider_warning[1]
            if provider_warning
            else "Ctrl+X stop   F4 model (after run)   F7 tools"
            if thread.get("state") in ACTIVE_STATES
            else f"{enter_action}   F4 model   Ctrl+D review" + ("   Ctrl+J newline" if content_width >= 65 else "")
        )
        self.put(screen, height - 2, content_left, controls, self.color(3 if provider_checking else 4 if provider_warning else 10), content_width)
        self._mouse_targets.append((composer_y + 1, content_left, 4, content_width, lambda: setattr(self, "focus", "composer")))
        view = self.workspace.thread_view()
        mark_seen = getattr(self.workspace, "mark_thread_seen", None)
        if (
            callable(mark_seen)
            and self.overlay is None
            and transcript_height > 0
            and int(view.get("scroll", 0)) == 0
        ):
            mark_seen(self.workspace.selected_machine_id, str(thread["id"]), visible=True)

    def _thread_activity(self, thread: dict[str, Any], machine_id: str | None = None) -> RunActivity:
        machine_id = machine_id or self.workspace.selected_machine_id
        machine = self.workspace.machines[machine_id]
        return summarize_activity(
            thread, self.workspace.thread_events(str(thread["id"]), machine_id),
            self.workspace.thread_run(str(thread["id"]), machine_id), now=time.time(),
            stale=bool(machine.get("stale")) or machine.get("connection") != "connected",
        )

    def _agents(self) -> list[dict[str, Any]]:
        thread = self.workspace.selected_thread
        if not thread:
            return []
        return summarize_agents(self.workspace.thread_events(str(thread["id"]), self.workspace.selected_machine_id))

    def _has_agents(self) -> bool:
        return bool(self._agents())

    def _agent_key(self) -> tuple[str, str] | None:
        thread = self.workspace.selected_thread
        return (self.workspace.selected_machine_id, str(thread["id"])) if thread else None

    def _toggle_agents(self) -> None:
        key = self._agent_key()
        if key is None:
            self.status = "Select a thread before opening agent details"
            return
        if not self._has_agents():
            self.status = "No automatic agent activity is available for this thread"
            return
        if key in self.expanded_agents:
            self.expanded_agents.remove(key)
            self.status = "Agent details collapsed"
        else:
            self.expanded_agents.add(key)
            if self._agent_details_overflow():
                self.agent_scroll = 0
                self.overlay = "agents"
                self.status = "Agent details opened"
            else:
                self.status = "Agent details expanded"

    def _agent_inline_geometry(self) -> tuple[int, int]:
        height, width = self._screen_size
        sidebar_width = min(32, max(25, width // 4)) if width >= 90 else 0
        content_width = max(10, width - (sidebar_width + 3) - 3)
        capacity = max(1, height - 15)
        return content_width, capacity

    def _agent_details_overflow(self) -> bool:
        content_width, capacity = self._agent_inline_geometry()
        return len(self._agent_lines(content_width, wrap_results=True)) > capacity

    def _agent_overlay_lines(self) -> list[str]:
        box_width = min(max(1, self._screen_size[1] - 4), 84)
        return self._agent_lines(max(1, box_width - 4), wrap_results=True)[1:]

    @staticmethod
    def _agent_depth(agent: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> int:
        depth = 0
        parent_id = agent.get("parent_id")
        visited = {str(agent.get("id"))}
        while parent_id is not None and str(parent_id) in by_id and str(parent_id) not in visited and depth < 8:
            visited.add(str(parent_id))
            depth += 1
            parent_id = by_id[str(parent_id)].get("parent_id")
        return depth

    @staticmethod
    def _agent_observed_at(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) / 1_000
        parsed = timestamp(value)
        if parsed is not None:
            return parsed
        if isinstance(value, str):
            try:
                return float(value) / 1_000
            except ValueError:
                return None
        return None

    def _agent_lines(self, width: int, *, wrap_results: bool = False) -> list[str]:
        agents = self._agents()
        if not agents:
            return []
        counts = {state: sum(agent.get("state") == state for agent in agents) for state in ("running", "completed", "failed", "cancelled")}
        parts = [f"{counts['running']} running"] if counts["running"] else []
        finished = counts["completed"] + counts["failed"] + counts["cancelled"]
        if finished:
            parts.append(f"{finished} finished")
        marker = "▾" if self._agent_key() in self.expanded_agents else "▸"
        machine = self.workspace.selected_machine
        offline = bool(machine.get("stale")) or machine.get("connection") != "connected"
        availability = " · cached/offline" if offline else ""
        lines = [f"{marker} Agents" + availability + " · " + " · ".join(parts or [f"{len(agents)} reported"]) + " · F9"]
        if self._agent_key() not in self.expanded_agents:
            return lines
        by_id = {str(agent["id"]): agent for agent in agents}
        for agent in agents:
            depth = self._agent_depth(agent, by_id)
            indent = "  " * depth
            state = str(agent.get("state") or "unknown")
            state_label = f"{state} (offline)" if offline and state == "running" else state
            detail = [state_label]
            elapsed = agent.get("elapsed")
            started_at = agent.get("started_at")
            if state == "running" and isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
                started_seconds = float(started_at) / 1_000
                observed_at = self._agent_observed_at(agent.get("updated_at")) if offline else time.time()
                elapsed = max(0.0, observed_at - started_seconds) if observed_at is not None else elapsed
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed >= 0:
                detail.append(duration(float(elapsed)))
            label = str(agent.get("label") or agent.get("id") or "agent")
            suffix = " · " + " · ".join(detail)
            max_indent = max(0, width - text_cells("↳ …" + suffix))
            indent = clip_cells(indent, max_indent)
            prefix = f"{indent}↳ "
            model_suffix = f" · {agent['model']}" if agent.get("model") else ""
            # Task and state are primary.  Include a model only when it leaves
            # useful task-label space, and reserve the state suffix before
            # truncating unusually long provider task text.
            minimum_label = min(12, max(1, text_cells(label)))
            if text_cells(prefix + suffix + model_suffix) + minimum_label > width:
                model_suffix = ""
            label_width = max(1, width - text_cells(prefix + suffix + model_suffix))
            rendered_label = ellipsize_cells(label, label_width)
            lines.append(clip_cells(prefix + rendered_label + suffix + model_suffix, width))
            if agent.get("result"):
                result = safe_terminal_text(agent["result"])
                result_prefix = f"{indent}   "
                available = max(1, width - text_cells(result_prefix))
                if wrap_results:
                    for result_line in wrap_text(result, available):
                        lines.append(result_prefix + clip_cells(result_line, available))
                else:
                    lines.append(result_prefix + clip_cells(result, available))
        return lines

    def _provider_warning(self, thread: dict[str, Any]) -> tuple[str, str] | None:
        provider_name = str(thread.get("provider") or "provider")
        readiness = self.workspace.selected_machine.get("providers", {}).get(provider_name, {})
        if readiness.get("available") is not False:
            return None
        display_name = PROVIDER_NAMES.get(provider_name.casefold(), provider_name)
        machine_name = str(self.workspace.selected_machine.get("alias") or self.workspace.selected_machine_id)
        if readiness.get("status") == "checking":
            return (
                f"Checking {display_name} on {machine_name}",
                "Provider discovery is still running; your draft is saved.",
            )
        detail = str(readiness.get("detail") or f"Finish {display_name} setup on {machine_name}, then reconnect.")
        return f"{display_name} unavailable on {machine_name}", detail

    def _draw_activity(self, screen: Any, y: int, x: int, width: int, thread: dict[str, Any]) -> None:
        activity = self._thread_activity(thread)
        if (self.workspace.selected_machine_id, str(thread["id"])) in self._sending:
            activity = RunActivity("running", "Sending prompt", "Waiting for the daemon to accept your message")
        elif self.workspace.state["uncertain_sends"].get(f"{self.workspace.selected_machine_id}:{thread['id']}"):
            activity = RunActivity("interrupted", "Send not confirmed", "Ctrl+Y checks the same request safely; your draft is saved")
        color = 8 if activity.stale else (3 if activity.state in {"awaiting_approval", "interrupted"} else 4 if activity.state == "failed" else 12)
        self._fill(screen, y, x, width, 2, self.color(7))
        headline = activity.headline(time.monotonic())
        quiet = f"Last update {duration(activity.quiet_for)} ago" if activity.quiet_for is not None and activity.state in ACTIVE_STATES else ""
        self.put(screen, y, x + 1, headline, self.color(color) | curses.A_BOLD, width - 2)
        detail = activity.detail
        if quiet and width >= len(headline) + len(quiet) + 5:
            self.put(screen, y, x + width - len(quiet) - 1, quiet, self.color(8), len(quiet))
        elif quiet and activity.state in ACTIVE_STATES and not activity.stale:
            detail = quiet + " · " + detail
        self.put(screen, y + 1, x + 1, detail, self.color(8), width - 2)

    def _draw_welcome(self, screen: Any, top: int, left: int, width: int, height: int) -> None:
        x = left + max(0, (width - 60) // 2)
        available = min(width, 64)
        y = max(top + 1, (height - 11) // 2)
        project = self.workspace.selected_project
        title = f"Build something in {project.get('name')}" if project else "Your next idea starts here."
        self.put(screen, y, x, title, curses.A_BOLD, available)
        lead = "Create a conversation with Codex or OpenCode."
        self.put(screen, y + 2, x, lead, self.color(10), available)
        self.put(screen, y + 3, x, "Choose a repository as part of creating your first thread.", self.color(10), available)
        self._button(screen, y + 5, x, "+ New thread   Enter", self._new_thread_form, primary=True)
        if width >= 52:
            self._button(screen, y + 5, x + 28, "Connect dev server", self._remote_server_form)
        self.put(screen, y + 7, x, "Ctrl+N new · Ctrl+O open project · Ctrl+G servers", self.color(10), available)
        if y + 9 < height - 2:
            self.put(screen, y + 9, x, "Your agents keep working when you leave this window.", self.color(10), available)

    def _draw_footer(self, screen: Any, y: int, width: int) -> None:
        self._fill(screen, y, 0, width, 1, self.color(8))
        count = self._attention_count()
        status = self.status if self.status not in {"Ready", "Opened cached state"} else ""
        if count and not status:
            status = f"! {count} need{'s' if count == 1 else ''} attention · F5"
        elif not status or status in {"Prompt accepted", "Prompt retry accepted"}:
            running = sum(thread.get("state") == "running" for machine_id, machine in self.workspace.machines.items()
                          if machine.get("connection") == "connected" and not machine.get("stale")
                          for thread in self.workspace.threads(machine_id))
            if running:
                status = f"{running} thread{'s' if running != 1 else ''} working · Ctrl+P"
        keys = "Ctrl+K actions   Ctrl+P switch   F1 help   Ctrl+Q quit"
        if width < 80:
            keys = "^K actions  ^P switch  F1 help  ^Q quit"
        if status:
            remaining = width - len(keys) - 7
            if remaining >= 12:
                self.put(screen, y, 2, status, self.color(8), remaining)
                if status.startswith("! "):
                    self._mouse_targets.append((y, 2, 1, remaining, self._open_attention))
                self.put(screen, y, width - len(keys) - 2, keys, self.color(8))
            else:
                self.put(screen, y, 2, status, self.color(8), width - 4)
        else:
            self.put(screen, y, 2, keys, self.color(8), width - 4)

    def _draw_overlay(self, screen: Any, height: int, width: int) -> None:
        box_width = min(width - 4, 84)
        desired = 10 + len(self.form.fields) * 3 if self.overlay == "form" and self.form else 25
        box_height = min(height - 2, desired)
        x, y = (width - box_width) // 2, (height - box_height) // 2
        self._cursor_position = None
        self._mouse_targets = []
        try:
            window = screen.derwin(box_height, box_width, y, x)
            window.bkgd(" ", self.color(6))
            window.erase()
            window.attrset(self.color(9))
            window.box()
            window.attrset(self.color(6))
        except curses.error:
            return
        if self.overlay == "search":
            self._overlay_search(window, box_height, box_width)
        elif self.overlay == "projects":
            self._overlay_projects(window, box_height, box_width)
        elif self.overlay == "form" and self.form:
            self._overlay_form(window, box_height, box_width)
            if self._cursor_position:
                self._cursor_position = (self._cursor_position[0] + y, self._cursor_position[1] + x)
        elif self.overlay == "models":
            self._overlay_models(window, box_height, box_width)
        elif self.overlay == "palette":
            self._overlay_palette(window, box_height, box_width)
        elif self.overlay == "attention":
            self._overlay_attention(window, box_height, box_width)
        elif self.overlay == "agents":
            self._overlay_agents(window, box_height, box_width)
        elif self.overlay == "diff":
            self._overlay_diff(window, box_height, box_width)
        elif self.overlay == "machines":
            self._overlay_machines(window, box_height, box_width)
        elif self.overlay == "approval":
            self._overlay_approval(window, box_height, box_width)
        elif self.overlay == "help":
            self._overlay_help(window, box_height, box_width)
        self._mouse_targets = [(row + y, col + x, h, w, action) for row, col, h, w, action in self._mouse_targets]
        window.noutrefresh()

    def _wput(self, window: Any, y: int, x: int, text: str, attr: int = 0, width: int | None = None) -> None:
        self.put(window, y, x, text, attr, width)

    def _overlay_search(self, window: Any, height: int, width: int) -> None:
        self._wput(window, 0, 2, " Thread switcher — Esc close ", self.color(1) | curses.A_BOLD)
        self._wput(window, 2, 2, "> " + self.search_query, curses.A_REVERSE, width - 4)
        results = self.workspace.search_threads(self.search_query)
        self.search_index = min(self.search_index, max(0, len(results) - 1))
        available = max(0, height - 5)
        start = max(0, min(self.search_index - available // 2, max(0, len(results) - available)))
        for index, result in enumerate(results[start:start + available], start=start):
            thread, project, machine = result["thread"], result["project"], result["machine"]
            text = f"{thread.get('title')}  · {project.get('name')}  · {machine.get('alias')}  · {thread.get('provider')}  · {thread.get('state')}"
            self._wput(window, 4 + index - start, 2, text, curses.A_REVERSE if index == self.search_index else 0, width - 4)

    def _overlay_projects(self, window: Any, height: int, width: int) -> None:
        machine = self.workspace.selected_machine
        alias = str(machine.get("alias") or self.workspace.selected_machine_id)
        self._wput(window, 0, 2, f" Open project on {alias} — Esc close ", self.color(1) | curses.A_BOLD)
        self._wput(window, 2, 2, "> " + self.search_query, curses.A_REVERSE, width - 4)
        results = project_picker_results(self.workspace, self.search_query)
        self.search_index = min(max(0, self.search_index), len(results))
        self._wput(
            window, 4, 2, "+ Add a repository by path…",
            curses.A_REVERSE if self.search_index == 0 else self.color(12), width - 4,
        )
        available = max(0, height - 9)
        selected_result = max(0, self.search_index - 1)
        start = max(0, min(selected_result - available // 2, max(0, len(results) - available)))
        for index, result in enumerate(results[start:start + available], start=start):
            option_index = index + 1
            self._wput(
                window, 6 + index - start, 2, str(result["label"]),
                curses.A_REVERSE if option_index == self.search_index else 0, width - 4,
            )
        if not results:
            self._wput(window, 6, 2, "No matching projects.", self.color(10), width - 4)
        self._wput(window, height - 2, 2, "Type to search name or path · ↑↓ choose · Enter open", self.color(10), width - 4)

    def _model_rows(self) -> list[dict[str, Any]]:
        context = self.model_context or {}
        return model_picker_results(
            self.workspace,
            self.search_query,
            machine_id=context.get("machine_id"),
            provider_name=context.get("provider"),
            current_model=context.get("current_model"),
        )

    def _overlay_models(self, window: Any, height: int, width: int) -> None:
        context = self.model_context or {}
        provider_name = PROVIDER_NAMES.get(str(context.get("provider", "provider")).casefold(), str(context.get("provider", "provider")))
        target = str(context.get("target") or "new thread")
        self._wput(window, 0, 2, f" Model for {target} — {provider_name} — Esc close ", self.color(1) | curses.A_BOLD, width - 4)
        if self.model_focus == "settings" and self.model_selected:
            self._overlay_model_settings(window, height, width)
            return
        self._wput(window, 2, 2, "> " + self.search_query, curses.A_REVERSE, width - 4)
        rows = self._model_rows()
        self.search_index = min(max(0, self.search_index), max(0, len(rows) - 1))
        available = max(1, height - 7)
        start = max(0, min(self.search_index - available // 2, max(0, len(rows) - available)))
        for index, row in enumerate(rows[start:start + available], start=start):
            y = 4 + index - start
            attr = curses.A_REVERSE if index == self.search_index else self.color(8 if row.get("available") is False else 6)
            self._wput(window, y, 2, str(row["label"]), attr, width - 4)
            self._mouse_targets.append((y, 2, 1, width - 4, lambda i=index: self._choose_model(i)))
        if not rows:
            self._wput(window, 4, 2, "No matching models. Clear the search to choose Provider default.", self.color(10), width - 4)
        readiness = self.workspace.machines.get(context.get("machine_id"), {}).get("providers", {}).get(context.get("provider"), {})
        status = str(readiness.get("status") or ("unavailable" if readiness.get("available") is False else "ready"))
        discovery = "cached" if readiness.get("cached") else status
        if readiness.get("refreshing"):
            discovery += " · refreshing"
        self._wput(window, height - 2, 2, f"Discovery: {discovery} · Type search · ↑↓ choose · Enter select", self.color(10), width - 4)

    def _overlay_model_settings(self, window: Any, height: int, width: int) -> None:
        context = self.model_context or {}
        fields = context.get("setting_fields") or []
        field_index = min(int(context.get("setting_field_index", 0)), max(0, len(fields) - 1))
        name, choices = fields[field_index]
        label = "Reasoning" if name == "reasoning_effort" else "Variant"
        selected_name = str((self.model_selected or {}).get("name") or (self.model_selected or {}).get("id") or "Provider default")
        self._wput(window, 2, 2, f"{label} for {selected_name}", curses.A_BOLD, width - 4)
        options = [""] + list(choices)
        self.model_setting_index = min(max(0, self.model_setting_index), len(options) - 1)
        available = max(1, height - 7)
        start = max(0, min(self.model_setting_index - available // 2, max(0, len(options) - available)))
        for index, value in enumerate(options[start:start + available], start=start):
            text = "Model default" if not value else str(value)
            y = 4 + index - start
            self._wput(window, y, 2, text, curses.A_REVERSE if index == self.model_setting_index else 0, width - 4)
            self._mouse_targets.append((y, 2, 1, width - 4, lambda i=index: self._choose_model_setting(i)))
        progress = f"{field_index + 1}/{len(fields)}" if len(fields) > 1 else ""
        self._wput(window, height - 2, 2, f"↑↓ choose · Enter apply {progress} · Esc back to models", self.color(10), width - 4)

    def _overlay_palette(self, window: Any, height: int, width: int) -> None:
        self._wput(window, 0, 2, " Command palette — Ctrl+K — Esc close ", self.color(1) | curses.A_BOLD, width - 4)
        self._wput(window, 2, 2, "> " + self.search_query, curses.A_REVERSE, width - 4)
        actions = command_palette_results(self._command_actions(), self.search_query)
        self.search_index = min(max(0, self.search_index), max(0, len(actions) - 1))
        row_height = 2
        available = max(1, (height - 7) // row_height)
        start = max(0, min(self.search_index - available // 2, max(0, len(actions) - available)))
        for index, action in enumerate(actions[start:start + available], start=start):
            y = 4 + (index - start) * row_height
            selected = index == self.search_index
            attr = curses.A_REVERSE if selected else self.color(6 if action.enabled else 8)
            shortcut = f"  {action.shortcut}" if action.shortcut else ""
            self._wput(window, y, 2, action.label + shortcut, attr | (curses.A_BOLD if action.enabled else 0), width - 4)
            detail = action.target if action.enabled else action.reason
            self._wput(window, y + 1, 4, "→ " + detail, self.color(10 if action.enabled else 4), width - 6)
            self._mouse_targets.append((y, 2, 2, width - 4, lambda i=index: self._execute_palette_index(i)))
        if not actions:
            self._wput(window, 4, 2, "No matching actions.", self.color(10), width - 4)
        self._wput(window, height - 2, 2, "Type to search actions and targets · ↑↓ choose · Enter run", self.color(10), width - 4)

    def _overlay_attention(self, window: Any, height: int, width: int) -> None:
        self._wput(window, 0, 2, " Attention — F5 — Esc close ", self.color(3) | curses.A_BOLD, width - 4)
        self._wput(window, 2, 2, "> " + self.search_query, curses.A_REVERSE, width - 4)
        items = self._filtered_attention_items()
        self.search_index = min(max(0, self.search_index), max(0, len(items) - 1))
        available = max(1, height - 7)
        start = max(0, min(self.search_index - available // 2, max(0, len(items) - available)))
        for index, item in enumerate(items[start:start + available], start=start):
            state = "approval" if item.get("needs_approval") else str(item.get("state") or "result")
            count = max(1, int(item.get("unread_count") or 0))
            stale = " · offline" if item.get("stale") else ""
            label = f"{item.get('title')}  ·  {state}  ·  {count} unread{stale}"
            y = 4 + index - start
            self._wput(window, y, 2, label, curses.A_REVERSE if index == self.search_index else 0, width - 4)
            self._mouse_targets.append((y, 2, 1, width - 4, lambda i=index: self._open_attention_index(i)))
        if not items:
            self._wput(window, 4, 2, "Nothing needs attention.", self.color(10), width - 4)
        self._wput(window, height - 2, 2, "Type to search thread, project, or machine · Enter open", self.color(10), width - 4)

    def _overlay_agents(self, window: Any, height: int, width: int) -> None:
        thread = self.workspace.selected_thread or {}
        self._wput(window, 0, 2, f" Agents for {thread.get('title') or 'thread'} — F9/Esc close ", self.color(1) | curses.A_BOLD, width - 4)
        lines = self._agent_lines(max(1, width - 4), wrap_results=True)[1:]
        available = max(1, height - 4)
        maximum = max(0, len(lines) - available)
        self.agent_scroll = min(max(0, self.agent_scroll), maximum)
        for row, line in enumerate(lines[self.agent_scroll:self.agent_scroll + available]):
            attr = self.color(4) if " · failed" in line else self.color(6)
            self._wput(window, 2 + row, 2, line, attr, width - 4)
        if not lines:
            self._wput(window, 2, 2, "No automatic agent activity is available.", self.color(10), width - 4)
        end = min(len(lines), self.agent_scroll + available)
        self._wput(window, height - 2, 2, f"↑↓/Pg scroll · details {self.agent_scroll + 1 if lines else 0}–{end} of {len(lines)}", self.color(10), width - 4)

    def _overlay_form(self, window: Any, height: int, width: int) -> None:
        assert self.form is not None
        form = self.form
        self._wput(window, 0, 2, f" {form.title} ", self.color(1) | curses.A_BOLD)
        machine = self.workspace.machines.get(self._form_machine_id or self.workspace.selected_machine_id, {})
        context = ("Projects and agents stay on your server · SSH connection"
                   if form.title == "Connect dev server" else f"On {machine.get('alias', 'local')}  ·  Tab next field  ·  Esc close")
        self._wput(window, 1, 3, context, self.color(10), width - 6)
        count = max(1, (height - 9) // 3)
        first = max(0, form.index - count + 1)
        for row, index in enumerate(range(first, min(len(form.fields), first + count))):
            name = form.fields[index][0]
            value = form.values[index]
            active = index == form.index
            y = 3 + row * 3
            label = form.labels.get(name, name.replace("_", " ").title())
            self._wput(window, y, 3, ("› " if active else "  ") + label, self.color(1 if active else 10), width - 6)
            self._fill(window, y + 1, 3, width - 6, 1, self.color(7))
            choices = form.choices.get(name)
            selector = form.selectors.get(name)
            if selector:
                self._wput(window, y + 1, 5, (value or "Provider default") + "  ›", self.color(12 if active else 8), width - 10)
            elif choices:
                self._wput(window, y + 1, 5, "‹  " + value + "  ›", self.color(12 if active else 8), width - 10)
            else:
                rendered, cursor = text_view(value, form.cursor if active else 0, width - 10)
                rendered = rendered if value else "(optional)"
                attr = self.color(13 if active and form.replace_on_type and value else 7 if value else 8)
                self._wput(window, y + 1, 5, rendered, attr, width - 10)
                if active and not form.busy:
                    self._cursor_position = (y + 1, 5 + cursor)
            self._mouse_targets.append((y, 3, 2, width - 6, lambda i=index: self._focus_form_field(form, i)))
        field = form.fields[form.index][0]
        hint = form.hints.get(
            field,
            "Enter or Space opens the searchable selector" if form.selectors.get(field)
            else "← → choose" if form.choices.get(field)
            else "Type to replace the default · Ctrl+U clear",
        )
        if form.busy and form.progress:
            hint = form.progress
        self._wput(window, height - 6, 3, hint, self.color(10), width - 6)
        if form.error:
            for row, line in enumerate(wrap_text(form.error, width - 6)[:2]):
                self._wput(window, height - 5 + row, 3, line, self.color(4), width - 6)
        label = "Working…" if form.busy else ("Create thread  Ctrl+S" if form.title == "New thread" else "Save  Ctrl+S")
        if form.title == "Connect dev server" and not form.busy:
            label = "Connect & import  Ctrl+S"
        self._button(window, height - 3, 3, label, lambda: form.key(19), primary=not form.busy)
        self._wput(window, height - 2, 3, "Enter next / finish    Shift+Tab back    ↑↓ navigate", self.color(10), width - 6)

    @staticmethod
    def _focus_form_field(form: Form, index: int) -> None:
        if not form.busy:
            form.index = index

    def _overlay_diff(self, window: Any, height: int, width: int) -> None:
        mode = "files" if self.diff_focus == "files" else "patch"
        self._wput(window, 0, 2, f" Diff review [{mode}] — Tab switch · Esc close ", self.color(1) | curses.A_BOLD)
        if self.diff is None:
            self._wput(window, 2, 2, "Loading from selected machine…")
            return
        notice = "Shared checkout: changes may belong to other threads." if self.diff.get("shared") else "Dedicated worktree"
        self._wput(window, 1, 2, f"{notice}  Branch: {self.diff.get('branch', '?')}", self.color(3), width - 4)
        if self.diff_focus == "files":
            self._draw_diff_files(window, height, width)
        else:
            selected = self.diff_files[self.diff_file_index].get("path") if self.diff_files else "all files"
            self._wput(window, 2, 2, f"Patch: {selected}", curses.A_BOLD, width - 4)
            lines = str(self.diff.get("diff", "")).splitlines()
            if self.diff.get("truncated"):
                lines.insert(0, "[diff truncated by server; select a file with Tab for its scoped patch]")
            for index, line in enumerate(lines[self.diff_scroll:self.diff_scroll + height - 5]):
                attr = self.color(5) if line.startswith("+") and not line.startswith("+++") else self.color(4) if line.startswith("-") and not line.startswith("---") else 0
                self._wput(window, 3 + index, 2, line, attr, width - 4)
            if lines:
                end = min(len(lines), self.diff_scroll + height - 5)
                self._wput(window, height - 2, 2, f"Lines {self.diff_scroll + 1}–{end} of {len(lines)}", curses.A_DIM)

    def _draw_diff_files(self, window: Any, height: int, width: int) -> None:
        if not self.diff_files:
            self._wput(window, 3, 2, "No changed files.")
            return
        available = max(1, height - 5)
        self.diff_file_index = min(self.diff_file_index, len(self.diff_files) - 1)
        if self.diff_file_index < self.diff_file_scroll:
            self.diff_file_scroll = self.diff_file_index
        elif self.diff_file_index >= self.diff_file_scroll + available:
            self.diff_file_scroll = self.diff_file_index - available + 1
        for row, item in enumerate(self.diff_files[self.diff_file_scroll:self.diff_file_scroll + available]):
            index = self.diff_file_scroll + row
            additions = "?" if item.get("additions") is None else str(item.get("additions"))
            deletions = "?" if item.get("deletions") is None else str(item.get("deletions"))
            text = f"{str(item.get('status') or '?'):>2}  +{additions} -{deletions}  {item.get('path', '?')}"
            self._wput(window, 3 + row, 2, text, curses.A_REVERSE if index == self.diff_file_index else 0, width - 4)
        self._wput(window, height - 2, 2, "↑↓ select · Enter load file patch", curses.A_DIM)

    def _overlay_machines(self, window: Any, height: int, width: int) -> None:
        self._wput(window, 0, 2, " Servers — a connect dev server · Esc close ", self.color(1) | curses.A_BOLD)
        for index, machine in enumerate(self.workspace.machines.values()):
            host = machine.get("host") or "local socket"
            text = f"{machine.get('alias')}  {machine.get('connection')}  {host}"
            self._wput(window, 2 + index * 2, 2, text, curses.A_BOLD, width - 4)
            if machine.get("last_error"):
                self._wput(window, 3 + index * 2, 4, machine["last_error"], self.color(4), width - 6)
            elif machine.get("providers"):
                summaries = []
                for name, provider in machine["providers"].items():
                    detail = "ready" if provider.get("available") else provider.get("detail", "unavailable")
                    models = provider.get("models") or []
                    model_names = ", ".join(
                        str(model.get("name") or model.get("id")) if isinstance(model, dict) else str(model)
                        for model in models[:3]
                    )
                    summaries.append(f"{name}: {detail}" + (f" · {model_names}" if model_names else ""))
                self._wput(window, 3 + index * 2, 4, " | ".join(summaries), curses.A_DIM, width - 6)

    def _selected_approval(self) -> tuple[str, dict[str, Any]] | None:
        selected = self.workspace.selected_thread
        choices: list[tuple[str, dict[str, Any]]] = []
        for machine_id in self.workspace.machines:
            choices.extend((machine_id, approval) for approval in self.workspace.approvals(machine_id))
        if selected:
            for choice in choices:
                if choice[0] == self.workspace.selected_machine_id and choice[1].get("thread_id") == selected.get("id"):
                    return choice
        return choices[0] if choices else None

    def _overlay_approval(self, window: Any, height: int, width: int) -> None:
        self._wput(window, 0, 2, " Approval — ↑↓/Pg scroll · y allow once · n reject · Esc close ", self.color(3) | curses.A_BOLD)
        selected = self.approval_choice
        if selected is None:
            self._wput(window, 2, 2, "No pending approvals.")
            return
        lines = self._approval_lines(width - 4)
        self.approval_line_count = len(lines)
        available = max(1, height - 5)
        maximum = max(0, len(lines) - available)
        self.approval_scroll = min(self.approval_scroll, maximum)
        for row, line in enumerate(lines[self.approval_scroll:self.approval_scroll + available]):
            self._wput(window, 2 + row, 2, line, 0, width - 4)
        end = min(len(lines), self.approval_scroll + available)
        self._wput(window, height - 2, 2, f"Lines {self.approval_scroll + 1 if lines else 0}–{end} of {len(lines)}", curses.A_DIM, width - 4)

    def _approval_lines(self, width: int) -> list[str]:
        if self.approval_choice is None:
            return []
        machine_id, approval = self.approval_choice
        machine = self.workspace.machines[machine_id]
        payload = approval.get("payload") if isinstance(approval.get("payload"), dict) else {}
        command = payload.get("command")
        details = payload.get("details")
        values = [
            f"Host: {payload.get('hostname') or machine.get('host') or machine.get('alias')}",
            f"Working directory: {payload.get('cwd') or '(not supplied)'}",
            f"Kind: {payload.get('kind') or '(not supplied)'}",
            f"Command: {command if command is not None else '(no command supplied)'}",
            f"Request: {approval.get('request_id') or approval.get('id') or '(missing id)'}",
        ]
        if details is not None:
            rendered = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False, indent=2, default=str)
            values.append(f"Details: {rendered}")
        lines: list[str] = []
        for value in values:
            lines.extend(wrap_text(safe_terminal_text(value), width))
        return lines

    def _overlay_help(self, window: Any, height: int, width: int) -> None:
        self._wput(window, 0, 2, " Keyboard help — Esc close ", self.color(1) | curses.A_BOLD)
        bindings = [
            "Ctrl+K              search actions and their exact targets",
            "Tab                 switch sidebar / prompt focus",
            "Enter / Ctrl+S      send prompt",
            "Ctrl+J / Alt+Enter  insert newline",
            "Ctrl+P              search all cached threads",
            "Ctrl+N              create thread (Enter on welcome screen)",
            "F4                  search models and supported settings",
            "F5                  open results needing attention",
            "F9                  expand/collapse automatic agents",
            "Forms               Tab move · arrows choose · Ctrl+S save",
            "Ctrl+D              open changed-files diff",
            "Page Up / Down      browse history without following output",
            "Ctrl+X              cancel selected thread run",
            "Ctrl+Y              retry an uncertain send with the same request ID",
            "Ctrl+G / Ctrl+O    machines / open project (F2 also opens machines)",
            "F3                  add repository by path",
            "F6 / F7             pending approval / expand tool details",
            "Diff Tab/↑↓/Enter   switch panes / select / load scoped patch",
            "Approval ↑↓/Pg      inspect the full pinned request",
            "F8                  toggle Enter binding (Ctrl+S always sends)",
            "Sidebar Ctrl+R/A    rename / archive thread",
            "Themes              Ctrl+K, then search Choose theme",
            "Ctrl+Q              quit (runs continue on daemon)",
        ]
        for index, binding in enumerate(bindings[: height - 3]):
            self._wput(window, 2 + index, 2, binding, 0, width - 4)

    def spawn(self, awaitable: Awaitable[Any], *, success: str = "Done", callback: Callable[[Any], None] | None = None,
              on_error: Callable[[Exception], None] | None = None) -> None:
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self.tasks.discard(done)
            if done.cancelled():
                return
            try:
                result = done.result()
                if callback:
                    callback(result)
                self.status = success
            except Exception as exc:
                self.status = str(exc)
                if on_error:
                    on_error(exc)

        task.add_done_callback(finished)

    def handle_key(self, key: int | str) -> None:
        raw_key = key
        if isinstance(key, str) and key.isprintable():
            # get_wch distinguishes Unicode text from curses key constants;
            # preserve that distinction (some code points share integer IDs).
            self._last_escape = False
            if self.overlay == "form" and self.form:
                provider_before = self._new_thread_provider()
                self.form.key(key)
                self._sync_new_thread_provider(provider_before)
                return
            if self.overlay in {"search", "projects", "palette", "attention"} or (
                self.overlay == "models" and self.model_focus == "models"
            ):
                self.search_query += key
                self.search_index = 1 if self.overlay == "projects" else 0
                return
            if self.overlay is None and self.focus == "composer" and self.workspace.selected_thread is not None:
                self._insert(key)
                return
        key = ord(key) if isinstance(key, str) else key
        if key == curses.KEY_RESIZE and isinstance(raw_key, int):
            return
        if key == 17:  # Ctrl+Q works from every view.
            self.running = False
            return
        if key == curses.KEY_MOUSE:
            try:
                _, x, y, _, buttons = curses.getmouse()
            except curses.error:
                return
            if buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                for row, col, height, width, action in reversed(self._mouse_targets):
                    if row <= y < row + height and col <= x < col + width:
                        action()
                        break
            return
        if self._last_escape:
            self._last_escape = False
            if key in (10, 13, curses.KEY_ENTER):
                self._insert("\n")
                return
        if key == 27:
            if self.overlay == "form" and self.form and self.form.busy:
                self.status = "Working… please wait for the result"
                return
            if self.overlay == "models" and self.model_focus == "settings":
                self.model_focus = "models"
                self.model_selected = None
            elif self.overlay == "models" and self.model_context and self.model_context.get("form") is self.form:
                self.overlay = "form"
                self.model_context = None
            elif self.overlay == "agents":
                key_for_agents = self._agent_key()
                if key_for_agents is not None:
                    self.expanded_agents.discard(key_for_agents)
                self.overlay = None
            elif self.overlay:
                self.overlay, self.form = None, None
                self.approval_choice = None
                self.model_context = None
            else:
                self._last_escape = True
            return
        if self.overlay:
            if self.overlay == "form" and self.form:
                if key == curses.KEY_F4 and self.form.title == "New thread":
                    self._open_creation_model_picker()
                    return
                provider_before = self._new_thread_provider()
                self.form.key(raw_key)
                self._sync_new_thread_provider(provider_before)
            else:
                self._handle_overlay_key(key)
            return
        if key == curses.KEY_F1:
            self.overlay = "help"
        elif key == 11:  # Ctrl+K
            self._open_palette()
        elif key == 16:  # Ctrl+P
            self._open_thread_search()
        elif key == 14:  # Ctrl+N
            self._new_thread_form()
        elif key == 4:  # Ctrl+D
            self._open_diff()
        elif key == 24:  # Ctrl+X
            self._cancel_selected()
        elif key == 25:  # Ctrl+Y: explicit idempotent retry after uncertain send.
            self._send_prompt(retry=True)
        elif key in (curses.KEY_F2, 7):
            self._show_machines()
        elif key == 15:  # Ctrl+O
            self._open_project_picker()
        elif key == curses.KEY_F3:
            self._new_project_form()
        elif key == curses.KEY_F4:
            self._model_form()
        elif key == curses.KEY_F5:
            self._open_attention()
        elif key == curses.KEY_F6:
            self._open_approval()
        elif key == curses.KEY_F7:
            self._toggle_tools()
        elif key == curses.KEY_F8:
            self._toggle_enter_sends()
        elif key == curses.KEY_F9:
            self._toggle_agents()
        elif key == 9:
            self.focus = "sidebar" if self.focus == "composer" else "composer"
        elif self.focus == "sidebar":
            self._handle_tree_key(key)
        elif self.workspace.selected_thread is None:
            if key in (10, 13, curses.KEY_ENTER, ord("n")):
                self._new_thread_form()
            elif key == ord("?"):
                self.overlay = "help"
        else:
            self._handle_composer_key(key)

    def _show_machines(self) -> None:
        self.overlay = "machines"

    def _open_thread_search(self) -> None:
        self.overlay, self.search_query, self.search_index = "search", "", 0

    def _open_palette(self) -> None:
        self.overlay, self.search_query, self.search_index = "palette", "", 0

    def _open_attention(self) -> None:
        self.overlay, self.search_query, self.search_index = "attention", "", 0

    def _open_approval(self, selected: tuple[str, dict[str, Any]] | None = None) -> None:
        self.approval_choice = selected or self._selected_approval()
        self.approval_scroll = 0
        self.overlay = "approval"

    def _open_diff(self) -> None:
        machine_id = self.workspace.selected_machine_id
        thread_id = (self.workspace.selected_thread or {}).get("id")
        if not thread_id:
            self.status = "Select a thread before reviewing changes"
            self.overlay = None
            return
        self.overlay, self.diff, self.diff_scroll = "diff", None, 0
        self.diff_files, self.diff_focus = [], "files"
        self.diff_file_index = self.diff_file_scroll = 0
        self.diff_machine_id, self.diff_thread_id = machine_id, str(thread_id)
        self.spawn(
            self.workspace.get_diff(machine_id=machine_id, thread_id=str(thread_id)),
            success="Diff loaded", callback=self._set_initial_diff,
        )

    def _cancel_selected(self) -> None:
        machine_id = self.workspace.selected_machine_id
        thread = self.workspace.selected_thread
        if thread and thread.get("state") in ACTIVE_STATES:
            self.spawn(self.workspace.cancel_thread(str(thread["id"]), machine_id=machine_id), success="Cancellation requested")
        else:
            self.status = "The selected thread has no active run to cancel"

    def _rename_thread_form(self) -> None:
        thread = self.workspace.selected_thread
        if thread:
            self._show_form("Rename thread", [("title", str(thread.get("title", "")))], self._submit_rename)
        else:
            self.status = "Select a thread before renaming it"

    def _archive_selected(self) -> None:
        machine_id = self.workspace.selected_machine_id
        thread = self.workspace.selected_thread
        if not thread:
            self.status = "Select a thread before archiving it"
            return
        if thread.get("state") in ACTIVE_STATES:
            self.status = "Archive unavailable while this thread has an active run"
            return
        self.spawn(
            self.workspace.update_thread(str(thread["id"]), machine_id=machine_id, archived=True),
            success=f"Archived {thread.get('title') or 'thread'}",
        )

    def _restore_thread(self, machine_id: str, thread: dict[str, Any]) -> None:
        thread_id = str(thread.get("id") or "")
        project_id = str(thread.get("project_id") or "") or None

        def opened(_: Any) -> None:
            self.workspace.switch(machine_id, project_id, thread_id)
            self._load_selected_composer()
            self.focus = "composer"

        self.spawn(
            self.workspace.update_thread(thread_id, machine_id=machine_id, archived=False),
            success=f"Restored {thread.get('title') or 'thread'}", callback=opened,
        )

    def _toggle_tools(self) -> None:
        self.expanded_tools = not self.expanded_tools
        self.status = "Tool details expanded" if self.expanded_tools else "Tool details collapsed"

    def _toggle_enter_sends(self) -> None:
        current = bool(self.workspace.state["settings"].get("enter_sends", True))
        self.workspace.state["settings"]["enter_sends"] = not current
        self.workspace._changed(force=True)
        self.status = "Enter sends" if not current else "Enter inserts newline; Ctrl+S sends"

    def _theme_form(self) -> None:
        current = str(self.workspace.state.get("settings", {}).get("theme") or "dark")
        if current not in THEMES:
            current = "dark"
        self._show_form(
            "Theme", [("theme", current)], self._submit_theme,
            choices={"theme": list(THEMES)},
            hints={"theme": "Dark, light, terminal default background, or monochrome."},
        )

    def _submit_theme(self, values: dict[str, str]) -> None:
        theme = values.get("theme", "dark")
        if theme not in THEMES:
            theme = "dark"
        self.workspace.state.setdefault("settings", {})["theme"] = theme
        self.workspace._changed(force=True)
        self.form, self.overlay = None, None
        try:
            self._init_colors()
        except curses.error:
            self._color_enabled = False
        self.status = f"Theme changed to {theme}"

    def _thread_target(self, machine_id: str, thread: dict[str, Any]) -> str:
        machine = self.workspace.machines[machine_id]
        projects = {str(project.get("id")): project for project in self.workspace.projects(machine_id)}
        project = projects.get(str(thread.get("project_id")), {})
        title = str(thread.get("title") or thread.get("id") or "thread")
        return f"{title} · {project.get('name') or 'project'} · {machine.get('alias') or machine_id}"

    def _command_actions(self) -> list[CommandAction]:
        machine_id = self.workspace.selected_machine_id
        machine = self.workspace.selected_machine
        machine_name = str(machine.get("alias") or machine_id)
        project = self.workspace.selected_project or {}
        thread = self.workspace.selected_thread
        thread_target = self._thread_target(machine_id, thread) if thread else f"no thread selected on {machine_name}"
        active = bool(thread and thread.get("state") in ACTIVE_STATES)
        approval = self._selected_approval()
        attention_count = self._attention_count()
        actions = [
            CommandAction("new", "New thread", f"{project.get('name') or 'choose repository'} · {machine_name}", self._new_thread_form, "Ctrl+N", keywords="create conversation"),
            CommandAction("project", "Open project", machine_name, self._open_project_picker, "Ctrl+O", keywords="repository"),
            CommandAction("switch", "Switch thread", "all cached machines", self._open_thread_search, "Ctrl+P", keywords="conversation search"),
            CommandAction("model", "Change model", thread_target, self._model_form, "F4", bool(thread and not active), "Wait for or cancel the active run before changing model settings." if active else "Select a thread first.", "reasoning variant provider"),
            CommandAction("agents", "Toggle agent details", thread_target, self._toggle_agents, "F9", bool(thread and self._has_agents()), "No automatic agent activity is available for this thread.", "subagents workers"),
            CommandAction("attention", "Open attention inbox", f"{attention_count} unread result{'s' if attention_count != 1 else ''}", self._open_attention, "F5", attention_count > 0, "Nothing needs attention.", "failures approvals completed"),
            CommandAction("diff", "Review diff", thread_target, self._open_diff, "Ctrl+D", bool(thread), "Select a thread first.", "changes files patch"),
            CommandAction("cancel", "Cancel run", thread_target, self._cancel_selected, "Ctrl+X", active, "The selected thread has no active run.", "stop interrupt"),
            CommandAction("rename", "Rename thread", thread_target, self._rename_thread_form, "", bool(thread), "Select a thread first."),
            CommandAction("archive", "Archive thread", thread_target, self._archive_selected, "", bool(thread and not active), "Wait for or cancel the active run before archiving." if active else "Select a thread first."),
            CommandAction("approval", "Review approval", self._approval_target(approval), lambda a=approval: self._open_approval(a), "F6", approval is not None, "No approval is waiting.", "allow reject permission"),
            CommandAction("tools", "Toggle tool output", thread_target, self._toggle_tools, "F7", bool(thread), "Select a thread first.", "expand collapse"),
            CommandAction("theme", "Choose theme", str(self.workspace.state.get("settings", {}).get("theme") or "dark"), self._theme_form, "", True, "", "dark light terminal monochrome settings"),
            CommandAction("enter", "Toggle Enter behavior", "composer · Enter sends" if self.workspace.state["settings"].get("enter_sends", True) else "composer · Enter inserts newline", self._toggle_enter_sends, "F8", True, "", "settings send newline"),
            CommandAction("servers", "Servers", machine_name, self._show_machines, "Ctrl+G", True, "", "machines ssh connect"),
            CommandAction("help", "Keyboard help", "current window", lambda: setattr(self, "overlay", "help"), "F1", True, "", "shortcuts"),
        ]
        for archived_machine_id, archived_thread in self._archived_threads():
            target = self._thread_target(archived_machine_id, archived_thread)
            actions.append(CommandAction(
                "restore:" + str(archived_thread.get("id")),
                f"Restore {archived_thread.get('title') or 'thread'}",
                target,
                lambda mid=archived_machine_id, item=archived_thread: self._restore_thread(mid, item),
                keywords="unarchive archived conversation",
            ))
        return actions

    def _archived_threads(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (machine_id, thread)
            for machine_id in self.workspace.machines
            for thread in self.workspace.threads(machine_id, include_archived=True)
            if thread.get("archived")
        ]

    def _approval_target(self, approval: tuple[str, dict[str, Any]] | None) -> str:
        if not approval:
            return "no pending approval"
        machine_id, record = approval
        thread_id = str(record.get("thread_id") or "")
        thread = next((item for item in self.workspace.threads(machine_id) if str(item.get("id")) == thread_id), {})
        return self._thread_target(machine_id, thread) if thread else str(self.workspace.machines[machine_id].get("alias") or machine_id)

    def _execute_palette_index(self, index: int) -> None:
        actions = command_palette_results(self._command_actions(), self.search_query)
        if not 0 <= index < len(actions):
            return
        self.search_index = index
        action = actions[index]
        if not action.enabled:
            self.status = action.reason
            return
        self.overlay = None
        action.run()

    def _filtered_attention_items(self) -> list[dict[str, Any]]:
        words = self.search_query.casefold().split()
        return [
            item for item in self._attention_items()
            if all(word in " ".join(str(item.get(key) or "") for key in (
                "title", "project_name", "machine_name", "state",
            )).casefold() for word in words)
        ]

    def _open_attention_index(self, index: int) -> None:
        items = self._filtered_attention_items()
        if not 0 <= index < len(items):
            return
        self.search_index = index
        item = items[index]
        machine_id = str(item.get("machine_id"))
        thread_id = str(item.get("thread_id"))
        thread = next((row for row in self.workspace.threads(machine_id) if str(row.get("id")) == thread_id), None)
        if thread is None:
            self.status = "That thread is no longer available"
            return
        self.workspace.switch(machine_id, str(thread.get("project_id")), thread_id)
        self._load_selected_composer()
        self.focus, self.overlay = "composer", None
        self.status = f"Opened {thread.get('title') or 'thread'}"

    def _open_project_picker(self) -> None:
        if not self.workspace.projects():
            self._new_project_form()
            return
        self.overlay, self.search_query, self.search_index = "projects", "", 1

    def _open_tree_row(self, index: int) -> None:
        rows = build_tree_rows(self.workspace)
        if not 0 <= index < len(rows):
            return
        self.tree_index = index
        row = rows[index]
        self.workspace.switch(row.machine_id, row.project_id, row.thread_id)
        self._load_selected_composer()
        self.focus = "composer" if row.kind == "thread" else "sidebar"
        self.status = "Ready"

    def _handle_tree_key(self, key: int) -> None:
        rows = build_tree_rows(self.workspace)
        if key == curses.KEY_UP:
            self.tree_index = max(0, self.tree_index - 1)
        elif key == curses.KEY_DOWN:
            self.tree_index = min(max(0, len(rows) - 1), self.tree_index + 1)
        elif key in (10, 13, curses.KEY_ENTER) and rows:
            self._open_tree_row(self.tree_index)
        elif key == 18 and self.workspace.selected_thread:  # Ctrl+R
            self._rename_thread_form()
        elif key == 1 and self.workspace.selected_thread:  # Ctrl+A
            self._archive_selected()

    def _handle_composer_key(self, key: int) -> None:
        if key == 19 or (key in (13, curses.KEY_ENTER) and self.workspace.state["settings"].get("enter_sends", True)):
            self._send_prompt()
        elif key in (10, 13, curses.KEY_ENTER, 15):
            self._insert("\n")
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if self.cursor:
                self.composer = self.composer[: self.cursor - 1] + self.composer[self.cursor:]
                self.cursor -= 1
                self.workspace.set_draft(self.composer)
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.composer), self.cursor + 1)
        elif key == curses.KEY_PPAGE:
            view = self.workspace.thread_view()
            self.workspace.anchor_scroll()
            view["scroll"] = int(view.get("scroll", 0)) + 10
            self.workspace.set_scroll(view["scroll"])
            events = self.workspace.view_events()
            if events and view["scroll"] >= len(events) - 20:
                machine_id = self.workspace.selected_machine_id
                thread_id = (self.workspace.selected_thread or {}).get("id")
                self.spawn(
                    self.workspace.load_older(
                        before=int(events[0].get("seq", 0)), machine_id=machine_id, thread_id=thread_id,
                    ),
                    success="Older history loaded",
                )
        elif key == curses.KEY_NPAGE:
            view = self.workspace.thread_view()
            self.workspace.set_scroll(max(0, int(view.get("scroll", 0)) - 10))
        elif 32 <= key <= 0x10FFFF and not curses.KEY_MIN <= key <= curses.KEY_MAX:
            try:
                self._insert(chr(key))
            except ValueError:
                pass

    def _insert(self, text: str) -> None:
        self.composer = self.composer[: self.cursor] + text + self.composer[self.cursor:]
        self.cursor += len(text)
        self.workspace.set_draft(self.composer)

    def _send_prompt(self, *, retry: bool = False) -> None:
        machine_id = self.workspace.selected_machine_id
        thread = self.workspace.selected_thread or {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            self.status = "Select a thread before sending"
            return
        if self._provider_warning(thread):
            self.workspace.set_draft(self.composer)
            readiness = self.workspace.selected_machine.get("providers", {}).get(str(thread.get("provider") or ""), {})
            self.status = (
                "Draft kept · provider discovery is still running"
                if readiness.get("status") == "checking"
                else "Draft kept · finish provider setup before sending"
            )
            return
        key = (machine_id, thread_id)
        if key in self._sending:
            self.status = "Sending prompt… waiting for the daemon"
            return
        prompt = self.composer
        if not retry and not prompt.strip():
            self.status = "Write a message before sending"
            return
        if retry:
            record = self.workspace.state["uncertain_sends"].get(f"{machine_id}:{thread_id}") or {}
            submitted = str(record.get("prompt") or "")
        else:
            submitted = prompt
            self.workspace.set_draft(prompt)
        self._sending.add(key)
        self.status = "Sending prompt…"

        async def send() -> Any:
            try:
                if retry:
                    return await self.workspace.retry_uncertain(thread_id, machine_id=machine_id)
                return await self.workspace.send_prompt(prompt, machine_id=machine_id, thread_id=thread_id)
            finally:
                self._sending.discard(key)

        self.spawn(send(), success="Prompt retry accepted" if retry else "Prompt accepted",
                   callback=lambda _: self._clear_composer_if(machine_id, thread_id, submitted))

    def _clear_composer(self) -> None:
        self.composer, self.cursor = "", 0

    def _clear_composer_if(self, machine_id: str, thread_id: str | None, submitted: str) -> None:
        if (
            self.workspace.selected_machine_id == machine_id
            and (self.workspace.selected_thread or {}).get("id") == thread_id
            and self.composer == submitted
        ):
            self._clear_composer()

    def _handle_overlay_key(self, key: int) -> None:
        if self.overlay == "form" and self.form:
            self.form.key(key)
            return
        if self.overlay == "search":
            results = self.workspace.search_threads(self.search_query)
            if key == curses.KEY_UP:
                self.search_index = max(0, self.search_index - 1)
            elif key == curses.KEY_DOWN:
                self.search_index = min(max(0, len(results) - 1), self.search_index + 1)
            elif key in (10, 13, curses.KEY_ENTER) and results:
                result = results[self.search_index]
                self.workspace.switch(result["machine_id"], result["project"].get("id"), result["thread"].get("id"))
                self.composer = self.workspace.thread_view().get("draft", "")
                self.cursor, self.overlay = len(self.composer), None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.search_query = self.search_query[:-1]
                self.search_index = 0
            elif 32 <= key <= 0x10FFFF and not curses.KEY_MIN <= key <= curses.KEY_MAX:
                self.search_query += chr(key)
                self.search_index = 0
        elif self.overlay == "projects":
            results = project_picker_results(self.workspace, self.search_query)
            self.search_index = min(max(0, self.search_index), len(results))
            if key == curses.KEY_UP:
                self.search_index = max(0, self.search_index - 1)
            elif key == curses.KEY_DOWN:
                self.search_index = min(len(results), self.search_index + 1)
            elif key in (10, 13, curses.KEY_ENTER):
                if self.search_index == 0:
                    self._new_project_form()
                elif results:
                    result = results[self.search_index - 1]
                    project = result["project"]
                    self.workspace.switch(result["machine_id"], str(project.get("id")))
                    self._load_selected_composer()
                    self._new_thread_form()
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.search_query = self.search_query[:-1]
                self.search_index = 1
            elif 32 <= key <= 0x10FFFF and not curses.KEY_MIN <= key <= curses.KEY_MAX:
                self.search_query += chr(key)
                self.search_index = 1
        elif self.overlay == "models":
            if self.model_focus == "settings":
                fields = (self.model_context or {}).get("setting_fields") or []
                field_index = min(int((self.model_context or {}).get("setting_field_index", 0)), max(0, len(fields) - 1))
                options = ([""] + list(fields[field_index][1])) if fields else [""]
                if key in (curses.KEY_UP, curses.KEY_LEFT):
                    self.model_setting_index = max(0, self.model_setting_index - 1)
                elif key in (curses.KEY_DOWN, curses.KEY_RIGHT):
                    self.model_setting_index = min(len(options) - 1, self.model_setting_index + 1)
                elif key in (10, 13, curses.KEY_ENTER):
                    self._choose_model_setting(self.model_setting_index)
            else:
                rows = self._model_rows()
                if key == curses.KEY_UP:
                    self.search_index = max(0, self.search_index - 1)
                elif key == curses.KEY_DOWN:
                    self.search_index = min(max(0, len(rows) - 1), self.search_index + 1)
                elif key in (10, 13, curses.KEY_ENTER, 9) and rows:
                    self._choose_model(self.search_index)
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    self.search_query = self.search_query[:-1]
                    self.search_index = 0
                elif 32 <= key <= 0x10FFFF and not curses.KEY_MIN <= key <= curses.KEY_MAX:
                    self.search_query += chr(key)
                    self.search_index = 0
        elif self.overlay == "palette":
            actions = command_palette_results(self._command_actions(), self.search_query)
            if key == curses.KEY_UP:
                self.search_index = max(0, self.search_index - 1)
            elif key == curses.KEY_DOWN:
                self.search_index = min(max(0, len(actions) - 1), self.search_index + 1)
            elif key in (10, 13, curses.KEY_ENTER) and actions:
                self._execute_palette_index(self.search_index)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.search_query = self.search_query[:-1]
                self.search_index = 0
            elif 32 <= key <= 0x10FFFF and not curses.KEY_MIN <= key <= curses.KEY_MAX:
                self.search_query += chr(key)
                self.search_index = 0
        elif self.overlay == "attention":
            items = self._filtered_attention_items()
            if key == curses.KEY_UP:
                self.search_index = max(0, self.search_index - 1)
            elif key == curses.KEY_DOWN:
                self.search_index = min(max(0, len(items) - 1), self.search_index + 1)
            elif key in (10, 13, curses.KEY_ENTER) and items:
                self._open_attention_index(self.search_index)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.search_query = self.search_query[:-1]
                self.search_index = 0
            elif 32 <= key <= 0x10FFFF and not curses.KEY_MIN <= key <= curses.KEY_MAX:
                self.search_query += chr(key)
                self.search_index = 0
        elif self.overlay == "agents":
            lines = self._agent_overlay_lines()
            if key in (curses.KEY_F9, 10, 13, curses.KEY_ENTER):
                agent_key = self._agent_key()
                if agent_key is not None:
                    self.expanded_agents.discard(agent_key)
                self.overlay = None
            elif key in (curses.KEY_DOWN, ord("j")):
                self.agent_scroll = min(max(0, len(lines) - 1), self.agent_scroll + 1)
            elif key in (curses.KEY_UP, ord("k")):
                self.agent_scroll = max(0, self.agent_scroll - 1)
            elif key == curses.KEY_NPAGE:
                self.agent_scroll = min(max(0, len(lines) - 1), self.agent_scroll + 10)
            elif key == curses.KEY_PPAGE:
                self.agent_scroll = max(0, self.agent_scroll - 10)
        elif self.overlay == "diff":
            if key == 9:
                self.diff_focus = "patch" if self.diff_focus == "files" else "files"
            elif self.diff_focus == "files":
                if key in (curses.KEY_DOWN, ord("j")):
                    self.diff_file_index = min(max(0, len(self.diff_files) - 1), self.diff_file_index + 1)
                elif key in (curses.KEY_UP, ord("k")):
                    self.diff_file_index = max(0, self.diff_file_index - 1)
                elif key in (10, 13, curses.KEY_ENTER) and self.diff_files:
                    path = str(self.diff_files[self.diff_file_index].get("path"))
                    self.diff_focus, self.diff_scroll = "patch", 0
                    self.status = f"Loading diff for {path}…"
                    self.spawn(
                        self.workspace.get_diff(
                            path, machine_id=self.diff_machine_id, thread_id=self.diff_thread_id,
                        ),
                        success=f"Loaded diff for {path}", callback=self._set_file_diff,
                    )
            else:
                lines = str((self.diff or {}).get("diff", "")).splitlines()
                if key in (curses.KEY_DOWN, ord("j")):
                    self.diff_scroll = min(max(0, len(lines) - 1), self.diff_scroll + 1)
                elif key in (curses.KEY_UP, ord("k")):
                    self.diff_scroll = max(0, self.diff_scroll - 1)
                elif key == curses.KEY_NPAGE:
                    self.diff_scroll = min(max(0, len(lines) - 1), self.diff_scroll + 15)
                elif key == curses.KEY_PPAGE:
                    self.diff_scroll = max(0, self.diff_scroll - 15)
        elif self.overlay == "machines" and key in (ord("a"), ord("A")):
            self._remote_server_form()
        elif self.overlay == "approval" and key in (ord("y"), ord("n")):
            selected = self.approval_choice
            if selected:
                machine_id, approval = selected
                request_id = str(approval.get("request_id") or approval.get("id"))
                decision = "allow" if key == ord("y") else "reject"
                self.spawn(
                    self.workspace.decide_approval(request_id, decision, machine_id=machine_id),
                    success="Allowed once" if decision == "allow" else "Approval rejected",
                )
                self.approval_choice = None
                self.overlay = None
        elif self.overlay == "approval":
            if key in (curses.KEY_DOWN, ord("j")):
                self.approval_scroll = min(max(0, self.approval_line_count - 1), self.approval_scroll + 1)
            elif key in (curses.KEY_UP, ord("k")):
                self.approval_scroll = max(0, self.approval_scroll - 1)
            elif key == curses.KEY_NPAGE:
                self.approval_scroll = min(max(0, self.approval_line_count - 1), self.approval_scroll + 15)
            elif key == curses.KEY_PPAGE:
                self.approval_scroll = max(0, self.approval_scroll - 15)

    def _set_initial_diff(self, result: dict[str, Any]) -> None:
        self.diff = result
        self.diff_files = list(result.get("files", []))
        self.diff_file_index = self.diff_file_scroll = self.diff_scroll = 0

    def _set_file_diff(self, result: dict[str, Any]) -> None:
        self.diff = result
        self.diff_scroll = 0

    def _show_form(self, title: str, fields: list[tuple[str, str]], submit: Callable[[dict[str, str]], None], **options: Any) -> None:
        self._form_machine_id = self.workspace.selected_machine_id
        self._form_selection = self._selection()
        self.form, self.overlay = Form(title, fields, submit, **options), "form"

    def _default_project_path(self) -> str:
        project = self.workspace.selected_project
        if project:
            return str(project.get("path", ""))
        return os.getcwd() if self.workspace.selected_machine.get("host") is None else ""

    def _new_thread_provider(self) -> str | None:
        if not self.form or self.form.title != "New thread":
            return None
        return next((self.form.values[index] for index, field in enumerate(self.form.fields) if field[0] == "provider"), None)

    def _sync_new_thread_provider(self, previous: str | None) -> None:
        current = self._new_thread_provider()
        if current is None or current == previous or not self.form:
            return
        self.form.set_value("model", "Provider default")
        self._new_thread_model_settings = {}

    def _open_creation_model_picker(self) -> None:
        form = self.form
        provider = self._new_thread_provider()
        if not form or form.title != "New thread" or not provider:
            return
        try:
            value = next(form.values[index] for index, field in enumerate(form.fields) if field[0] == "model")
        except StopIteration:
            value = "Provider default"
        self._open_model_picker(
            provider=provider,
            current_model=None if value == "Provider default" else value,
            settings=self._new_thread_model_settings,
            target="new thread",
            form=form,
        )

    def _new_thread_form(self) -> None:
        providers = self.workspace.selected_machine.get("providers", {})
        preferred = str((self.workspace.selected_thread or {}).get("provider") or "codex")
        if not providers.get(preferred, {}).get("available") and providers.get("opencode", {}).get("available"):
            preferred = "opencode"
        self._new_thread_model_settings = {}
        self._show_form(
            "New thread", [("path", self._default_project_path()), ("title", "New conversation"),
                           ("provider", preferred), ("model", "Provider default"), ("isolation", "Shared checkout")],
            self._submit_thread,
            choices={"provider": ["codex", "opencode"], "isolation": ["Shared checkout", "New worktree"]},
            selectors={"model": lambda _: self._open_creation_model_picker()},
            labels={"path": "Repository folder", "title": "Thread name", "provider": "Coding agent",
                    "model": "Model", "isolation": "Working files"},
            hints={"path": "Existing Git repository on this machine. Type to replace; Ctrl+U clears.",
                   "title": "A name you can find later.",
                   "provider": "← → choose. Codex defaults to YOLO: full access, no approval prompts.",
                   "model": "Enter opens every discovered model. Provider default never guesses a model.",
                   "isolation": "Shared checkout uses existing files. A new worktree isolates this thread."},
        )

    def _new_project_form(self) -> None:
        self._show_form("Add repository", [("path", self._default_project_path()), ("name", "")], self._submit_project,
                        labels={"path": "Repository folder", "name": "Display name (optional)"},
                        hints={"path": "Existing Git repository. Type to replace; Ctrl+U clears.",
                               "name": "Leave blank to use the folder name."})

    def _run_form_request(self, awaitable: Awaitable[Any], *, success: str,
                          callback: Callable[[Any], None] | None = None) -> None:
        form = self.form
        if form is None or form.busy:
            if hasattr(awaitable, "close"):
                awaitable.close()
            return
        form.busy, form.error = True, ""

        def completed(result: Any) -> None:
            form.busy = False
            if self.form is form:
                self.form, self.overlay = None, None
                if callback:
                    callback(result)

        def failed(exc: Exception) -> None:
            form.busy = False
            form.error = str(exc)

        self.spawn(awaitable, success=success, callback=completed, on_error=failed)

    def _model_form(self) -> None:
        thread = self.workspace.selected_thread
        if not thread:
            self.status = "Select a thread before changing model settings"
            return
        if thread.get("state") in ACTIVE_STATES:
            self.status = "Model change unavailable during an active run · cancel it or wait; the current run is unchanged"
            return
        provider_name = str(thread.get("provider", "codex"))
        settings = thread.get("settings") if isinstance(thread.get("settings"), dict) else {}
        self._open_model_picker(
            provider=provider_name,
            current_model=str(thread.get("model") or "") or None,
            settings=dict(settings),
            target=str(thread.get("title") or "thread"),
            machine_id=self.workspace.selected_machine_id,
            thread_id=str(thread["id"]),
        )

    def _open_model_picker(
        self,
        *,
        provider: str,
        current_model: str | None,
        settings: dict[str, Any],
        target: str,
        machine_id: str | None = None,
        thread_id: str | None = None,
        form: Form | None = None,
    ) -> None:
        machine_id = machine_id or self.workspace.selected_machine_id
        self.model_context = {
            "provider": provider,
            "current_model": current_model,
            "settings": dict(settings),
            "target": target,
            "machine_id": machine_id,
            "thread_id": thread_id,
            "form": form,
        }
        self.model_focus = "models"
        self.model_selected = None
        self.search_query = ""
        rows = model_picker_results(
            self.workspace, machine_id=machine_id, provider_name=provider, current_model=current_model,
        )
        self.search_index = next((index for index, row in enumerate(rows) if row.get("id") == current_model), 0)
        self.overlay = "models"

    def _choose_model(self, index: int) -> None:
        rows = self._model_rows()
        if not 0 <= index < len(rows):
            return
        self.search_index = index
        selected = rows[index]
        self.model_selected = selected
        fields: list[tuple[str, list[str]]] = []
        if selected.get("reasoning_efforts"):
            fields.append(("reasoning_effort", list(selected["reasoning_efforts"])))
        if selected.get("variants"):
            fields.append(("variant", list(selected["variants"])))
        context = self.model_context or {}
        context["setting_fields"] = fields
        context["setting_field_index"] = 0
        context["setting_values"] = {}
        if not fields:
            self._apply_model_selection()
            return
        current = context.get("settings", {}).get(fields[0][0])
        options = [""] + fields[0][1]
        self.model_setting_index = options.index(str(current)) if str(current) in options else 0
        self.model_focus = "settings"

    def _choose_model_setting(self, index: int) -> None:
        context = self.model_context or {}
        fields = context.get("setting_fields") or []
        if not fields:
            self._apply_model_selection()
            return
        field_index = min(int(context.get("setting_field_index", 0)), len(fields) - 1)
        name, choices = fields[field_index]
        options = [""] + list(choices)
        index = min(max(0, index), len(options) - 1)
        context.setdefault("setting_values", {})[name] = options[index]
        if field_index + 1 < len(fields):
            context["setting_field_index"] = field_index + 1
            next_name, next_choices = fields[field_index + 1]
            current = context.get("settings", {}).get(next_name)
            next_options = [""] + list(next_choices)
            self.model_setting_index = next_options.index(str(current)) if str(current) in next_options else 0
            return
        self._apply_model_selection()

    def _apply_model_selection(self) -> None:
        context = self.model_context
        selected = self.model_selected
        if not context or selected is None:
            return
        model_id = selected.get("id")
        settings = dict(context.get("settings") or {})
        for name, value in (context.get("setting_values") or {}).items():
            if value:
                settings[name] = value
            else:
                settings.pop(name, None)
        form = context.get("form")
        if isinstance(form, Form):
            form.set_value("model", str(model_id) if model_id else "Provider default")
            self._new_thread_model_settings = settings
            self.model_context = None
            self.model_selected = None
            self.model_focus = "models"
            self.overlay = "form"
            return
        machine_id = str(context.get("machine_id"))
        thread_id = str(context.get("thread_id"))
        self.model_context = None
        self.model_selected = None
        self.model_focus = "models"
        self.overlay = None
        self.spawn(
            self.workspace.update_thread(
                thread_id, machine_id=machine_id, model=model_id, settings=settings,
            ),
            success="Model updated · applies to the next message",
        )

    def _remote_server_form(self, host: str = "") -> None:
        self._show_form(
            "Connect dev server", [("host", host), ("projects_root", "~/projects"), ("alias", "")],
            self._submit_machine,
            labels={"host": "SSH destination", "projects_root": "Projects folder on the server", "alias": "Display name (optional)"},
            hints={"host": "SSH alias or user@host, e.g. dev. First verify that ssh dev works.",
                   "projects_root": "Connect installs/starts Zeus and imports repositories here. Files stay remote.",
                   "alias": "Leave blank to use the SSH destination. Python 3.11+ is required on the server."},
        )

    def _submit_machine(self, values: dict[str, str]) -> None:
        if self.form is None or self.form.busy:
            return
        form = self.form

        def progress(message: str) -> None:
            form.progress = message
            self.status = message

        async def connect() -> dict[str, Any]:
            return await self.workspace.connect_remote(values["host"], values.get("projects_root", "~/projects"),
                                                       alias=values.get("alias", ""), on_progress=progress)

        def connected(result: dict[str, Any]) -> None:
            self.workspace.start_polling()
            self._load_selected_composer()
            self.focus = "sidebar"
            machine_id = result["machine"]["id"]
            self.tree_index = next((i for i, row in enumerate(build_tree_rows(self.workspace)) if row.machine_id == machine_id), 0)
            detail = f"Connected to {result['machine']['alias']} · {result['projects']} projects · Ctrl+O opens one"
            if result.get("warnings") or result.get("truncated"):
                detail += " · Some repositories were skipped; use Ctrl+O to add another folder"
            # spawn sets its generic status after callbacks; publish the result next tick.
            asyncio.get_running_loop().call_soon(setattr, self, "status", detail)

        self._run_form_request(connect(), success="Connected to dev server", callback=connected)

    def _submit_project(self, values: dict[str, str]) -> None:
        machine_id = self._form_machine_id or self.workspace.selected_machine_id
        if not values["path"].strip():
            if self.form:
                self.form.error = "Enter the folder of an existing Git repository."
            return
        self._run_form_request(
            self.workspace.add_project(values["path"], values["name"] or None, machine_id=machine_id),
            success="Repository added · Ctrl+N creates a thread", callback=lambda _: self._load_selected_composer(),
        )

    def _submit_thread(self, values: dict[str, str]) -> None:
        if self.form is None or self.form.busy:
            return
        machine_id = self._form_machine_id or self.workspace.selected_machine_id
        selection = self._form_selection
        path = values.get("path", "").strip()
        if not path:
            self.form.error = "Enter the folder of an existing Git repository."
            self.form.index = 0
            return
        provider = values["provider"].strip().lower()
        if provider not in {"codex", "opencode"}:
            self.form.error = "Choose Codex or OpenCode using the arrow keys."
            return
        title = values["title"].strip() or "New conversation"
        isolated = values.get("isolation") == "New worktree"
        model_value = values.get("model", "Provider default").strip()
        model = None if model_value in {"", "Provider default"} else model_value
        settings = dict(self._new_thread_model_settings)

        async def create() -> dict[str, Any]:
            canonical = str(Path(path).expanduser().resolve()) if self.workspace.machines[machine_id].get("host") is None else path
            project = next((p for p in self.workspace.projects(machine_id) if p.get("path") == canonical), None)
            if project is None:
                project = await self.workspace.add_project(path, machine_id=machine_id)
            if self._selection() == selection:
                self.workspace.switch(machine_id, str(project["id"]))
            return await self.workspace.create_thread(
                str(project["id"]), title, provider, model=model, settings=settings or None,
                worktree=isolated, machine_id=machine_id,
            )

        def opened(result: dict[str, Any]) -> None:
            if self.workspace.selected_machine_id == machine_id and (self.workspace.selected_thread or {}).get("id") == result.get("id"):
                self._load_selected_composer()
                self.focus = "composer"
                rows = build_tree_rows(self.workspace)
                self.tree_index = next((i for i, row in enumerate(rows) if row.thread_id == result.get("id") and row.machine_id == machine_id), self.tree_index)

        self._run_form_request(create(), success="Thread created · write your first message", callback=opened)

    def _submit_rename(self, values: dict[str, str]) -> None:
        machine_id = self.workspace.selected_machine_id
        thread = self.workspace.selected_thread
        if thread:
            self._run_form_request(
                self.workspace.update_thread(str(thread["id"]), machine_id=machine_id, title=values["title"]),
                success="Thread renamed",
            )

    def _load_selected_composer(self) -> None:
        self.composer = self.workspace.thread_view().get("draft", "")
        self.cursor = len(self.composer)


def run_tui(data_dir: Path | None = None, initial_host: str | None = None) -> None:
    """Run the Zeus Code terminal UI until the user exits."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Zeus Code's TUI needs an interactive terminal (TTY). Run it directly in a terminal.")
    workspace = Workspace(data_dir=data_dir)
    setup_host = None
    if initial_host:
        machine = next((m for m in workspace.machines.values() if m.get("host") == initial_host), None)
        if machine is not None and machine.get("remote_command"):
            workspace.switch(machine["id"])
        else:
            setup_host = initial_host

    def wrapped(screen: Any) -> None:
        app = TUIApplication(workspace)
        if setup_host:
            app._remote_server_form(setup_host)
        asyncio.run(app.run(screen))

    curses.wrapper(wrapped)


__all__ = [
    "TUIApplication", "TreeRow", "build_tree_rows", "conversation_lines", "event_lines", "execution_label",
    "project_picker_results", "run_tui",
]
