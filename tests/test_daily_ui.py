"""Focused daily-workflow UI regressions, including the minimum terminal."""

from __future__ import annotations

import asyncio
import curses
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zeus_code.tui import TUIApplication, build_tree_rows
from zeus_code.workspace import Workspace


WORK_TMP = Path(__file__).resolve().parents[1] / ".work" / "tmp"


class FakeScreen:
    """Small curses surface that records visible text without a terminal."""

    def __init__(self, height: int, width: int, *, writes=None, root: bool = True):
        self.height = height
        self.width = width
        self.writes = [] if writes is None else writes
        self.root = root

    def getmaxyx(self):
        return self.height, self.width

    def bkgd(self, *_args):
        pass

    def erase(self):
        if self.root:
            self.writes.clear()

    def addstr(self, _y, _x, text, _attr=0):
        self.writes.append(str(text))

    def chgat(self, *_args):
        pass

    def refresh(self):
        pass

    def noutrefresh(self):
        pass

    def attrset(self, *_args):
        pass

    def box(self):
        pass

    def move(self, *_args):
        pass

    def derwin(self, height, width, _y, _x):
        return FakeScreen(height, width, writes=self.writes, root=False)

    @property
    def text(self) -> str:
        return "\n".join(self.writes)


class DailyUIBase:
    def make_workspace(self, *, threads=None, projects=None) -> Workspace:
        workspace = Workspace(
            cache_path=Path(self.temp.name) / "client.json",
            rpc_factory=lambda **_: None,
        )
        machine = workspace.selected_machine
        machine.update(connection="connected", stale=False, server_id="server-1")
        machine["providers"] = {"codex": {"available": True, "status": "ready"}}
        machine["snapshot"] = {
            "projects": projects or [{"id": "p1", "name": "Zeus", "path": "/repo"}],
            "threads": threads or [{
                "id": "t1", "project_id": "p1", "title": "Alpha", "provider": "codex",
                "state": "idle", "archived": False, "updated_at": "2026-09-08T12:00:00Z",
            }],
            "approvals": [],
            "last_seq": 0,
        }
        first = machine["snapshot"]["threads"][0] if machine["snapshot"]["threads"] else None
        if first:
            workspace.switch("local", str(first["project_id"]), str(first["id"]))
        return workspace


