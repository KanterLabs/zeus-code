import curses
import unittest

from zeus_code.ui_forms import Form


class FormTests(unittest.TestCase):
    def test_typing_replaces_defaults_and_enter_advances_then_submits(self):
        submitted = []
        form = Form(
            "New thread",
            [("title", "New conversation"), ("model", "codex-latest")],
            submitted.append,
        )

        self.assertTrue(form.selected_default)
        self.assertEqual(form.cursor, len("New conversation"))
        form.key("P")
        form.key("l")
        form.key("a")
        form.key("n")
        self.assertEqual(form.values[0], "Plan")
        self.assertEqual(form.cursor, 4)
        self.assertFalse(form.replace_on_type)

        form.key("\r")
        self.assertEqual(form.index, 1)
        self.assertTrue(form.selected_default)
        form.key(curses.KEY_ENTER)
        self.assertEqual(
            submitted,
            [{"title": "Plan", "model": "codex-latest"}],
        )

    def test_ctrl_s_submits_untouched_defaults_from_any_field(self):
        submitted = []
        form = Form(
            "Settings",
            [("model", "gpt-5"), ("effort", "high")],
            submitted.append,
        )

        form.key(curses.KEY_DOWN)
        self.assertTrue(form.key("\x13"))
        self.assertEqual(submitted, [{"model": "gpt-5", "effort": "high"}])
        self.assertEqual(form.index, 1)

    def test_choice_fields_cycle_and_never_accept_freeform_text(self):
        form = Form(
            "Settings",
            [("provider", "codex")],
            lambda _: None,
            choices={"provider": ["codex", "opencode"]},
            labels={"provider": "Provider"},
            hints={"provider": "Select with Left or Right"},
        )

        self.assertEqual(form.labels["provider"], "Provider")
        self.assertEqual(form.hints["provider"], "Select with Left or Right")
        form.key(curses.KEY_RIGHT)
        self.assertEqual(form.values, ["opencode"])
        form.key(" ")
        self.assertEqual(form.values, ["codex"])
        form.key(curses.KEY_LEFT)
        self.assertEqual(form.values, ["opencode"])
        self.assertTrue(form.key("x"))
        self.assertTrue(form.key(curses.KEY_BACKSPACE))
        self.assertEqual(form.values, ["opencode"])

    def test_unicode_insertion_and_curses_integer_keys_are_distinct(self):
        form = Form("Identity", [("name", "")], lambda _: None)

        for character in "Zoë 🐇":
            self.assertTrue(form.key(character))
        self.assertEqual(form.values, ["Zoë 🐇"])

        before = form.values[0]
        self.assertFalse(form.key(curses.KEY_F1))
        self.assertEqual(form.values[0], before)

        # A string remains an unambiguous Unicode character even when its code
        # point happens to equal one of curses' integer key constants.
        character = chr(curses.KEY_F1)
        self.assertTrue(form.key(character))
        self.assertEqual(form.values[0], before + character)

    def test_cursor_editing_supports_home_end_backspace_delete_and_clear(self):
        form = Form("Edit", [("value", "sample")], lambda _: None)

        form.key(curses.KEY_HOME)
        form.key("X")
        form.key(curses.KEY_RIGHT)
        form.key(curses.KEY_DC)
        self.assertEqual(form.values, ["Xsmple"])
        self.assertEqual(form.cursor, 2)

        form.key(curses.KEY_END)
        form.key(curses.KEY_BACKSPACE)
        self.assertEqual(form.values, ["Xsmpl"])
        form.key("\x15")
        self.assertEqual(form.values, [""])
        self.assertEqual(form.cursor, 0)

    def test_tab_shift_tab_and_arrows_navigate_without_losing_default_selection(self):
        form = Form(
            "Navigation",
            [("first", "one"), ("second", "two"), ("third", "three")],
            lambda _: None,
        )

        form.key(curses.KEY_DOWN)
        self.assertEqual(form.index, 1)
        self.assertTrue(form.selected_default)
        form.key(curses.KEY_UP)
        self.assertEqual(form.index, 0)
        form.key(curses.KEY_BTAB)
        self.assertEqual(form.index, 2)
        form.key("\t")
        self.assertEqual(form.index, 0)
        self.assertEqual(form.values, ["one", "two", "three"])

    def test_busy_form_ignores_edits_and_repeated_submits(self):
        submissions = []
        form = Form(
            "Save",
            [("name", "default"), ("detail", "unchanged")],
            lambda values: submissions.append(values),
        )

        def submit_once(values):
            submissions.append(values)
            form.busy = True

        form.submit = submit_once
        form.key(19)
        initial_state = (list(form.values), form.index, form.cursor, form.selected_default)
        form.key(19)
        form.key("x")
        form.key(curses.KEY_BACKSPACE)
        form.key(curses.KEY_ENTER)
        form.key("\t")
        form.key(curses.KEY_DOWN)
        form.key(curses.KEY_UP)
        form.key(curses.KEY_LEFT)
        form.key(curses.KEY_HOME)
        form.key(curses.KEY_END)

        self.assertEqual(submissions, [{"name": "default", "detail": "unchanged"}])
        self.assertEqual(
            (form.values, form.index, form.cursor, form.selected_default),
            initial_state,
        )


if __name__ == "__main__":
    unittest.main()
