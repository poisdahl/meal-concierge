from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from core import HouseholdError, StateStore
import planner
from planner import MAX_EXPLORED_STATES, MAX_HISTORY_RECORDS, PlannerError
from service import Application, Server


CONFIG = {
    "instance": "planner-test",
    "household": "Planner Test",
    "provider": "oda",
    "email_automation_profile": "test-email",
    "profile_overrides": {},
}


class NoProviderCalls:
    def __init__(self):
        self.calls = []

    def probe(self):
        return {
            "protocol_version": "2025-11-25",
            "server": {"name": "fixture", "version": "1"},
            "tool_count": 0,
        }

    def call(self, tool, arguments, **kwargs):
        self.calls.append((tool, arguments, kwargs))
        raise AssertionError("the planner must not call a provider")


def recipe(
    name: str, identity: str, *, ingredient: str = "gulrot", unit: str = "g",
    active_minutes: int | None = 30,
) -> dict:
    value = {
        "name": name,
        "language": "nb-NO",
        "portions": 2,
        "ingredients": [{
            "raw": f"200 {unit} {ingredient}",
            "quantity": 200,
            "unit": unit,
            "item": ingredient,
            "scalable": True,
        }],
        "steps": ["Tilbered."],
        "tags": [],
        "source": {
            "kind": "user",
            "publisher": "Fixture",
            "title": name,
            "external_id": identity,
            "relationship": "user_supplied",
        },
        "rights": {"storage": "full", "credit": "Fixture"},
    }
    if active_minutes is not None:
        value["times"] = {"active_minutes": active_minutes}
    return value


def explicit_facts(
    *, active_minutes: int | None = None, dietary: list[str] | None = None,
    complete: bool = False, vegetables: list[str] | None = None,
    perishability: str | None = None, variety: list[str] | None = None,
) -> dict:
    result = {}
    if active_minutes is not None:
        result["active_minutes"] = {"source": "explicit", "value": active_minutes}
    if dietary is not None:
        result["dietary_facets"] = {
            "source": "explicit",
            "values": dietary,
            "complete": complete,
            "vegetable_types": vegetables or [],
        }
    if perishability is not None:
        result["perishability"] = {"source": "explicit", "value": perishability}
    if variety is not None:
        result["variety_facets"] = {"source": "explicit", "values": variety}
    return result


class WeeklyPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name), CONFIG)
        self.provider = NoProviderCalls()
        self.app = Application(self.store, self.provider, object())
        with self.store.locked() as state:
            state["setup"]["status"] = "complete"
            for target in planner.SAVED_MINIMUM_TARGETS:
                state["profile"]["diet"][target] = 0

    def tearDown(self):
        self.temp.cleanup()

    def save_candidates(self, count: int, *, ingredient: str = "gulrot") -> list[dict]:
        result = []
        for index in range(count):
            saved = self.app.handle({
                "operation": "recipes",
                "action": "save",
                "recipe": recipe(f"Recipe {index}", f"recipe-{index}", ingredient=ingredient),
                "idempotency_key": f"save-{index}",
            })["recipe"]
            result.append({"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}})
        return result

    @staticmethod
    def request(candidates: list[dict], *, dates: list[str] | None = None, **changes) -> dict:
        return {
            "week": "2026-W37",
            "dates": dates or ["2026-09-07"],
            "candidates": candidates,
            **changes,
        }

    def plan(self, planner_input: dict) -> dict:
        return self.app.handle({
            "operation": "menu", "action": "plan", "planner_input": planner_input,
        })["plan"]

    def socket_call(self, request: dict) -> dict:
        class Connection:
            def __init__(self, payload):
                self.payload = payload
                self.sent = b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def recv(self, _size):
                payload, self.payload = self.payload, b""
                return payload

            def sendall(self, value):
                self.sent += value

        connection = Connection((json.dumps(request) + "\n").encode())
        server = Server(
            Path(self.temp.name) / "service.sock", os.getgid(), os.getuid(), self.app
        )
        with mock.patch("service.peer_uid", return_value=os.getuid()):
            server._serve(connection)
        return json.loads(connection.sent)

    def test_order_restart_ties_and_repeated_input_are_byte_stable(self):
        candidates = self.save_candidates(3)
        request = self.request(
            candidates, dates=["2026-09-07", "2026-09-08"], alternatives=3,
        )
        first = self.plan(request)
        reordered = self.plan({**request, "candidates": list(reversed(candidates))})
        restarted = Application(self.store, NoProviderCalls(), object())
        third = restarted.handle({
            "operation": "menu", "action": "plan", "planner_input": request,
        })["plan"]
        encoded = lambda value: json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertEqual(encoded(first), encoded(reordered))
        self.assertEqual(encoded(first), encoded(third))
        self.assertEqual(first["status"], "planned")
        self.assertEqual(len(first["selections"]), 3)
        for selection in first["selections"]:
            self.assertEqual(
                selection["total_score"],
                sum(slot["score"] for slot in selection["slots"])
                + sum(reason["weight"] for reason in selection["plan_reason_contributions"]),
            )
            for slot in selection["slots"]:
                self.assertEqual(
                    slot["score"], sum(reason["weight"] for reason in slot["reason_contributions"])
                )

    def test_maximum_week_materializes_only_returned_alternatives(self):
        candidates = self.save_candidates(8)
        request = self.request(
            candidates,
            dates=[f"2026-09-{day:02}" for day in range(7, 14)],
            alternatives=3,
        )
        original = planner._selection
        materializations = 0

        def bounded_materialization(*args, **kwargs):
            nonlocal materializations
            materializations += 1
            if materializations > request["alternatives"]:
                raise AssertionError("planner materialized a discarded permutation")
            return original(*args, **kwargs)

        with mock.patch("planner._selection", side_effect=bounded_materialization):
            result = self.plan(request)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["explored_states"], 40_320)
        self.assertEqual(len(result["selections"]), 3)
        self.assertEqual(materializations, 3)

    def test_full_week_saved_minimums_are_automatic_hard_constraints(self):
        candidates = self.save_candidates(7)
        with self.store.locked() as state:
            state["profile"]["diet"].update({
                "minimum_fish_portions": 1,
                "minimum_legume_dinners": 0,
                "minimum_wholegrain_or_potato_dinners": 0,
                "minimum_vegetable_types": 0,
            })
        request = self.request(candidates, dates=[f"2026-09-{day:02}" for day in range(7, 14)])
        result = self.plan(request)
        self.assertEqual(result["status"], "needs_input")
        self.assertIn("minimum_fish_portions", result["request"]["strict_targets"])
        fish = deepcopy(request)
        fish["candidates"][0]["facts"] = explicit_facts(dietary=["fish"], complete=False)
        planned = self.plan(fish)
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned["selection"]["strict_targets"]["status"], "pass")
        saved = self.app.handle({
            "operation": "menu", "action": "save", "planner_ref": planned["save_ref"],
        })["menu"]
        menu_ref = {key: saved[key] for key in ("menu_id", "revision", "digest")}
        decisions = [{
            "source": {"collection": "dishes", "recipe_index": index, "ingredient_index": 0},
            "action": "have_all",
        } for index in range(7)]
        products = self.app.handle({
            "operation": "products", "action": "prepare", "menu_ref": menu_ref,
            "ingredient_decisions": decisions,
        })["product_plan"]
        self.assertEqual(products["status"], "prepared")
        self.assertEqual(products["requirements"], [])

    def test_explicit_lunch_slots_never_count_as_legacy_dinners(self):
        facts = {
            "values": ["legume"], "vegetable_types": [], "complete": True,
        }
        menu = {
            "dishes": [{"recipe_key": "recipe:lunch"}], "salads": [],
            "slots": [{"recipe_key": "recipe:lunch", "meal_type": "lunch"}],
            "planner_selection": {"selection": {"slots": [{
                "recipe_key": "recipe:lunch", "dietary_facets": facts,
            }]}},
        }
        profile = {
            "meals": {"dinner_days": 1},
            "diet": {"minimum_legume_dinners": 1},
        }
        evaluation = planner.saved_menu_minimum_evaluation(menu, profile)
        self.assertFalse(evaluation["complete_menu"])
        self.assertEqual(evaluation["status"], "unknown")
        legacy = deepcopy(menu)
        legacy.pop("slots")
        self.assertEqual(
            planner.saved_menu_minimum_evaluation(legacy, profile)["status"], "pass",
        )

    def test_vegetable_minimum_collapses_spelling_and_fresh_variants(self):
        candidates = []
        for index, ingredient in enumerate((
            "brokkoli", "fersk brokkoli", "gulrot", "carrots",
            "rødløk", "fersk rødløk", "vårløk",
        )):
            saved = self.app.handle({
                "operation": "recipes", "action": "save",
                "recipe": recipe(f"Recipe {index}", f"vegetable-{index}", ingredient=ingredient),
                "idempotency_key": f"vegetable-{index}",
            })["recipe"]
            candidates.append({"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}})
        with self.store.locked() as state:
            state["profile"]["diet"]["minimum_vegetable_types"] = 5
        result = self.plan(self.request(
            candidates, dates=[f"2026-09-{day:02}" for day in range(7, 14)],
        ))
        self.assertEqual(result["status"], "needs_input")
        issue = next(item for item in result["issues"] if item["target"] == "minimum_vegetable_types")
        self.assertEqual(issue["detail"]["observed"], ["brokkoli", "gulrot", "løk"])

    def test_complete_legacy_week_that_misses_saved_minimum_is_rejected(self):
        candidates = self.save_candidates(7)
        with self.store.locked() as state:
            state["profile"]["diet"].update({
                "minimum_fish_portions": 1,
                "minimum_legume_dinners": 0,
                "minimum_wholegrain_or_potato_dinners": 0,
                "minimum_vegetable_types": 0,
            })
        with self.assertRaisesRegex(PlannerError, "minimum_fish_portions"):
            self.app.handle({
                "operation": "menu", "action": "save",
                "menu": {"week": "2026-W37", "dishes": candidates, "salads": []},
            })

    def test_plan_score_preserves_selected_order(self):
        candidates = self.save_candidates(2)
        request = self.request(
            candidates, dates=["2026-09-07", "2026-09-08"], alternatives=1,
        )

        def order_sensitive_reasons(selected, _profile):
            reference_keys = [item["reference_key"] for item in selected]
            return [{
                "code": "test:order_sensitive",
                "weight": 100 if reference_keys[0] == max(reference_keys) else 0,
                "detail": reference_keys,
            }]

        with mock.patch("planner._plan_reasons", side_effect=order_sensitive_reasons):
            result = self.plan(request)
        tie_break = result["selection"]["tie_break"]
        self.assertEqual(tie_break[0], max(tie_break))
        self.assertEqual(result["selection"]["total_score"], 100 + sum(
            slot["score"] for slot in result["selection"]["slots"]
        ))

    def test_missing_safety_metadata_is_advisory_without_accepting_caller_clearance(self):
        candidate = self.save_candidates(1)[0]
        with self.store.locked() as state:
            state["profile"]["diet"]["allergies_or_sensitivities"] = ["Milk"]
        unknown = self.plan(self.request([candidate]))
        self.assertEqual(unknown["status"], "planned")
        self.assertEqual(
            unknown["candidate_evaluations"][0]["hard_constraints"]["status"], "pass"
        )
        untrusted_clearance = {
            **candidate,
            "facts": {"safety": {
                "source": "explicit",
                "allergies_or_sensitivities": {"milk": "free"},
                "avoid": {},
            }},
        }
        with self.assertRaisesRegex(PlannerError, "candidate facts have unknown fields"):
            self.plan(self.request([untrusted_clearance]))

    def test_unknown_soft_facts_are_named_and_strict_time_blocks(self):
        saved = self.app.handle({
            "operation": "recipes", "action": "save",
            "recipe": recipe("Unknown time", "unknown-time", active_minutes=None),
            "idempotency_key": "unknown-time",
        })["recipe"]
        candidate = {"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}}
        normal = self.plan(self.request([candidate]))
        self.assertEqual(normal["status"], "planned")
        self.assertIn("active_minutes", normal["selection"]["soft_relaxations"])
        strict = self.plan(self.request([candidate], strict_targets=["active_minutes"]))
        self.assertEqual(strict["status"], "needs_input")
        too_slow = {**candidate, "facts": explicit_facts(active_minutes=61)}
        self.assertEqual(
            self.plan(self.request([too_slow], strict_targets=["active_minutes"]))["status"],
            "no_plan",
        )
        known = {**candidate, "facts": explicit_facts(active_minutes=30)}
        self.assertEqual(
            self.plan(self.request([known], strict_targets=["active_minutes"]))["status"],
            "planned",
        )

    def test_strict_dietary_unknown_infeasible_and_satisfied_are_distinct(self):
        candidate = self.save_candidates(1)[0]
        target = ["minimum_legume_dinners"]
        with self.store.locked() as state:
            state["profile"]["diet"]["minimum_legume_dinners"] = 1
        incomplete = {
            **candidate, "facts": explicit_facts(dietary=[], complete=False),
        }
        self.assertEqual(
            self.plan(self.request([incomplete], strict_targets=target))["status"],
            "needs_input",
        )
        with self.store.locked() as state:
            state["profile"]["diet"]["minimum_legume_dinners"] = 2
        impossible = self.plan(self.request([incomplete], strict_targets=target))
        self.assertEqual(impossible["status"], "no_plan")
        with self.store.locked() as state:
            state["profile"]["diet"]["minimum_legume_dinners"] = 1
        complete = {**candidate, "facts": explicit_facts(dietary=[], complete=True)}
        self.assertEqual(
            self.plan(self.request([complete], strict_targets=target))["status"],
            "no_plan",
        )
        legume = {
            **candidate,
            "facts": explicit_facts(dietary=["legume"], complete=True),
        }
        self.assertEqual(
            self.plan(self.request([legume], strict_targets=target))["status"],
            "planned",
        )

    def test_link_only_and_malformed_references_never_enter_a_plan(self):
        link = self.app.handle({
            "operation": "recipes", "action": "save", "idempotency_key": "link",
            "recipe": {
                "name": "Provider link", "ingredients": [], "steps": [],
                "source": {
                    "kind": "oda", "publisher": "oda.com", "title": "Provider link",
                    "url": "https://oda.com/no/products/1", "external_id": "1",
                    "relationship": "original",
                },
                "rights": {"storage": "link_only", "credit": "Oda"},
            },
        })["recipe"]
        reference = {"recipe_ref": {"id": link["id"], "revision": link["revision"]}}
        result = self.plan(self.request([reference]))
        self.assertEqual(result["status"], "no_plan")
        self.assertEqual(
            result["candidate_evaluations"][0]["hard_constraints"]["reasons"][0]["code"],
            "not_materializable",
        )
        for invalid in ({"name": "guess"}, {"recipe_ref": {"id": link["id"]}}, {"url": "https://example.test"}):
            with self.subTest(invalid=invalid), self.assertRaises(PlannerError):
                self.plan(self.request([invalid]))
        with self.assertRaisesRegex(PlannerError, "exact id"):
            self.plan(self.request([{
                "recipe_ref": {"id": f" {link['id']} ", "revision": link["revision"]},
            }]))
        snapshot = self.app.recipes.persist_discovery(recipe("Exact", "exact-ref"))
        with self.assertRaisesRegex(PlannerError, "bounded exact text"):
            self.plan(self.request([{
                "discovery_ref": f" {snapshot['discovery_ref']} ",
            }]))

    def test_cooldown_requires_exact_current_override_and_save_preserves_it(self):
        candidate = self.save_candidates(1)[0]
        stored = self.app.recipes.get(candidate["recipe_ref"]["id"])
        key = stored["recipe_key"]
        with self.store.locked() as state:
            state["recipe_usage"]["old"] = {
                "week": "2026-W36", "status": "cooked", "recipe_keys": [key],
                "cooked_keys": [key], "not_cooked_keys": [], "cooldown_overrides": {},
                "order_id": None,
            }
        self.assertEqual(self.plan(self.request([candidate]))["status"], "no_plan")
        with self.assertRaisesRegex(PlannerError, "exact candidate"):
            self.plan(self.request([candidate], cooldown_overrides={"wrong": "requested"}))
        plan = self.plan(self.request(
            [candidate], cooldown_overrides={key: "User explicitly requested this repeat"},
        ))
        saved = self.app.handle({
            "operation": "menu", "action": "save", "planner_handoff": plan["save_handoff"],
        })["menu"]
        self.assertEqual(
            self.store.read()["recipe_usage"][saved["menu_id"]]["cooldown_overrides"],
            {key: "User explicitly requested this repeat"},
        )

    def test_planner_save_freezes_exact_selection_and_is_idempotent(self):
        candidates = self.save_candidates(2)
        plan = self.plan(self.request(candidates))
        handoff = plan["save_handoff"]
        saved = self.app.handle({
            "operation": "menu", "action": "save", "planner_handoff": handoff,
        })["menu"]
        self.assertEqual(saved["planner_selection"]["input_digest"], plan["input_digest"])
        self.assertEqual(saved["planner_selection"]["selection_digest"], plan["selection_digest"])
        self.assertEqual(saved["schedule"][0]["reference"], plan["selection"]["slots"][0]["reference"])
        repeated = self.app.handle({
            "operation": "menu", "action": "save", "planner_handoff": handoff,
        })
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["menu"], saved)
        self.assertEqual(self.provider.calls, [])

    def test_replacing_active_menu_requires_exact_current_reference(self):
        candidates = self.save_candidates(2)
        first_plan = self.plan(self.request([candidates[0]]))
        first = self.app.handle({
            "operation": "menu", "action": "save",
            "planner_ref": first_plan["save_ref"],
        })["menu"]
        second_plan = self.plan(self.request([candidates[1]]))
        with self.assertRaisesRegex(HouseholdError, "exact menu_ref"):
            self.app.handle({
                "operation": "menu", "action": "save",
                "planner_ref": second_plan["save_ref"],
            })
        updated = self.app.handle({
            "operation": "menu", "action": "save",
            "planner_ref": second_plan["save_ref"],
            "menu_ref": {key: first[key] for key in ("menu_id", "revision", "digest")},
        })["menu"]
        self.assertEqual(updated["menu_id"], first["menu_id"])
        self.assertEqual(updated["revision"], first["revision"] + 1)

    def test_compact_ref_saves_complete_alternative_and_retries_after_restart(self):
        plan = self.plan(self.request(self.save_candidates(3), alternatives=3))
        choice = plan["alternatives"][-1]
        response = self.socket_call({
            "operation": "menu", "action": "save", "planner_ref": choice["save_ref"],
        })
        self.assertTrue(response["ok"], response)
        saved = response["result"]["menu"]
        self.assertEqual(saved["planner_selection"], plan["save_handoffs"][-1])
        restarted = Application(self.store, self.provider, object())
        for field, value in (("planner_ref", choice["save_ref"]),
                             ("planner_handoff", plan["save_handoffs"][-1])):
            repeated = restarted.handle({"operation": "menu", "action": "save", field: value})
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(repeated["menu"], saved)
        self.assertEqual(self.provider.calls, [])

    def test_compact_ref_rejects_tampering_and_mixed_forms_without_writes(self):
        plan = self.plan(self.request(self.save_candidates(2)))
        ref = plan["save_ref"]
        invalid = [{key: value for key, value in ref.items() if key != "request"},
                   [], {**ref, "selection": plan["selection"]},
                   {**ref, "selected_slot_ids": []},
                   {**ref, "planner_version": "unknown"}]
        for field in ("input_digest", "selection_digest"):
            invalid.extend([{**ref, field: "a" * 64}, {**ref, field: False}])
        for change in ({"portions": 9}, {"candidates": None},
                       {"candidates": []}, {"as_of_date": "2026-09-01"}):
            invalid.append({**ref, "request": {**ref["request"], **change}})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(PlannerError):
                self.app.handle({"operation": "menu", "action": "save", "planner_ref": value})
        for extra in ({"planner_handoff": plan["save_handoff"]}, {"menu": {}},
                      {"allow_repeat_keys": ["invented"]}, {"override_reason": "invented"},
                      {"action": "plan"}):
            with self.subTest(extra=extra), self.assertRaises(PlannerError):
                self.app.handle({"operation": "menu", "action": "save", "planner_ref": ref, **extra})
        self.assertIsNone(self.store.read()["menu"])
        self.assertEqual(self.provider.calls, [])

    def test_compact_ref_rejects_profile_history_and_recipe_drift(self):
        candidates = self.save_candidates(2)
        plan = self.plan(self.request(candidates))
        with self.store.locked() as state:
            state["profile"]["cuisine"]["wanted"] = ["Changed"]
        with self.assertRaisesRegex(PlannerError, "stale"):
            self.app.handle({"operation": "menu", "action": "save", "planner_ref": plan["save_ref"]})
        plan = self.plan(self.request(candidates))
        with self.store.locked() as state:
            state["recipe_usage"]["other"] = {
                "week": "2026-W36", "status": "cooked", "recipe_keys": [plan["selection"]["slots"][0]["recipe_key"]],
                "cooked_keys": [plan["selection"]["slots"][0]["recipe_key"]],
                "not_cooked_keys": [], "cooldown_overrides": {}, "order_id": None,
            }
        with self.assertRaisesRegex(PlannerError, "stale"):
            self.app.handle({"operation": "menu", "action": "save", "planner_ref": plan["save_ref"]})
        with self.store.locked() as state:
            state["recipe_usage"].clear()
        plan = self.plan(self.request(candidates))
        exact = plan["selection"]["slots"][0]["reference"]["recipe_ref"]
        self.app.recipes.archive(exact["id"], exact["revision"])
        with self.assertRaises(PlannerError):
            self.app.handle({"operation": "menu", "action": "save", "planner_ref": plan["save_ref"]})
        self.assertIsNone(self.store.read()["menu"])

    def test_compact_ref_rechecks_profile_race_before_commit(self):
        plan = self.plan(self.request(self.save_candidates(2)))
        original = self.app._materialize_planner_menu

        def materialize_then_change_profile(handoff, resolved):
            menu = original(handoff, resolved)
            with self.store.locked() as state:
                state["profile"]["cuisine"]["wanted"] = ["Changed concurrently"]
            return menu

        with mock.patch.object(self.app, "_materialize_planner_menu", side_effect=materialize_then_change_profile):
            with self.assertRaisesRegex(PlannerError, "became stale before save"):
                self.app.handle({"operation": "menu", "action": "save", "planner_ref": plan["save_ref"]})
        self.assertIsNone(self.store.read()["menu"])

    def test_handoff_remains_anchored_when_save_crosses_household_midnight(self):
        candidate = self.save_candidates(1)[0]
        before_midnight = datetime(2026, 9, 7, 21, 59, tzinfo=timezone.utc)
        after_midnight = datetime(2026, 9, 7, 22, 1, tzinfo=timezone.utc)
        with mock.patch("service.now", return_value=before_midnight):
            plan = self.plan(self.request([candidate]))
        self.assertEqual(plan["request"]["as_of_date"], "2026-09-07")
        with mock.patch("service.now", return_value=after_midnight):
            saved = self.app.handle({
                "operation": "menu", "action": "save",
                "planner_ref": plan["save_ref"],
            })["menu"]
        self.assertEqual(
            saved["planner_selection"]["request"]["as_of_date"], "2026-09-07"
        )

    def test_socket_runtime_path_plans_and_saves_the_returned_handoff(self):
        candidate = self.save_candidates(1)[0]
        planned = self.socket_call({
            "operation": "menu", "action": "plan",
            "planner_input": self.request([candidate]),
            "allow_repeat_keys": [], "interactive": True,
        })
        self.assertTrue(planned["ok"])
        handoff = planned["result"]["plan"]["save_handoff"]
        saved = self.socket_call({
            "operation": "menu", "action": "save", "planner_handoff": handoff,
            "allow_repeat_keys": [], "interactive": True,
        })
        self.assertTrue(saved["ok"])
        self.assertEqual(
            saved["result"]["menu"]["planner_selection"]["selection_digest"],
            handoff["selection_digest"],
        )
        self.assertEqual(self.provider.calls, [])

    def test_profile_or_handoff_drift_fails_before_state_mutation(self):
        plan = self.plan(self.request(self.save_candidates(2)))
        tampered = deepcopy(plan["save_handoff"])
        tampered["selection"]["slots"][0]["reason_contributions"][0]["weight"] += 1
        with self.assertRaisesRegex(PlannerError, "stale, changed or fabricated"):
            self.app.handle({
                "operation": "menu", "action": "save", "planner_handoff": tampered,
            })
        self.assertIsNone(self.store.read()["menu"])
        with self.store.locked() as state:
            state["profile"]["cuisine"]["wanted"] = ["Thai"]
        with self.assertRaisesRegex(PlannerError, "stale, changed or fabricated"):
            self.app.handle({
                "operation": "menu", "action": "save", "planner_handoff": plan["save_handoff"],
            })
        self.assertIsNone(self.store.read()["menu"])

    def test_profile_race_between_verification_and_commit_is_rechecked(self):
        plan = self.plan(self.request(self.save_candidates(2)))
        original = self.app._materialize_planner_menu

        def materialize_then_change_profile(handoff, resolved):
            menu = original(handoff, resolved)
            with self.store.locked() as state:
                state["profile"]["cuisine"]["wanted"] = ["Changed concurrently"]
            return menu

        with mock.patch.object(
            self.app, "_materialize_planner_menu", side_effect=materialize_then_change_profile
        ), self.assertRaisesRegex(PlannerError, "became stale before save"):
            self.app.handle({
                "operation": "menu", "action": "save",
                "planner_handoff": plan["save_handoff"],
            })
        self.assertIsNone(self.store.read()["menu"])

    def test_cross_instance_recipe_archive_waits_until_planner_save_commits(self):
        candidate = self.save_candidates(1)[0]
        plan = self.plan(self.request([candidate]))
        other_app = Application(self.store, NoProviderCalls(), object())
        entered = threading.Event()
        release = threading.Event()
        original = self.app._materialize_planner_menu
        materialize_calls = 0

        def pause_first_materialization(handoff, resolved):
            nonlocal materialize_calls
            menu = original(handoff, resolved)
            materialize_calls += 1
            if materialize_calls == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            return menu

        save_result = {}
        archive_result = {}
        failures = []

        def save():
            try:
                save_result.update(self.app.handle({
                    "operation": "menu", "action": "save",
                    "planner_ref": plan["save_ref"],
                }))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        def archive():
            try:
                archive_result.update(other_app.handle({
                    "operation": "recipes", "action": "archive",
                    "recipe_id": candidate["recipe_ref"]["id"],
                    "expected_revision": candidate["recipe_ref"]["revision"],
                    "idempotency_key": "archive-after-planner-save",
                }))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        with mock.patch.object(
            self.app, "_materialize_planner_menu", side_effect=pause_first_materialization
        ):
            save_thread = threading.Thread(target=save)
            archive_thread = threading.Thread(target=archive)
            save_thread.start()
            self.assertTrue(entered.wait(2))
            archive_thread.start()
            archive_thread.join(0.05)
            self.assertTrue(archive_thread.is_alive())
            release.set()
            save_thread.join(2)
            archive_thread.join(2)
        self.assertFalse(save_thread.is_alive())
        self.assertFalse(archive_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertIn("menu", save_result)
        self.assertEqual(archive_result["recipe"]["status"], "archived")

    def test_discovery_is_local_exact_expires_and_never_calls_provider(self):
        snapshot = self.app.recipes.persist_discovery(recipe(
            "Discovered", "discovered", ingredient="linser"
        ))
        candidate = {"discovery_ref": snapshot["discovery_ref"]}
        plan = self.plan(self.request([candidate]))
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(self.provider.calls, [])
        connection = sqlite3.connect(self.app.recipes.path)
        try:
            connection.execute(
                "UPDATE discovery_snapshots SET expires_at='2000-01-01T00:00:00+00:00'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(Exception, "discovery reference was not found"):
            self.app.handle({
                "operation": "menu", "action": "save",
                "planner_handoff": plan["save_handoff"],
            })
        self.assertIsNone(self.store.read()["menu"])
        self.assertEqual(self.provider.calls, [])

    def test_fresh_is_placed_early_and_reuse_is_capped_by_monotony(self):
        candidates = self.save_candidates(3, ingredient="potet")
        candidates[0]["facts"] = explicit_facts(
            perishability="fresh", variety=["same"]
        )
        candidates[1]["facts"] = explicit_facts(
            perishability="shelf_stable", variety=["same"]
        )
        candidates[2]["facts"] = explicit_facts(
            perishability="unknown", variety=["same"]
        )
        plan = self.plan(self.request(
            candidates,
            dates=["2026-09-07", "2026-09-08", "2026-09-09"],
        ))
        slots = plan["selection"]["slots"]
        fresh_ref = json.dumps(candidates[0]["recipe_ref"], sort_keys=True)
        self.assertEqual(
            next(slot["date"] for slot in slots if json.dumps(slot["reference"]["recipe_ref"], sort_keys=True) == fresh_ref),
            "2026-09-07",
        )
        reasons = {item["code"]: item for item in plan["selection"]["plan_reason_contributions"]}
        self.assertLessEqual(reasons["ingredients:exact_reuse"]["weight"], 16)
        self.assertLess(reasons["variety:monotony"]["weight"], 0)
        self.assertLess(reasons["ingredients:monotony"]["weight"], 0)

    def test_ingredient_reuse_counts_meals_not_duplicate_recipe_rows(self):
        value = recipe("Duplicate row", "duplicate-row", ingredient="fløte")
        value["ingredients"].append(deepcopy(value["ingredients"][0]))
        saved = self.app.handle({
            "operation": "recipes", "action": "save", "recipe": value,
            "idempotency_key": "duplicate-row",
        })["recipe"]
        plan = self.plan(self.request([{
            "recipe_ref": {"id": saved["id"], "revision": saved["revision"]},
        }]))
        reasons = {
            item["code"]: item
            for item in plan["selection"]["plan_reason_contributions"]
        }
        self.assertEqual(reasons["ingredients:exact_reuse"]["weight"], 0)

    def test_latest_usage_week_drives_recency_score(self):
        candidate = self.save_candidates(1)[0]
        key = self.app.recipes.get(candidate["recipe_ref"]["id"])["recipe_key"]
        with self.store.locked() as state:
            state["recipe_usage"]["old-cooked"] = {
                "week": "2026-W01", "status": "manual", "recipe_keys": [key],
                "cooked_keys": [key], "not_cooked_keys": [],
                "cooldown_overrides": {}, "order_id": None,
            }
            state["recipe_usage"]["recent-planned"] = {
                "week": "2026-W40", "status": "planned", "recipe_keys": [key],
                "cooked_keys": [], "not_cooked_keys": [],
                "cooldown_overrides": {}, "order_id": None,
            }
        plan = self.plan(self.request(
            [candidate], dates=["2026-12-07"], week="2026-W50"
        ))
        reason = next(
            item for item in plan["selection"]["slots"][0]["reason_contributions"]
            if item["code"] == "recency:recorded_use"
        )
        self.assertEqual(reason["detail"]["last_week"], "2026-W40")
        self.assertEqual(reason["detail"]["weeks_since"], 10)

    def test_duplicate_revisions_count_as_one_candidate_identity(self):
        first = self.app.handle({
            "operation": "recipes", "action": "save",
            "recipe": recipe("Revisioned", "revisioned"),
            "idempotency_key": "revisioned-save",
        })["recipe"]
        second = self.app.handle({
            "operation": "recipes", "action": "update",
            "recipe_id": first["id"], "expected_revision": first["revision"],
            "recipe": recipe("Revisioned", "revisioned"),
            "idempotency_key": "revisioned-update",
        })["recipe"]
        unique = self.save_candidates(1)[0]
        plan = self.plan(self.request([
            {"recipe_ref": {"id": first["id"], "revision": first["revision"]}},
            {"recipe_ref": {"id": second["id"], "revision": second["revision"]}},
            unique,
        ], dates=["2026-09-07", "2026-09-08"]))
        self.assertEqual(plan["status"], "planned")
        duplicate_reasons = [
            reason
            for evaluation in plan["candidate_evaluations"]
            for reason in evaluation["hard_constraints"]["reasons"]
            if reason["code"] == "duplicate_recipe_identity"
        ]
        self.assertEqual(len(duplicate_reasons), 1)

    def test_extra_effort_is_placed_on_weekend(self):
        short = self.app.handle({
            "operation": "recipes", "action": "save",
            "recipe": recipe("Short", "short", active_minutes=30),
            "idempotency_key": "short",
        })["recipe"]
        long = self.app.handle({
            "operation": "recipes", "action": "save",
            "recipe": recipe("Long", "long", active_minutes=50),
            "idempotency_key": "long",
        })["recipe"]
        plan = self.plan(self.request([
            {"recipe_ref": {"id": short["id"], "revision": short["revision"]}},
            {"recipe_ref": {"id": long["id"], "revision": long["revision"]}},
        ], dates=["2026-09-11", "2026-09-12"]))
        self.assertEqual(
            next(slot["date"] for slot in plan["selection"]["slots"] if slot["name"] == "Long"),
            "2026-09-12",
        )

    def test_large_ordinary_week_uses_bounded_deterministic_search(self):
        candidates = self.save_candidates(10)
        dates = [f"2026-09-{day:02d}" for day in range(7, 14)]
        first = self.plan(self.request(candidates, dates=dates))
        repeated = self.plan(self.request(list(reversed(candidates)), dates=dates))
        self.assertEqual(first["status"], "planned")
        self.assertEqual(first["search_strategy"], "bounded_dynamic_programming")
        self.assertLessEqual(first["explored_states"], MAX_EXPLORED_STATES)
        self.assertEqual(first["selection_digest"], repeated["selection_digest"])
        self.assertEqual(len(first["selection"]["slots"]), 7)

    def test_bounded_search_retains_weekly_minimum_candidates(self):
        candidates = self.save_candidates(10)
        candidates[0]["facts"] = explicit_facts(dietary=["fish"])
        candidates[1]["facts"] = explicit_facts(dietary=["fish"])
        with self.store.locked() as state:
            state["profile"]["diet"].update({
                "minimum_fish_portions": 2,
                "minimum_legume_dinners": 0,
                "minimum_wholegrain_or_potato_dinners": 0,
                "minimum_vegetable_types": 0,
            })
        result = self.plan(self.request(
            candidates,
            dates=[f"2026-09-{day:02d}" for day in range(7, 14)],
        ))
        self.assertEqual(result["status"], "planned")
        fish = sum(
            "fish" in slot["dietary_facets"]["values"]
            for slot in result["selection"]["slots"]
        )
        self.assertEqual(fish, 2)

    def test_bounded_search_does_not_prune_penalized_strict_candidates(self):
        candidates = self.save_candidates(12)
        candidates[-2]["facts"] = explicit_facts(dietary=["fish"])
        candidates[-1]["facts"] = explicit_facts(dietary=["fish"])
        with self.store.locked() as state:
            state["profile"]["diet"].update({
                "minimum_fish_portions": 2,
                "minimum_legume_dinners": 0,
                "minimum_wholegrain_or_potato_dinners": 0,
                "minimum_vegetable_types": 0,
            })
        original = planner._slot_reasons

        def penalize_fish(candidate, *args, **kwargs):
            reasons = original(candidate, *args, **kwargs)
            if "fish" in candidate["facts"]["dietary_facets"]["values"]:
                reasons.append({
                    "code": "test:advisory_penalty", "weight": -300,
                    "detail": "strict candidates may still rank poorly",
                })
            return reasons

        with mock.patch("planner._slot_reasons", side_effect=penalize_fish):
            result = self.plan(self.request(
                candidates,
                dates=[f"2026-09-{day:02d}" for day in range(7, 14)],
            ))
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["search_strategy"], "bounded_dynamic_programming")
        self.assertEqual(sum(
            "fish" in slot["dietary_facets"]["values"]
            for slot in result["selection"]["slots"]
        ), 2)

    def test_bounded_batch_search_retains_strict_relevant_slot_assignment(self):
        candidates = self.save_candidates(12)
        candidates[0]["facts"] = explicit_facts(dietary=["fish"], complete=True)
        for candidate in candidates[1:]:
            candidate["facts"] = explicit_facts(dietary=[], complete=True)
        with self.store.locked() as state:
            state["profile"]["meals"].update({
                "dinner_days": 7,
                "dishes": 6,
                "batch_dishes": 1,
                "meal_mode": "mixed",
                "cook_days": [
                    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                ],
                "recurring_batch_accepted": True,
                "prepared_portion_range": [4, 8],
            })
            state["profile"]["diet"].update({
                "minimum_fish_portions": 2,
                "minimum_legume_dinners": 0,
                "minimum_wholegrain_or_potato_dinners": 0,
                "minimum_vegetable_types": 0,
            })
        dates = [f"2026-09-{day:02d}" for day in range(7, 14)]
        original = planner._slot_reasons

        def prefer_fish_early(candidate, day, index, count, profile):
            reasons = original(candidate, day, index, count, profile)
            if "fish" in candidate["facts"]["dietary_facets"]["values"]:
                reasons.append({
                    "code": "test:prefer_fish_early",
                    "weight": 300 if index == 0 else -300,
                    "detail": index,
                })
            return reasons

        with mock.patch("planner._slot_reasons", side_effect=prefer_fish_early):
            result = self.plan(self.request(
                candidates, dates=dates,
            ))
        self.assertEqual(result["status"], "planned")
        source_slots = result["selection"]["source_slots"]
        fish_slot = next(
            slot for slot in source_slots
            if "fish" in slot["dietary_facets"]["values"]
        )
        self.assertEqual(fish_slot["date"], dates[5])

    def test_candidate_day_alternative_and_date_bounds_are_exact(self):
        candidates = self.save_candidates(13)
        with self.assertRaisesRegex(PlannerError, "one to 12"):
            self.plan(self.request(candidates))
        accepted = self.plan(self.request(candidates[:12]))
        self.assertEqual(accepted["status"], "planned")
        with self.assertRaisesRegex(PlannerError, "one to 7"):
            self.plan(self.request(
                candidates[:1],
                dates=[f"2026-09-{day:02d}" for day in range(7, 15)],
            ))
        with self.assertRaisesRegex(PlannerError, "one to 3"):
            self.plan(self.request(candidates[:1], alternatives=4))
        with self.assertRaisesRegex(PlannerError, "must belong"):
            self.plan(self.request(candidates[:1], dates=["2026-09-14"]))
        with self.assertRaisesRegex(PlannerError, "unique"):
            self.plan(self.request(
                candidates[:2], dates=["2026-09-07", "2026-09-07"],
            ))

    def test_history_work_bound_fails_without_partial_ranking(self):
        candidate = self.save_candidates(1)[0]
        with self.store.locked() as state:
            state["recipe_usage"] = {
                f"unrelated-{index}": {
                    "week": "2020-W01", "status": "cancelled", "recipe_keys": [f"x-{index}"],
                    "cooked_keys": [], "not_cooked_keys": [], "cooldown_overrides": {},
                    "order_id": None,
                }
                for index in range(MAX_HISTORY_RECORDS + 1)
            }
        with self.assertRaisesRegex(PlannerError, str(MAX_HISTORY_RECORDS)):
            self.plan(self.request([candidate]))


if __name__ == "__main__":
    unittest.main()
