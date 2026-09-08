"""Explicit terminal palettes; no background-color probing or external libraries."""

THEMES = ("dark", "light", "terminal", "monochrome")


def color_pairs(theme: str = "dark", colors: int = 256) -> dict[int, tuple[int, int]]:
    if theme not in THEMES:
        theme = "dark"
    if colors < 8 or theme == "monochrome":
        return {}
    if colors >= 256:
        if theme == "light":
            base, panel, foreground, muted, accent = 255, 254, 234, 240, 24
            border, selected, yellow, red, green, button_text = 245, 153, 130, 160, 28, 255
        else:
            base, panel, foreground, muted, accent = 234, 235, 252, 245, 115
            border, selected, yellow, red, green, button_text = 239, 24, 221, 210, 150, 0
    elif theme == "light":
        base = panel = 7
        foreground, muted, accent, border, selected = 0, 0, 4, 0, 6
        yellow, red, green, button_text = 3, 1, 2, 7
    else:
        base = panel = 0
        foreground, muted, accent, border, selected = 7, 7, 6, 7, 4
        yellow, red, green, button_text = 3, 1, 2, 0
    if theme == "terminal":
        base = panel = -1
        # Let the terminal choose readable normal text with its default background.
        foreground = muted = border = -1
    return {
        1: (accent, base), 2: (button_text, accent),
        3: (yellow, base), 4: (red, base), 5: (green, base),
        6: (foreground, base), 7: (foreground, panel), 8: (muted, panel),
        9: (border, base), 10: (muted, base), 11: (foreground, panel),
        12: (accent, panel), 13: (0 if theme == "light" else 7, selected),
        14: (0 if theme == "light" else accent, selected), 15: (border, panel),
    }
