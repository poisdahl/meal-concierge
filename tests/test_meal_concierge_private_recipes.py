"""Private retailer recipes through Application; all content and transports synthetic.

Oda/Mathem fixtures are private normalized input/restore examples, not claims
about a native recipe-detail protocol or permission to redistribute recipes.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from test_meal_concierge_recipes import (
    Application, CONFIG, FakeBrowser, FakeOda, HouseholdError,
    RecipeError, RecipeStore, StateStore, SyntheticLibraryAdapter, full_recipe, normalize_recipe,
)
from recipes import recipe_digest, source_ingredient


PROVIDERS = ("oda", "meny", "mathem")
DOMAINS = {"oda": "oda.com", "meny": "meny.no", "mathem": "mathem.se"}


def private_recipe(provider, *, trusted=False):
    value = full_recipe(
        f"Synthetic {provider} fish", external_id=f"private-{provider}",
        url=f"https://{DOMAINS[provider]}/oppskrifter/synthetic-fish/",
        relationship="original",
    )
    value.update(schema_version=2, source_provider=None)
    value["source"].update(kind="web", publisher=provider.upper())
    value["rights"].update(credit=f"Synthetic {provider.upper()} attribution", license=None)
    value["ingredients"] = [source_ingredient("400 g torsk")]
    value["portions_evidence"] = {"basis": "source", "input": "4 portions"}
    if not trusted:
        # Direct caller data can assert household input, never source authority.
        value["portions_evidence"] = {"basis": "user", "input": "4 portions entered by user"}
        for evidence in value["ingredients"][0]["evidence"].values():
            evidence.update(basis="user", input="400 g torsk entered by user")
    else:
        value["source_provider"] = provider
    return value


class PrivateRecipeApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mc42-private-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sequence = 0

    def application(self, provider="oda", directory=None):
        self.sequence += 1
        directory = directory or self.root / str(self.sequence)
        store = StateStore(directory, {**CONFIG, "provider": provider})
        client = FakeOda()
        browser = FakeBrowser()
        browser.oda = client
        return Application(store, client, browser)

    def save(self, app, recipe=None, *, discovery_ref=None, key="private-save"):
        request = {"operation": "recipes", "action": "save", "idempotency_key": key}
        request.update({"discovery_ref": discovery_ref} if discovery_ref else {"recipe": recipe})
        return app.handle(request)["recipe"]

    def save_menu(self, app, recipe):
        return app.handle({"operation": "menu", "action": "save", "menu": {
            "week": "2026-W40", "dishes": [recipe], "salads": [],
        }})["menu"]

    def test_original_full_private_direct_save_and_menu_for_each_provider(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                app = self.application(provider)
                source = private_recipe(provider)
                saved = self.save(app, source)
                self.assertEqual(saved["source_provider"], provider)
                self.assertEqual(saved["source"]["relationship"], "original")
                self.assertEqual(saved["rights"]["storage"], "full")
                self.assertEqual(saved["steps"], source["steps"])
                self.assertEqual(saved["entry_origin"], "user")
                current = self.save_menu(app, {"recipe_ref": {
                    "id": saved["id"], "revision": saved["revision"],
                }, "portions": 4})
                dish = current["dishes"][0]
                self.assertEqual(dish["source_provider"], provider)
                self.assertEqual(dish["source"], saved["source"])
                self.assertEqual(dish["steps"], saved["steps"])
                self.assertNotIn("entry_origin", dish)
                restarted = self.application(provider, app.store.directory)
                self.assertEqual(restarted.handle({"operation": "menu", "action": "get"})["menu"], current)
                reread = restarted.handle({"operation": "recipes", "action": "get", "recipe_id": saved["id"]})["recipe"]
                self.assertEqual(reread["source_provider"], provider)
                self.assertEqual(reread["entry_origin"], "user")

    def test_trusted_discovery_stays_unsaved_until_explicit_save_or_favorite(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                app = self.application(provider)
                original = normalize_recipe(private_recipe(provider, trusted=True))
                discovery = app.recipes.persist_discovery(original)
                resolved = app.handle({"operation": "recipes", "action": "resolve", "discovery_ref": discovery["discovery_ref"]})
                self.assertEqual(resolved["recipe"], original)
                self.assertNotIn("entry_origin", resolved["recipe"])
                self.assertEqual(app.recipes.search("", include_archived=True), [])
                request = {"operation": "recipes", "action": "set_favorite", "discovery_ref": discovery["discovery_ref"],
                           "is_favorite": True, "idempotency_key": "favorite-private"}
                favorite = app.handle(request)
                replay = app.handle(request)
                self.assertTrue(favorite["is_favorite"])
                self.assertEqual(replay["recipe"]["id"], favorite["recipe"]["id"])
                self.assertEqual(len(app.recipes.search("", include_archived=True)), 1)
                saved = favorite["recipe"]
                self.assertEqual(saved["entry_origin"], "user")
                self.assertEqual(normalize_recipe(saved), original)
                self.save_menu(app, {"library_recipe_ref": saved["library_recipe_ref"], "portions": 4})

    def test_favorite_partial_failure_retries_one_private_entry(self):
        app = self.application()
        discovery = app.recipes.persist_discovery(private_recipe("oda", trusted=True))
        request = {"operation": "recipes", "action": "set_favorite", "discovery_ref": discovery["discovery_ref"],
                   "is_favorite": True, "idempotency_key": "favorite-after-interruption"}
        with mock.patch.object(app.recipes, "set_favorite", side_effect=RecipeError("synthetic interruption")):
            with self.assertRaisesRegex(RecipeError, "synthetic interruption"):
                app.handle(request)
        self.assertEqual(len(app.recipes.search("", include_archived=True)), 0)
        self.assertTrue(app.handle(request)["is_favorite"])
        self.assertEqual(len(app.recipes.search("", include_archived=True)), 1)

    def test_exact_discovery_menu_uses_full_source_facts_without_personal_save(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                app = self.application(provider)
                original = normalize_recipe(private_recipe(provider, trusted=True))
                discovery = app.recipes.persist_discovery(original)
                current = self.save_menu(app, {"discovery_ref": discovery["discovery_ref"], "portions": 4})
                dish = current["dishes"][0]
                self.assertEqual(dish["source"], original["source"])
                self.assertEqual(dish["source_provider"], provider)
                self.assertEqual(dish["steps"], original["steps"])
                self.assertEqual(dish["portions_evidence"], original["portions_evidence"])
                self.assertEqual(app.recipes.search("", include_archived=True), [])
                self.assertNotIn("entry_origin", dish)
                self.assertEqual(app.handle({"operation": "recipes", "action": "resolve", "discovery_ref": discovery["discovery_ref"]})["recipe"], original)

    def test_favorite_idempotency_conflict_does_not_save_another_discovery(self):
        app = self.application()
        first = app.recipes.persist_discovery(private_recipe("oda", trusted=True))
        another = private_recipe("oda", trusted=True)
        another["name"] = "Another synthetic fish"
        another["source"].update(external_id="another", url="https://oda.com/no/recipes/another/")
        second = app.recipes.persist_discovery(another)
        request = {"operation": "recipes", "action": "set_favorite", "discovery_ref": first["discovery_ref"],
                   "is_favorite": True, "idempotency_key": "one-exact-favorite"}
        app.handle(request)
        with self.assertRaises(RecipeError):
            app.handle({**request, "discovery_ref": second["discovery_ref"]})
        self.assertEqual(len(app.recipes.search("", include_archived=True)), 1)

    def test_cross_provider_direct_save_menu_discovery_save_and_favorite_are_rejected(self):
        for active in PROVIDERS:
            for source in PROVIDERS:
                if active == source:
                    continue
                with self.subTest(active=active, source=source):
                    app = self.application(active)
                    value = private_recipe(source)
                    discovery = app.recipes.persist_discovery(private_recipe(source, trusted=True))
                    calls = deepcopy(app.provider_client.calls)
                    requests = [
                        {"operation": "recipes", "action": "save", "recipe": value},
                        {"operation": "recipes", "action": "save", "discovery_ref": discovery["discovery_ref"], "idempotency_key": "blocked-save"},
                        {"operation": "recipes", "action": "set_favorite", "discovery_ref": discovery["discovery_ref"], "is_favorite": True, "idempotency_key": "blocked-favorite"},
                        {"operation": "menu", "action": "save", "menu": {"week": "2026-W40", "dishes": [value]}},
                    ]
                    for request in requests:
                        with self.subTest(action=request["action"], operation=request["operation"]):
                            with self.assertRaisesRegex((RecipeError, HouseholdError), "selected grocery provider"):
                                app.handle(request)
                    self.assertEqual(app.recipes.search("", include_archived=True), [])
                    self.assertEqual(app.provider_client.calls, calls)

    def test_existing_private_entry_cannot_be_newly_favorited_or_planned_elsewhere(self):
        app = self.application("oda")
        saved = self.save(app, private_recipe("oda"))
        app.provider = "mathem"  # Synthetic eligibility context; no runtime switch API.
        before = deepcopy(app.store.read())
        with self.assertRaisesRegex((RecipeError, HouseholdError), "selected grocery provider"):
            app.handle({"operation": "recipes", "action": "set_favorite", "library_recipe_ref": saved["library_recipe_ref"],
                        "is_favorite": True, "idempotency_key": "wrong-provider"})
        with self.assertRaisesRegex((RecipeError, HouseholdError), "selected grocery provider"):
            self.save_menu(app, {"library_recipe_ref": saved["library_recipe_ref"], "portions": 4})
        self.assertEqual(app.store.read()["menu"], before["menu"])
        self.assertEqual(app.store.read()["recipe_usage"], before["recipe_usage"])
        inspected = app.handle({"operation": "recipes", "action": "get", "recipe_id": saved["id"]})["recipe"]
        self.assertEqual(inspected["source"], saved["source"])

    def test_saved_menu_product_and_cart_preparation_reject_provider_mismatch_without_effects(self):
        app = self.application("oda")
        current = self.save_menu(app, private_recipe("oda"))
        reference = app._cart_menu_ref(current)
        app.provider = "mathem"
        before = deepcopy(app.store.read())
        calls = deepcopy(app.provider_client.calls)
        for request in (
            {"operation": "products", "action": "prepare", "menu_ref": reference},
            {"operation": "cart", "action": "sync", "menu_ref": reference, "requirements": [{"product_id": 10, "quantity": 1}]},
            {"operation": "checkout", "action": "prepare"},
        ):
            with self.subTest(operation=request["operation"]):
                with self.assertRaisesRegex((RecipeError, HouseholdError), "selected grocery provider"):
                    app.handle(request)
        self.assertEqual(app.provider_client.calls, calls)
        self.assertEqual(app.store.read(), before)
        self.assertEqual(app.handle({"operation": "menu", "action": "get"})["menu"], current)

    def test_copy_update_import_style_relabel_cannot_clear_known_binding(self):
        app = self.application()
        saved = self.save(app, private_recipe("oda"))
        for change in ("clear", "relabel", "downgrade", "conflicting_ref"):
            with self.subTest(change=change):
                copied = deepcopy(saved)
                if change == "clear":
                    copied["source_provider"] = None
                elif change == "relabel":
                    copied["source"] = full_recipe()["source"]
                    copied.pop("source_provider")
                elif change == "downgrade":
                    copied["schema_version"] = 1
                else:
                    copied["recipe_ref"] = {"id": "missing", "revision": 1}
                for request in (
                    {"operation": "recipes", "action": "save", "recipe": copied},
                    {"operation": "recipes", "action": "update", "recipe_id": saved["id"], "expected_revision": saved["revision"], "recipe": copied},
                ):
                    with self.assertRaises((RecipeError, HouseholdError)):
                        app.handle(request)
        self.assertEqual(app.recipes.get(saved["id"])["revision"], 1)
        self.assertEqual(len(app.recipes.search("", include_archived=True)), 1)

    def test_known_original_url_retains_binding_after_adapted_relabel(self):
        app = self.application("mathem")
        value = private_recipe("oda")
        original = {key: value["source"].get(key) for key in ("url", "external_id", "publisher", "author", "title")}
        value["source"] = full_recipe(relationship="adapted")["source"]
        value["source"]["original"] = original
        with self.assertRaisesRegex((RecipeError, HouseholdError), "selected grocery provider"):
            self.save(app, value)

    def test_contradictory_copied_id_and_library_reference_are_rejected(self):
        app = self.application()
        first = self.save(app, private_recipe("oda"))
        other = full_recipe("Different household recipe", external_id="different")
        second = self.save(app, other, key="second-save")
        copied = deepcopy(first)
        copied["library_recipe_ref"] = second["library_recipe_ref"]
        with self.assertRaisesRegex(RecipeError, "references disagree"):
            self.save(app, copied, key="contradictory-copy")
        self.assertEqual(len(app.recipes.search("", include_archived=True)), 2)

    def test_source_evidence_cannot_be_asserted_by_direct_caller(self):
        app = self.application()
        with self.assertRaisesRegex(RecipeError, "source evidence requires"):
            self.save(app, private_recipe("oda", trusted=True))
        self.assertEqual(app.recipes.search("", include_archived=True), [])

    def test_legacy_provider_snapshot_remains_exact_and_ineligible_elsewhere(self):
        app = self.application()
        legacy = full_recipe(url="https://oda.com/no/recipes/legacy/", relationship="adapted")
        legacy["source"]["publisher"] = "Oda"
        saved = app.recipes.save(legacy)  # Represents an existing schema-1 private bank.
        exact = deepcopy(app.recipes.get(saved["id"]))
        app.provider = "mathem"
        fetched = app.handle({"operation": "recipes", "action": "get", "recipe_id": saved["id"]})["recipe"]
        self.assertEqual(fetched["schema_version"], 1)
        self.assertNotIn("source_provider", fetched)
        self.assertEqual(recipe_digest(fetched), recipe_digest(exact))
        with self.assertRaisesRegex((RecipeError, HouseholdError), "selected grocery provider"):
            self.save_menu(app, {"recipe_ref": {"id": saved["id"], "revision": 1}, "portions": 4})
        self.assertEqual(app.recipes.get(saved["id"]), exact)

    def test_pack_rejects_known_store_url_even_when_explicit_binding_is_null(self):
        app = self.application()
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(RecipeError, "store-bound"):
                    app.recipes.import_pack_record(private_recipe(provider), pack_id="synthetic-pack", recipe_id=provider, version="1")
        self.assertEqual(app.recipes.search("", include_archived=True), [])

    def test_private_backup_restore_keeps_original_fulltext_favorite_and_origin(self):
        app = self.application()
        saved = self.save(app, private_recipe("oda"))
        app.handle({"operation": "recipes", "action": "set_favorite", "library_recipe_ref": saved["library_recipe_ref"],
                    "is_favorite": True, "idempotency_key": "backup-favorite"})
        snapshot = self.save_menu(app, {"library_recipe_ref": saved["library_recipe_ref"], "portions": 4})
        expected = app.handle({"operation": "recipes", "action": "get", "recipe_id": saved["id"]})["recipe"]
        destination = self.root / "restored"
        destination.mkdir()
        app.recipes.backup(destination / "recipes.sqlite3")
        shutil.copy2(app.store.directory / "state.json", destination / "state.json")
        restored = self.application("oda", destination)
        actual = restored.handle({"operation": "recipes", "action": "get", "recipe_id": saved["id"]})["recipe"]
        self.assertEqual(actual, expected)
        self.assertTrue(actual["is_favorite"])
        self.assertEqual(actual["entry_origin"], "user")
        self.assertEqual(actual["source_provider"], "oda")
        self.assertEqual(restored.handle({"operation": "menu", "action": "get"})["menu"], snapshot)
        with self.assertRaisesRegex(HouseholdError, "belongs to provider oda"):
            StateStore(destination, {**CONFIG, "provider": "mathem"})

    def test_exact_reference_imports_share_transaction_and_dry_run_preserves_live_database(self):
        app = self.application()
        saved = self.save(app, private_recipe("oda"))
        discovery = app.recipes.persist_discovery(full_recipe("Expired synthetic", external_id="expired"))
        with sqlite3.connect(app.recipes.path) as connection:
            connection.execute("UPDATE discovery_snapshots SET expires_at='2000-01-01T00:00:00+00:00' WHERE discovery_ref=?", (discovery["discovery_ref"],))
        before = app.recipes.path.read_bytes()
        for dry_run in (True, False):
            result = app.recipes.import_records([saved], dry_run=dry_run, provider="oda")
            self.assertEqual((result["created"], result["skipped"]), (0, 1))
            if dry_run:
                self.assertEqual(app.recipes.path.read_bytes(), before)
        copied = deepcopy(saved)
        copied["source_provider"] = None
        with self.assertRaisesRegex(RecipeError, "preserve"):
            app.recipes.import_records([copied], provider="oda")
        empty = RecipeStore(self.root / "absent.sqlite3", "Synthetic household")
        with self.assertRaisesRegex(RecipeError, "not found"):
            empty.import_records([saved], dry_run=True, provider="oda")
        self.assertFalse(empty.path.exists())
        before_count = len(app.recipes.search("", include_archived=True))
        with self.assertRaisesRegex(RecipeError, "selected grocery provider"):
            app.recipes.import_records([full_recipe("Rollback", external_id="rollback"), private_recipe("meny")], provider="oda")
        self.assertEqual(len(app.recipes.search("", include_archived=True)), before_count)

    def test_favorite_discovery_replays_after_snapshot_eviction_and_provider_mismatch(self):
        app = self.application()
        discovered = app.recipes.persist_discovery(private_recipe("oda", trusted=True))
        request = {"operation": "recipes", "action": "set_favorite", "discovery_ref": discovered["discovery_ref"],
                   "is_favorite": True, "idempotency_key": "durable-favorite"}
        first = app.handle(request)
        with sqlite3.connect(app.recipes.path) as connection:
            connection.execute("DELETE FROM discovery_snapshots WHERE discovery_ref=?", (discovered["discovery_ref"],))
        app.provider = "mathem"
        replay = app.handle(request)
        self.assertEqual(replay["recipe"]["id"], first["recipe"]["id"])
        self.assertEqual(replay["favorite_revision"], first["favorite_revision"])
        with self.assertRaisesRegex(RecipeError, "selected grocery provider"):
            app.handle({**request, "idempotency_key": "new-favorite"})
        app.provider = "oda"
        self.assertTrue(app.handle({**request, "idempotency_key": "new-favorite"})["is_favorite"])
        self.assertEqual(len(app.recipes.search("", include_archived=True)), 1)

    def test_ambiguous_legacy_attribution_remains_readable_and_ineligible(self):
        from service_common import menu_email_html
        app = self.application()
        legacy = full_recipe("Historical ambiguity", url="https://oda.com/no/recipes/history/", relationship="adapted")
        legacy["source"]["publisher"] = "MENY"
        saved = app.recipes.save(legacy)
        before = normalize_recipe(saved)
        fetched = app.handle({"operation": "recipes", "action": "get", "recipe_id": saved["id"]})
        self.assertFalse(fetched["provider_eligibility"]["eligible"])
        self.assertIn("conflicting", fetched["provider_eligibility"]["reason"])
        self.assertEqual(normalize_recipe(fetched["recipe"]), before)
        search = app.handle({"operation": "recipes", "action": "search", "query": "Historical", "include_ineligible": True})
        self.assertEqual(len(search["recipes"]), 1)
        self.assertFalse(search["recipes"][0]["provider_eligibility"]["eligible"])
        html = menu_email_html({"week": "2026-W40", "dishes": [saved], "salads": []})
        self.assertIn("Historical ambiguity", html)
        self.assertIn("oda.com", html)
        self.assertIn("MENY", html)
        with self.assertRaisesRegex(RecipeError, "conflicting"):
            self.save_menu(app, {"recipe_ref": {"id": saved["id"], "revision": 1}, "portions": 4})
        self.assertEqual(normalize_recipe(app.recipes.get(saved["id"])), before)

    def test_original_external_favorite_reconciles_without_new_provider_guard_or_write(self):
        settings = {**CONFIG, "recipe_libraries": [{"library_id": "family-mealie", "provider": "mealie",
                    "base_url": "https://recipes.example", "read_only": False}]}
        adapter = SyntheticLibraryAdapter("family-mealie", private_recipe("meny", trusted=True), favorite_state=True)
        app = Application(StateStore(self.root / "legacy-favorite", settings), FakeOda(), FakeBrowser(),
                          recipe_library_adapters={"family-mealie": adapter})
        operation = app.recipes.begin_library_favorite(adapter.reference, True, idempotency_key="original-intent")
        app.recipes.claim_library_dispatch(operation["operation_id"])
        app.recipes.finish_library_favorite(operation["operation_id"], "uncertain", error_code="transport", error="synthetic lost response")
        request = {"operation": "recipes", "action": "set_favorite", "library_recipe_ref": adapter.reference,
                   "is_favorite": True, "idempotency_key": "original-intent"}
        with mock.patch.object(app, "_external_library_get", side_effect=AssertionError("must use original favorite journal")):
            reconciled = app.handle(request)
            self.assertEqual(reconciled["status"], "confirmed")
            self.assertTrue(reconciled["reconciled"])
            self.assertEqual(app.handle(request), reconciled)
        self.assertEqual(adapter.favorite_write_calls, 0)
        with self.assertRaisesRegex(RecipeError, "selected grocery provider"):
            app.handle({**request, "idempotency_key": "fresh-intent"})
        self.assertEqual(adapter.favorite_write_calls, 0)

    def test_original_order_and_email_context_survive_reads_rejection_and_restore(self):
        app = self.application()
        discovery = app.recipes.persist_discovery(private_recipe("oda", trusted=True))
        current = self.save_menu(app, {"discovery_ref": discovery["discovery_ref"], "portions": 4})
        original = {**deepcopy(current), "order_id": "synthetic-order-42"}
        with app.store.locked() as state:
            state["order_snapshots"]["synthetic-order-42"] = original
            state["order_snapshot_providers"]["synthetic-order-42"] = "oda"
            state["email_recipient"] = "synthetic@example.test"
        app.handle({"operation": "email", "action": "schedule", "order_id": "synthetic-order-42", "delivery_date": "2026-10-01"})
        # An already-dispatched operation is fixture state, never dispatched here.
        pending = {"provider": "oda", "status": "uncertain", "confirmation_id": "synthetic-confirmation",
                   "idempotency_key": "synthetic-original-attempt", "menu_snapshot": deepcopy(original)}
        with app.store.locked() as state:
            state["pending_checkout"] = deepcopy(pending)
        before = app.store.read()
        app.provider = "mathem"
        status = app.handle({"operation": "email", "action": "status"})
        self.assertEqual(status["jobs"][0]["provider"], "oda")
        rendered = app.handle({"operation": "email", "action": "test", "provider": "oda", "order_id": "synthetic-order-42"})
        self.assertIn("Synthetic oda fish", rendered["html"])
        self.assertIn("oda.com", rendered["html"])
        with self.assertRaisesRegex((RecipeError, HouseholdError), "selected grocery provider"):
            app.handle({"operation": "products", "action": "prepare", "menu_ref": app._cart_menu_ref(current)})
        self.assertEqual(app.store.read(), before)
        directory = self.root / "original-order-restored"
        directory.mkdir()
        app.recipes.backup(directory / "recipes.sqlite3")
        shutil.copy2(app.store.directory / "state.json", directory / "state.json")
        restored = self.application("oda", directory)
        restored.handle({"operation": "email", "action": "status"})
        for key in ("pending_checkout", "order_snapshots", "order_snapshot_providers", "email_jobs"):
            self.assertEqual(restored.store.read()[key], before[key])


if __name__ == "__main__":
    unittest.main()
