"""Selection behavior using real normalized recipes and the production planner."""
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import DEFAULT_PROFILE
from core import StateStore
from planner import canonical, digest, plan_week, prepare_candidate, _listed_fish_mass, _preference_reasons, _slot_reasons
from recipes import normalize_recipe, scale_recipe, RecipeError
from recipe_selection import (PAGE_SIZE, candidate_groups, collect_candidates, compact_candidate,
                              context_queries, shortlist, shortlist_capacity, source_identities)
from recipe_selection import history_source_index, family_history_usage, MAX_HISTORY_DOCUMENTS
from recipe_libraries import library_recipe_key
from service import Application
from test_meal_concierge_planner import NoProviderCalls, CONFIG


def candidate(index, *, tags=None, saved=False, error=None, source_id=None):
    recipe = normalize_recipe({
        "name": f"Synthetic meal {index}", "portions": 2,
        "ingredients": [{"item": f"gulrot {index}", "quantity": 200, "unit": "g", "raw": "200 g gulrot"}],
        "steps": ["Prepare the synthetic meal. " * 40], "tags": tags or [],
        "source": {"kind": "user", "publisher": "Fixture", "external_id": str(source_id or index), "relationship": "user_supplied"},
        "rights": {"storage": "full"}, "times": {"active_minutes": 30},
    })
    reference = {"recipe_ref": {"id": f"rec_{index:024x}", "revision": 1}} if saved else {"discovery_ref": f"discovery:fixture:{index}"}
    return {"reference": reference, "reference_key": canonical(reference), "recipe": recipe,
            "recipe_key": f"fixture:{index}", "dedupe_key": f"fixture:{index}", "content_digest": digest(recipe),
            "usage": {"eligible": True}, "facts": {}, "supplied_facts": {}, "materialization_error": error}


def request(days=7):
    return {"week": "2026-W37", "dates": [(date(2026, 9, 7) + timedelta(days=i)).isoformat() for i in range(days)],
            "portions": 2, "candidates": [], "as_of_date": "2026-09-06"}


