"""Native recipe editor prose and optional provenance stay usable without network."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from recipe_libraries import RecipeLibraryError
from recipe_library_mealie import MealieAdapter
from recipe_library_recipesage import RecipeSageAdapter
import recipe_library_setup


def reader(provider):
    cls = MealieAdapter if provider == "mealie" else RecipeSageAdapter
    return cls({"library_id": "synthetic-" + provider, "provider": provider,
                "base_url": "https://recipes.example", "read_only": True}, {"token": "SYNTHETIC"})


def fixture(provider):
    version = "v3.24.0.json" if provider == "mealie" else "v4.0.6.json"
    return json.loads((ROOT / "tests" / "fixtures" / provider / version).read_text())["recipe_get"]


class NativeRecipeProseTests(unittest.TestCase):
    def test_mealie_retains_multiline_steps_notes_and_ignores_empty_editor_rows(self):
        raw = fixture("mealie")
        raw["recipeIngredient"] += [{"originalText": ""}, {"originalText": " \t "}, {}]
        raw["recipeInstructions"] = [{"text": "Chop the carrots.\nSimmer.\tServe."}]
        raw["notes"] = [{"text": "First note.\nSecond line."}]
        mapped = reader("mealie")._mapped_recipe(raw)
        self.assertEqual(mapped["steps"], ["Chop the carrots.\nSimmer.\tServe."])
        self.assertEqual(mapped["notes"], "First note.\nSecond line.")
        self.assertEqual(len(mapped["ingredients"]), len(raw["recipeIngredient"]) - 3)
        for body in ("Simmer.\x00", "Simmer.\x1b", "Simmer.\x7f", "Simmer.\ud800"):
            with self.subTest(body=repr(body)):
                raw["recipeInstructions"] = [{"text": body}]
                with self.assertRaises(RecipeLibraryError):
                    reader("mealie")._mapped_recipe(raw)

    def test_invalid_optional_original_urls_do_not_replace_the_safe_url(self):
        for provider, field in (("mealie", "orgURL"), ("recipesage", "url")):
            for value in ("Grandmother's recipe card", "not a URL", "https://u:p@recipes.example/soup"):
                with self.subTest(provider=provider, value=value):
                    raw = fixture(provider)
                    raw[field] = value
                    mapped = reader(provider)._mapped_recipe(raw)
                    self.assertIsNone(mapped["source"]["url"])
                    self.assertIsNone((mapped["source"].get("original") or {}).get("url"))
                    self.assertTrue(mapped["ingredients"])
                    self.assertNotIn(value, json.dumps(mapped))
            raw = fixture(provider)
            raw[field] = "https://recipes.example/soup?utm_source=fixture&id=12"
            mapped = reader(provider)._mapped_recipe(raw)
            self.assertEqual(mapped["source"]["original"]["url"], "https://recipes.example/soup?id=12")

    def test_mealie_identity_fields_do_not_accept_line_breaks(self):
        raw = fixture("mealie")
        raw["name"] = "Soup\nSecond title"
        with self.assertRaises(RecipeLibraryError):
            reader("mealie")._mapped_recipe(raw)


class LibrarySetupCompatibilityTests(unittest.TestCase):
    def test_documented_read_only_flag_parses(self):
        args = recipe_library_setup.parser().parse_args([
            "--config", "/synthetic/config.json", "--home", "/synthetic",
            "add", "--library-id", "synthetic-mealie", "--provider", "mealie",
            "--base-url", "https://recipes.example", "--read-only"])
        self.assertTrue(args.read_only)

    def test_macos_restart_defaults_to_installer_label_and_retains_override(self):
        for override, label in (({}, "com.meal-concierge"),
                                ({"MEAL_CONCIERGE_LAUNCHD_LABEL": "custom.service"}, "custom.service")):
            with self.subTest(label=label), mock.patch.object(recipe_library_setup.os, "uname") as uname, \
                    mock.patch.dict(recipe_library_setup.os.environ, override, clear=True), \
                    mock.patch.object(recipe_library_setup.os, "getuid", return_value=501), \
                    mock.patch.object(recipe_library_setup.subprocess, "run") as run:
                uname.return_value.sysname = "Darwin"
                run.return_value = subprocess.CompletedProcess([], 0, stdout="state = running")
                recipe_library_setup._restart_running_service()
                target = "gui/501/" + label
                self.assertEqual(run.call_args_list[0].args[0], ["launchctl", "print", target])
                self.assertEqual(run.call_args_list[1].args[0], ["launchctl", "kickstart", "-k", target])


if __name__ == "__main__":
    unittest.main()
