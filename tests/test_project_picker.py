import curses
import tempfile
import unittest
from pathlib import Path

from zeus_code.tui import TUIApplication, build_tree_rows, project_picker_results
from zeus_code.workspace import Workspace


class ProjectPickerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Workspace(
            cache_path=Path(self.temporary.name) / "client.json",
            rpc_factory=lambda **_: None,
        )
        machine = self.workspace.selected_machine
        machine.update(alias="dev", host="devbox", connection="connected", stale=False)
        projects = [
            {"id": f"p{index}", "name": f"Project {index:02d}", "path": f"/srv/projects/project-{index:02d}"}
            for index in range(97)
        ]
        projects[0].update(name="Zeus", path="/srv/team-a/zeus")
        projects[1].update(name="Zeus", path="/srv/team-b/zeus")
        projects[42].update(name="Billing", path="/srv/clients/acme-api")
        projects[95].update(name="Active", path="/srv/projects/active")
        projects[96].update(name="Archived", path="/srv/projects/archived")
        machine["snapshot"] = {
            "projects": projects,
            "threads": [
                {
                    "id": "t-active", "project_id": "p95", "title": "Current work",
                    "provider": "codex", "state": "idle", "archived": False,
                },
                {
                    "id": "t-archived", "project_id": "p96", "title": "Old work",
                    "provider": "codex", "state": "completed", "archived": True,
                },
            ],
            "approvals": [],
        }
        self.workspace.switch("local", "p0")
        self.app = TUIApplication(self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    def test_sidebar_hides_empty_projects_but_picker_searches_all_97(self):
        rows = build_tree_rows(self.workspace)
        self.assertEqual(
            [row.project_id for row in rows if row.kind == "project"],
            ["p0", "p95"],
        )
        self.assertEqual([row.thread_id for row in rows if row.kind == "thread"], ["t-active"])
        self.assertEqual(len(project_picker_results(self.workspace)), 97)
        self.assertEqual(
            [result["project"]["id"] for result in project_picker_results(self.workspace, "clients ACME")],
            ["p42"],
        )

    def test_duplicate_project_names_are_disambiguated_by_path(self):
        matches = project_picker_results(self.workspace, "zeus")
        self.assertEqual(len(matches), 2)
        self.assertNotEqual(matches[0]["label"], matches[1]["label"])
        self.assertIn("/srv/team-a/zeus", {match["label"].split("·", 1)[1].strip() for match in matches})
        self.assertIn("/srv/team-b/zeus", {match["label"].split("·", 1)[1].strip() for match in matches})

    def test_ctrl_o_search_selects_project_and_starts_new_thread_form(self):
        self.app.handle_key("\x0f")
        self.assertEqual(self.app.overlay, "projects")
        for character in "acme-api":
            self.app.handle_key(character)
        self.app.handle_key("\r")

        self.assertEqual(self.workspace.state["selected_project"], "p42")
        self.assertEqual(self.app.form.title, "New thread")
        self.assertEqual(self.app.form.values[0], "/srv/clients/acme-api")

    def test_picker_manual_path_action_opens_add_repository_form(self):
        self.app.handle_key("\x0f")
        self.app.handle_key(curses.KEY_UP)
        self.app.handle_key("\r")
        self.assertEqual(self.app.form.title, "Add repository")

    def test_ctrl_o_on_empty_remote_opens_manual_form_with_empty_path(self):
        self.workspace.selected_machine["snapshot"]["projects"] = []
        self.workspace.switch("local")
        app = TUIApplication(self.workspace)
        app.handle_key("\x0f")
        self.assertEqual(app.form.title, "Add repository")
        self.assertEqual(app.form.values[0], "")

    def test_unavailable_provider_is_shown_at_composer_and_keeps_draft(self):
        self.workspace.selected_machine["providers"] = {
            "codex": {"available": False, "detail": "Run codex login on the dev server."},
        }
        self.workspace.switch("local", "p95", "t-active")
        app = TUIApplication(self.workspace)
        app.composer = "keep this"
        app.cursor = len(app.composer)
        written = []
        app.put = lambda _screen, _y, _x, text, *_args, **_kwargs: written.append(str(text))
        app._fill = lambda *_args, **_kwargs: None
        app._draw_activity = lambda *_args, **_kwargs: None
        app.color = lambda _pair: 0

        app._draw_conversation(None, 4, 32, 120, 40)
        self.assertTrue(any("Codex unavailable on dev" in text for text in written))
        self.assertTrue(any("Run codex login on the dev server." in text for text in written))

        app._send_prompt()
        self.assertEqual(self.workspace.thread_view()["draft"], "keep this")
        self.assertIn("Draft kept", app.status)


if __name__ == "__main__":
    unittest.main()
