"""The production curses interface for Zeus Code."""

from __future__ import annotations

import asyncio
import curses
import json
import os
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .transcript import render_transcript
from .workspace import Workspace


RABBIT = "(/) Zeus Code"
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


@dataclass
class Form:
    title: str
    fields: list[tuple[str, str]]
    submit: Callable[[dict[str, str]], None]
    values: list[str] = field(default_factory=list)
    index: int = 0

    def __post_init__(self) -> None:
        if not self.values:
            self.values = [default for _, default in self.fields]

    def key(self, key: int) -> bool:
        if key in (curses.KEY_ENTER, 10, 13):
            if self.index < len(self.fields) - 1:
                self.index += 1
            else:
                self.submit({name: self.values[i] for i, (name, _) in enumerate(self.fields)})
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.values[self.index] = self.values[self.index][:-1]
            return True
        if key == 9:
            self.index = (self.index + 1) % len(self.fields)
            return True
        if 32 <= key <= 0x10FFFF:
            try:
                self.values[self.index] += chr(key)
            except ValueError:
                pass
            return True
        return False


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
                    self.handle_key(ord(key) if isinstance(key, str) else key)
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
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_GREEN, -1)

    def color(self, pair: int) -> int:
        return curses.color_pair(pair) if curses.has_colors() else 0

    @staticmethod
    def put(screen: Any, y: int, x: int, text: str, attr: int = 0, width: int | None = None) -> None:
        height, columns = screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= columns:
            return
        text = safe_terminal_text(text)
        maximum = max(0, columns - x - 1)
        if width is not None:
            maximum = min(maximum, max(0, width))
        try:
            screen.addnstr(y, x, text, maximum, attr)
        except curses.error:
            pass

    def draw(self, screen: Any) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 10 or width < 38:
            self.put(screen, 0, 0, "Zeus Code needs a terminal at least 38×10.", self.color(3))
            screen.refresh()
            return
        sidebar_width = min(34, max(24, width // 4)) if width >= 72 else 0
        self._draw_header(screen, width)
        if sidebar_width:
            self._draw_sidebar(screen, 1, sidebar_width, height - 3)
            for y in range(1, height - 2):
                self.put(screen, y, sidebar_width, "│", curses.A_DIM)
        self._draw_conversation(screen, 1, sidebar_width + (1 if sidebar_width else 0), width, height)
        self._draw_footer(screen, height - 1, width)
        if self.overlay:
            self._draw_overlay(screen, height, width)
        elif self.focus == "composer":
            composer_y = height - 5
            before = self.composer[: self.cursor]
            row = min(2, before.count("\n"))
            column = len(before.rsplit("\n", 1)[-1])
            try:
                screen.move(composer_y + 1 + row, (sidebar_width + 3 if sidebar_width else 2) + min(column, width - 5))
            except curses.error:
                pass
        screen.refresh()

    def _draw_header(self, screen: Any, width: int) -> None:
        machine = self.workspace.selected_machine
        project = self.workspace.selected_project or {}
        thread = self.workspace.selected_thread or {}
        parts = [str(machine.get("alias", "local")), str(project.get("name", "no project")), str(thread.get("title", "no thread"))]
        if thread:
            parts.extend([str(thread.get("provider", "")), str(thread.get("branch", ""))])
        connection = machine.get("connection", "disconnected")
        text = f" {RABBIT}  " + " / ".join(filter(None, parts)) + f"  [{connection}] "
        self.put(screen, 0, 0, text.ljust(width - 1), self.color(2) | curses.A_BOLD)

    def _draw_sidebar(self, screen: Any, top: int, width: int, height: int) -> None:
        rows = build_tree_rows(self.workspace)
        if rows:
            self.tree_index = max(0, min(self.tree_index, len(rows) - 1))
        self.put(screen, top, 1, "WORKSPACE", self.color(1) | curses.A_BOLD, width - 2)
        available = max(0, height - 2)
        start = max(0, min(self.tree_index - available // 2, max(0, len(rows) - available)))
        for index, row in enumerate(rows[start:start + available], start=start):
            attr = curses.A_REVERSE if self.focus == "sidebar" and index == self.tree_index else 0
            if row.kind == "thread":
                attr |= {
                    "running": self.color(1),
                    "awaiting_approval": self.color(3),
                    "completed": self.color(5),
                    "failed": self.color(4),
                }.get(row.state or "", 0)
                label = execution_label(row.state, stale=row.stale)
                suffix = f"  {label}"
                if row.attention:
                    suffix += f" !{row.attention}"
                available_text = max(1, width - 2)
                title_width = max(1, available_text - len(suffix))
                text = row.text[:title_width] + suffix
                self.put(screen, top + 1 + index - start, 1, text, attr, width - 2)
            else:
                suffix = f" !{row.attention}" if row.attention else ""
                self.put(screen, top + 1 + index - start, 1, row.text + suffix, attr, width - 2)

    def _draw_conversation(self, screen: Any, top: int, left: int, width: int, height: int) -> None:
        content_width = max(10, width - left - 2)
        bottom = height - 6
        events = self.workspace.view_events()
        visible = visible_conversation_lines(
            events, content_width, max(1, bottom - top), self.workspace.thread_view(),
            expanded_tools=self.expanded_tools,
        )
        if not events:
            hint = "Select or create a thread. Ctrl+P searches cached threads instantly."
            self.put(screen, top + 2, left + 1, hint, curses.A_DIM, content_width)
        for offset, line in enumerate(visible):
            self.put(screen, top + offset, left + 1, line, 0, content_width)
        composer_y = height - 5
        self.put(screen, composer_y, left + 1, "PROMPT", self.color(1) | curses.A_BOLD, content_width)
        rendered = self.composer.splitlines()[-3:] or [""]
        for offset in range(3):
            line = rendered[offset] if offset < len(rendered) else ""
            self.put(screen, composer_y + 1 + offset, left + 1, ("> " if offset == 0 else "  ") + line, curses.A_REVERSE if self.focus == "composer" else 0, content_width)

    def _draw_footer(self, screen: Any, y: int, width: int) -> None:
        count = self.workspace.pending_approval_count()
        keys = "Ctrl+P threads · Ctrl+N new · Ctrl+D diff · F1 help · Ctrl+Q quit"
        if count:
            keys = f"! {count} approval{'s' if count != 1 else ''}  " + keys
        if self.status:
            keys = f"{self.status}  │  {keys}"
        self.put(screen, y, 0, (" " + keys).ljust(width - 1), curses.A_REVERSE, width - 1)

    def _draw_overlay(self, screen: Any, height: int, width: int) -> None:
        box_width = min(width - 6, 86)
        box_height = min(height - 4, 22)
        x, y = (width - box_width) // 2, (height - box_height) // 2
        try:
            window = screen.derwin(box_height, box_width, y, x)
            window.erase()
            window.box()
        except curses.error:
            return
        if self.overlay == "search":
            self._overlay_search(window, box_height, box_width)
        elif self.overlay == "form" and self.form:
            self._overlay_form(window, box_height, box_width)
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
        maximum = window.getmaxyx()[1] - x - 1 if width is None else width
        try:
            window.addnstr(y, x, safe_terminal_text(text), max(0, maximum), attr)
        except curses.error:
            pass

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

    def _overlay_form(self, window: Any, height: int, width: int) -> None:
        assert self.form is not None
        self._wput(window, 0, 2, f" {self.form.title} — Esc close ", self.color(1) | curses.A_BOLD)
        for index, ((name, _), value) in enumerate(zip(self.form.fields, self.form.values)):
            self._wput(window, 2 + index * 2, 2, name.replace("_", " ").title() + ":", curses.A_BOLD)
            self._wput(window, 3 + index * 2, 2, value, curses.A_REVERSE if index == self.form.index else 0, width - 4)
        self._wput(window, height - 2, 2, "Enter next/submit · Tab next", curses.A_DIM)

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
        self._wput(window, 0, 2, " Machines — a add · Esc close ", self.color(1) | curses.A_BOLD)
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
            "Ctrl+N              create provider thread",
            "Ctrl+D              open changed-files diff",
            "Page Up / Down      browse history without following output",
            "Ctrl+X              cancel selected thread run",
            "Ctrl+Y              retry an uncertain send with the same request ID",
            "F2 / F3             machines / add project",
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

    def spawn(self, awaitable: Awaitable[Any], *, success: str = "Done", callback: Callable[[Any], None] | None = None) -> None:
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

        task.add_done_callback(finished)

    def handle_key(self, key: int) -> None:
        if self._last_escape:
            self._last_escape = False
            if key in (10, 13, curses.KEY_ENTER):
                self._insert("\n")
                return
        if key == 27:
            if self.overlay:
                self.overlay, self.form = None, None
                self.approval_choice = None
            else:
                self._last_escape = True
            return
        if self.overlay:
            self._handle_overlay_key(key)
            return
        if key == 17:  # Ctrl+Q
            self.running = False
        elif key == curses.KEY_F1:
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
            machine_id = self.workspace.selected_machine_id
            thread_id = (self.workspace.selected_thread or {}).get("id")
            prompt = self.composer
            if thread_id:
                self.spawn(
                    self.workspace.retry_uncertain(thread_id, machine_id=machine_id), success="Prompt retry accepted",
                    callback=lambda _: self._clear_composer_if(machine_id, thread_id, prompt),
                )
        elif key == curses.KEY_F2:
            self.overlay = "machines"
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
        else:
            self._handle_composer_key(key)

    def _handle_tree_key(self, key: int) -> None:
        rows = build_tree_rows(self.workspace)
        if key == curses.KEY_UP:
            self.tree_index = max(0, self.tree_index - 1)
        elif key == curses.KEY_DOWN:
            self.tree_index = min(max(0, len(rows) - 1), self.tree_index + 1)
        elif key in (10, 13, curses.KEY_ENTER) and rows:
            row = rows[self.tree_index]
            self.workspace.switch(row.machine_id, row.project_id, row.thread_id)
            self.composer = self.workspace.thread_view().get("draft", "")
            self.cursor = len(self.composer)
            self.focus = "composer" if row.kind == "thread" else "sidebar"
            self.status = "Opened cached state"
        elif key == 18 and self.workspace.selected_thread:  # Ctrl+R
            thread = self.workspace.selected_thread
            self._show_form("Rename thread", [("title", str(thread.get("title", "")))], self._submit_rename)
        elif key == 1 and self.workspace.selected_thread:  # Ctrl+A
            machine_id = self.workspace.selected_machine_id
            thread = self.workspace.selected_thread
            self.spawn(self.workspace.update_thread(str(thread["id"]), machine_id=machine_id, archived=True), success="Thread archived")

    def _handle_composer_key(self, key: int) -> None:
        if key == 19 or (key in (13, curses.KEY_ENTER) and self.workspace.state["settings"].get("enter_sends", True)):
            prompt = self.composer
            machine_id = self.workspace.selected_machine_id
            thread_id = (self.workspace.selected_thread or {}).get("id")
            self.workspace.set_draft(prompt)
            if thread_id:
                self.spawn(
                    self.workspace.send_prompt(prompt, machine_id=machine_id, thread_id=thread_id),
                    success="Prompt accepted",
                    callback=lambda _: self._clear_composer_if(machine_id, thread_id, prompt),
                )
            else:
                self.status = "Select a thread before sending"
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
        elif 32 <= key <= 0x10FFFF:
            try:
                self._insert(chr(key))
            except ValueError:
                pass

    def _insert(self, text: str) -> None:
        self.composer = self.composer[: self.cursor] + text + self.composer[self.cursor:]
        self.cursor += len(text)
        self.workspace.set_draft(self.composer)

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
            elif 32 <= key <= 0x10FFFF:
                self.search_query += chr(key)
                self.search_index = 0
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
            self._show_form("Add SSH machine", [("alias", ""), ("host", "")], self._submit_machine)
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

    def _show_form(self, title: str, fields: list[tuple[str, str]], submit: Callable[[dict[str, str]], None]) -> None:
        self.form, self.overlay = Form(title, fields, submit), "form"

    def _new_thread_form(self) -> None:
        if not self.workspace.selected_project:
            self.status = "Select a project before creating a thread"
            return
        self._show_form(
            "New thread", [("title", "New conversation"), ("provider", "codex"), ("model", ""), ("worktree_y_n", "n")],
            self._submit_thread,
        )

    def _new_project_form(self) -> None:
        default_path = os.getcwd() if self.workspace.selected_machine.get("host") is None else "~"
        self._show_form("Add project", [("path", default_path), ("name", "")], self._submit_project)

    def _model_form(self) -> None:
        thread = self.workspace.selected_thread
        if not thread:
            self.status = "Select a thread before changing model settings"
            return
        provider_name = str(thread.get("provider", "codex"))
        provider = self.workspace.selected_machine.get("providers", {}).get(provider_name, {})
        models = provider.get("models") or []
        options = ", ".join(
            str(model.get("name") or model.get("id")) if isinstance(model, dict) else str(model)
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

    def _submit_machine(self, values: dict[str, str]) -> None:
        try:
            machine = self.workspace.add_machine(values["alias"], values["host"])
            self.workspace.start_polling()
            self.overlay, self.form = None, None
            self.status = f"Added {machine['alias']}"
        except Exception as exc:
            self.status = str(exc)

    def _submit_project(self, values: dict[str, str]) -> None:
        machine_id = self.workspace.selected_machine_id
        self.overlay, self.form = None, None
        self.spawn(
            self.workspace.add_project(values["path"], values["name"] or None, machine_id=machine_id),
            success="Project added",
        )

    def _submit_thread(self, values: dict[str, str]) -> None:
        project = self.workspace.selected_project
        if not project:
            return
        machine_id = self.workspace.selected_machine_id
        provider = values["provider"].strip().lower()
        if provider not in {"codex", "opencode"}:
            self.status = "Provider must be codex or opencode"
            return
        self.overlay, self.form = None, None
        self.spawn(
            self.workspace.create_thread(
                str(project["id"]), values["title"], provider, model=values["model"] or None,
                worktree=values["worktree_y_n"].strip().lower().startswith("y"), machine_id=machine_id,
            ),
            success="Thread created",
            callback=lambda _: self._load_selected_composer(),
        )

    def _submit_rename(self, values: dict[str, str]) -> None:
        machine_id = self.workspace.selected_machine_id
        thread = self.workspace.selected_thread
        self.overlay, self.form = None, None
        if thread:
            self.spawn(
                self.workspace.update_thread(str(thread["id"]), machine_id=machine_id, title=values["title"]),
                success="Thread renamed",
            )

    def _submit_model(self, values: dict[str, str]) -> None:
        machine_id = self.workspace.selected_machine_id
        thread = self.workspace.selected_thread
        self.overlay, self.form = None, None
        if thread:
            provider = str(thread.get("provider", "codex"))
            setting_name = "reasoning_effort" if provider == "codex" else "variant"
            settings = dict(thread.get("settings") or {})
            if values.get(setting_name):
                settings[setting_name] = values[setting_name]
            else:
                settings.pop(setting_name, None)
            self.spawn(
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
    if initial_host:
        machine_id = workspace.ensure_host(initial_host)
        workspace.switch(machine_id)

    def wrapped(screen: Any) -> None:
        asyncio.run(TUIApplication(workspace).run(screen))

    curses.wrapper(wrapped)


__all__ = ["TUIApplication", "TreeRow", "build_tree_rows", "conversation_lines", "event_lines", "execution_label", "run_tui"]