class SelectionTests(unittest.TestCase):
    def test_compact_is_smaller_and_cannot_establish_ingredient_feasibility(self):
        full = candidate(1)
        summary = compact_candidate(full["recipe"], full["reference"])
        self.assertNotIn("ingredients", summary)
        self.assertNotIn("steps", summary)
        self.assertEqual(summary["detail_fields"]["ingredients"], "not_loaded")
        self.assertLess(len(json.dumps(summary)), len(json.dumps(full["recipe"])) / 2)
        full["recipe"] = summary
        self.assertEqual(prepare_candidate(full, DEFAULT_PROFILE, {})["hard_constraints"]["status"], "fail")

    def test_source_aliases_preserve_revision_queries_and_opaque_case(self):
        a = {"source": {"url": "https://www.meny.no/oppskrifter/suppe/?utm_source=x&revision=1", "publisher": "MENY", "external_id": "ABC"}}
        b = {"source": {"url": "https://meny.no/oppskrifter/suppe?revision=1", "publisher": "meny", "external_id": "ABC"}}
        self.assertEqual(source_identities(a), source_identities(b))
        c = {"source": {"url": "https://meny.no/oppskrifter/suppe?revision=2", "publisher": "meny", "external_id": "abc"}}
        self.assertFalse(source_identities(a).intersection(source_identities(c)))

    def test_family_keeps_versions_and_draft_does_not_hide_ready_web(self):
        draft = candidate(1, saved=True, source_id="same", error="draft")
        ready = candidate(2, source_id="same")
        other = candidate(3)
        other["recipe"]["name"] = ready["recipe"]["name"]
        groups = candidate_groups([draft, ready, other])
        self.assertEqual(sorted(map(len, groups)), [1, 2])
        result = shortlist([draft, ready, other], request(1), DEFAULT_PROFILE)
        self.assertEqual(result["suitable_count"], 2)
        self.assertIn(ready["reference"], [item["reference"] for item in result["candidates"]])
        self.assertEqual(draft["materialization_error"], "draft")

    def test_preferences_change_selection_and_work_budget_is_real(self):
        values = [candidate(i) for i in range(1, 11)]
        values[-1]["recipe"]["tags"] = ["Thai", "spicy"]
        profile = deepcopy(DEFAULT_PROFILE)
        profile["cuisine"].update(wanted=["Thai"], flavours=["spicy"])
        selected = shortlist(values, request(), profile)
        self.assertEqual(shortlist_capacity(7), 8)
        self.assertEqual(selected["candidates"][0]["reference"], values[-1]["reference"])
        self.assertEqual(selected["shortlisted_out"], 2)
        small = request(2)
        small["candidates"] = [item["reference"] for item in selected["candidates"][:3]]
        result = plan_week(small, profile=profile, candidates=selected["candidates"][:3], history={})
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["explored_states"], 6)

    def test_stale_duplicate_unsuitable_first_page_refills(self):
        values = {str(i): candidate(i, error="unsuitable" if i < 10 else None) for i in range(1, 19)}
        calls = []
        def page(source, query, cursor, limit, deadline):
            calls.append((source, cursor))
            if source == "store":
                return {"candidates": [], "exhausted": True}
            ids = ["stale", "1", "1", "2"] if cursor is None else [str(i) for i in range(10, 19)]
            return {"candidates": [{"discovery_ref": key} for key in ids], "next_cursor": "page2" if cursor is None else None, "exhausted": cursor is not None}
        def resolve(summary, deadline):
            return values[summary["discovery_ref"]]
        result = collect_candidates(source_queries={"internal": [""], "store": ["middag"]}, fetch_page=page,
                                    resolve=resolve, request=request(), profile=DEFAULT_PROFILE)
        self.assertEqual(result["suitable_count"], 9)
        self.assertEqual(calls, [("internal", None), ("store", None), ("internal", "page2")])
        self.assertEqual(result["sources"][0]["rejected_details"], 1)
        self.assertFalse(result["ai_fallback_eligible"])

    def test_zero_statuses_and_ai_policy(self):
        for page_result, expected, eligible in [
            ({"candidates": [], "exhausted": True}, "empty", True),
            ({"candidates": [], "exhausted": False}, "search_limit", False),
            ({"candidates": [], "status": "rate_limited"}, "rate_limited", False),
            ({"candidates": [], "status": "timeout"}, "timeout", False),
            ({"candidates": None}, "unavailable", False),
        ]:
            with self.subTest(expected):
                result = collect_candidates(source_queries={"internal": [""], "store": None},
                    fetch_page=lambda *args: page_result, resolve=lambda *args: None, request=request(), profile=DEFAULT_PROFILE)
                self.assertEqual([s["status"] for s in result["sources"]], [expected, "disabled"])
                self.assertEqual(result["ai_fallback_eligible"], eligible)

    def test_shortfall_and_unknown_hard_constraints_never_generate(self):
        for allergy in (False, True):
            profile = deepcopy(DEFAULT_PROFILE)
            if allergy:
                profile["diet"]["avoid"] = ["nuts"]
            result = collect_candidates(source_queries={"internal": [""]},
                fetch_page=lambda *args: {"candidates": [{"discovery_ref": "one"}], "exhausted": True},
                resolve=lambda *args: candidate(1), request=request(), profile=profile)
            self.assertFalse(result["ai_fallback_eligible"])
            self.assertEqual(result["suitable_count"], 0 if allergy else 1)
            self.assertEqual(len(result["unknown"]), 1 if allergy else 0)

    def test_queries_use_actual_preferences_without_user_query(self):
        profile = deepcopy(DEFAULT_PROFILE)
        profile["cuisine"]["wanted"] = ["Thai"]
        self.assertEqual(context_queries(profile, "meny")[0], "Thai")
        self.assertLessEqual(len(context_queries(profile, "meny")), 6)

    def test_family_history_cannot_be_bypassed_by_web_reference(self):
        saved = candidate(1, saved=True, source_id="shared")
        saved["usage"] = {"eligible": False, "blocked_by": [{"week": "2026-W36"}]}
        web = candidate(2, source_id="shared")
        selected = shortlist([web, saved], request(1), DEFAULT_PROFILE)
        self.assertEqual(selected["suitable_count"], 0)
        direct = request(1)
        direct["candidates"] = [item["reference"] for item in (web, saved)]
        result = plan_week(direct, profile=DEFAULT_PROFILE, candidates=[web, saved], history={})
        self.assertEqual(result["status"], "no_plan")
        self.assertEqual(web["usage"], {"eligible": True})

    def test_unknown_quantities_are_needs_input_and_not_ai_permission(self):
        item = candidate(1)
        item["recipe"]["ingredients"][0].update(quantity=None, scalable=False)
        result = collect_candidates(source_queries={"internal": [""]},
            fetch_page=lambda *args: {"candidates": [{"discovery_ref": "one"}], "exhausted": True},
            resolve=lambda *args: item, request=request(), profile=DEFAULT_PROFILE)
        self.assertEqual(result["status"], "needs_input")
        self.assertFalse(result["ai_fallback_eligible"])

    def test_resolver_readiness_error_and_missing_details_block_ai(self):
        for missing in ("portions", "steps", "ingredients"):
            with self.subTest(missing):
                item = candidate(1)
                item["recipe"][missing] = None if missing == "portions" else []
                try:
                    scale_recipe(item["recipe"], 2)
                except RecipeError as exc:
                    item["materialization_error"] = str(exc)
                result = collect_candidates(source_queries={"internal": [""]},
                    fetch_page=lambda *args: {"candidates": [{"discovery_ref": "one"}], "exhausted": True},
                    resolve=lambda *args: item, request=request(), profile=DEFAULT_PROFILE)
                self.assertEqual(result["status"], "needs_input")
                self.assertFalse(result["ai_fallback_eligible"])

    def test_unsearched_enabled_source_is_not_empty(self):
        result = collect_candidates(source_queries={"internal": []},
            fetch_page=lambda *args: self.fail("no query"), resolve=lambda *args: None,
            request=request(), profile=DEFAULT_PROFILE)
        self.assertEqual(result["sources"][0]["status"], "search_limit")
        self.assertEqual(result["sources"][0]["pages"], 0)
        self.assertFalse(result["ai_fallback_eligible"])

    def test_failed_saved_family_cannot_hide_unknown_web_readiness(self):
        saved = candidate(1, saved=True, source_id="same", error="archived")
        web = candidate(2, source_id="same")
        web["recipe"]["portions"] = None
        try:
            scale_recipe(web["recipe"], 2)
        except RecipeError as exc:
            web["materialization_error"] = str(exc)
        lookup = {item["reference_key"]: item for item in (saved, web)}
        result = collect_candidates(source_queries={"internal": [""]},
            fetch_page=lambda *args: {"candidates": [item["reference"] for item in (saved, web)], "exhausted": True},
            resolve=lambda summary, deadline: lookup[canonical(summary)], request=request(), profile=DEFAULT_PROFILE)
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(len(result["unknown"]), 1)
        self.assertEqual(result["sources"][0]["needs_input"], 1)
        self.assertFalse(result["ai_fallback_eligible"])

    def test_cursors_are_scoped_to_query_and_last_budget_page_can_exhaust(self):
        calls = []
        def page(source, query, cursor, limit, deadline):
            calls.append((query, cursor))
            return {"candidates": [{"discovery_ref": "one"}] if query == "second" and cursor == 2 else [],
                    "next_cursor": 2 if cursor is None else None, "exhausted": cursor is not None}
        result = collect_candidates(source_queries={"internal": ["first", "second", "third"]},
            fetch_page=page, resolve=lambda *args: candidate(1), request=request(), profile=DEFAULT_PROFILE)
        self.assertEqual(calls, [(query, cursor) for query in ("first", "second", "third") for cursor in (None, 2)])
        self.assertEqual(result["suitable_count"], 1)
        self.assertEqual(result["sources"][0]["status"], "ready")

    def test_provider_outage_does_not_erase_bank_and_repeated_cursor_is_bounded(self):
        def page(source, query, cursor, limit, deadline):
            if source == "store":
                raise OSError("offline")
            return {"candidates": [{"discovery_ref": "one"}], "next_cursor": "same"}
        result = collect_candidates(source_queries={"internal": [""], "store": ["middag"]},
            fetch_page=page, resolve=lambda *args: candidate(1), request=request(), profile=DEFAULT_PROFILE)
        self.assertEqual(result["suitable_count"], 1)
        self.assertEqual([s["status"] for s in result["sources"]], ["search_limit", "unavailable"])
        self.assertEqual(result["work"]["detail_calls"], 1)

    def test_fish_mass_counts_pantry_and_never_counts_mixed_product_mass(self):
        recipe = candidate(1)["recipe"]
        recipe["ingredients"][0].update(item="salmon", quantity=400, pantry=True)
        self.assertEqual(_listed_fish_mass(recipe), {"grams_per_serving": 200.0, "unknown": []})
        recipe["ingredients"][0]["item"] = "salmon sauce"
        self.assertEqual(_listed_fish_mass(recipe), {"grams_per_serving": 0.0, "unknown": ["salmon sauce"]})

    def test_wholegrain_preference_does_not_reward_potatoes(self):
        item = candidate(1)
        item["recipe"]["ingredients"][0]["item"] = "potatoes"
        prepared = prepare_candidate(item, DEFAULT_PROFILE, {})
        reasons = _preference_reasons(prepared, "2026-09-07", DEFAULT_PROFILE)
        reason = next(reason for reason in reasons if reason["code"] == "diet:prioritise" and reason["detail"]["preference"] == "whole grains")
        self.assertEqual(reason["weight"], 0)

    def test_same_title_and_ingredients_without_source_do_not_merge(self):
        one, two = candidate(1, saved=True), candidate(2, saved=True)
        for item in (one, two):
            item["recipe"]["source"] = {"kind": "user"}
            item["recipe"]["name"] = "Same title"
            item["recipe"]["ingredients"][0]["item"] = "carrot"
        two["recipe"]["steps"] = ["Different local method."]
        self.assertEqual(len(candidate_groups([one, two])), 2)

    def test_real_snapshot_details_feed_application_week_without_personal_saves(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), CONFIG)
            provider = NoProviderCalls()
            app = Application(store, provider, object())
            with store.locked() as state:
                state["setup"]["status"] = "complete"
            snapshots = [app.recipes.persist_discovery(candidate(i)["recipe"]) for i in range(1, 8)]
            summaries = [compact_candidate(item["recipe"], {"discovery_ref": item["discovery_ref"]}) for item in snapshots]
            effective = app._effective_planner_request({"week": "2026-W37"}, store.read(), anchor_current_date=True)
            result = collect_candidates(source_queries={"internal": [""]},
                fetch_page=lambda *args: {"candidates": summaries, "exhausted": True},
                resolve=lambda summary, deadline: app._resolve_planner_candidates({**effective, "candidates": [{"discovery_ref": summary["discovery_ref"]}]}, store.read())[0],
                request=effective, profile=store.read()["profile"])
            planned = app.handle({"operation": "menu", "action": "plan", "planner_input": {
                "week": "2026-W37", "candidates": [item["reference"] for item in result["candidates"]]}})["plan"]
            self.assertEqual(planned["status"], "planned")
            self.assertEqual(len(planned["selection"]["slots"]), 7)
            self.assertEqual(app.recipes.search("", limit=50), [])
            self.assertEqual(provider.calls, [])
            self.assertFalse(result["ai_fallback_eligible"])


