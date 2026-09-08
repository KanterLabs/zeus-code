import curses
import tempfile
import unittest
from pathlib import Path

from zeus_code.tui import TUIApplication, command_palette_results
from zeus_code.workspace import Workspace


class CommandPaletteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Workspace(
            cache_path=Path(self.temporary.name) / "client.json",
            rpc_factory=lambda **_: None,
        )
        self.workspace.selected_machine.update(
            alias="dev", connection="connected", stale=False, server_id="server-1",
            providers={"codex": {"available": True, "status": "ready", "models": []}},
        )
        self.workspace.selected_machine["snapshot"] = {
            "server_id": "server-1",
            "projects": [{"id": "p1", "name": "Zeus", "path": "/repo"}],
            "threads": [
                {
                    "id": "t1", "project_id": "p1", "title": "Current work", "provider": "codex",
                    "state": "idle", "archived": False,
                },
                {
                    "id": "t2", "project_id": "p1", "title": "Needs review", "provider": "codex",
                    "state": "completed", "archived": False,
                },
                {
                    "id": "old", "project_id": "p1", "title": "Old work", "provider": "codex",
                    "state": "completed", "archived": True,
                },
            ],
            "approvals": [],
        }
        self.workspace.selected_machine["events"] = {
            "t1": [{"seq": 1, "kind": "message", "data": {"role": "assistant", "text": "Visible result"}}],
        }
        self.workspace.switch("local", "p1", "t1")
        self.app = TUIApplication(self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    def test_palette_names_exact_targets_and_unavailable_reasons(self):
        actions = self.app._command_actions()
        by_label = {action.label: action for action in actions}
        for label in (
            "New thread", "Open project", "Switch thread", "Change model", "Toggle agent details",
            "Open attention inbox", "Review diff", "Cancel run", "Rename thread", "Archive thread",
            "Review approval", "Choose theme", "Servers", "Keyboard help", "Restore Old work",
        ):
            self.assertIn(label, by_label)
        self.assertIn("Current work · Zeus · dev", by_label["Change model"].target)
        self.assertFalse(by_label["Cancel run"].enabled)
        self.assertIn("no active run", by_label["Cancel run"].reason)
        self.assertFalse(by_label["Toggle agent details"].enabled)

        matches = command_palette_results(actions, "old dev")
        self.assertEqual([action.label for action in matches], ["Restore Old work"])

    def test_ctrl_k_search_and_disabled_action_stays_explained(self):
        self.workspace.selected_thread["state"] = "running"
        self.app.handle_key(11)
        self.assertEqual(self.app.overlay, "palette")
        for character in "change model":
            self.app.handle_key(character)
        actions = command_palette_results(self.app._command_actions(), self.app.search_query)
        self.assertEqual(len(actions), 1)
        self.assertFalse(actions[0].enabled)
        self.app.handle_key(13)
        self.assertEqual(self.app.overlay, "palette")
        self.assertIn("active run", self.app.status)

    def test_attention_selection_preserves_parent_draft_and_does_not_mark_seen_early(self):
        self.workspace.thread_view("t1", "local").update(draft="parent draft", scroll=9, anchor_seq=1)
        self.workspace.thread_view("t2", "local").update(draft="review draft", scroll=0)
        self.app.composer = "parent draft"
        self.app.cursor = len(self.app.composer)
        self.app._attention_items = lambda: [{
            "machine_id": "local", "thread_id": "t2", "title": "Needs review",
            "project_name": "Zeus", "machine_name": "dev", "state": "completed",
            "unread_count": 1, "needs_approval": False, "stale": False,
        }]
        marked = []
        self.workspace.mark_thread_seen = lambda *args, **kwargs: marked.append((args, kwargs))

        self.app._open_attention()
        self.assertEqual(marked, [])
        self.app._open_attention_index(0)
        self.assertEqual(self.workspace.state["selected_thread"], "t2")
        self.assertEqual(self.app.composer, "review draft")
        self.assertEqual(self.workspace.thread_view("t1", "local")["draft"], "parent draft")
        self.assertEqual(self.workspace.thread_view("t1", "local")["scroll"], 9)
        self.assertEqual(marked, [])

    def test_seen_advances_only_after_visible_live_tail_is_drawn(self):
        marked = []
        self.workspace.mark_thread_seen = lambda *args, **kwargs: marked.append((args, kwargs))
        self.app.put = lambda *_args, **_kwargs: None
        self.app._fill = lambda *_args, **_kwargs: None
        self.app._draw_activity = lambda *_args, **_kwargs: None
        self.app.color = lambda _pair: 0

        self.app._draw_conversation(None, 4, 0, 80, 24)
        self.assertEqual(marked, [(('local', 't1'), {"visible": True})])

        marked.clear()
        self.app.overlay = "attention"
        self.app._draw_conversation(None, 4, 0, 80, 24)
        self.assertEqual(marked, [])

        self.app.overlay = None
        self.workspace.thread_view()["scroll"] = 1
        self.app._draw_conversation(None, 4, 0, 80, 24)
        self.assertEqual(marked, [])

    def test_theme_action_persists_explicit_choice(self):
        self.app._theme_form()
        self.assertEqual(self.app.form.title, "Theme")
        self.app.form.set_value("theme", "monochrome")
        self.app._init_colors = lambda: None
        self.app.form.key(curses.KEY_ENTER)
        self.assertEqual(self.workspace.state["settings"]["theme"], "monochrome")
        self.assertIsNone(self.app.overlay)


if __name__ == "__main__":
    unittest.main()
