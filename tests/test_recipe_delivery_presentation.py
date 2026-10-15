"""Presentation preferences retain recipe evidence without rendering it."""
from __future__ import annotations

import sys
from pathlib import Path
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from core import StateStore, initial_recipe_delivery
from service_common import menu_email_html
from service import Application


class _Provider:
    def probe(self, **kwargs):
        return {"server": {"name": "synthetic"}, "tool_count": 0}


class RecipeDeliveryPresentationTests(unittest.TestCase):
    def test_estimate_labels_can_be_hidden_without_removing_recipe_evidence(self):
        menu = {
            "week": "2026-W38",
            "dishes": [{
                "name": "Syntetisk kontroll",
                "portions": 2,
                "portions_evidence": {"basis": "estimate", "input": "2–3 porsjoner", "assumptions": "Midtpunkt"},
                "ingredients": [{"amount": "200 g", "item": "gulrot", "raw": "200 g gulrot",
                                 "evidence": {"quantity": {"basis": "estimate", "input": "én gulrot", "assumptions": "Vektanslag"}}}],
                "steps": ["Kok."],
                "source": {"kind": "user", "relationship": "user_supplied"},
                "rights": {"storage": "full"},
            }],
            "salads": [],
        }

        shown = menu_email_html(menu)
        hidden = menu_email_html(menu, show_estimate_labels=False)

        self.assertIn("anslag", shown)
        self.assertIn("Midtpunkt", shown)
        self.assertNotIn("anslag", hidden.casefold())
        self.assertNotIn("midtpunkt", hidden.casefold())
        self.assertNotIn("vektanslag", hidden.casefold())
        self.assertIn("200 g gulrot", hidden)
        self.assertIn("2 porsjoner", hidden)
        self.assertEqual("estimate", menu["dishes"][0]["portions_evidence"]["basis"])

    def test_weekdays_neutral_source_credit_and_safe_html(self):
        menu = {"week": "2026-W42", "schedule": [{"day": "2026-10-12", "meal": "Soup"}],
                "dishes": [{"name": "Soup <test>", "portions": 2, "ingredients": [{"item": "Carrot"}], "steps": ["Cook."],
                            "notes": "First line\nSecond line",
                            "source": {"kind": "pasted_text", "relationship": "adapted", "title": "Original printed title",
                                       "original": {"publisher": "Example Kitchen", "author": "A & B",
                                                    "url": "https://example.com/soup"}},
                            "rights": {"storage": "full", "credit": "Original credit"}}]}
        rendered = menu_email_html(menu)
        self.assertIn("Mandag 12. oktober 2026", rendered)
        self.assertNotIn("pasted_text", rendered)
        self.assertNotIn("Familiens egen", rendered)
        self.assertIn("Example Kitchen – A &amp; B", rendered)
        self.assertIn('href="https://example.com/soup"', rendered)
        self.assertIn("Original printed title", rendered)
        self.assertIn("Original credit", rendered)
        self.assertIn("Soup &lt;test&gt;", rendered)
        self.assertIn("First line<br>Second line", rendered)
        from recipe_delivery import _PDFBlocks
        parser = _PDFBlocks()
        parser.feed(rendered)
        parser.flush()
        self.assertIn(('p', 'First line\nSecond line'), parser.blocks)
        menu['output_language'] = 'en'
        self.assertIn('Monday 12 October 2026', menu_email_html(menu))
        menu['dishes'][0]['rights']['storage'] = 'link_only'
        linked = menu_email_html(menu)
        self.assertNotIn('Carrot', linked)
        self.assertNotIn('Cook.', linked)
        self.assertIn('Original credit', linked)

    def test_web_source_title_without_publisher_is_not_reported_missing(self):
        source = {"kind": "web", "relationship": "adapted", "title": "Original <recipe>",
                  "url": "https://example.com/original"}
        menu = {"dishes": [{"name": "Adapted dinner", "source": source,
                            "rights": {"storage": "full", "credit": "Example Kitchen"}}]}
        for language, label in (("no", "Tilpasset"), ("en", "Adapted")):
            with self.subTest(language=language):
                menu["output_language"] = language
                rendered = menu_email_html(menu)
                self.assertIn(f'<strong>{label}:</strong> <a href="https://example.com/original">Original &lt;recipe&gt;</a>. Example Kitchen', rendered)
                self.assertNotIn("Kilde ikke registrert", rendered)
                self.assertNotIn("Source not recorded", rendered)
        self.assertNotIn("publisher", source)

    def test_source_caption_fallbacks_preserve_known_credits_and_safe_links(self):
        cases = [
            ({"kind": "web", "publisher": "Example Kitchen", "title": "Original",
              "url": "https://example.com/recipe"}, "Example Kitchen – Original", True),
            ({"kind": "web", "url": "https://example.com/recipe"}, "example.com", True),
            ({"kind": "web", "title": "Printed original"}, "Printed original", False),
            ({"kind": "pasted_text", "title": "Original"}, "Innsendt oppskrift – Original", False),
            ({"kind": "generated"}, "Generert oppskrift", False),
            ({}, "Kilde ikke registrert", False),
            ({"kind": "web", "url": "javascript:alert(1)"}, "Kilde ikke registrert", False),
        ]
        for source, caption, linked in cases:
            with self.subTest(source=source):
                rendered = menu_email_html({"dishes": [{"name": "Dinner", "source": source}]})
                self.assertIn(caption, rendered)
                self.assertEqual('<a href=' in rendered, linked)
                self.assertNotIn("javascript:", rendered)

    def test_new_delivery_preferences_default_to_visible_estimate_labels(self):
        preferences = initial_recipe_delivery()["preferences"]

        self.assertTrue(preferences["chat"]["show_estimate_labels"])
        self.assertTrue(preferences["email"]["show_estimate_labels"])

    def test_delivery_configuration_persists_hidden_labels_for_both_channels(self):
        with tempfile.TemporaryDirectory() as root:
            app = Application(StateStore(Path(root) / "state", {"household": "Synthetic", "provider": "oda"}), _Provider(), None)
            result = app.handle({"operation": "recipe_delivery", "action": "configure", "changes": {
                "chat": {"show_estimate_labels": False},
                "email": {"show_estimate_labels": False},
            }})

            self.assertFalse(result["preferences"]["chat"]["show_estimate_labels"])
            self.assertFalse(result["preferences"]["email"]["show_estimate_labels"])
            self.assertFalse(app.store.read()["recipe_delivery"]["preferences"]["chat"]["show_estimate_labels"])
            self.assertFalse(app.store.read()["recipe_delivery"]["preferences"]["email"]["show_estimate_labels"])


if __name__ == "__main__":
    unittest.main()
