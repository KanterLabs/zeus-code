from __future__ import annotations

import unittest

from zeus_code.agents import MAX_AGENT_EVENTS, MAX_AGENTS, summarize_agents


def event(seq, agents, created_at=None):
    value = {"seq": seq, "kind": "tool", "data": {"agents": agents}}
    if created_at is not None:
        value["created_at"] = created_at
    return value


class AgentSummaryTests(unittest.TestCase):
    def test_merges_nested_concurrent_agents_by_stable_id(self):
        events = [
            event(
                1,
                [
                    {
                        "id": "agent-a",
                        "parent_id": "root",
                        "label": "Research API",
                        "state": "pendingInit",
                        "model": "gpt-sol",
                    },
                    {
                        "id": "agent-b",
                        "parent_id": "agent-a",
                        "label": "Read fixtures",
                        "state": "running",
                    },
                ],
                "2026-09-08T00:00:00Z",
            ),
            {"seq": 2, "kind": "status", "data": {"text": "unrelated"}},
            event(
                3,
                [
                    {
                        "id": "agent-a",
                        "parent_id": "root",
                        "label": "Research API",
                        "state": "completed",
                        "result": "Contract mapped",
                        "model": "gpt-sol",
                        "elapsed": 12.5,
                    },
                    {
                        "id": "agent-b",
                        "parent_id": "agent-a",
                        "label": "Read fixtures",
                        "state": "errored",
                        "message": "Fixture was invalid",
                    },
                ],
                "2026-09-08T00:00:12Z",
            ),
        ]

        self.assertEqual(
            summarize_agents(events),
            [
                {
                    "id": "agent-a",
                    "parent_id": "root",
                    "label": "Research API",
                    "state": "completed",
                    "result": "Contract mapped",
                    "model": "gpt-sol",
                    "updated_at": "2026-09-08T00:00:12Z",
                    "elapsed": 12.5,
                },
                {
                    "id": "agent-b",
                    "parent_id": "agent-a",
                    "label": "Read fixtures",
                    "state": "failed",
                    "result": "Fixture was invalid",
                    "updated_at": "2026-09-08T00:00:12Z",
                },
            ],
        )

    def test_sequence_order_ignores_old_snapshot_and_new_event_can_resume_agent(self):
        events = [
            event(3, [{"id": "a", "state": "completed", "result": "done"}]),
            event(2, [{"id": "a", "state": "running"}]),
            event(
                4,
                [{"id": "a", "state": "running", "started_at": 1_789_000_010_000}],
                "2026-09-08T00:00:10Z",
            ),
            event(
                5,
                [{"id": "a", "state": "completed", "finished_at": 1_789_000_014_500}],
                "2026-09-08T00:00:14.500Z",
            ),
            event(5, [{"id": "a", "state": "running"}]),
            event(6, [{"id": "b", "state": "shutdown"}]),
            event(7, [{"id": "c", "state": "provider-specific"}]),
            event(8, [{"id": "a", "state": "unknown"}]),
        ]

        summary = summarize_agents(events)
        self.assertEqual(summary[0]["state"], "completed")
        self.assertIsNone(summary[0]["result"])
        self.assertEqual(summary[0]["elapsed"], 4.5)
        self.assertEqual(summary[1]["state"], "cancelled")
        self.assertEqual(summary[2]["state"], "provider-specific")

    def test_ignores_invalid_entries_and_bounds_history_and_output(self):
        old = event(1, [{"id": "outside-window", "state": "running"}])
        padding = [{"seq": seq, "kind": "status", "data": {}} for seq in range(2, MAX_AGENT_EVENTS + 2)]
        many = [
            {"id": f"agent-{index}", "state": "running" if index % 7 == 0 else "completed"}
            for index in range(MAX_AGENTS * 2)
        ]
        events = [old, *padding, event(MAX_AGENT_EVENTS + 2, [None, {"state": "running"}, *many])]

        summary = summarize_agents(events)
        self.assertNotIn("outside-window", {agent["id"] for agent in summary})
        self.assertLessEqual(len(summary), MAX_AGENTS)
        self.assertTrue(all(set(agent) >= {"id", "parent_id", "label", "state", "result"} for agent in summary))

    def test_numeric_lifecycle_timestamps_are_milliseconds(self):
        summary = summarize_agents(
            [
                event(1, [{"id": "agent", "state": "running", "started_at": 1000}]),
                event(2, [{"id": "agent", "state": "completed", "finished_at": 2500}]),
            ]
        )

        self.assertEqual(summary[0]["elapsed"], 1.5)
        self.assertEqual(summary[0]["started_at"], 1000)


if __name__ == "__main__":
    unittest.main()
