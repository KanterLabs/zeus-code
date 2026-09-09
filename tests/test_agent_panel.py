from __future__ import annotations

import math
import unittest
import unicodedata

from zeus_code.agent_panel import build_agent_panel


def cells(text: str) -> int:
    total = 0
    for character in text:
        if unicodedata.combining(character) or unicodedata.category(character) in {"Mn", "Me", "Cf"}:
            continue
        total += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return total


class AgentPanelTests(unittest.TestCase):
    def test_live_view_orders_running_branches_and_collapses_finished_agents(self):
        summary = [
            {"id": "done-root", "label": "Old work", "state": "completed", "elapsed": 8},
            {"id": "active-root", "label": "Check release", "state": "running"},
            {
                "id": "done-child", "parent_id": "active-root", "label": "Read docs",
                "state": "completed",
            },
            {
                "id": "active-child-a", "parent_id": "active-root", "label": "Run tests",
                "state": "running",
            },
            {
                "id": "active-child-b", "parent_id": "active-root", "label": "Inspect build",
                "state": "running",
            },
            {"id": "failed", "label": "Publish", "state": "failed", "result": "Registry rejected it"},
        ]

        view = build_agent_panel(summary, width=100, now=100, max_agents=8)

        self.assertEqual(
            [card.agent_id for card in view.selectable],
            ["active-root", "active-child-a", "active-child-b", "failed"],
        )
        child_line = next(row for row in view.rows if row.agent_id == "active-child-a" and row.kind == "task")
        self.assertEqual(child_line.depth, 1)
        self.assertIn("↳", child_line.text)
        self.assertEqual(view.running_count, 3)
        self.assertEqual(view.attention_count, 1)
        self.assertEqual(view.finished_count, 3)
        self.assertEqual(view.collapsed_count, 2)
        self.assertIn("1 needs attention", view.rows[0].text)
        self.assertIn("3 finished", view.rows[0].text)
        self.assertFalse(any(row.agent_id in {"done-root", "done-child"} for row in view.rows))

    def test_live_view_is_bounded_and_selection_only_targets_visible_cards(self):
        summary = [
            {"id": f"running-{index}", "label": f"Task {index}", "state": "running"}
            for index in range(6)
        ]

        view = build_agent_panel(summary, width=48, selected_index=99, max_agents=2, now=10)

        self.assertEqual([card.agent_id for card in view.selectable], ["running-0", "running-1"])
        self.assertEqual(view.selected_index, 1)
        self.assertEqual(view.selected_id, "running-1")
        self.assertEqual(view.hidden_count, 4)
        self.assertEqual(sum(row.kind == "overflow" for row in view.rows), 1)
        self.assertIn("+4 more agents", view.rows[-1].text)
        self.assertTrue(all(row.agent_id != "running-2" for row in view.rows))

    def test_expanded_view_shows_full_tree_and_only_selected_public_detail(self):
        result = "First public line\n" + "wrapped " * 12 + "tail"
        summary = [
            {
                "id": "parent", "label": "Coordinate release", "state": "completed",
                "model": "gpt-sol", "elapsed": 12, "result": "Parent complete",
            },
            {
                "id": "child", "parent_id": "parent", "label": "Verify the deployment artifacts",
                "state": "failed", "model": "gpt-luna", "elapsed": 3, "result": result,
                "reasoning": "private chain of thought must never render",
            },
        ]

        view = build_agent_panel(summary, width=34, selected_index=1, expanded=True, now=100)

        self.assertEqual([card.agent_id for card in view.selectable], ["parent", "child"])
        self.assertEqual(view.selected_id, "child")
        self.assertEqual(view.collapsed_count, 0)
        self.assertTrue(any(row.kind == "detail" and "Task:" in row.text for row in view.rows))
        self.assertTrue(any(row.kind == "detail" and "State:" in row.text for row in view.rows))
        self.assertTrue(any(row.kind == "detail" and "Elapsed:" in row.text for row in view.rows))
        self.assertTrue(any(row.kind == "detail" and "Model:" in row.text for row in view.rows))
        self.assertTrue(any(row.kind == "result" and "First public line" in row.text for row in view.rows))
        self.assertTrue(any(row.kind == "result" and row.text.endswith("tail") for row in view.rows))
        rendered = "\n".join(view.lines)
        self.assertNotIn("private chain of thought", rendered)
        self.assertNotIn("Parent complete", rendered)

    def test_offline_and_historical_running_agents_freeze_at_last_observation(self):
        summary = [{
            "id": "worker", "label": "Wait for provider", "state": "running",
            "started_at": 1_000, "updated_at": "6000",
        }]

        first = build_agent_panel(summary, width=80, stale=True, now=50)
        second = build_agent_panel(summary, width=80, stale=True, now=90)
        historical_compact = build_agent_panel(summary, width=80, historical=True, now=90)
        historical = build_agent_panel(summary, width=80, historical=True, expanded=True, now=90)
        live = build_agent_panel(summary, width=80, now=50)

        self.assertEqual(first.lines, second.lines)
        self.assertEqual(first.selectable[0].elapsed_seconds, 5)
        self.assertIn("last known", first.rows[0].text.casefold())
        self.assertTrue(any("Last known running · offline · 5s" in row.text for row in first.rows))
        self.assertEqual(historical_compact.selectable, ())
        self.assertEqual(historical_compact.running_count, 0)
        self.assertEqual(historical_compact.reported_running_count, 1)
        self.assertEqual(historical.selectable[0].elapsed_seconds, 5)
        self.assertTrue(any("Last reported running · 5s" in row.text for row in historical.rows))
        self.assertFalse(any("offline" in row.text.casefold() for row in historical.rows))
        self.assertEqual(live.selectable[0].elapsed_seconds, 49)

    def test_current_run_only_animates_agents_observed_in_that_run(self):
        summary = [
            {
                "id": "old", "label": "Old child", "state": "running",
                "run_id": "run-old", "started_at": 1_000, "updated_at": "6000",
            },
            {
                "id": "current", "label": "Current child", "state": "running",
                "run_id": "run-current", "started_at": 10_000, "updated_at": "12000",
            },
        ]

        compact = build_agent_panel(
            summary, width=100, current_run_id="run-current", now=20,
        )
        expanded = build_agent_panel(
            summary, width=100, current_run_id="run-current", now=20, expanded=True,
        )

        self.assertEqual([card.agent_id for card in compact.selectable], ["current"])
        self.assertEqual(compact.running_count, 1)
        self.assertEqual(compact.reported_running_count, 1)
        self.assertEqual(compact.collapsed_count, 1)
        self.assertIn("1 running", compact.rows[0].text)
        self.assertIn("1 last reported running", compact.rows[0].text)
        by_id = {card.agent_id: card for card in expanded.selectable}
        self.assertFalse(by_id["current"].historical)
        self.assertTrue(by_id["current"].run_current)
        self.assertEqual(by_id["current"].elapsed_seconds, 10)
        self.assertTrue(by_id["old"].historical)
        self.assertFalse(by_id["old"].run_current)
        self.assertEqual(by_id["old"].elapsed_seconds, 5)
        self.assertEqual(by_id["old"].status, "Last reported running")

    def test_unknown_run_association_is_explicit_and_never_false_live(self):
        summary = [{
            "id": "legacy", "label": "Legacy child", "state": "running",
            "started_at": 1_000, "updated_at": "6000",
        }]

        compact = build_agent_panel(
            summary, width=90, current_run_id="run-current", now=50,
        )
        expanded = build_agent_panel(
            summary, width=90, current_run_id="run-current", now=50, expanded=True,
        )
        legacy = build_agent_panel(summary, width=90, now=50)

        self.assertEqual(compact.selectable, ())
        self.assertEqual(compact.running_count, 0)
        self.assertEqual(compact.reported_running_count, 1)
        self.assertIn("last reported", compact.rows[0].text.casefold())
        self.assertIsNone(expanded.selectable[0].run_current)
        self.assertTrue(expanded.selectable[0].run_unknown)
        self.assertEqual(expanded.selectable[0].elapsed_seconds, 5)
        self.assertIn("run unknown", expanded.selectable[0].status.casefold())
        self.assertEqual(legacy.running_count, 1)
        self.assertEqual(legacy.selectable[0].elapsed_seconds, 49)

    def test_unknown_malformed_and_custom_states_stay_honest(self):
        summary = [
            {
                "id": "unknown", "label": "No lifecycle", "state": None,
                "elapsed": math.inf, "started_at": math.nan,
            },
            {"id": "custom", "label": "Provider task", "state": "provider-specific\nstate"},
        ]

        view = build_agent_panel(summary, width=72, now=math.nan)

        self.assertEqual(view.unknown_count, 1)
        self.assertEqual(view.other_count, 1)
        self.assertIsNone(view.selectable[0].elapsed)
        self.assertTrue(any("Status unknown" in row.text for row in view.rows))
        self.assertTrue(any("provider-specific↵state" in row.text for row in view.rows))
        self.assertNotIn("\n", "".join(view.lines))

    def test_unicode_and_control_characters_are_safe_and_cell_bounded(self):
        summary = [{
            "id": "wide",
            "label": "界" * 40 + "\x1b\nunsafe",
            "state": "failed",
            "model": "model\x00name",
            "result": "First\x07 result\n" + "界" * 30 + " tail",
        }]

        view = build_agent_panel(summary, width=24, expanded=True)

        self.assertTrue(all(cells(row.text) <= 24 for row in view.rows))
        rendered = "\n".join(view.lines)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertNotIn("\x00", rendered)
        self.assertIn("^G", rendered)
        self.assertIn("tail", rendered)

    def test_empty_summary_produces_no_panel_column(self):
        view = build_agent_panel([], width=40, stale=True)

        self.assertEqual(view.rows, ())
        self.assertEqual(view.selectable, ())
        self.assertIsNone(view.selected_id)
        self.assertEqual(view.selected_index, 0)


if __name__ == "__main__":
    unittest.main()
