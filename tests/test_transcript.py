import unittest

from zeus_code.transcript import MAX_RENDER_LINES, render_transcript


class TranscriptTests(unittest.TestCase):
    def test_streamed_message_is_replaced_once_by_authoritative_final(self):
        events = [
            self.event(1, "message", {"role": "user", "text": "Please check it."}),
            self.event(1, "message_delta", {"item_id": "answer", "text": "Old "}),
            self.event(1, "message_delta", {"item_id": "answer", "text": "draft"}),
            self.event(
                1,
                "message",
                {"item_id": "answer", "role": "assistant", "text": "Final answer"},
            ),
        ]

        rendered = render_transcript(events, 80)

        self.assertEqual(rendered, ["you: Please check it.", "agent: Final answer"])

    def test_same_item_id_in_two_runs_remains_two_distinct_turns(self):
        events = [
            self.event("run-one", "message_delta", {"item_id": "answer", "text": "first"}),
            self.event("run-one", "run_state", {"state": "completed"}),
            self.event("run-two", "message", {"role": "user", "text": "again"}),
            self.event("run-two", "message_delta", {"item_id": "answer", "text": "second"}),
        ]

        rendered = render_transcript(events, 80)

        self.assertEqual(rendered.count("agent: first"), 1)
        self.assertEqual(rendered.count("agent: second"), 1)
        self.assertLess(rendered.index("agent: first"), rendered.index("you: again"))

    def test_failed_run_renders_actionable_error_and_hides_session_id(self):
        events = [
            self.event("r1", "provider_session", {"session_id": "secret-internal-id"}),
            self.event("r1", "reasoning", {"text": "private chain of thought"}),
            self.event(
                "r1",
                "run_state",
                {"state": "failed", "error": "Sign in to Codex and retry."},
            ),
        ]

        rendered = render_transcript(events, 80)

        self.assertEqual(rendered, ["× failed: Sign in to Codex and retry."])
        self.assertNotIn("secret-internal-id", repr(rendered))
        self.assertNotIn("private chain of thought", repr(rendered))

    def test_tools_and_approvals_collapse_and_details_are_opt_in(self):
        events = [
            self.event(
                "r1",
                "tool",
                {"item_id": "tool-1", "title": "Tests", "status": "running", "text": "partial"},
            ),
            self.event(
                "r1",
                "approval",
                {
                    "id": "approval-1",
                    "approval_state": "pending",
                    "payload": {"command": "python3 -m unittest", "details": {"large": "body"}},
                },
            ),
            self.event(
                "r1",
                "tool",
                {"item_id": "tool-1", "title": "Tests", "status": "completed", "text": "all passed"},
            ),
            self.event(
                "r1",
                "approval",
                {"id": "approval-1", "approval_state": "resolved", "decision": "allow"},
            ),
        ]

        collapsed = render_transcript(events, 120)
        expanded = render_transcript(events, 120, expanded_tools=True)

        self.assertEqual(collapsed, ["▸ Tests  completed", "! approval allowed: python3 -m unittest"])
        self.assertEqual(expanded[0:2], ["▸ Tests  completed", "  all passed"])
        self.assertNotIn("partial", "\n".join(expanded))
        self.assertNotIn("large", "\n".join(expanded))

    def test_live_tool_deltas_append_until_final_aggregate_replaces_them(self):
        streaming = [
            self.event(
                "r1",
                "tool",
                {
                    "item_id": "command-1",
                    "title": "python3 -m unittest",
                    "status": "running",
                },
            ),
            self.event(
                "r1",
                "tool",
                {
                    "item_id": "command-1",
                    "title": "Command",
                    "status": "running",
                    "text": "first line\n",
                    "delta": True,
                },
            ),
            self.event(
                "r1",
                "tool",
                {
                    "item_id": "command-1",
                    "title": "Command",
                    "status": "running",
                    "text": "second line",
                    "delta": True,
                },
            ),
        ]

        live = render_transcript(streaming, 120, expanded_tools=True)
        self.assertEqual(
            live,
            ["▸ python3 -m unittest  running", "  first line", "second line"],
        )

        completed = streaming + [
            self.event(
                "r1",
                "tool",
                {
                    "item_id": "command-1",
                    "title": "python3 -m unittest",
                    "status": "completed",
                    "text": "authoritative aggregate",
                },
            )
        ]
        self.assertEqual(
            render_transcript(completed, 120, expanded_tools=True),
            ["▸ python3 -m unittest  completed", "  authoritative aggregate"],
        )

    def test_controls_are_visible_newlines_and_tabs_remain_structured(self):
        events = [
            self.event(
                "r1",
                "message",
                {
                    "role": "assistant",
                    "text": "safe\x1b[2J\rline\n\tindented\u202e",
                },
            )
        ]

        rendered = render_transcript(events, 80)
        joined = "\n".join(rendered)

        self.assertIn("␛[2J␍line", joined)
        self.assertIn("    indented<U+202E>", joined)
        self.assertNotIn("\x1b", joined)
        self.assertEqual(len(rendered), 2)

    def test_daemon_continuation_chunks_reassemble_final_message(self):
        events = [
            self.event("r1", "message_delta", {"item_id": "m1", "text": "draft"}),
            self.event(
                "r1", "message", {"item_id": "m1", "role": "assistant", "text": "final "}
            ),
            self.event(
                "r1",
                "message",
                {"item_id": "m1", "role": "assistant", "text": "answer", "continuation": True},
            ),
        ]

        self.assertEqual(render_transcript(events, 80), ["agent: final answer"])

    def test_wrapping_counts_wide_unicode_cells(self):
        events = [
            self.event("r1", "message", {"role": "assistant", "text": "界界"})
        ]

        self.assertEqual(render_transcript(events, 9), ["agent: 界", "界"])

    def test_line_output_is_bounded_with_explicit_omission_marker(self):
        events = [self.event("r1", "status", {"text": str(index)}) for index in range(MAX_RENDER_LINES + 5)]

        rendered = render_transcript(events, 80)

        self.assertEqual(len(rendered), MAX_RENDER_LINES)
        self.assertIn("earlier output omitted", rendered[0])
        self.assertTrue(rendered[-1].endswith(str(MAX_RENDER_LINES + 4)))

    @staticmethod
    def event(run_id, kind, data):
        return {"run_id": run_id, "kind": kind, "data": data}


if __name__ == "__main__":
    unittest.main()
