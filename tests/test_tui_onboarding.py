"""First-use UI regressions across the real daemon and Git boundaries."""
import asyncio
import curses
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from zeus_code.daemon import Daemon
from zeus_code.tui import TUIApplication, text_view
from zeus_code.workspace import Workspace


class EchoProvider:
    async def check(self):
        return {"available": True, "detail": "Test provider"}

    async def run(self, context, prompt):
        await context.emit("message", {"item_id": "reply", "role": "assistant", "text": "Received: " + prompt})


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        work = Path(__file__).resolve().parents[1] / ".work"
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.root = Path(self.temp.name)
        self.repo = self.root / "my repo β"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.daemon = Daemon(self.root / "state", providers={"codex": EchoProvider, "opencode": EchoProvider})
        await self.daemon.start()
        self.workspace = Workspace(data_dir=self.root / "state", cache_path=self.root / "client.json")
        await self.workspace.sync_machine("local")
        self.app = TUIApplication(self.workspace)

    async def asyncTearDown(self):
        for task in self.app.tasks:
            task.cancel()
        if self.app.tasks:
            await asyncio.gather(*self.app.tasks, return_exceptions=True)
        await self.workspace.close()
        await self.daemon.close()
        self.temp.cleanup()

    async def settle(self):
        if self.app.tasks:
            await asyncio.wait_for(asyncio.gather(*self.app.tasks, return_exceptions=True), 5)
        await asyncio.sleep(0)

    def type(self, text):
        for character in text:
            self.app.handle_key(character)

    def repository_field(self, path):
        self.app.form.index = 0
        self.app.handle_key("\x15")
        self.type(str(path))

    async def test_empty_workspace_enter_creates_named_provider_thread_and_sends(self):
        self.app.handle_key("\r")
        self.assertEqual(self.app.form.title, "New thread")
        self.repository_field(self.repo)
        self.app.handle_key("\r")
        self.type("Fix greeting")
        self.app.handle_key("\r")
        self.app.handle_key(curses.KEY_RIGHT)
        self.app.handle_key("\x13")
        await self.settle()

        self.assertIsNone(self.app.form)
        thread = self.workspace.selected_thread
        self.assertEqual(thread["title"], "Fix greeting")
        self.assertEqual(thread["provider"], "opencode")
        self.assertEqual(Path(thread["cwd"]), self.repo)
        self.assertEqual(len(self.workspace.projects()), 1)
        self.assertEqual(self.app.focus, "composer")
        self.type("Hello β")
        self.app.handle_key("\r")
        await self.settle()
        for _ in range(100):
            await self.workspace.sync_machine("local")
            if self.workspace.selected_thread["state"] == "completed":
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.workspace.selected_thread["state"], "completed")
        self.assertTrue(any(e["data"].get("text") == "Received: Hello β" for e in self.workspace.thread_events()))
        self.assertEqual(self.app.composer, "")

    async def test_invalid_repository_preserves_form_then_retry_creates_only_one_thread(self):
        self.app.handle_key("\x0e")
        form = self.app.form
        self.repository_field(self.root / "missing")
        self.app.handle_key("\x13")
        await self.settle()
        self.assertIs(self.app.form, form)
        self.assertTrue(form.error)
        self.assertFalse(form.busy)
        self.assertEqual(self.workspace.threads(), [])

        self.repository_field(self.repo)
        self.app.handle_key("\x13")
        self.app.handle_key("\x13")
        await self.settle()
        self.assertIsNone(self.app.form)
        snapshot = await self.workspace.rpc("snapshot")
        self.assertEqual(len(snapshot["threads"]), 1)
        self.assertEqual(self.workspace.selected_thread["id"], snapshot["threads"][0]["id"])

    async def test_repository_shortcut_selects_it_for_immediate_thread_creation(self):
        self.app.handle_key("\x0f")
        self.assertEqual(self.app.form.title, "Add repository")
        self.repository_field(self.repo)
        self.app.handle_key("\x13")
        await self.settle()
        self.assertEqual(Path(self.workspace.selected_project["path"]), self.repo)
        self.app.handle_key("\x0e")
        self.assertEqual(self.app.form.values[0], str(self.repo))
        self.app.handle_key("\x13")
        await self.settle()
        self.assertIsNotNone(self.workspace.selected_thread)

    def test_quit_from_creation_dialog_and_unknown_special_key_does_not_insert(self):
        self.app.handle_key("\x0e")
        value = self.app.form.values[0]
        self.app.handle_key(curses.KEY_RESIZE)
        self.assertEqual(self.app.form.values[0], value)
        self.app.handle_key("\x11")
        self.assertFalse(self.app.running)

    async def test_resize_keeps_draft_and_unicode_key_code_collisions_are_literal(self):
        self.app.handle_key("\x0e")
        self.repository_field(self.repo)
        self.app.handle_key("\x13")
        await self.settle()
        text = "draft " + chr(curses.KEY_LEFT) + chr(curses.KEY_RESIZE) + " β"
        self.type(text)
        self.app.handle_key(curses.KEY_RESIZE)
        self.app.handle_key(curses.KEY_F12)
        self.assertEqual(self.app.composer, text)
        self.assertEqual(self.workspace.thread_view()["draft"], text)

    def test_pending_creation_keeps_modal_and_field_focus_until_result(self):
        self.app.handle_key("\x0e")
        form = self.app.form
        form.busy = True
        self.app.handle_key("\x1b")
        self.app._mouse_targets = [(0, 0, 1, 1, lambda: self.app._focus_form_field(form, 2))]
        with patch("curses.getmouse", return_value=(0, 0, 0, 0, curses.BUTTON1_PRESSED)):
            self.app.handle_key(curses.KEY_MOUSE)
        self.assertIs(self.app.form, form)
        self.assertEqual(form.index, 0)
        form.busy = False
        self.app.handle_key("\x1b")
        self.assertIsNone(self.app.form)

    def test_wide_and_combining_text_caret_uses_terminal_columns(self):
        value = "A界🐇e\u0301"
        self.assertEqual(text_view(value, len(value), 8), (value, 6))
        self.assertEqual(text_view(value, len(value), 4), ("🐇e\u0301", 3))
