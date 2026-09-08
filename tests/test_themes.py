import unittest
from zeus_code.themes import color_pairs


class ThemeTests(unittest.TestCase):
    def test_explicit_backgrounds_and_terminal_defaults(self):
        dark, light, terminal = (color_pairs(name, 256) for name in ('dark', 'light', 'terminal'))
        self.assertNotEqual(dark[6][1], light[6][1])
        self.assertEqual(terminal[6], (-1, -1))
        for pairs in (dark, light):
            self.assertEqual(len(pairs), 15)
            self.assertTrue(all(fg != bg for fg, bg in pairs.values()))

    def test_eight_colors_and_monochrome_need_no_extended_colors(self):
        for theme in ('dark', 'light', 'terminal'):
            self.assertTrue(all(-1 <= n < 8 for pair in color_pairs(theme, 8).values() for n in pair))
        self.assertEqual(color_pairs('monochrome', 256), {})
        self.assertEqual(color_pairs('dark', 0), {})