class DailyUIModelTests(DailyUIBase, unittest.TestCase):
    def setUp(self):
        WORK_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK_TMP)

    def tearDown(self):
        self.temp.cleanup()

    def test_sidebar_collapses_inactive_projects_but_keeps_running_and_pinned_work_visible(self):
        projects = [
            {"id": f"p{index}", "name": f"Project {index}", "path": f"/repo/{index}"}
            for index in range(4)
        ]
        threads = [
            {"id": "selected", "project_id": "p0", "title": "Selected", "provider": "codex", "state": "idle"},
            {"id": "inactive", "project_id": "p1", "title": "Inactive", "provider": "codex", "state": "idle"},
            {"id": "running", "project_id": "p2", "title": "Running", "provider": "codex", "state": "running"},
            {"id": "pinned", "project_id": "p3", "title": "Pinned", "provider": "codex", "state": "idle"},
        ]
        workspace = self.make_workspace(projects=projects, threads=threads)
        workspace.set_thread_pinned("local", "pinned", True)

        rows = build_tree_rows(workspace)
        projects_by_id = {row.project_id: row for row in rows if row.kind == "project"}
        visible_threads = {row.thread_id for row in rows if row.kind == "thread"}
        self.assertTrue(projects_by_id["p1"].collapsed)
        self.assertFalse(projects_by_id["p2"].collapsed)
        self.assertFalse(projects_by_id["p3"].collapsed)
        self.assertNotIn("inactive", visible_threads)
        self.assertIn("running", visible_threads)
        self.assertIn("pinned", visible_threads)

        workspace.set_project_collapsed("local", "p1", False)
        self.assertIn("inactive", {row.thread_id for row in build_tree_rows(workspace) if row.kind == "thread"})

        app = TUIApplication(workspace)
        current_project = next(
            index for index, row in enumerate(build_tree_rows(workspace))
            if row.kind == "project" and row.project_id == "p0"
        )
        app._open_tree_row(current_project)
        self.assertTrue(workspace.project_collapse_preference("local", "p0"))
        self.assertNotIn("selected", {row.thread_id for row in build_tree_rows(workspace) if row.kind == "thread"})

        # Navigating away from a running project leaves it visible in the
        # sidebar; switching to a thread always expands its target project.
        app._switch_to_thread({"machine_id": "local", "project": projects[2], "thread": threads[2]})
        app._switch_to_thread({"machine_id": "local", "project": projects[1], "thread": threads[1]})
        self.assertFalse(workspace.project_collapsed("local", "p2"))
        self.assertIn("running", {row.thread_id for row in build_tree_rows(workspace) if row.kind == "thread"})

    def test_minimum_terminal_switcher_preserves_draft_scroll_and_running_state(self):
        threads = [
            {"id": "t1", "project_id": "p1", "title": "Alpha", "provider": "codex", "state": "running"},
            {"id": "t2", "project_id": "p1", "title": "Beta", "provider": "codex", "state": "idle"},
        ]
        workspace = self.make_workspace(threads=threads)
        events = [
            {"seq": index, "thread_id": "t1", "run_id": "run-1", "kind": "message",
             "data": {"role": "assistant", "text": f"result {index}"}}
            for index in range(1, 4)
        ]
        workspace.selected_machine["events"]["t1"] = list(events)
        workspace.anchor_scroll()
        workspace.set_scroll(1)
        workspace.selected_machine["events"]["t1"].append({
            "seq": 4, "thread_id": "t1", "run_id": "run-1", "kind": "message",
            "data": {"role": "assistant", "text": "new result"},
        })
        workspace.set_draft("draft alpha")
        workspace.thread_view("t2", "local")["draft"] = "draft beta"
        app = TUIApplication(workspace)
        screen = FakeScreen(16, 48)

        app.draw(screen)
        self.assertIn("Viewing history", screen.text)
        app.handle_key(9)
        self.assertEqual(app.overlay, "search")
        app.draw(screen)
        self.assertIn("Thread switcher", screen.text)
        self.assertEqual(workspace.thread_view("t1", "local")["draft"], "draft alpha")

        for character in "Beta":
            app.handle_key(character)
        app.handle_key(13)
        self.assertEqual(workspace.selected_thread["id"], "t2")
        self.assertEqual(app.composer, "draft beta")
        app._switch_to_thread({
            "machine_id": "local", "project": workspace.projects()[0], "thread": threads[0],
        })
        self.assertEqual(app.composer, "draft alpha")
        self.assertEqual(workspace.thread_view()["scroll"], 1)
        self.assertEqual(next(item for item in workspace.threads() if item["id"] == "t1")["state"], "running")

    def test_recovery_requires_an_explicit_action_and_preserves_draft_on_edit_race(self):
        workspace = self.make_workspace()
        app = TUIApplication(workspace)
        prompt = "possibly accepted"
        workspace.set_draft(prompt)
        app.composer = prompt
        workspace.uncertain_send = lambda *_args, **_kwargs: {
            "prompt": prompt, "request_id": "request-1", "retryable": True,
        }
        workspace.dismiss_uncertain = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("The send was resolved while this dialog was open")
        )
        sent = []
        app._send_prompt = lambda **kwargs: sent.append(kwargs)

        app._open_recovery()
        self.assertEqual(app.overlay, "recovery")
        self.assertEqual(sent, [])
        recovery_screen = FakeScreen(16, 48)
        app.draw(recovery_screen)
        self.assertIn("same request ID", recovery_screen.text)
        self.assertIn("sends nothing", recovery_screen.text)
        app._edit_recovery()
        self.assertEqual(app.composer, prompt)
        self.assertEqual(workspace.thread_view()["draft"], prompt)
        self.assertIn("resolved", app.recovery_error)
        self.assertEqual(sent, [])

        workspace.uncertain_send = lambda *_args, **_kwargs: None
        workspace.failed_prompt = lambda *_args, **_kwargs: {"prompt": "failed prompt"}

        def recover(*_args, **_kwargs):
            workspace.thread_view()["draft"] = "failed prompt"
            return {"prompt": "failed prompt"}

        workspace.recover_failed_prompt = recover
        app._retry_recovery()
        self.assertEqual(sent, [{"success": "Retry started as a new run"}])

    def test_send_ack_reloads_revision_guarded_draft_instead_of_value_clearing(self):
        workspace = self.make_workspace()
        app = TUIApplication(workspace)
        # The user can edit away and back to the same bytes while send is in
        # flight. Workspace retains this newer revision even though it equals
        # the submitted value.
        workspace.set_draft("same text")
        app.composer = "same text"
        app._sync_composer_after_send("local", "t1")
        self.assertEqual(app.composer, "same text")

    def test_automatic_title_and_daily_actions_are_discoverable(self):
        workspace = self.make_workspace()
        workspace.selected_machine["snapshot"]["threads"].append({
            "id": "old", "project_id": "p1", "title": "Old work", "provider": "codex",
            "state": "completed", "archived": True,
        })
        app = TUIApplication(workspace)
        app._new_thread_form()
        title_index = next(index for index, field in enumerate(app.form.fields) if field[0] == "title")
        self.assertEqual(app.form.values[title_index], "")
        app.form = None
        app.overlay = None
        labels = {action.label for action in app._command_actions()}
        self.assertTrue({"Pin thread", "Archived threads", "Health", "Review diff"} <= labels)

    def test_diff_totals_do_not_claim_zero_when_stats_are_unavailable(self):
        app = TUIApplication(self.make_workspace())
        files = [{"path": "large.bin", "additions": 0, "deletions": 0, "stats_unavailable": True}]
        self.assertEqual(app._diff_totals(files), (None, None))
        self.assertNotIn("+0 -0", app._diff_summary_label({"files": files}, 80))


