import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from zeus_code.tui import TUIApplication
from zeus_code.workspace import Workspace


class AgentSummaryUITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Workspace(
            cache_path=Path(self.temporary.name) / "client.json",
            rpc_factory=lambda **_: None,
        )
        self.workspace.selected_machine.update(connection="connected", stale=False)
        self.workspace.selected_machine["snapshot"] = {
            "projects": [{"id": "p1", "name": "Zeus", "path": "/repo"}],
            "threads": [{
                "id": "t1", "project_id": "p1", "title": "Agents", "provider": "codex",
                "state": "running", "archived": False,
            }],
            "approvals": [],
        }
        self.workspace.selected_machine["events"] = {
            "t1": [{
                "seq": 1,
                "kind": "tool",
                "data": {"agents": [
                    {
                        "id": "parent", "label": "Inspect authentication", "state": "completed",
                        "elapsed": 12, "result": "Found the validation path.",
                    },
                    {
                        "id": "child", "parent_id": "parent", "label": "Check fixtures",
                        "state": "failed", "elapsed": 3, "result": "Fixture was stale.",
                    },
                    {
                        "id": "running", "label": "Run focused tests", "state": "running",
                        "started_at": (time.time() - 5) * 1_000,
                    },
                ]},
            }],
        }
        self.workspace.switch("local", "p1", "t1")
        self.app = TUIApplication(self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    def test_collapsed_summary_counts_agents_without_sidebar_or_unknown_model(self):
        lines = self.app._agent_lines(80)
        self.assertEqual(len(lines), 1)
        self.assertIn("1 running", lines[0])
        self.assertIn("2 finished", lines[0])
        self.assertNotIn("model", lines[0].casefold())

    def test_keyboard_expansion_shows_nested_task_state_elapsed_and_result(self):
        before = deepcopy(self.workspace.thread_view())
        composer = self.app.composer
        self.app._toggle_agents()
        lines = self.app._agent_lines(80)

        self.assertTrue(lines[0].startswith("▾ Agents"))
        self.assertTrue(any("Inspect authentication · completed · 12s" in line for line in lines))
        self.assertTrue(any(line.startswith("  ↳ Check fixtures · failed · 3s") for line in lines))
        self.assertTrue(any("Fixture was stale." in line for line in lines))
        self.assertTrue(any("Run focused tests · running · 5s" in line for line in lines))
        self.assertEqual(self.workspace.thread_view(), before)
        self.assertEqual(self.app.composer, composer)

        self.app._toggle_agents()
        self.assertEqual(len(self.app._agent_lines(80)), 1)

    def test_agent_controls_offer_visibility_only(self):
        labels = [action.label.casefold() for action in self.app._command_actions()]
        self.assertIn("toggle agent details", labels)
        self.assertFalse(any("spawn agent" in label or "launch agent" in label for label in labels))

    def test_narrow_terminal_uses_scrollable_details_overlay(self):
        self.app._screen_size = (16, 48)
        self.app._toggle_agents()
        self.assertEqual(self.app.overlay, "agents")
        lines = self.app._agent_lines(40)[1:]
        self.assertTrue(any("Inspect authenticat… · completed · 12s" in line for line in lines))
        self.assertTrue(any("Check fixtures" in line for line in lines))

    def test_stale_running_agent_is_offline_and_elapsed_does_not_tick(self):
        self.workspace.selected_machine.update(connection="disconnected", stale=True)
        self.workspace.selected_machine["events"]["t1"] = [{
            "seq": 2,
            "kind": "tool",
            "data": {"agents": [{
                "id": "offline", "label": "Wait for server", "state": "running",
                "started_at": 1_000, "updated_at": 6_000,
            }]},
        }]
        self.app._toggle_agents()
        with patch("zeus_code.tui.time.time", return_value=50_000):
            first = self.app._agent_lines(80)
        with patch("zeus_code.tui.time.time", return_value=90_000):
            second = self.app._agent_lines(80)

        self.assertEqual(first, second)
        self.assertIn("cached/offline", first[0])
        self.assertTrue(any("running (offline) · 5s" in line for line in first))

    def test_long_task_label_keeps_state_and_elapsed_visible(self):
        self.workspace.selected_machine["events"]["t1"] = [{
            "seq": 3,
            "kind": "tool",
            "data": {"agents": [{
                "id": "long", "label": "x" * 300, "state": "completed", "elapsed": 7,
            }]},
        }]
        self.app._toggle_agents()
        detail = next(line for line in self.app._agent_lines(40) if "completed" in line)
        self.assertIn("…", detail)
        self.assertIn("completed · 7s", detail)
        self.assertLessEqual(len(detail), 40)

    def test_many_agents_route_to_overlay_instead_of_hiding_overflow(self):
        self.workspace.selected_machine["events"]["t1"] = [{
            "seq": 4,
            "kind": "tool",
            "data": {"agents": [
                {"id": f"agent-{index}", "label": f"Task {index}", "state": "running"}
                for index in range(12)
            ]},
        }]
        self.app._screen_size = (24, 80)
        self.app._toggle_agents()
        self.assertEqual(self.app.overlay, "agents")
        self.assertEqual(len([line for line in self.app._agent_overlay_lines() if "Task " in line]), 12)

    def test_agent_overlay_wraps_complete_public_result(self):
        result = " ".join(["result"] * 30) + " tail"
        self.workspace.selected_machine["events"]["t1"] = [{
            "seq": 5,
            "kind": "tool",
            "data": {"agents": [{
                "id": "wrapped", "label": "Summarize", "state": "completed", "result": result,
            }]},
        }]
        self.app._toggle_agents()
        lines = self.app._agent_lines(30, wrap_results=True)[1:]
        self.assertGreater(len(lines), 3)
        self.assertTrue(lines[-1].endswith("tail"))


if __name__ == "__main__":
    unittest.main()
