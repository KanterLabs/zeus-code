import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zeus_code.activity import duration, permission_label, summarize_activity, timestamp
from zeus_code.transcript import render_transcript
from zeus_code.tui import TUIApplication
from zeus_code.workspace import Workspace


BASE = timestamp("2026-09-07T12:00:00Z")


def event(kind, data, second=0, run_id="run-1", seq=1):
    return {"seq": seq, "thread_id": "t1", "run_id": run_id, "kind": kind, "data": data,
            "created_at": f"2026-09-07T12:00:{second:02d}Z"}


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.thread = {"id": "t1", "state": "running", "provider": "codex"}
        self.run = {"id": "run-1", "created_at": "2026-09-07T12:00:00Z"}

    def activity(self, events, **kwargs):
        return summarize_activity(self.thread, events, self.run, now=BASE + 20, **kwargs)

    def test_quiet_thinking_keeps_timer_and_animation_without_inventing_events(self):
        events = [event("status", {"phase": "thinking", "text": "Thinking"}, 4)]
        result = self.activity(events)
        later = summarize_activity(self.thread, events, self.run, now=BASE + 35)
        self.assertEqual(result.label, "Thinking")
        self.assertEqual((result.elapsed, result.quiet_for), (20, 16))
        self.assertEqual((later.elapsed, later.quiet_for), (35, 31))
        self.assertNotEqual(result.marker(0), result.marker(.15))
        self.assertEqual(len(events), 1)

    def test_command_output_preserves_command_title_and_completes_cleanly(self):
        events = [
            event("tool", {"item_id": "cmd", "tool_type": "commandExecution", "title": "python3 -m unittest", "status": "running"}),
            event("tool", {"item_id": "cmd", "title": "Command", "status": "running", "delta": True, "text": "some output"}, 5),
        ]
        result = self.activity(events)
        self.assertEqual((result.label, result.detail), ("Running command", "python3 -m unittest"))
        events.append(event("tool", {"item_id": "cmd", "status": "completed"}, 8))
        events.append(event("message_delta", {"text": "Done"}, 9))
        result = self.activity(events)
        self.assertEqual(result.label, "Writing response")

    def test_overlapping_tools_remain_visible_until_each_finishes(self):
        events = [event("tool", {"item_id": item, "title": item, "status": "running"}) for item in ("one", "two")]
        self.assertEqual(self.activity(events).detail, "two (+1 more active)")
        events.append(event("tool", {"item_id": "two", "status": "completed"}))
        self.assertEqual(self.activity(events).detail, "one")

    def test_old_run_tools_and_status_never_appear_on_new_turn(self):
        events = [
            event("tool", {"item_id": "cmd", "title": "old command", "status": "running"}, run_id="old"),
            event("run_state", {"state": "failed", "error": "old failure"}, run_id="old"),
            event("status", {"phase": "starting", "text": "Resuming Codex"}, 10),
        ]
        result = self.activity(events)
        self.assertEqual(result.label, "Starting Codex")
        self.assertEqual(result.detail, "Resuming Codex")
        self.assertNotIn("old", repr(result))

    def test_terminal_outcomes_stop_animation_and_freeze_elapsed(self):
        for state, label in (("completed", "Completed"), ("failed", "Failed"), ("cancelled", "Stopped"), ("interrupted", "Interrupted")):
            with self.subTest(state=state):
                self.thread["state"] = state
                result = self.activity([event("run_state", {"state": state, "error": "Specific failure" if state == "failed" else None}, 8)])
                self.assertEqual(result.label, label)
                self.assertEqual(result.elapsed, 8)
                self.assertFalse(result.active)
                self.assertEqual(result.marker(0), result.marker(10))
                if state == "failed":
                    self.assertEqual(result.detail, "Specific failure")

    def test_offline_activity_is_frozen_and_explicitly_last_known(self):
        result = self.activity([event("status", {"phase": "thinking"}, 6)], stale=True)
        self.assertEqual(result.elapsed, 6)
        self.assertEqual(result.quiet_for, 14)
        self.assertFalse(result.active)
        self.assertEqual(result.marker(0), result.marker(10))
        self.assertIn("Last known: thinking", result.detail)
        self.assertIn("offline", result.headline(10))

    def test_approval_never_looks_like_active_execution(self):
        self.thread["state"] = "awaiting_approval"
        result = self.activity([event("tool", {"item_id": "cmd", "status": "running"})])
        self.assertEqual(result.label, "Waiting for approval")
        self.assertFalse(result.active)
        self.assertIn("F6", result.detail)

    def test_new_run_state_wins_over_older_snapshot(self):
        self.thread["state"] = "completed"
        self.run["state"] = "running"
        self.assertTrue(self.activity([]).active)

    def test_offline_failure_keeps_error_and_never_claims_work_continues(self):
        self.thread["state"] = "failed"
        result = self.activity([event("run_state", {"state": "failed", "error": "Sign in again"}, 4)], stale=True)
        self.assertIn("Sign in again", result.detail)
        self.assertNotIn("may continue", result.detail)

    def test_trimmed_events_keep_metadata_elapsed_without_guessing_start(self):
        self.run["created_at"] = "2026-09-07T11:00:00Z"
        result = self.activity([event("message_delta", {"text": "reply"}, 15)])
        self.assertEqual(result.elapsed, 3620)
        self.run["created_at"] = None
        self.assertIsNone(self.activity([]).elapsed)
        self.assertEqual(duration(3620), "1h 00m")

    def test_yolo_badge_matches_defaults_and_explicit_settings(self):
        self.assertEqual(permission_label(self.thread), "YOLO")
        self.thread["settings"] = {"sandbox": "workspace-write"}
        self.assertEqual(permission_label(self.thread), "Custom permissions")
        self.thread["settings"] = {"approvalPolicy": "on-request", "sandbox": "dangerFullAccess"}
        self.assertEqual(permission_label(self.thread), "Custom permissions")
        self.thread["provider"] = "opencode"
        self.assertEqual(permission_label(self.thread), "")

    def test_generic_phases_stay_out_of_conversation_but_warnings_remain(self):
        events = [event("status", {"phase": "thinking", "text": "Thinking"}),
                  event("status", {"text": "A provider warning"}), event("reasoning", {"text": "private reasoning"})]
        self.assertEqual(render_transcript(events, 80), ["— A provider warning"])


class ActivityUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(cache_path=Path(self.temp.name) / "client.json", rpc_factory=lambda **_: None)
        machine = self.workspace.selected_machine
        machine.update(connection="connected", stale=False)
        machine["snapshot"]["threads"] = [{"id": "t1", "provider": "codex", "state": "running", "title": "Build"}]
        self.workspace.switch("local", thread_id="t1")
        self.app = TUIApplication(self.workspace)

    async def asyncTearDown(self):
        if self.app.tasks:
            await asyncio.gather(*self.app.tasks)
        await self.workspace.close()
        self.temp.cleanup()

    async def test_sending_feedback_is_immediate_and_double_submit_is_deduplicated(self):
        waiting = asyncio.Event()
        calls = []

        async def send(prompt, **kwargs):
            calls.append(prompt)
            await waiting.wait()
            # Match Workspace.send_prompt's acknowledged-send contract. The
            # TUI mirrors the revision-guarded workspace draft after success.
            self.workspace.set_draft("")
            return {"id": "run-1", "state": "running"}

        self.workspace.send_prompt = send
        self.app.composer = "Please build"
        self.app.handle_key("\r")
        self.app.handle_key("\r")
        self.assertIn(("local", "t1"), self.app._sending)
        self.assertIn("Sending", self.app.status)
        await asyncio.sleep(0)
        self.assertEqual(calls, ["Please build"])
        waiting.set()
        await asyncio.gather(*self.app.tasks)
        await asyncio.sleep(0)
        self.assertEqual(self.app._sending, set())
        self.assertEqual(self.app.composer, "")

    async def test_live_activity_uses_current_events_while_history_is_scrolled(self):
        machine = self.workspace.selected_machine
        Workspace._apply_event(machine, event("run_state", {"state": "running"}, seq=1))
        self.workspace.thread_view().update(scroll=10, anchor_seq=1)
        Workspace._apply_event(machine, event("tool", {"item_id": "tool", "tool_type": "fileChange", "title": "File changes", "status": "running"}, 5, seq=2))
        lines = []
        self.app.put = lambda screen, y, x, text, *args, **kwargs: lines.append((y, text))
        self.app.color = lambda _: 0
        with patch("zeus_code.tui.time.time", return_value=BASE + 12):
            self.app._draw_activity(None, 8, 3, 74, self.workspace.selected_thread)
        rendered = "\n".join(text for _, text in lines)
        self.assertIn("Editing files", rendered)
        self.assertIn("12s", rendered)
        self.assertIn("File changes", rendered)
        self.assertEqual(self.workspace.thread_view()["anchor_seq"], 1)

    async def test_retry_of_old_uncertain_prompt_preserves_new_draft(self):
        self.workspace.state["uncertain_sends"]["local:t1"] = {"prompt": "old request"}
        self.app.composer = "new follow-up"
        self.workspace.set_draft(self.app.composer)

        async def retry(*args, **kwargs):
            return {"id": "run-1", "state": "running"}

        self.workspace.retry_uncertain = retry
        self.app.handle_key("\x19")
        await asyncio.gather(*self.app.tasks)
        await asyncio.sleep(0)
        self.assertEqual(self.app.composer, "new follow-up")
        self.assertEqual(self.workspace.thread_view()["draft"], "new follow-up")

    def test_minimum_height_sidebar_keeps_selected_thread_and_activity_visible(self):
        machine = self.workspace.selected_machine
        machine["snapshot"]["projects"] = [{"id": "p1", "name": "Repo"}]
        machine["snapshot"]["threads"][0]["project_id"] = "p1"
        lines = []
        self.app.put = lambda screen, y, x, text, *args, **kwargs: lines.append((y, text))
        self.app.color = lambda _: 0
        self.app._draw_sidebar(None, 4, 25, 9)
        self.assertIn("Build", [text for _, text in lines])
        status = [(y, text) for y, text in lines if "codex ·" in text]
        self.assertEqual(len(status), 1)
        self.assertLess(status[0][0], 11)


if __name__ == "__main__":
    unittest.main()
