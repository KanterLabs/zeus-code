import asyncio
import curses
import tempfile
import unittest
from pathlib import Path

from zeus_code.tui import (
    Form, TUIApplication, build_tree_rows, conversation_lines, event_lines, execution_label,
    safe_terminal_text, visible_conversation_lines,
)
from zeus_code.workspace import Workspace


class TUIModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Workspace(
            cache_path=Path(self.temporary.name) / "client.json",
            rpc_factory=lambda **_: None,
        )
        machine = self.workspace.selected_machine
        machine["connection"] = "disconnected"
        machine["stale"] = True
        machine["snapshot"] = {
            "projects": [{"id": "p1", "name": "Zeus", "path": "/repo"}],
            "threads": [{
                "id": "t1", "project_id": "p1", "title": "TUI", "provider": "codex",
                "state": "awaiting_approval", "archived": False, "updated_at": "2026-09-07T12:00:00Z",
            }],
            "approvals": [{
                "id": "a1", "thread_id": "t1", "state": "pending", "decision": None,
                "payload": {"command": "make test", "cwd": "/repo", "hostname": "laptop"},
            }],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_tree_is_single_machine_project_thread_hierarchy_with_text_status(self):
        rows = build_tree_rows(self.workspace)
        self.assertEqual([row.kind for row in rows], ["machine", "project", "thread"])
        self.assertEqual(rows[-1].state, "awaiting_approval")
        self.assertTrue(rows[-1].stale)
        self.assertEqual(rows[-1].attention, 1)
        self.assertIn("approval", execution_label(rows[-1].state, stale=True))
        self.assertIn("stale", execution_label(rows[-1].state, stale=True))

    def test_tool_events_are_collapsed_and_messages_wrap(self):
        tool = {"kind": "tool", "data": {"title": "Run tests", "status": "running", "text": "huge output"}}
        self.assertEqual(event_lines(tool, 80), ["▸ Run tests  running"])
        message = {"kind": "message", "data": {"role": "assistant", "text": "one two three four"}}
        lines = event_lines(message, 10)
        self.assertGreater(len(lines), 1)
        self.assertTrue(lines[0].startswith("agent:"))

    def test_stream_deltas_and_final_message_reconcile_by_item_id(self):
        events = [
            {"kind": "message_delta", "data": {"item_id": "m1", "text": "hello "}},
            {"kind": "message_delta", "data": {"item_id": "m1", "text": "world"}},
            {"kind": "message", "data": {"item_id": "m1", "role": "assistant", "text": "hello world"}},
            {"kind": "tool", "data": {"item_id": "tool1", "title": "Shell", "status": "running"}},
            {"kind": "tool", "data": {"item_id": "tool1", "title": "Shell", "status": "completed"}},
        ]
        lines = conversation_lines(events, 80)
        self.assertEqual(sum("hello world" in line for line in lines), 1)
        self.assertNotIn("running", "\n".join(lines))
        self.assertIn("completed", "\n".join(lines))

    def test_cached_search_is_recent_first_and_cross_machine(self):
        old = dict(self.workspace.threads()[0])
        old.update(id="t-old", title="Older", updated_at="2026-01-01T00:00:00Z")
        self.workspace.selected_machine["snapshot"]["threads"].append(old)
        results = self.workspace.search_threads("codex")
        self.assertEqual([item["thread"]["id"] for item in results], ["t1", "t-old"])

    def test_form_collects_fields_without_curses_screen(self):
        submitted = []
        form = Form("Machine", [("alias", ""), ("host", "")], submitted.append)
        for key in map(ord, "box"):
            form.key(key)
        form.key(10)
        for key in map(ord, "box.example"):
            form.key(key)
        form.key(10)
        self.assertEqual(submitted, [{"alias": "box", "host": "box.example"}])

    def test_scrolled_view_stays_anchored_when_new_output_arrives(self):
        events = [
            {"seq": seq, "kind": "message", "data": {"role": "assistant", "text": f"line {seq}"}}
            for seq in range(1, 8)
        ]
        view = {"scroll": 2, "anchor_seq": 7}
        before = visible_conversation_lines(events, 80, 3, view)
        events.append({"seq": 8, "kind": "message", "data": {"role": "assistant", "text": "new output"}})
        after = visible_conversation_lines(events, 80, 3, view)
        self.assertEqual(after, before)
        view["scroll"], view["anchor_seq"] = 0, None
        self.assertIn("new output", "\n".join(visible_conversation_lines(events, 80, 3, view)))

    def test_control_characters_are_safe_for_single_line_rendering(self):
        self.assertEqual(safe_terminal_text("a\x1bb\n\tc"), "a^[b↵    c")

    def test_raw_enter_and_ctrl_j_have_distinct_composer_actions(self):
        self.workspace.switch("local", "p1", "t1")
        app = TUIApplication(self.workspace)
        app.composer = "hello"
        app.cursor = len(app.composer)
        app.handle_key(10)  # Ctrl+J in raw/nonl mode.
        self.assertEqual(app.composer, "hello\n")

        spawned = []
        def capture(awaitable, **kwargs):
            spawned.append(kwargs.get("success"))
            awaitable.close()
        app.spawn = capture
        app.handle_key(13)  # Physical Enter remains carriage return under nonl().
        self.assertEqual(spawned, ["Prompt accepted"])

    def test_approval_details_are_complete_scrollable_and_pinned(self):
        app = TUIApplication(self.workspace)
        approval = self.workspace.approvals()[0]
        approval["payload"].pop("command")
        approval["payload"]["details"] = {"paths": [f"/very/long/path/{index}" for index in range(100)]}
        app.approval_choice = ("local", approval)
        lines = app._approval_lines(24)
        self.assertIn("Command: (no command", " ".join(lines))
        self.assertIn("supplied)", " ".join(lines))
        self.assertTrue(any("/very/long/path/99" in line for line in lines))
        app.overlay = "approval"
        app.approval_line_count = len(lines)
        app._handle_overlay_key(curses.KEY_NPAGE)
        self.assertGreater(app.approval_scroll, 0)
        self.assertEqual(app.approval_choice[1]["id"], "a1")


class TUIAsyncReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Workspace(cache_path=Path(self.temporary.name) / "client.json", rpc_factory=lambda **_: None)
        machine = self.workspace.selected_machine
        machine["snapshot"] = {
            "projects": [{"id": "p1", "name": "Repo", "path": "/repo"}],
            "threads": [{"id": "t1", "project_id": "p1", "title": "Review", "provider": "codex", "state": "idle", "archived": False}],
            "approvals": [],
        }
        self.workspace.switch("local", "p1", "t1")

    async def asyncTearDown(self):
        await self.workspace.close()
        self.temporary.cleanup()

    async def test_diff_file_selection_loads_scoped_patch_for_original_thread(self):
        calls = []
        async def get_diff(path=None, *, machine_id=None, thread_id=None):
            calls.append((path, machine_id, thread_id))
            return {"branch": "main", "shared": True, "files": [], "diff": f"patch for {path}", "truncated": False}
        self.workspace.get_diff = get_diff
        app = TUIApplication(self.workspace)
        app.overlay = "diff"
        app.diff = {"branch": "main", "shared": True, "files": [], "diff": "all", "truncated": True}
        app.diff_files = [
            {"path": "first.py", "status": "M", "additions": 1, "deletions": 2},
            {"path": "second.py", "status": "?", "additions": None, "deletions": None},
        ]
        app.diff_machine_id, app.diff_thread_id = "local", "t1"
        app._handle_overlay_key(curses.KEY_DOWN)
        app._handle_overlay_key(10)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(calls, [("second.py", "local", "t1")])
        self.assertEqual(app.diff["diff"], "patch for second.py")
        self.assertEqual(app.diff_focus, "patch")


if __name__ == "__main__":
    unittest.main()
