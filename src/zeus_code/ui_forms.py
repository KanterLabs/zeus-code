"""Reusable keyboard-editing model for terminal forms."""

from __future__ import annotations

import curses
from dataclasses import dataclass, field
from typing import Callable


_CURSES_KEY_CODES = frozenset(
    value
    for name in dir(curses)
    if name.startswith("KEY_")
    and isinstance((value := getattr(curses, name)), int)
)


@dataclass
class Form:
    """State and keyboard behavior for a small terminal form.

    ``values`` and ``index`` remain public so a renderer can draw all fields
    without owning their editing behavior.  ``cursor`` and
    ``replace_on_type`` describe the currently focused text field.
    """

    title: str
    fields: list[tuple[str, str]]
    submit: Callable[[dict[str, str]], None]
    values: list[str] = field(default_factory=list)
    index: int = 0
    choices: dict[str, list[str]] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    hints: dict[str, str] = field(default_factory=dict)
    selectors: dict[str, Callable[[str], None]] = field(default_factory=dict)
    error: str = ""
    busy: bool = False
    progress: str = ""
    _cursors: list[int] = field(init=False, repr=False)
    _replace_defaults: list[bool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.fields = list(self.fields)
        self.choices = {name: list(options) for name, options in self.choices.items()}
        self.labels = dict(self.labels)
        self.hints = dict(self.hints)
        self.selectors = dict(self.selectors)

        if not self.values:
            self.values = [default for _, default in self.fields]
        else:
            self.values = list(self.values)
            if len(self.values) != len(self.fields):
                raise ValueError("form values must match form fields")

        for field_index, (name, _) in enumerate(self.fields):
            options = self.choices.get(name)
            if options and self.values[field_index] not in options:
                self.values[field_index] = options[0]

        if self.fields:
            self.index = min(max(0, self.index), len(self.fields) - 1)
        else:
            self.index = 0
        self._cursors = [len(value) for value in self.values]
        self._replace_defaults = [
            bool(value) and name not in self.choices and value == default
            for (name, default), value in zip(self.fields, self.values)
        ]

    @property
    def cursor(self) -> int:
        """Insertion cursor for the currently focused field."""
        if not self.fields:
            return 0
        return min(self._cursors[self.index], len(self.values[self.index]))

    @cursor.setter
    def cursor(self, position: int) -> None:
        if not self.fields:
            return
        self._cursors[self.index] = min(max(0, int(position)), len(self.values[self.index]))
        self._replace_defaults[self.index] = False

    @property
    def replace_on_type(self) -> bool:
        """Whether the focused default will be replaced by the next edit."""
        return bool(self.fields and self._replace_defaults[self.index])

    @replace_on_type.setter
    def replace_on_type(self, selected: bool) -> None:
        if self.fields:
            self._replace_defaults[self.index] = bool(selected)

    @property
    def selected_default(self) -> bool:
        """Alias used by renderers that present default selection explicitly."""
        return self.replace_on_type

    @selected_default.setter
    def selected_default(self, selected: bool) -> None:
        self.replace_on_type = selected

    def set_value(self, name: str, value: str, *, selected_default: bool = False) -> None:
        """Replace one field value while keeping its caret state coherent."""
        index = next((index for index, field in enumerate(self.fields) if field[0] == name), None)
        if index is None:
            raise KeyError(name)
        options = self.choices.get(name)
        if options and value not in options:
            raise ValueError(f"{value!r} is not a valid choice for {name}")
        self.values[index] = str(value)
        self._cursors[index] = len(self.values[index])
        self._replace_defaults[index] = bool(selected_default)

    def key(self, key: str | int) -> bool:
        """Handle one ``get_wch`` value and report whether it was consumed."""
        if self.busy:
            return True

        if key in (19, "\x13"):  # Ctrl+S
            self._submit()
            return True

        if key in (curses.KEY_ENTER, 10, 13, "\n", "\r"):
            if self.fields:
                name = self.fields[self.index][0]
                selector = self.selectors.get(name)
                if selector is not None:
                    selector(self.values[self.index])
                    return True
            if self.fields and self.index < len(self.fields) - 1:
                self.index += 1
            else:
                self._submit()
            return True

        if key in (9, "\t"):
            self._navigate(1, wrap=True)
            return True
        if key == getattr(curses, "KEY_BTAB", -1):
            self._navigate(-1, wrap=True)
            return True
        if key == curses.KEY_UP:
            self._navigate(-1)
            return True
        if key == curses.KEY_DOWN:
            self._navigate(1)
            return True

        if not self.fields:
            return False

        name, _ = self.fields[self.index]
        selector = self.selectors.get(name)
        if selector is not None:
            if key in (curses.KEY_RIGHT, 32, " "):
                selector(self.values[self.index])
            # Selector fields are display-only.  Consume editing input so the
            # value can only be replaced by the selector's validated result.
            return key in (
                curses.KEY_LEFT,
                curses.KEY_RIGHT,
                curses.KEY_HOME,
                curses.KEY_END,
                curses.KEY_BACKSPACE,
                curses.KEY_DC,
                127,
                8,
                21,
                32,
                "\x7f",
                "\b",
                "\x15",
                " ",
            ) or self._printable_character(key) is not None
        if name in self.choices:
            return self._choice_key(key, name)

        if key == curses.KEY_LEFT:
            self._finish_default_selection()
            self._cursors[self.index] = max(0, self.cursor - 1)
            return True
        if key == curses.KEY_RIGHT:
            self._finish_default_selection()
            self._cursors[self.index] = min(len(self.values[self.index]), self.cursor + 1)
            return True
        if key == curses.KEY_HOME:
            self._finish_default_selection()
            self._cursors[self.index] = 0
            return True
        if key == curses.KEY_END:
            self._finish_default_selection()
            self._cursors[self.index] = len(self.values[self.index])
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8, "\x7f", "\b"):
            self._backspace()
            return True
        if key == curses.KEY_DC:
            self._delete()
            return True
        if key in (21, "\x15"):  # Ctrl+U
            self._clear()
            return True

        character = self._printable_character(key)
        if character is not None:
            self._insert(character)
            return True
        return False

    def _navigate(self, change: int, *, wrap: bool = False) -> None:
        if not self.fields:
            return
        if wrap:
            self.index = (self.index + change) % len(self.fields)
        else:
            self.index = min(max(0, self.index + change), len(self.fields) - 1)

    def _choice_key(self, key: str | int, name: str) -> bool:
        if key in (curses.KEY_LEFT,):
            self._cycle_choice(name, -1)
            return True
        if key in (curses.KEY_RIGHT, 32, " "):
            self._cycle_choice(name, 1)
            return True

        # Editing keys and printable characters are consumed so they cannot
        # turn a constrained choice into free-form text.
        if key in (
            curses.KEY_HOME,
            curses.KEY_END,
            curses.KEY_BACKSPACE,
            curses.KEY_DC,
            127,
            8,
            21,
            "\x7f",
            "\b",
            "\x15",
        ):
            return True
        return self._printable_character(key) is not None

    def _cycle_choice(self, name: str, change: int) -> None:
        options = self.choices[name]
        if not options:
            return
        value = self.values[self.index]
        try:
            option_index = options.index(value)
        except ValueError:
            option_index = -1 if change > 0 else 0
        value = options[(option_index + change) % len(options)]
        self.values[self.index] = value
        self._cursors[self.index] = len(value)

    def _finish_default_selection(self) -> None:
        self._replace_defaults[self.index] = False

    def _backspace(self) -> None:
        if self.replace_on_type:
            self._clear()
            return
        cursor = self.cursor
        if cursor:
            value = self.values[self.index]
            self.values[self.index] = value[: cursor - 1] + value[cursor:]
            self._cursors[self.index] = cursor - 1

    def _delete(self) -> None:
        if self.replace_on_type:
            self._clear()
            return
        cursor = self.cursor
        value = self.values[self.index]
        if cursor < len(value):
            self.values[self.index] = value[:cursor] + value[cursor + 1 :]

    def _clear(self) -> None:
        self.values[self.index] = ""
        self._cursors[self.index] = 0
        self._finish_default_selection()

    def _insert(self, character: str) -> None:
        if self.replace_on_type:
            self.values[self.index] = character
            self._cursors[self.index] = len(character)
            self._finish_default_selection()
            return
        cursor = self.cursor
        value = self.values[self.index]
        self.values[self.index] = value[:cursor] + character + value[cursor:]
        self._cursors[self.index] = cursor + len(character)

    def _submit(self) -> None:
        self.submit({
            name: self.values[field_index]
            for field_index, (name, _) in enumerate(self.fields)
        })

    @staticmethod
    def _printable_character(key: str | int) -> str | None:
        if isinstance(key, str):
            return key if len(key) == 1 and key.isprintable() else None
        if not isinstance(key, int) or isinstance(key, bool):
            return None
        if key in _CURSES_KEY_CODES:
            return None
        key_min = getattr(curses, "KEY_MIN", 257)
        key_max = getattr(curses, "KEY_MAX", 511)
        if key_min <= key <= key_max or not 0 <= key <= 0x10FFFF:
            return None
        character = chr(key)
        return character if character.isprintable() else None


__all__ = ["Form"]
