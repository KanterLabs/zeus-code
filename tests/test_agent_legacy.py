import json
import unittest
from zeus_code.agents import summarize_agents
from zeus_code.agent_panel import build_agent_panel


def activity(seq, kind, *, state="completed", agent_id="child", run_id="parent"):
    return {"seq": seq, "run_id": run_id, "kind": "tool", "data": {
        "tool_type": "subAgentActivity", "status": state,
        "text": json.dumps({"agentThreadId": agent_id, "agentPath": "/root/tests", "kind": kind}),
    }}


class LegacyAgentTests(unittest.TestCase):
    def test_persisted_activity_renders_without_agent_array(self):
        events = [activity(1, "started"), activity(2, "interacted")]
        agents = summarize_agents(events)
        self.assertEqual(agents[0]["state"], "running")
        self.assertEqual(agents[0]["run_id"], "parent")
        self.assertEqual(agents[0]["label"], "/root/tests")
        view = build_agent_panel(agents, width=38, current_run_id="parent")
        self.assertTrue(view.selectable)
        self.assertEqual(view.selectable[0].agent_id, "child")
        events.append(activity(3, "completed", state="running"))
        self.assertEqual(summarize_agents(events)[0]["state"], "completed")
        events.extend([activity(4, "started"), activity(5, "interrupted")])
        self.assertEqual(summarize_agents(events)[0]["state"], "cancelled")

    def test_interaction_is_not_proof_of_running_or_completed_state(self):
        self.assertEqual(summarize_agents([activity(1, "interacted")])[0]["state"], "unknown")
        agents = summarize_agents([activity(1, "completed"), activity(2, "interacted")])
        self.assertEqual(agents[0]["state"], "completed")
        agents = summarize_agents([activity(1, "started"), activity(2, "interacted", run_id="next")])
        self.assertEqual(agents[0]["state"], "unknown")

    def test_invalid_legacy_data_is_ignored_and_normalized_agents_win(self):
        for text in ("not JSON", "[]", json.dumps({"agentThreadId": "child", "kind": []})):
            event = activity(1, "started")
            event["data"]["text"] = text
            self.assertEqual(summarize_agents([event]), [])
        event = activity(1, "started")
        event["data"]["agents"] = [{"id": "normalized", "state": "completed"}]
        self.assertEqual([a["id"] for a in summarize_agents([event])], ["normalized"])