class HistorySourceTests(unittest.TestCase):
    def test_frozen_document_retains_source_after_discovery_expiry(self):
        saved = candidate(1, saved=True, source_id="shared")
        bank_key = "bank:" + saved["reference"]["recipe_ref"]["id"]
        canonical_key = library_recipe_key({"library_id": "builtin", "recipe_id": saved["reference"]["recipe_ref"]["id"]})
        state = {"order_snapshots": {"old": {"menu_id": "old", "dishes": [{**saved["recipe"], "recipe_key": bank_key}]}},
                 "recipe_usage": {"old": {"recipe_keys": [canonical_key]}}}
        index = history_source_index(state, lambda ref: self.fail("frozen source requires no refetch"))
        self.assertEqual(index["status"], "complete")
        used = []
        def usage(key):
            used.append(key)
            return {"eligible": key != bank_key, "blocked_by": [{"week": "2026-W36"}] if key == bank_key else []}
        result = family_history_usage(candidate(2, source_id="shared"), index, usage)
        self.assertFalse(result["eligible"])
        self.assertEqual(set(used), {bank_key, "fixture:2"})
        self.assertEqual(result["blocked_by"], [{"week": "2026-W36"}])

    def test_exact_archived_bank_version_is_read_without_latest_lookup(self):
        saved = candidate(1, saved=True)
        calls = []
        state = {"recipe_usage": {"old": {"recipe_keys": ["fixture:1"],
                 "slots": [{"recipe_key": "fixture:1", "reference": saved["reference"]}]}}}
        def resolve(ref):
            calls.append(ref)
            return {**saved["recipe"], "status": "archived"}
        index = history_source_index(state, resolve)
        self.assertEqual(calls, [saved["reference"]])
        self.assertEqual(index["status"], "complete")
        self.assertEqual(index["work"]["exact_reads"], 1)

    def test_old_local_library_reference_is_exact_but_external_is_not_fetched(self):
        item = candidate(1, saved=True)
        calls = []
        references = [{"library_recipe_ref": {"library_id": source, "recipe_id": item["reference"]["recipe_ref"]["id"], "version": "3"}} for source in ("builtin", "old-library")]
        state = {"recipe_usage": {"old": {"recipe_keys": ["local", "external"],
                 "slots": [{"recipe_key": key, "reference": ref} for key, ref in zip(("local", "external"), references)]}}}
        def resolve(ref):
            calls.append(ref)
            return item["recipe"]
        index = history_source_index(state, resolve)
        self.assertEqual(calls, [{"recipe_ref": {"id": item["reference"]["recipe_ref"]["id"], "revision": 3}}])
        self.assertEqual(index["status"], "partial")

    def test_unresolved_legacy_history_is_not_rewarded_as_never_used(self):
        index = history_source_index({"recipe_usage": {"old": {"recipe_keys": ["bank:missing"]}}}, lambda ref: self.fail("no exact ref"))
        self.assertEqual(index["status"], "partial")
        item = candidate(1)
        item["usage"] = family_history_usage(item, index, lambda key: {"eligible": True})
        prepared = prepare_candidate(item, DEFAULT_PROFILE, {})
        reasons = _slot_reasons(prepared, "2026-09-07", 0, 1, DEFAULT_PROFILE)
        self.assertIn("recency:history_incomplete", [reason["code"] for reason in reasons])
        self.assertNotIn("recency:no_recorded_use", [reason["code"] for reason in reasons])

    def test_aggregate_history_work_is_bounded_and_reported(self):
        recipe = {**candidate(1)["recipe"], "recipe_key": "fixture:1"}
        index = history_source_index({"menu_planning": {"history": {str(i): {"dishes": [recipe]} for i in range(MAX_HISTORY_DOCUMENTS + 1)}}},
                                     lambda ref: self.fail("bounded frozen path"))
        self.assertEqual(index["status"], "history_work_limit")
        self.assertEqual(index["work"]["documents"], MAX_HISTORY_DOCUMENTS)
        item = candidate(1)
        item["usage"] = family_history_usage(item, index, lambda key: {"eligible": True})
        self.assertEqual(prepare_candidate(item, DEFAULT_PROFILE, {})["hard_constraints"]["status"], "unknown")

    def test_retired_alias_does_not_clear_an_active_alias(self):
        recipe = candidate(1)["recipe"]
        index = history_source_index({"menu": {"dishes": [{**recipe, "recipe_key": "retired"}, {**recipe, "recipe_key": "active"}]}},
                                     lambda ref: self.fail("frozen path"))
        calls = []
        def usage(key):
            calls.append(key)
            return {"eligible": key != "active", "blocked_by": [{"menu_id": "still-active"}] if key == "active" else []}
        result = family_history_usage(candidate(2, source_id="1"), index, usage)
        self.assertEqual(set(calls), {"fixture:2", "retired", "active"})
        self.assertFalse(result["eligible"])
        self.assertEqual(result["blocked_by"], [{"menu_id": "still-active"}])

    def test_newer_source_does_not_hide_older_exact_historical_version(self):
        newer, older = candidate(1, source_id="new"), candidate(1, source_id="old")
        ref = {"recipe_ref": {"id": "rec_" + "1" * 24, "revision": 1}}
        state = {"menu": {"dishes": [{**newer["recipe"], "recipe_key": "bank:one"}]},
                 "recipe_usage": {"old": {"recipe_keys": ["bank:one"], "slots": [{"recipe_key": "bank:one", "reference": ref}]}}}
        calls = []
        def resolve(reference):
            calls.append(reference)
            return older["recipe"]
        index = history_source_index(state, resolve)
        usage = family_history_usage(candidate(2, source_id="old"), index, lambda key: {"eligible": key != "bank:one"})
        self.assertEqual(calls, [ref])
        self.assertFalse(usage["eligible"])

    def test_history_alias_join_is_transitive(self):
        first, second = candidate(1), candidate(2)
        first["recipe"]["source"]["url"] = "https://example.org/shared"
        second["recipe"]["source"] = {"url": "https://example.org/shared"}
        index = history_source_index({"menu": {"dishes": [{**item["recipe"], "recipe_key": item["recipe_key"]} for item in (first, second)]}},
                                     lambda ref: self.fail("frozen path"))
        result = family_history_usage(candidate(3, source_id="1"), index, lambda key: {"eligible": key != "fixture:2"})
        self.assertFalse(result["eligible"])
        self.assertIn("fixture:2", result["family_recipe_keys"])

    def test_failed_candidate_cannot_hide_history_work_limit_for_ai(self):
        item = candidate(1, error="archived")
        item["usage"]["history_coverage"] = "history_work_limit"
        result = collect_candidates(source_queries={"internal": [""]},
            fetch_page=lambda *args: {"candidates": [{"discovery_ref": "one"}], "exhausted": True},
            resolve=lambda *args: item, request=request(), profile=DEFAULT_PROFILE)
        self.assertEqual(result["status"], "needs_input")
        self.assertFalse(result["ai_fallback_eligible"])

    def test_exact_frozen_slot_avoids_expired_discovery_read(self):
        item = candidate(1)
        recipe = {**item["recipe"], "recipe_key": item["recipe_key"]}
        slot = {"recipe_key": item["recipe_key"], "reference": item["reference"], "snapshot_digest": digest(recipe)}
        state = {"menu": {"dishes": [recipe], "slots": [slot]},
                 "recipe_usage": {"old": {"recipe_keys": [item["recipe_key"]], "slots": [slot]}}}
        index = history_source_index(state, lambda ref: self.fail("exact frozen slot must not refetch"))
        self.assertEqual(index["work"]["exact_reads"], 0)
        self.assertEqual(index["status"], "complete")

    def test_known_new_version_cannot_erase_old_unknown_source_evidence(self):
        unknown, known = candidate(1), candidate(1)
        unknown["recipe"]["source"] = {"kind": "user"}
        slots = [{"recipe_key": "bank:one", "reference": {"recipe_ref": {"id": "rec_" + "1" * 24, "revision": version}}} for version in (1, 2)]
        state = {"recipe_usage": {"old": {"recipe_keys": ["bank:one"], "slots": slots}}}
        index = history_source_index(state, lambda ref: (unknown if ref["recipe_ref"]["revision"] == 1 else known)["recipe"])
        self.assertEqual(index["status"], "partial")
        self.assertEqual(len(index["unresolved"]), 1)
        self.assertEqual(index["unresolved"][0]["reason"], "source_identity_unknown")

    def test_current_menu_does_not_certify_key_only_older_history(self):
        item = candidate(1)
        state = {"menu": {"menu_id": "new", "dishes": [{**item["recipe"], "recipe_key": item["recipe_key"]}]},
                 "recipe_usage": {"old": {"recipe_keys": [item["recipe_key"]]}}}
        index = history_source_index(state, lambda ref: self.fail("no exact reference"))
        self.assertEqual(index["status"], "partial")


class ApplicationSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name), {**CONFIG, "provider": "meny"})
        self.provider = NoProviderCalls()
        self.app = Application(self.store, self.provider, object())
        with self.store.locked() as state:
            state["setup"]["status"] = "complete"
            state["profile"]["recipes"]["sources"] = {key: key == "internal" for key in state["profile"]["recipes"]["sources"]}

    def test_legacy_bank_key_and_frozen_snapshot_preserve_case_distinct_ids(self):
        import recipes as storage
        from unittest import mock
        upper = candidate(1, source_id="ABC")["recipe"]
        lower = candidate(2, source_id="abc")["recipe"]
        modern = storage.source_key
        # Populate precisely the historical lookup format, then reopen current code.
        with mock.patch.object(storage, "source_key", side_effect=lambda recipe: modern(recipe).casefold()):
            saved = self.app.recipes.save(upper)
            snapshot = self.app.recipes.persist_discovery(upper)
        reopened = storage.RecipeStore(self.app.recipes.path, self.app.recipes.household)
        self.assertEqual(reopened.resolve_discovery(snapshot["discovery_ref"])["recipe"], upper)
        self.assertEqual(reopened.save(upper)["id"], saved["id"])
        self.assertNotEqual(reopened.save(lower)["id"], saved["id"])
        self.assertEqual(len(reopened.search()), 2)
        self.assertNotEqual(storage.recipe_key(upper), storage.recipe_key(lower))
        self.assertNotEqual(reopened.persist_discovery(lower)["discovery_ref"], snapshot["discovery_ref"])
        self.assertEqual(reopened.save_discovery(snapshot["discovery_ref"])["id"], saved["id"])

    def test_exact_conversion_cache_survives_restart_and_rejects_changed_binding(self):
        from recipes import RecipeStore, recipe_digest
        original = self.app.recipes.persist_discovery(normalize_recipe({**candidate(1)["recipe"], "schema_version": 2}))
        converted = deepcopy(original["recipe"])
        converted["steps"] = ["An explicit corrected instruction."]
        request_value = {"operation": "recipes", "action": "convert", "discovery_ref": original["discovery_ref"],
                         "recipe_digest": recipe_digest(original["recipe"]), "source_schema_version": original["recipe"]["schema_version"], "recipe": converted}
        result = self.app.handle(request_value)
        self.assertNotEqual(result["discovery_ref"], original["discovery_ref"])
        reopened = RecipeStore(self.app.recipes.path, self.app.recipes.household)
        cached = reopened.cached_discovery_transform(original["discovery_ref"], "conversion")
        self.assertEqual(cached["discovery_ref"], result["discovery_ref"])
        self.assertEqual(reopened.search(), [])
        self.assertEqual(reopened.resolve_discovery(original["discovery_ref"])["recipe"]["steps"], original["recipe"]["steps"])
        for changes in ({"recipe_digest": "0" * 64}, {"source_schema_version": 999}, {"recipe": {**converted, "schema_version": 1}}):
            with self.assertRaises(RecipeError):
                self.app.handle({**request_value, **changes})
        converted["source"]["external_id"] = "different"
        with self.assertRaises(RecipeError):
            self.app.handle(request_value)

    def test_auto_refills_after_ineligible_page_and_retains_saved_metadata(self):
        for index in range(21):
            saved = self.app.recipes.save(candidate(index + 1)["recipe"])
            if index == 0:
                first = saved
        # Newest twenty are archived revisions' draft replacements; active first
        # query still reaches the sole ready version without consuming draft quota.
        for row in self.app.recipes.search(limit=50):
            if row["id"] != first["id"]:
                self.app.recipes.update(row["id"], row["revision"], row, status="draft")
        self.app.handle({"operation": "recipes", "action": "set_favorite", "library_recipe_ref": first["library_recipe_ref"], "is_favorite": True, "idempotency_key": "favorite"})
        planned, resolved, _ = self.app._plan_menu({"week": "2026-W37", "dates": ["2026-09-07"]})
        self.assertEqual(planned["status"], "planned")
        self.assertTrue(resolved[0]["is_favorite"])
        self.assertEqual(resolved[0]["entry_origin"], "user")
        self.assertEqual(planned["discovery"]["shortfall"], 0)

    def test_known_non_dinners_do_not_fill_weekly_dinner_slots(self):
        for index, name in enumerate(("Chocolate cookies", "Mango smoothie", "Potatoes with butter", "Basic pizza crust")):
            item = candidate(index + 1)
            item["recipe"]["name"] = name
            checked = prepare_candidate(item, DEFAULT_PROFILE, {}, 2)
            self.assertIn("meal_role:non_dinner", [reason["code"] for reason in checked["hard_constraints"]["reasons"]])
        for index, name in enumerate(("Chicken with tomato sauce", "Fish cakes", "Crab cakes", "Chicken in bread sauce", "Bread soup", "Vegetable cakes", "Potato cakes", "Bread pizza"), 7):
            item = candidate(index)
            item["recipe"]["name"] = name
            saved = self.app.recipes.save(item["recipe"], idempotency_key=name)
            planned = self.app.handle({"operation": "menu", "action": "plan", "planner_input": {
                "week": "2026-W37", "dates": ["2026-09-07"], "candidates": [{"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}}]}})["plan"]
            self.assertEqual(planned["status"], "planned", name)


    def test_unknown_source_measure_stays_unready_through_saved_menu_and_products(self):
        from recipes import source_ingredient
        from product_planner import menu_requirements
        recipe = normalize_recipe({**candidate(1)["recipe"], "schema_version": 2})
        recipe["ingredients"] = [source_ingredient("2 ladles ris", item="ris", measure="2 ladles")]
        snapshot = self.app.recipes.persist_discovery(recipe)
        saved = self.app.handle({"operation": "recipes", "action": "save", "discovery_ref": snapshot["discovery_ref"], "idempotency_key": "unknown-unit"})["recipe"]
        menu = self.app.handle({"operation": "menu", "action": "save", "interactive": False,
            "menu": {"week": "2026-W37", "dishes": [{"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}, "portions": 4}], "salads": []}})["menu"]
        dish = menu["dishes"][0]
        self.assertFalse(dish["readiness"]["scaling_ready"])
        self.assertIn("ingredients.0.quantity", dish["readiness"]["missing_decisions"])
        self.assertEqual(dish["ingredients"][0]["original_text"], "2 ladles ris")
        self.assertIsNone(dish["ingredients"][0]["quantity"])
        self.assertEqual(dish["ingredients"][0]["unit"], "ladles")
        needs, unresolved = menu_requirements(menu)
        self.assertEqual(needs, [])
        self.assertEqual(len(unresolved), 1)
        product = self.app.handle({"operation": "products", "action": "prepare", "menu_ref": self.app._cart_menu_ref(menu)})
        self.assertEqual(product["product_plan"]["status"], "needs_input")
        self.assertEqual(self.provider.calls, [])

    def test_optional_unknown_garnish_does_not_block_required_scaling(self):
        from recipes import source_ingredient
        value = normalize_recipe({**candidate(1)["recipe"], "schema_version": 2})
        garnish = source_ingredient("parsley to taste", item="parsley", measure="to taste")
        garnish["optional"] = True
        value["ingredients"].append(garnish)
        scaled = scale_recipe(value, 4)
        self.assertTrue(scaled["readiness"]["scaling_ready"])
        self.assertIsNone(scaled["ingredients"][1]["quantity"])
        self.assertTrue(scaled["ingredients"][1]["optional"])

    def test_saved_source_rice_preserves_exact_two_and_four_portion_quantities(self):
        recipe = normalize_recipe({**candidate(1)["recipe"], "schema_version": 2})
        saved = self.app.recipes.save(recipe)
        for target, expected in ((2, 200), (4, 400)):
            menu = self.app._materialize_menu({"week": "2026-W37", "dishes": [{"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}, "portions": target}], "salads": []})
            self.assertEqual(menu["dishes"][0]["shopping_requirements"][0]["quantity"], {"numerator": expected, "denominator": 1})
            self.assertTrue(menu["dishes"][0]["readiness"]["scaling_ready"])

    def test_compact_retailer_failures_remain_source_statuses(self):
        from urllib.error import HTTPError
        with self.store.locked() as state:
            state["profile"]["recipes"]["sources"]["meny"] = True
        for error, status in ((TimeoutError(), "timeout"), (RecipeError("unavailable"), "unavailable"),
                              (HTTPError("https://meny.no", 429, "rate limited", {}, None), "rate_limited")):
            def call(*args, **kwargs):
                raise error
            self.provider.call = call
            result = self.app.handle({"operation": "recipes", "action": "discover", "projection": "summary", "source": "meny", "query": "dinner"})
            self.assertEqual(result["sources"][0]["status"], status)
            self.assertEqual(result["recipes"], [])

    def test_actual_meny_detail_is_reused_after_restart_without_personal_save(self):
        from unittest import mock
        from meny import MenyClient
        from test_meal_concierge_retailer_recipes import page, browser_evaluate, RECIPE_PATH, RECIPE_URL
        document = page()
        raw = json.loads(document["scripts"][0])
        raw["recipeIngredient"] = ["200 g ris"]
        document["scripts"] = [json.dumps(raw)]
        reader = MenyClient(instance="synthetic", binary="unused", executable="unused", profile="unused",
                            home="unused", socket_directory="unused", uid=1000, gid=1000)
        reader._require_login = mock.Mock()
        reader._assert_authenticated = mock.Mock()
        reader._open = mock.Mock()
        reader._eval = lambda script: browser_evaluate(script, document)
        calls = []
        def call(tool, args, **kwargs):
            calls.append(tool)
            if tool == "recipe_search":
                return {"provider": "meny", "query": args["query"], "recipes": [{"recipe_id": RECIPE_PATH,
                        "recipe_url": RECIPE_URL, "name": "Synthetic rice", "summary": "Synthetic recipe"}]}
            if tool == "recipe_detail":
                return reader.call(tool, args, **kwargs)
            self.fail("Unexpected provider effect: " + tool)
        self.provider.call = call
        with self.store.locked() as state:
            state["profile"]["recipes"]["sources"]["meny"] = True
        request_value = {"operation": "menu", "action": "plan", "planner_input": {"week": "2026-W37", "dates": ["2026-09-07"], "portions": 2}}
        first = self.app.handle(request_value)["plan"]
        self.assertEqual(first["status"], "planned", first)
        self.assertEqual(calls.count("recipe_detail"), 1)
        second = Application(self.store, self.provider, object()).handle(request_value)["plan"]
        self.assertEqual(second["selection_digest"], first["selection_digest"])
        self.assertEqual(calls.count("recipe_detail"), 1)
        self.assertEqual(self.app.recipes.search(), [])
        self.assertFalse(first["discovery"]["ai_fallback_eligible"])
        exact = first["selection"]["slots"][0]["reference"]["discovery_ref"]
        full = self.app.recipes.resolve_discovery(exact)["recipe"]
        self.assertEqual(full["source_provider"], "meny")
        self.assertEqual(scale_recipe(full, 2)["ingredients"][0]["quantity"], {"numerator": 100, "denominator": 1})
        from recipes import recipe_digest, ESTIMATE_CONFIRMATION
        changed = deepcopy(full)
        changed["portions"] = 6
        changed["portions_evidence"] = {"basis": "estimate", "input": "synthetic conversion", "assumptions": "Six smaller synthetic servings."}
        conversion = self.app.handle({"operation": "recipes", "action": "convert", "discovery_ref": exact,
            "recipe_digest": recipe_digest(full), "source_schema_version": 2, "recipe": changed})
        pending = self.app.handle(request_value)["plan"]
        self.assertNotEqual(pending["status"], "planned")
        self.assertFalse(pending["discovery"]["ai_fallback_eligible"])
        accepted = self.app.handle({"operation": "recipes", "action": "accept_estimates",
            "discovery_ref": conversion["discovery_ref"], "recipe_digest": conversion["recipe_digest"],
            "estimate_fields": ["portions"], "confirmation_statement": ESTIMATE_CONFIRMATION})
        final = Application(self.store, self.provider, object()).handle(request_value)["plan"]
        self.assertEqual(final["status"], "planned")
        self.assertEqual(final["selection"]["slots"][0]["reference"]["discovery_ref"], accepted["discovery_ref"])
        self.assertEqual(calls.count("recipe_detail"), 1)
        self.assertEqual(self.app.recipes.search(), [])


if __name__ == "__main__":
    unittest.main()
