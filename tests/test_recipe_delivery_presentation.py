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
