import asyncio
import curses
import tempfile
import unittest
from pathlib import Path

from zeus_code.tui import TUIApplication, model_indicator, model_picker_results
from zeus_code.workspace import Workspace


class ModelPickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Workspace(
            cache_path=Path(self.temporary.name) / "client.json",
            rpc_factory=lambda **_: None,
        )
        self.workspace.selected_machine.update(
            alias="dev",
            connection="connected",
            stale=False,
            providers={
                "codex": {
                    "available": False,
                    "status": "unavailable",
                    "cached": True,
                    "models": [
                        {
                            "id": "fixture-fast",
                            "name": "Fast",
                            "reasoning_efforts": ["low", "high"],
                            "is_default": True,
                        },
                        *[
                            {"id": f"model-{index:03d}", "name": f"Model {index:03d}"}
                            for index in range(75)
                        ],
                    ],
                }
            },
        )
        self.workspace.selected_machine["snapshot"] = {
            "projects": [{"id": "p1", "name": "Zeus", "path": "/repo"}],
            "threads": [{
                "id": "t1",
                "project_id": "p1",
                "title": "Picker",
                "provider": "codex",
                "model": "custom-local",
                "settings": {"approval_policy": "never", "sandbox": "danger-full-access"},
                "state": "idle",
                "archived": False,
            }],
            "approvals": [],
        }
        self.workspace.switch("local", "p1", "t1")

    async def asyncTearDown(self):
        await self.workspace.close()
        self.temporary.cleanup()

    def test_full_discovery_has_explicit_default_custom_and_cache_labels(self):
        rows = model_picker_results(self.workspace, provider_name="codex", current_model="custom-local")
        self.assertEqual(rows[0]["id"], None)
        self.assertIn("Provider default", rows[0]["label"])
        self.assertEqual(rows[1]["id"], "custom-local")
        self.assertTrue(rows[1]["custom"])
        self.assertEqual(len(rows), 78)
        self.assertTrue(all("cached" in row["label"] for row in rows))
        self.assertTrue(all("provider unavailable" in row["label"] for row in rows))

        match = model_picker_results(
            self.workspace, "fast high", provider_name="codex", current_model="custom-local",
        )
        self.assertEqual([row["id"] for row in match], ["fixture-fast"])
        self.assertEqual(match[0]["reasoning_efforts"], ["low", "high"])
        self.assertEqual(match[0]["variants"], [])

    def test_provider_default_indicator_never_guesses_reported_default(self):
        provider = self.workspace.selected_machine["providers"]["codex"]
        thread = {"provider": "codex", "model": None, "settings": {"reasoning_effort": "high"}}
        self.assertEqual(model_indicator(thread, provider), "Codex · Provider default · high")
        explicit = {"provider": "codex", "model": "fixture-fast", "settings": {"reasoning_effort": "low"}}
        self.assertEqual(model_indicator(explicit, provider, compact=True), "Codex · fixture-fast · low")

    async def test_search_choose_and_reasoning_persist_without_losing_permissions(self):
        calls = []

        async def update_thread(thread_id, *, machine_id=None, **changes):
            calls.append((thread_id, machine_id, changes))
            return {"id": thread_id, **changes}

        self.workspace.update_thread = update_thread
        app = TUIApplication(self.workspace)
        app.handle_key(curses.KEY_F4)
        self.assertEqual(app.overlay, "models")
        for character in "fixture-fast":
            app.handle_key(character)
        app.handle_key(13)
        self.assertEqual(app.model_focus, "settings")
        app.handle_key(curses.KEY_DOWN)  # Model default -> low.
        app.handle_key(13)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(app.overlay, None)
        self.assertEqual(calls[0][0:2], ("t1", "local"))
        self.assertEqual(calls[0][2]["model"], "fixture-fast")
        self.assertEqual(calls[0][2]["settings"], {
            "approval_policy": "never",
            "sandbox": "danger-full-access",
            "reasoning_effort": "low",
        })

    def test_active_run_blocks_picker_and_preserves_draft_and_scroll(self):
        self.workspace.selected_thread["state"] = "running"
        self.workspace.thread_view().update(draft="keep me", scroll=7, anchor_seq=42)
        app = TUIApplication(self.workspace)
        app.handle_key(curses.KEY_F4)
        self.assertIsNone(app.overlay)
        self.assertIn("active run", app.status)
        self.assertEqual(self.workspace.thread_view()["draft"], "keep me")
        self.assertEqual(self.workspace.thread_view()["scroll"], 7)

    def test_new_thread_form_opens_same_searchable_picker(self):
        app = TUIApplication(self.workspace)
        app._new_thread_form()
        form = app.form
        self.assertIsNotNone(form)
        model_index = next(index for index, field in enumerate(form.fields) if field[0] == "model")
        form.index = model_index
        app.handle_key(13)
        self.assertEqual(app.overlay, "models")
        for character in "model-074":
            app.handle_key(character)
        app.handle_key(13)
        self.assertEqual(app.overlay, "form")
        self.assertEqual(form.values[model_index], "model-074")


if __name__ == "__main__":
    unittest.main()
