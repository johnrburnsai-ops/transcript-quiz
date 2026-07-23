from __future__ import annotations

import unittest

from theme import (
    CHERRY_BLOSSOM_COLORS,
    DEFAULT_THEME_NAME,
    ELECTRIC_BLUE_COLORS,
    THEME_NAMES,
    THEME_PRESETS,
    resolve_theme_name,
)


class ThemePresetTests(unittest.TestCase):
    def test_electric_blue_is_the_default_and_keeps_the_original_palette(self) -> None:
        self.assertEqual(DEFAULT_THEME_NAME, "Electric Blue")
        self.assertEqual(THEME_NAMES[0], DEFAULT_THEME_NAME)
        self.assertEqual(THEME_PRESETS[DEFAULT_THEME_NAME].appearance_mode, "dark")
        self.assertEqual(THEME_PRESETS[DEFAULT_THEME_NAME].colors, ELECTRIC_BLUE_COLORS)

    def test_researched_palette_candidates_are_selectable(self) -> None:
        expected = {
            "Cherry Blossom",
            "Tidal Teal",
            "Aurora Violet",
            "Moss Terminal",
            "Ember Study",
            "Oxblood Ledger",
            "Parchment & Ink",
            "Arctic Glass",
            "Midnight Orchid",
            "Copper Circuit",
            "Sage Paper",
        }
        self.assertTrue(expected.issubset(THEME_NAMES))
        self.assertEqual(len(THEME_NAMES), 12)
        self.assertEqual(set(THEME_NAMES), set(THEME_PRESETS))
        for name in THEME_NAMES:
            with self.subTest(name=name):
                self.assertTrue(THEME_PRESETS[name].description)
                self.assertEqual(
                    set(THEME_PRESETS[name].colors),
                    set(ELECTRIC_BLUE_COLORS),
                )

    def test_cherry_blossom_is_a_light_pink_and_green_palette(self) -> None:
        preset = THEME_PRESETS["Cherry Blossom"]
        self.assertEqual(preset.appearance_mode, "light")
        self.assertEqual(preset.colors, CHERRY_BLOSSOM_COLORS)
        self.assertEqual(preset.colors["panel"], "#FFFFFF")
        self.assertEqual(preset.colors["panel_alt"], "#FFF0F3")
        self.assertEqual(preset.colors["success_surface"], "#DCEFE2")

    def test_resolve_theme_name_has_a_safe_fallback(self) -> None:
        self.assertEqual(resolve_theme_name("Cherry Blossom"), "Cherry Blossom")
        self.assertEqual(resolve_theme_name("not-a-preset"), DEFAULT_THEME_NAME)
        self.assertEqual(
            resolve_theme_name("not-a-preset", fallback="Parchment & Ink"),
            "Parchment & Ink",
        )
        self.assertEqual(
            resolve_theme_name("not-a-preset", fallback="not-a-preset"),
            DEFAULT_THEME_NAME,
        )


if __name__ == "__main__":
    unittest.main()
