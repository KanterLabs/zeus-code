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

from .activity import ACTIVE_STATES, RunActivity, duration, permission_label, summarize_activity
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
        approvals = workspace.approvals(machine_id)
        rows.append(TreeRow("machine", machine_id, None, None, f"{alias} [{connection}]", attention=len(approvals)))
        threads = workspace.threads(machine_id)
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
                attention = sum(1 for approval in approvals if approval.get("thread_id") == thread_id)
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

    @staticmethod
    def _init_colors() -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        if curses.COLORS >= 256:
            # Explicit backgrounds stay legible with translucent terminal themes.
            base, panel, foreground, muted, accent = 234, 235, 252, 245, 115
            border, selected = 239, 24
            yellow, red, green = 221, 210, 150
        else:
            base = panel = curses.COLOR_BLACK
            foreground, muted, accent = curses.COLOR_WHITE, curses.COLOR_WHITE, curses.COLOR_CYAN
            border, selected = curses.COLOR_WHITE, curses.COLOR_BLUE
            yellow, red, green = curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_GREEN
        pairs = {
            1: (accent, base), 2: (curses.COLOR_BLACK, accent),
            3: (yellow, base), 4: (red, base), 5: (green, base),
            6: (foreground, base), 7: (foreground, panel), 8: (muted, panel),
            9: (border, base), 10: (muted, base), 11: (foreground, panel),
            12: (accent, panel), 13: (foreground, selected),
            14: (accent, selected), 15: (border, panel),
        }
        for pair, (foreground_color, background_color) in pairs.items():
            curses.init_pair(pair, foreground_color, background_color)

    def color(self, pair: int) -> int:
        return curses.color_pair(pair) if curses.has_colors() else 0

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

    def draw(self, screen: Any) -> None:
        screen.bkgd(" ", self.color(6))
        screen.erase()
        self._mouse_targets = []
        self._cursor_position = None
        height, width = screen.getmaxyx()
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
        self.put(screen, 1, max(24, width - len(connection) - 4), "● " + connection, self.color(5 if connection == "connected" else 3))
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
                self.put(screen, y, 3, f"{mark} {machine.get('alias', 'local')}", self.color(14 if active else 12) | curses.A_BOLD, width - 5)
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
                label = activity.marker(time.monotonic()) + " " + str(thread.get("provider", "")) + " · " + ("offline" if activity.stale else phase)
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
        activity_y = composer_y - 3
        events = self.workspace.view_events()
        if not events:
            self.put(screen, top, content_left + 1, "What would you like to build?", curses.A_BOLD, content_width - 2)
            if activity_y - top >= 5:
                self.put(screen, top + 2, content_left + 1, "Ask a question, describe a change, or paste an error.", self.color(10), content_width - 2)
                self.put(screen, top + 3, content_left + 1, "Your draft stays here when you switch conversations.", self.color(10), content_width - 2)
        else:
            visible = visible_conversation_lines(events, content_width, max(1, activity_y - top), self.workspace.thread_view(), expanded_tools=self.expanded_tools)
            for offset, line in enumerate(visible):
                attr = self.color(6)
                if line.strip() in {"you", "assistant", "agent"} or line.startswith("you:"):
                    attr = self.color(1) | curses.A_BOLD
                elif line.startswith(("▸", "▾", "—")):
                    attr = self.color(10)
                self.put(screen, top + offset, content_left, line, attr, content_width)
        self._draw_activity(screen, activity_y, content_left, content_width, thread)
        self._fill(screen, composer_y, content_left, content_width, 5, self.color(7))
        provider_warning = self._provider_warning(thread)
        border = self.color(4 if provider_warning else 12 if self.focus == "composer" else 15)
        self.put(screen, composer_y, content_left, "╭" + "─" * (content_width - 2) + "╮", border, content_width)
        mode = permission_label(thread)
        title = (
            " " + provider_warning[0] + " "
            if provider_warning
            else " Message " + str(thread.get("provider", "agent")) + (" · " + mode if mode else "") + " "
        )
        self.put(screen, composer_y, content_left + 2, title, border)
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
            else "Ctrl+X stop   F7 tool output   Ctrl+D review"
            if thread.get("state") in ACTIVE_STATES
            else f"{enter_action}   Ctrl+J newline   Ctrl+D review"
        )
        self.put(screen, height - 2, content_left, controls, self.color(4 if provider_warning else 10), content_width)
        self._mouse_targets.append((composer_y, content_left, 5, content_width, lambda: setattr(self, "focus", "composer")))

    def _thread_activity(self, thread: dict[str, Any], machine_id: str | None = None) -> RunActivity:
        machine_id = machine_id or self.workspace.selected_machine_id
        machine = self.workspace.machines[machine_id]
        return summarize_activity(
            thread, self.workspace.thread_events(str(thread["id"]), machine_id),
            self.workspace.thread_run(str(thread["id"]), machine_id), now=time.time(),
            stale=bool(machine.get("stale")) or machine.get("connection") != "connected",
        )

    def _provider_warning(self, thread: dict[str, Any]) -> tuple[str, str] | None:
        provider_name = str(thread.get("provider") or "provider")
        readiness = self.workspace.selected_machine.get("providers", {}).get(provider_name, {})
        if readiness.get("available") is not False:
            return None
        display_name = {"codex": "Codex", "opencode": "OpenCode"}.get(provider_name.casefold(), provider_name)
        machine_name = str(self.workspace.selected_machine.get("alias") or self.workspace.selected_machine_id)
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
        count = self.workspace.pending_approval_count()
        status = self.status if self.status not in {"Ready", "Opened cached state"} else ""
        if count:
            status = f"{count} approval{'s' if count != 1 else ''} waiting · F6"
        elif not status or status in {"Prompt accepted", "Prompt retry accepted"}:
            running = sum(thread.get("state") == "running" for machine_id, machine in self.workspace.machines.items()
                          if machine.get("connection") == "connected" and not machine.get("stale")
                          for thread in self.workspace.threads(machine_id))
            if running:
                status = f"{running} thread{'s' if running != 1 else ''} working · Ctrl+P"
        keys = "Ctrl+P switch   Ctrl+N new   F1 help   Ctrl+Q quit"
        if width < 80:
            keys = "^N new  ^P switch  F1 help  ^Q quit"
        if status:
            remaining = width - len(keys) - 7
            if remaining >= 12:
                self.put(screen, y, 2, status, self.color(8), remaining)
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
            self._mouse_targets = [(row + y, col + x, h, w, action) for row, col, h, w, action in self._mouse_targets]
        elif self.overlay == "diff":
            self._overlay_diff(window, box_height, box_width)
        elif self.overlay == "machines":
            self._overlay_machines(window, box_height, box_width)
        elif self.overlay == "approval":
            self._overlay_approval(window, box_height, box_width)
        elif self.overlay == "help":
            self._overlay_help(window, box_height, box_width)
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
            if choices:
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
        hint = form.hints.get(field, "← → choose" if form.choices.get(field) else "Type to replace the default · Ctrl+U clear")
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
            "Tab                 switch sidebar / prompt focus",
            "Enter / Ctrl+S      send prompt",
            "Ctrl+J / Alt+Enter  insert newline",
            "Ctrl+P              search all cached threads",
            "Ctrl+N              create thread (Enter on welcome screen)",
            "Forms               Tab move · arrows choose · Ctrl+S save",
            "Ctrl+D              open changed-files diff",
            "Page Up / Down      browse history without following output",
            "Ctrl+X              cancel selected thread run",
            "Ctrl+Y              retry an uncertain send with the same request ID",
            "Ctrl+G / Ctrl+O    machines / open project (F2 also opens machines)",
            "F3                  add repository by path",
            "F4                  model and reasoning setting",
            "F6 / F7             pending approval / expand tool details",
            "Diff Tab/↑↓/Enter   switch panes / select / load scoped patch",
            "Approval ↑↓/Pg      inspect the full pinned request",
            "F8                  toggle Enter binding (Ctrl+S always sends)",
            "Sidebar Ctrl+R/A    rename / archive thread",
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
                self.form.key(key)
                return
            if self.overlay in {"search", "projects"}:
                self.search_query += key
                self.search_index = 0 if self.overlay == "search" else 1
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
            if self.overlay:
                self.overlay, self.form = None, None
                self.approval_choice = None
            else:
                self._last_escape = True
            return
        if self.overlay:
            if self.overlay == "form" and self.form:
                self.form.key(raw_key)
            else:
                self._handle_overlay_key(key)
            return
        if key == curses.KEY_F1:
            self.overlay = "help"
        elif key == 16:  # Ctrl+P
            self.overlay, self.search_query, self.search_index = "search", "", 0
        elif key == 14:  # Ctrl+N
            self._new_thread_form()
        elif key == 4:  # Ctrl+D
            self.overlay, self.diff, self.diff_scroll = "diff", None, 0
            self.diff_files, self.diff_focus = [], "files"
            self.diff_file_index = self.diff_file_scroll = 0
            machine_id = self.workspace.selected_machine_id
            thread_id = (self.workspace.selected_thread or {}).get("id")
            self.diff_machine_id, self.diff_thread_id = machine_id, thread_id
            if thread_id:
                self.spawn(
                    self.workspace.get_diff(machine_id=machine_id, thread_id=thread_id),
                    success="Diff loaded", callback=self._set_initial_diff,
                )
            else:
                self.status, self.overlay = "Select a thread before reviewing changes", None
        elif key == 24:  # Ctrl+X
            machine_id = self.workspace.selected_machine_id
            thread_id = (self.workspace.selected_thread or {}).get("id")
            if thread_id:
                self.spawn(self.workspace.cancel_thread(str(thread_id), machine_id=machine_id), success="Cancellation requested")
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
        elif key == curses.KEY_F6:
            self.approval_choice = self._selected_approval()
            self.approval_scroll = 0
            self.overlay = "approval"
        elif key == curses.KEY_F7:
            self.expanded_tools = not self.expanded_tools
            self.status = "Tool details expanded" if self.expanded_tools else "Tool details collapsed"
        elif key == curses.KEY_F8:
            current = bool(self.workspace.state["settings"].get("enter_sends", True))
            self.workspace.state["settings"]["enter_sends"] = not current
            self.workspace._changed(force=True)
            self.status = "Enter sends" if not current else "Enter inserts newline; Ctrl+S sends"
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
            thread = self.workspace.selected_thread
            self._show_form("Rename thread", [("title", str(thread.get("title", "")))], self._submit_rename)
        elif key == 1 and self.workspace.selected_thread:  # Ctrl+A
            machine_id = self.workspace.selected_machine_id
            thread = self.workspace.selected_thread
            self.spawn(self.workspace.update_thread(str(thread["id"]), machine_id=machine_id, archived=True), success="Thread archived")

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
            self.status = "Draft kept · finish provider setup before sending"
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

    def _new_thread_form(self) -> None:
        providers = self.workspace.selected_machine.get("providers", {})
        preferred = str((self.workspace.selected_thread or {}).get("provider") or "codex")
        if not providers.get(preferred, {}).get("available") and providers.get("opencode", {}).get("available"):
            preferred = "opencode"
        self._show_form(
            "New thread", [("path", self._default_project_path()), ("title", "New conversation"),
                           ("provider", preferred), ("isolation", "Shared checkout")],
            self._submit_thread,
            choices={"provider": ["codex", "opencode"], "isolation": ["Shared checkout", "New worktree"]},
            labels={"path": "Repository folder", "title": "Thread name", "provider": "Coding agent", "isolation": "Working files"},
            hints={"path": "Existing Git repository on this machine. Type to replace; Ctrl+U clears.",
                   "title": "A name you can find later. Change model settings with F4 after creation.",
                   "provider": "← → choose. Codex defaults to YOLO: full access, no approval prompts.",
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
        provider_name = str(thread.get("provider", "codex"))
        provider = self.workspace.selected_machine.get("providers", {}).get(provider_name, {})
        models = provider.get("models") or []
        options = ", ".join(
            str(model.get("id") or model.get("name")) if isinstance(model, dict) else str(model)
            for model in models[:5]
        )
        settings = thread.get("settings") if isinstance(thread.get("settings"), dict) else {}
        setting_name = "reasoning_effort" if provider_name == "codex" else "variant"
        title = "Model settings" + (f" · available: {options}" if options else "")
        self._show_form(
            title,
            [("model", str(thread.get("model") or "")), (setting_name, str(settings.get(setting_name, "")))],
            self._submit_model,
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

        async def create() -> dict[str, Any]:
            canonical = str(Path(path).expanduser().resolve()) if self.workspace.machines[machine_id].get("host") is None else path
            project = next((p for p in self.workspace.projects(machine_id) if p.get("path") == canonical), None)
            if project is None:
                project = await self.workspace.add_project(path, machine_id=machine_id)
            if self._selection() == selection:
                self.workspace.switch(machine_id, str(project["id"]))
            return await self.workspace.create_thread(str(project["id"]), title, provider, worktree=isolated, machine_id=machine_id)

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

    def _submit_model(self, values: dict[str, str]) -> None:
        machine_id = self.workspace.selected_machine_id
        thread = self.workspace.selected_thread
        if thread:
            provider = str(thread.get("provider", "codex"))
            setting_name = "reasoning_effort" if provider == "codex" else "variant"
            settings = dict(thread.get("settings") or {})
            if values.get(setting_name):
                settings[setting_name] = values[setting_name]
            else:
                settings.pop(setting_name, None)
            self._run_form_request(
                self.workspace.update_thread(
                    str(thread["id"]), machine_id=machine_id,
                    model=values["model"] or None, settings=settings,
                ),
                success="Model settings updated",
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