class DailyUIAsyncTests(DailyUIBase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        WORK_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK_TMP)

    async def asyncTearDown(self):
        workspace = getattr(self, "workspace", None)
        app = getattr(self, "app", None)
        if app and app.tasks:
            for task in tuple(app.tasks):
                task.cancel()
            await asyncio.gather(*app.tasks, return_exceptions=True)
        if workspace:
            await workspace.close()
        self.temp.cleanup()

    async def settle(self):
        await asyncio.sleep(0)
        if self.app.tasks:
            await asyncio.gather(*tuple(self.app.tasks), return_exceptions=True)
        await asyncio.sleep(0)

    async def test_health_stays_cached_until_check_and_update_are_explicit(self):
        self.workspace = self.make_workspace()
        self.app = TUIApplication(self.workspace)
        release_calls = []
        update_calls = []

        async def release():
            release_calls.append(True)
            return {"current_version": "1.2.0", "latest_version": "9.9.9", "update_available": True}

        async def update(data_dir):
            update_calls.append(Path(data_dir))
            return "Candidate remains runnable at ~/.local/bin/zeus-code\n" + "\n".join(
                f"detail {index}" for index in range(20)
            )

        with patch("zeus_code.tui.check_latest_release", release), patch("zeus_code.tui.update_client", update):
            self.app._open_health()
            screen = FakeScreen(16, 48)
            self.app.draw(screen)
            self.assertIn("not checked", screen.text)
            self.assertIn("c Check · r Reconnect · u Update", screen.text)
            self.assertEqual(release_calls, [])
            self.app.handle_key("c")
            await self.settle()
            self.assertEqual(len(release_calls), 1)
            self.assertEqual(self.app.health_result["latest_version"], "9.9.9")
            self.app.handle_key("u")
            await self.settle()
            self.assertEqual(len(update_calls), 1)
            self.assertEqual(self.app.overlay, "update")
            self.assertIn("~/.local/bin/zeus-code", self.app.update_output)
            self.app.handle_key(curses.KEY_NPAGE)
            self.assertGreater(self.app.update_scroll, 0)

    async def test_automatic_diff_preview_is_isolated_bounded_and_thread_scoped(self):
        threads = [
            {"id": "t1", "project_id": "p1", "title": "One", "provider": "codex", "state": "idle"},
            {"id": "t2", "project_id": "p1", "title": "Two", "provider": "codex", "state": "idle"},
        ]
        self.workspace = self.make_workspace(threads=threads)
        self.app = TUIApplication(self.workspace)
        gate = asyncio.Event()
        calls = []

        async def get_diff(path=None, *, machine_id=None, thread_id=None, independent=False):
            calls.append((path, machine_id, thread_id, independent))
            if thread_id == "t1":
                await gate.wait()
            return {"files": [{"path": f"{thread_id}.py", "additions": 1, "deletions": 0}]}

        self.workspace.get_diff = get_diff
        self.app._maybe_refresh_diff_summary()
        self.workspace.switch("local", "p1", "t2")
        self.app._load_selected_composer()
        self.app._maybe_refresh_diff_summary()
        await asyncio.sleep(0)
        gate.set()
        await self.settle()

        self.assertEqual({call[2] for call in calls}, {"t1", "t2"})
        self.assertTrue(all(call[3] for call in calls))
        self.assertEqual(self.app._selected_diff_summary()["files"][0]["path"], "t2.py")
        before = len(calls)
        self.app._maybe_refresh_diff_summary()
        await asyncio.sleep(0)
        self.assertEqual(len(calls), before)

    async def test_remote_setup_progress_and_actionable_failure_preserve_fields_at_48x16(self):
        self.workspace = self.make_workspace()
        self.app = TUIApplication(self.workspace)

        class SetupFailure(RuntimeError):
            stage = "requirements"
            action = "Install Python 3.11 on dev, then retry setup."

        async def connect(host, projects_root, *, alias="", on_progress=None):
            on_progress("Checking SSH")
            on_progress("Preparing Zeus Code")
            raise SetupFailure(f"Projects folder {projects_root} was not found on {host}")

        self.workspace.connect_remote = connect
        self.app._remote_server_form("dev")
        form = self.app.form
        form.set_value("projects_root", "/srv/projects")
        form.set_value("alias", "Dev box")
        form.submit({name: form.values[index] for index, (name, _) in enumerate(form.fields)})
        await self.settle()

        self.assertIs(self.app.form, form)
        self.assertFalse(form.busy)
        self.assertEqual(form.values, ["dev", "/srv/projects", "Dev box"])
        self.assertEqual(form.failure_stage, "requirements")
        self.assertIn("Python 3.11", form.failure_action)
        screen = FakeScreen(16, 48)
        self.app.draw(screen)
        self.assertIn("Retry setup", screen.text)
        self.assertIn("Next: Install Python", screen.text)

    async def test_wide_agent_selection_uses_stable_id_and_preserves_parent_view(self):
        self.workspace = self.make_workspace(threads=[{
            "id": "t1", "project_id": "p1", "title": "Agents", "provider": "codex", "state": "running",
        }])
        machine = self.workspace.selected_machine
        machine["runs"]["t1"] = {"id": "run-new", "state": "running"}
        machine["events"]["t1"] = [{
            "seq": 1, "thread_id": "t1", "run_id": "run-new", "kind": "tool",
            "data": {"agents": [
                {"id": "done-parent", "label": "Old parent", "state": "completed", "run_id": "run-old"},
                {"id": "live-child", "parent_id": "done-parent", "label": "Review tests", "state": "running", "run_id": "run-new"},
            ]},
        }]
        self.workspace.set_draft("parent draft")
        self.workspace.thread_view()["scroll"] = 3
        self.app = TUIApplication(self.workspace)
        self.app._screen_size = (40, 160)
        screen = FakeScreen(40, 160)
        self.app.draw(screen)

        self.assertIn("AGENTS", screen.text)
        self.assertIn("Review tests", screen.text)
        live = self.app._agent_panel_view(36)
        self.assertEqual(live.selected_id, "live-child")
        self.app._open_agent_details()
        self.assertEqual(self.app.agent_detail_id, "live-child")
        self.assertEqual(self.app.overlay, "agents")
        self.assertEqual(self.app.composer, "parent draft")
        self.assertEqual(self.workspace.thread_view()["scroll"], 3)


if __name__ == "__main__":
    unittest.main()
