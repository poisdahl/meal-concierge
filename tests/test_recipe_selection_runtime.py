"""MC41 real MCP/CLI -> Unix Server/Application with synthetic local recipes.

Run with the pinned MCP 2.1.1 Python using -I -B. This verifies local-bank
selection during a source outage, not an Oda/Mathem recipe API contract.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve()
SOURCE = HERE.parents[1]
SCRATCH = Path(os.environ.get("MC41_SCRATCH", tempfile.gettempdir()))
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


def synthetic_recipe(index):
    return {
        "name": f"Syntetisk blåbærmiddag {index}", "portions": 2,
        "ingredients": [{"raw": f"200 g gulrot {index}", "item": f"gulrot {index}",
                         "quantity": 200, "unit": "g", "scalable": True}],
        "steps": ["Prepare this synthetic ingredient carefully. " * 50],
        "times": {"active_minutes": 30}, "tags": ["Synthetic"],
        "source": {"kind": "user", "publisher": "MC41 synthetic fixture",
                   "external_id": str(index), "relationship": "user_supplied"},
        "rights": {"storage": "full", "credit": "Synthetic test content"},
    }


def blocker_recipe(index):
    recipe = synthetic_recipe(100 + index)
    recipe["name"] = f"Syntetisk melkedessert {index}"
    recipe["tags"] = ["dessert"]
    recipe["ingredients"] = [
        {"raw": f"10 g melkekomponent {index}-{item}",
         "item": f"melkekomponent {index}-{item}", "quantity": 10,
         "unit": "g", "scalable": True}
        for item in range(40)
    ]
    return recipe


def serve(root, empty):
    sys.path.insert(0, str(SOURCE))
    from core import HouseholdError, StateStore
    from service import Application, Server

    def no_network(event, args):
        if event in {"socket.connect", "socket.bind"}:
            assert args[0].family == socket.AF_UNIX, "test attempted non-Unix network access"
    sys.addaudithook(no_network)

    class UnavailableProvider:
        def probe(self, **kwargs):
            return {"protocol_version": "2025-11-25", "tool_count": 0,
                    "server": {"name": "MC41 synthetic unavailable source", "version": "fixture"}}

        def call(self, tool, arguments, **kwargs):
            with (root / "provider.jsonl").open("a") as log:
                log.write(json.dumps({"tool": tool, "arguments": arguments}) + "\n")
            if tool != "recipe_search":
                raise AssertionError("unexpected provider operation: " + tool)
            raise HouseholdError("synthetic recipe source unavailable")

    config = {"household": "MC41 runtime synthetic", "instance": root.name, "provider": "oda",
              "confirmation_policy": "fresh", "profile_overrides": {"diet": {
                  "minimum_fish_portions": 0, "minimum_legume_dinners": 0,
                  "minimum_vegetable_types": 0, "minimum_wholegrain_or_potato_dinners": 0},
                  "recipes": {"sources": {
                  "internal": True, "oda": True, "meny": False, "mathem": False,
                  "themealdb": False, "wikibooks": False}}}}
    app = Application(StateStore(root / "state", config), UnavailableProvider(), None)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        origins = {}
        planner_candidates = []
        blocker_candidates = []
        if not empty:
            for index in range(1, 8):
                if index % 2:
                    stored = app.recipes.save(synthetic_recipe(index), idempotency_key=f"seed-{index}")
                else:
                    stored = app.recipes.import_pack_record(synthetic_recipe(index), pack_id="synthetic-mc41",
                        recipe_id=str(index), version="1")["recipe"]
                origins[stored["id"]] = stored["entry_origin"]
                planner_candidates.append({"recipe_ref": {"id": stored["id"], "revision": stored["revision"]}})
            # Drafts make the compact catalog genuinely span multiple pages.
            for index in range(8, 30):
                stored = app.recipes.save(synthetic_recipe(index), status="draft", idempotency_key=f"seed-{index}")
                if index < 10:
                    planner_candidates.append({"recipe_ref": {"id": stored["id"], "revision": stored["revision"]}})
            for index in range(12):
                stored = app.recipes.save(blocker_recipe(index), idempotency_key=f"blocker-{index}")
                blocker_candidates.append({"recipe_ref": {
                    "id": stored["id"], "revision": stored["revision"]}})
        manifest_path.write_text(json.dumps({
            "origins": origins, "planner_candidates": planner_candidates,
            "blocker_candidates": blocker_candidates,
        }))
    Server(root / "s.sock", os.getgid(), os.getuid(), app).run()


def relay_mcp(root):
    """Record and forward the real newline-framed MCP server stdout."""
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(SOURCE / "mcp_server.py")],
        stdin=sys.stdin.buffer, stdout=subprocess.PIPE, stderr=sys.stderr.buffer,
        env=os.environ.copy(),
    )
    try:
        with (root / "mcp-stdout.jsonl").open("ab", buffering=0) as log:
            while line := process.stdout.readline():
                log.write(line)
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@unittest.skipUnless(MCP_AVAILABLE, "requires the pinned MCP 2.1.1 runtime")
class MenuProjectionTests(unittest.TestCase):
    def test_projection_summarizes_rejections_and_preserves_input(self):
        spec = importlib.util.spec_from_file_location("menu_projection_test", SOURCE / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        first = {"name": "Draft", "recipe_ref": {"id": "one", "revision": 1},
                 "hard_constraints": {"status": "fail", "reasons": [
                     {"code": "not_materializable", "status": "fail", "detail": "draft"}]},
                 "detail_fields": {"steps": "not_loaded"}, "source": {"author": None}}
        second = {**first, "name": "Other", "recipe_ref": {"id": "two", "revision": 2}}
        missing = {"name": "No metadata", "source": {"author": None}}
        explicit_null = {**missing, "detail_fields": None}
        rejected = [first, second, missing, explicit_null, first]
        original = {"status": "no_plan", "discovery": {"rejected": rejected, "unknown": ["kept"]}}
        before = deepcopy(original)
        projected = module._menu_plan_projection(original)
        self.assertEqual(projected["discovery"]["rejected_summary"], {
            "count": 5,
            "reasons": [{"code": "not_materializable", "status": "fail", "count": 3}],
        })
        self.assertEqual(projected["discovery"]["unknown_summary"], {
            "count": 1, "examples": [],
        })
        self.assertNotIn("rejected", projected["discovery"])
        self.assertEqual(original, before)

    def test_projection_bounds_unknown_discovery_examples(self):
        spec = importlib.util.spec_from_file_location("menu_projection_unknown_test", SOURCE / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        unknown = [{
            "name": (f'Unknown recipe {index} "\\' * 20),
            "discovery_ref": {"source": "synthetic", "id": f"unknown-{index}"},
            "hard_constraints": {"status": "unknown", "reasons": [{
                "code": "dietary_assessment", "status": "unknown",
                "detail": {"term": (f'ingredient {index} "\\' * 20),
                           "condition": "metadata_missing"},
            }]},
        } for index in range(80)]
        projected = module._menu_plan_projection({
            "status": "no_plan", "discovery": {"unknown": unknown, "rejected": []},
        })
        summary = projected["discovery"]["unknown_summary"]
        self.assertEqual((summary["count"], len(summary["examples"]), summary["omitted"]),
                         (80, 6, 74))
        text = json.dumps({"plan": projected}, ensure_ascii=False, separators=(",", ":"))
        wire = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": text}], "isError": False,
        }}, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(wire), 40000)

    def test_projection_preserves_distinct_warnings_for_winner_and_alternative(self):
        spec = importlib.util.spec_from_file_location("menu_projection_warning_test", SOURCE / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        first = {"recipe_ref": {"id": "one", "revision": 1}}
        second = {"recipe_ref": {"id": "two", "revision": 1}}
        dietary = lambda term: {
            "code": "dietary_assessment", "status": "advisory",
            "detail": {"kind": "sensitivity", "term": term, "condition": "unknown"},
        }
        plan = {
            "status": "planned",
            "selection": {"slots": [{"reference": first}]},
            "alternatives": [{"selection": {"slots": [{"reference": second}]}}],
            "candidate_evaluations": [
                {"reference": first, "hard_constraints": {
                    "status": "pass", "reasons": [dietary("melk"), dietary("nøtter")] }},
                {"reference": second, "hard_constraints": {
                    "status": "pass", "reasons": [dietary("selleri")] }},
            ],
        }
        projected = module._menu_plan_projection(plan)
        warnings = projected["candidate_summary"]["warnings"]
        self.assertEqual({json.dumps(item["reference"], sort_keys=True) for item in warnings},
                         {json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)})
        first_reason = next(item for item in warnings if item["reference"] == first)["reasons"][0]
        self.assertEqual(first_reason["count"], 2)
        self.assertEqual({detail["term"] for detail in first_reason["details"]}, {"melk", "nøtter"})

    def test_projection_preserves_warning_for_recurring_source(self):
        spec = importlib.util.spec_from_file_location("menu_projection_batch_warning_test", SOURCE / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reference = {"recipe_ref": {"id": "one", "revision": 1}}
        plan = {
            "status": "planned",
            "selection": {
                "slots": [{"date": "2026-09-21", "source_date": "2026-09-21", "portions": 2}],
                "source_slots": [{"date": "2026-09-21", "reference": reference}],
            },
            "candidate_evaluations": [{
                "reference": reference,
                "hard_constraints": {"status": "pass", "reasons": [{
                    "code": "dietary_assessment", "status": "advisory",
                    "detail": {"kind": "sensitivity", "term": "selleri", "condition": "unknown"},
                }]},
            }],
        }
        warnings = module._menu_plan_projection(plan)["candidate_summary"]["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("selleri", json.dumps(warnings, ensure_ascii=False))

    def test_projection_groups_fifty_reasons_across_three_seven_slot_selections(self):
        spec = importlib.util.spec_from_file_location("menu_projection_budget_test", SOURCE / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        request = {"week": "2026-W39", "candidates": [
            {"recipe_ref": {"id": f"rec_{index:024x}", "revision": 1}}
            for index in range(7)
        ]}
        reasons = [
            {"code": "dietary_preference", "weight": -30,
             "detail": {"kind": "preference",
                        "term": (f'preferanseingrediens {index} "\\' * 12)[:200],
                        "condition": "preference_deviation"}}
            for index in range(50)
        ]
        def selection(rank):
            return {
                "selection_digest": f"digest-{rank}", "total_score": -10_500,
                "soft_relaxations": [],
                "slots": [{
                    "date": f"2026-09-{21 + day}", "reference": request["candidates"][day],
                    "recipe_key": f"recipe-{day}", "name": f"Middag {day}", "portions": 2,
                    "reason_contributions": deepcopy(reasons),
                } for day in range(7)],
                "plan_reason_contributions": [],
            }
        selections = [selection(index) for index in range(3)]
        def save_ref(index):
            return {"planner_version": "weekly-menu-v4", "input_digest": "a" * 64,
                    "selection_digest": "b" * 63 + str(index), "request": request}
        plan = {
            "status": "planned", "save_ref": save_ref(0), "selection": selections[0],
            "alternatives": [{"save_ref": save_ref(index), "selection": selections[index]}
                             for index in range(1, 3)],
        }
        projected = module._menu_plan_projection(plan)
        reason = projected["selection"]["slots"][0]["reason_contributions"][0]
        self.assertEqual((reason["count"], len(reason["details"]), reason["omitted_details"]),
                         (50, 3, 47))
        text = json.dumps({"plan": projected}, ensure_ascii=False, separators=(",", ":"))
        wire = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": text}], "isError": False,
        }}, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(wire), 40000)

    def test_projection_compacts_escape_heavy_strict_issue(self):
        spec = importlib.util.spec_from_file_location("menu_projection_issue_test", SOURCE / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tokens = [(f'vegetable {index} "\\' * 8)[:80] for index in range(350)]
        plan = {
            "status": "needs_input",
            "request": {"strict_targets": ["minimum_vegetable_types"]},
            "issues": [{
                "target": "minimum_vegetable_types", "status": "unknown",
                "detail": {"minimum": 400, "observed": tokens},
            }],
        }
        projected = module._menu_plan_projection(plan)
        issue = projected["issues"][0]
        self.assertEqual((issue["target"], issue["status"]),
                         ("minimum_vegetable_types", "unknown"))
        self.assertIn("omitted_items", json.dumps(issue))
        text = json.dumps({"plan": projected}, ensure_ascii=False, separators=(",", ":"))
        wire = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": text}], "isError": False,
        }}, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(wire), 40000)


@unittest.skipUnless(MCP_AVAILABLE, "requires the pinned MCP 2.1.1 runtime")
class ProductProjectionTests(unittest.TestCase):
    @staticmethod
    def module():
        spec = importlib.util.spec_from_file_location("product_projection_test", SOURCE / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_projection_preserves_candidate_choice_and_summarizes_dietary_evidence(self):
        module = self.module()
        findings = [{
            "product_ref": 66905,
            "finding_id": f"finding-{index}",
            "kind": "preference",
            "term": f"term-{index}",
            "condition": "unknown",
            "blocked": False,
            "source": "retailer_fields",
            "evidence": {"ingredients": "very long evidence " * 100},
        } for index in range(9)]
        plan = {
            "status": "needs_input",
            "product_plan_digest": "a" * 64,
            "requirements": [{
                "requirement_id": "req:chicken",
                "item": "Strimlet kylling",
                "quantity": {"numerator": 900, "denominator": 1},
                "unit": "g",
                "status": "needs_input",
                "dietary_assessments": findings,
                "observation": {
                    "provider": "oda",
                    "query": "strimlet kylling",
                    "products": [{
                        "product_ref": 66905,
                        "name": "Ytterøy Kylling lårbiff strimlet",
                        "availability": "available",
                        "package": {"quantity": {"numerator": 500, "denominator": 1}, "unit": "g"},
                        "purchase_options": [{"package_count": 1, "price_kind": "exact", "merchandise_ore": 10140}],
                        "dietary_evidence": {"ingredients": "Lårkjøtt av kylling " * 100},
                    }],
                },
            }],
            "unresolved_requirements": [{"requirement_id": "req:chicken", "reason": "exact_candidate_scope_needs_selection"}],
        }
        before = deepcopy(plan)
        projected = module._product_result_projection({"apply_arguments": None, "product_plan": plan})
        requirement = projected["product_plan"]["requirements"][0]
        self.assertEqual(requirement["observation"]["products"][0]["product_ref"], 66905)
        self.assertNotIn("gross_quantity", requirement)
        self.assertEqual(requirement["dietary_summary"][0]["unknown"][0]["terms"],
                         [f"term-{index}" for index in range(9)])
        self.assertNotIn("dietary_assessments", requirement)
        self.assertEqual(plan, before)

    def test_thirteen_selected_requirements_fit_wire_budget_and_keep_package_count(self):
        module = self.module()
        findings = [{
            "product_ref": 66905,
            "finding_id": f"finding-{index}",
            "kind": "preference",
            "term": (f'preference {index} "\\' * 10),
            "condition": "unknown",
            "blocked": False,
            "source": "retailer_fields",
            "evidence": {"ingredients": "evidence " * 200},
        } for index in range(9)]
        requirements = []
        for index in range(13):
            product_ref = 66905 + index
            candidates = [{
                "product_ref": product_ref + offset,
                "name": f"Candidate {index}-{offset}",
                "availability": "available",
                "package": {"quantity": {"numerator": 500, "denominator": 1}, "unit": "g"},
                "purchase_options": [{"package_count": 1, "price_kind": "exact", "merchandise_ore": 10140}],
                "dietary_evidence": {"ingredients": "retailer ingredients " * 100},
            } for offset in range(5)]
            requirements.append({
                "requirement_id": f"req:{index}", "item": f"Ingredient {index}",
                "quantity": {"numerator": 900, "denominator": 1}, "unit": "g",
                "status": "selected", "dietary_assessments": findings,
                "observation": {"provider": "oda", "query": f"ingredient {index}", "products": candidates},
                "selection": {
                    "coverage": {"numerator": 1000, "denominator": 1},
                    "required": {"numerator": 900, "denominator": 1},
                    "unit": "g", "package_count": 2, "merchandise_ore": 20280,
                    "mandatory_deposit_ore": 0, "total_payable_ore": 20280,
                    "products": [{
                        "product_ref": product_ref, "name": f"Selected {index}", "quantity": 2,
                        "merchandise_ore": 20280, "mandatory_deposit_ore": 0,
                        "total_payable_ore": 20280, "dietary_assessments": findings,
                    }],
                },
            })
        result = {
            "apply_arguments": {"action": "apply", "menu_ref": {"menu_id": "menu", "revision": 1, "digest": "b" * 64},
                                "product_plan_digest": "a" * 64},
            "product_plan": {"status": "prepared", "product_plan_digest": "a" * 64,
                             "requirements": requirements, "unresolved_requirements": []},
        }
        projected = module._bounded_product_result(result)
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertEqual(len(projected["product_plan"]["requirements"]), 13)
        chicken = projected["product_plan"]["requirements"][0]
        self.assertEqual(chicken["selection"]["package_count"], 2)
        self.assertEqual(chicken["observation"]["candidate_count"], 5)
        self.assertEqual(projected["apply_arguments"], result["apply_arguments"])

    def test_sixty_four_candidate_requirements_fit_wire_budget_and_remain_actionable(self):
        module = self.module()
        requirements = []
        unresolved = []
        for index in range(64):
            products = [{
                "provider": "oda",
                "product_ref": index * 10 + offset,
                "product_id": index * 10 + offset,
                "name": f"Candidate {index}-{offset}",
                "availability": "available",
                "package": {
                    "quantity": {"numerator": 500, "denominator": 1}, "unit": "g",
                },
                "purchase_options": [{
                    "package_count": 1,
                    "price_kind": "exact",
                    "eligibility": "confirmed",
                    "offer_kind": "regular",
                    "merchandise_ore": 1000 + offset,
                    "mandatory_deposit_ore": 0,
                    "total_payable_ore": 1000 + offset,
                    "comparable_merchandise_unit_price": {
                        "numerator": 2, "denominator": 1, "unit": "g",
                        "display_ore_per_unit": "2.00",
                    },
                }],
                "display": {
                    "package": "Synthetic package, 500 g",
                    "price": "10.00",
                    "unit_price": "2.00",
                    "unit_name": "kg",
                },
            } for offset in range(5)]
            requirement_id = f"req:{index:024d}"
            requirements.append({
                "requirement_id": requirement_id,
                "identity": f"ingredient {index}",
                "item": f"Ingredient {index}",
                "search": f"Ingredient {index}",
                "quantity": {"numerator": 900, "denominator": 1},
                "gross_quantity": {"numerator": 900, "denominator": 1},
                "confirmed_pantry_quantity": {"numerator": 0, "denominator": 1},
                "unit": "g",
                "sources": [{"collection": "dishes", "recipe_index": index // 10,
                             "ingredient_index": index % 10}],
                "status": "needs_input",
                "observation": {
                    "provider": "oda", "query": f"Ingredient {index}",
                    "observed_at": "2026-09-19T12:00:00+00:00",
                    "scope": {"kind": "provider_search", "returned": 5,
                              "semantics": "bounded_relevance_ranked"},
                    "products": products,
                },
            })
            unresolved.append({
                "requirement_id": requirement_id,
                "item": f"Ingredient {index}",
                "reason": "exact_candidate_scope_needs_selection",
            })
        result = {
            "apply_arguments": None,
            "product_plan": {
                "status": "needs_input",
                "product_plan_digest": "a" * 64,
                "requirements": requirements,
                "unresolved_requirements": unresolved,
            },
        }
        result["product_plan"]["unresolved_requirements"][0] = {
            "requirement_id": requirements[0]["requirement_id"],
            "item": requirements[0]["item"],
            "reason": "provider_search_deadline",
        }
        result["product_plan"]["unresolved_requirements"].append({
            "reason": "product_budget_exceeded",
            "budget_ore": 10_000,
            "known_minimum_ore": 12_000,
        })
        before = deepcopy(result)
        projected = module._bounded_product_result(result)
        projection_sizes = {
            limit: module._mcp_text_wire_chars(json.dumps(
                module._product_result_projection(result, candidate_limit=limit),
                ensure_ascii=False, separators=(",", ":"),
            ))
            for limit in (5, 3, 1)
        }
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertIn("product_plan", projected, projection_sizes)
        self.assertEqual(projected["product_plan"]["status"], "needs_input")
        self.assertEqual(projected["product_plan"]["projection"], "minimal_actionable")
        self.assertEqual(len(projected["product_plan"]["requirements"]), 64)
        self.assertEqual(
            projected["product_plan"]["unresolved_requirements"],
            [{"reason": "product_budget_exceeded", "budget_ore": 10_000,
              "known_minimum_ore": 12_000}],
        )
        self.assertEqual(projected["product_plan"]["requirements"][0]["issue"]["reason"],
                         "provider_search_deadline")
        self.assertIn("sources", projected["product_plan"]["requirements"][0])
        for requirement in projected["product_plan"]["requirements"][1:]:
            self.assertEqual(
                requirement["issue"]["reason"],
                "exact_candidate_scope_needs_selection",
            )
            self.assertNotIn("sources", requirement)
            products = requirement["observation"]["products"]
            self.assertGreaterEqual(len(products), 1)
            self.assertIn("product_ref", products[0])
            self.assertIn("package", products[0])
            self.assertIn("purchase_options", products[0])
            self.assertNotIn("display", products[0])
        self.assertEqual(result, before)

    def test_forty_eight_requirement_failure_returns_every_issue_under_wire_budget(self):
        module = self.module()
        reason_ranges = (
            (15, "exact_candidate_scope_needs_selection"),
            (18, "candidate_package_incompatible"),
            (8, "candidate_price_or_eligibility_unresolved"),
            (7, "practical_package_choice_unavailable"),
        )
        reasons = [reason for count, reason in reason_ranges for _ in range(count)]
        requirements = []
        unresolved = []
        approvals = []
        for index, reason in enumerate(reasons):
            requirement_id = f"req:{index:024d}"
            product_ref = 10_000 + index
            product = {
                "product_ref": product_ref,
                "name": f"Candidate {index} " + '\\"' * 2_000,
                "availability": "available",
                "package": {
                    "quantity": {"numerator": 500, "denominator": 1},
                    "unit": "g",
                },
                "purchase_options": [{
                    "package_count": 1, "price_kind": "exact",
                    "eligibility": "confirmed", "offer_kind": "regular",
                    "merchandise_ore": 1_000, "mandatory_deposit_ore": 0,
                    "total_payable_ore": 1_000,
                }],
            }
            requirement = {
                "requirement_id": requirement_id,
                "item": f"Ingredient {index}",
                "quantity": {"numerator": 900, "denominator": 1},
                "unit": "g", "status": "needs_input",
                "sources": [{"collection": "dishes", "recipe_index": index // 8,
                             "ingredient_index": index % 8}],
                "observation": {"products": [product]},
            }
            if index >= 15:
                approval = {
                    "requirement_id": requirement_id,
                    "candidate_refs": [product_ref],
                }
                requirement["candidate_approval"] = deepcopy(approval)
                approvals.append(approval)
            issue = {
                "requirement_id": requirement_id,
                "item": requirement["item"],
                "reason": reason,
            }
            if reason != "exact_candidate_scope_needs_selection":
                issue["candidate_diagnostics"] = [{
                    "product_ref": product_ref,
                    "reason": "unit_conversion_required",
                    "required_unit": "g", "observed_unit": "ml",
                    "irrelevant_verbose_detail": "x" * 10_000,
                }]
            requirements.append(requirement)
            unresolved.append(issue)
        shared_groups = ((0, 1), (2, 3), (4, 5))
        for first, second in shared_groups:
            unresolved.append({
                "reason": "shared_package_group_unavailable",
                "requirement_ids": [
                    requirements[first]["requirement_id"],
                    requirements[second]["requirement_id"],
                ],
                "candidate_ref": 20_000 + first,
            })
        result = {
            "apply_arguments": None,
            "partial_apply_arguments": None,
            "product_plan": {
                "product_plan_version": "product-plan-v3", "provider": "oda",
                "binding": {"kind": "saved_menu", "menu_ref": {
                    "menu_id": "menu_fixture", "revision": 1, "digest": "b" * 64,
                }},
                "status": "needs_input", "coverage_status": "unresolved",
                "cost_status": "unresolved", "budget_status": "not_set",
                "price_mode": "exact", "product_plan_digest": "a" * 64,
                "requirements": requirements,
                "unresolved_requirements": unresolved,
            },
        }
        self.assertEqual(len(approvals), 33)
        before = deepcopy(result)
        projected = module._bounded_product_result(result)
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertEqual(projected["projection"], "issues_only")
        self.assertNotEqual(projected.get("reason"), "mcp_action_response_too_large")
        plan = projected["product_plan"]
        self.assertEqual(plan["projection"], "issues_only")
        self.assertEqual(plan["binding"], result["product_plan"]["binding"])
        self.assertEqual(plan["product_plan_digest"], "a" * 64)
        self.assertEqual(len(plan["requirements"]), 48)
        self.assertEqual(
            {row["requirement_id"] for row in plan["requirements"]},
            {row["requirement_id"] for row in requirements},
        )
        all_issues = [row["issue"] for row in plan["requirements"]]
        all_issues += plan["unresolved_requirements"]
        self.assertEqual(len(all_issues), 51)
        observed_counts = {}
        for issue in all_issues:
            observed_counts[issue["reason"]] = observed_counts.get(issue["reason"], 0) + 1
        self.assertEqual(observed_counts, {
            **{reason: count for count, reason in reason_ranges},
            "shared_package_group_unavailable": 3,
        })
        self.assertTrue(all(
            set(row) >= {"requirement_id", "item", "quantity", "unit", "status", "issue"}
            for row in plan["requirements"]
        ))
        self.assertTrue(all(
            row["candidate_search"]["query"] == "$item"
            and (
                len(row["candidate_search"]["candidates"]) == 1
                or row["candidate_search"].get("omitted_products", 0) >= 1
            )
            for row in plan["requirements"][:15]
        ))
        self.assertTrue(all(
            not ({"observation", "selection", "sources"} & set(row))
            for row in plan["requirements"]
        ))
        self.assertNotIn("reduce menu scope", projected["next"])
        self.assertIn("candidate_approvals", projected["next"])
        self.assertIn("price_mode", projected["next"])
        self.assertIn("entire same menu", projected["next"])
        self.assertEqual(result, before)

    def test_ids_and_reasons_survive_when_even_one_candidate_ref_cannot_fit(self):
        module = self.module()
        requirements = []
        unresolved = []
        for index in range(64):
            requirement_id = f"req:{index:024d}"
            requirements.append({
                "requirement_id": requirement_id,
                "item": f"Ingredient {index} " + "i" * 180,
                "quantity": {"numerator": 1, "denominator": 1},
                "unit": "piece", "status": "needs_input",
                "observation": {"products": [{
                    "product_ref": f"{index:02d}" + "r" * 498,
                    "name": "n" * 2_000,
                }]},
            })
            unresolved.append({
                "requirement_id": requirement_id,
                "reason": "exact_candidate_scope_needs_selection",
                "candidate_diagnostics": [{
                    "product_ref": index, "reason": "package_size_unresolved",
                    "package_limit": {"count": 1, "detail": "x" * 5_000},
                } for _ in range(5)],
            })
        result = {"product_plan": {
            "status": "needs_input", "product_plan_digest": "a" * 64,
            "requirements": requirements, "unresolved_requirements": unresolved,
        }}
        projected = module._bounded_product_result(result)
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertEqual(projected["projection"], "issues_only")
        rows = projected["product_plan"]["requirements"]
        self.assertEqual(len(rows), 64)
        self.assertTrue(all(
            row["issue"]["reason"] == "exact_candidate_scope_needs_selection"
            and "candidate_refs" not in row["issue"]
            and "candidate_diagnostics" not in row["issue"]
            for row in rows
        ))
        self.assertTrue(all(
            row["candidate_search"]["query"] == "$item"
            and row["candidate_search"]["candidates"] == []
            for row in rows
        ))

    def test_oversized_apply_binding_returns_bounded_non_actionable_result(self):
        module = self.module()
        projected = module._bounded_product_result({
            "apply_arguments": {
                "action": "apply", "planner_handoff": {"payload": "x" * 60_000},
                "product_plan_digest": "a" * 64,
            },
            "product_plan": {
                "status": "prepared", "product_plan_digest": "a" * 64,
                "requirements": [],
            },
            "unexpected_diagnostics": "y" * 60_000,
        })
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertEqual(projected["reason"], "mcp_action_response_too_large")
        self.assertNotIn("apply_arguments", projected)

    def test_oversized_prepared_plan_returns_exact_bounded_apply_arguments(self):
        module = self.module()
        arguments = {
            "action": "apply",
            "menu_ref": {"menu_id": "menu", "revision": 1, "digest": "b" * 64},
            "candidate_approvals": [{"requirement_id": "req:1", "candidate_refs": [123]}],
            "ingredient_decisions": [],
            "budget_ore": None,
            "price_mode": "exact",
            "product_plan_digest": "a" * 64,
        }
        result = {
            "apply_arguments": arguments,
            "product_plan": {
                "status": "prepared",
                "product_plan_digest": "a" * 64,
                "requirements": [{
                    "requirement_id": "req:1",
                    "item": "Oversized prepared detail " + "x" * 60_000,
                    "status": "selected",
                }],
                "unresolved_requirements": [],
            },
            "observation_drift": {
                "status": "changed",
                "previous_product_plan_digest": "c" * 64,
                "current_product_plan_digest": "a" * 64,
            },
        }
        before = deepcopy(result)
        projected = module._bounded_product_result(result)
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertEqual(projected["status"], "prepared")
        self.assertEqual(projected["projection"], "apply_arguments_only")
        self.assertIs(projected["details_omitted"], True)
        self.assertEqual(projected["product_plan_digest"], "a" * 64)
        self.assertEqual(projected["apply_arguments"], arguments)
        self.assertEqual(projected["observation_drift"], result["observation_drift"])
        self.assertNotIn("cart_change_requested", projected["apply_arguments"])
        self.assertNotIn("product_plan", projected)
        self.assertEqual(result, before)

    def test_oversized_partial_plan_keeps_exact_partial_apply_arguments(self):
        module = self.module()
        arguments = {
            "action": "apply", "partial_apply": True,
            "menu_ref": {"menu_id": "menu", "revision": 1, "digest": "b" * 64},
            "candidate_approvals": [{
                "requirement_id": f"req:{index}", "candidate_refs": [10_000 + index],
            } for index in range(33)],
            "ingredient_decisions": [], "budget_ore": None, "price_mode": "estimate",
            "partial_product_plan_digest": "c" * 64,
        }
        result = {
            "apply_arguments": None,
            "partial_apply_arguments": arguments,
            "product_plan": {
                "status": "needs_input", "product_plan_digest": "a" * 64,
                "partial_product_plan_digest": "c" * 64,
                "requirements": [{
                    "requirement_id": "req:0", "item": "x" * 60_000,
                    "quantity": {"numerator": 1, "denominator": 1},
                    "unit": "piece", "status": "selected",
                }],
                "unresolved_requirements": [{
                    "requirement_id": "req:unresolved", "item": "Remaining",
                    "reason": "exact_candidate_scope_needs_selection",
                }],
            },
        }
        before = deepcopy(result)
        projected = module._bounded_product_result(result)
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertEqual(projected["projection"], "partial_apply_arguments_with_issues")
        self.assertEqual(projected["partial_apply_arguments"], arguments)
        self.assertNotIn("cart_change_requested", projected["partial_apply_arguments"])
        self.assertEqual(
            projected["remaining_issues"][0]["reason"],
            "exact_candidate_scope_needs_selection",
        )
        self.assertEqual(result, before)

    def test_forty_four_selected_partial_keeps_all_four_unresolved_issues(self):
        module = self.module()
        requirements = []
        unresolved = []
        approvals = []
        for index in range(48):
            requirement_id = f"req:{index:024d}"
            product_ref = 30_000 + index
            requirement = {
                "requirement_id": requirement_id,
                "item": f"Ingredient {index}",
                "quantity": {"numerator": 500, "denominator": 1},
                "unit": "g",
                "status": "selected" if index < 44 else "needs_input",
                "observation": {"products": [{
                    "product_ref": product_ref,
                    "name": f"Candidate {index} " + "x" * 2_000,
                }]},
            }
            if index < 44:
                approvals.append({
                    "requirement_id": requirement_id,
                    "candidate_refs": [product_ref],
                })
                requirement["selection"] = {
                    "products": [{
                        "product_ref": product_ref,
                        "name": f"Selected {index} " + "y" * 2_000,
                        "quantity": 1,
                    }],
                }
            else:
                unresolved.append({
                    "requirement_id": requirement_id,
                    "item": requirement["item"],
                    "reason": "exact_candidate_scope_needs_selection",
                })
            requirements.append(requirement)
        arguments = {
            "action": "apply", "partial_apply": True,
            "menu_ref": {"menu_id": "menu", "revision": 1, "digest": "b" * 64},
            "candidate_approvals": approvals,
            "ingredient_decisions": [], "budget_ore": None, "price_mode": "estimate",
            "partial_product_plan_digest": "f" * 64,
        }
        result = {
            "apply_arguments": None,
            "partial_apply_arguments": arguments,
            "product_plan": {
                "product_plan_version": "product-plan-v3", "provider": "oda",
                "binding": {"kind": "saved_menu", "menu_ref": arguments["menu_ref"]},
                "status": "needs_input", "coverage_status": "unresolved",
                "cost_status": "unresolved", "price_mode": "estimate",
                "product_plan_digest": "a" * 64,
                "partial_product_plan_digest": "f" * 64,
                "requirements": requirements,
                "unresolved_requirements": unresolved,
            },
        }
        before = deepcopy(result)
        projected = module._bounded_product_result(result)
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertEqual(projected["projection"], "partial_apply_arguments_with_issues")
        self.assertEqual(projected["partial_apply_arguments"], arguments)
        self.assertEqual(projected["partial_product_plan_digest"], "f" * 64)
        self.assertEqual(projected["remaining_issue_count"], 4)
        issue_rows = projected["remaining_issues"]
        self.assertEqual(len(issue_rows), 4)
        self.assertEqual(
            {row["requirement_id"] for row in issue_rows},
            {row["requirement_id"] for row in requirements[44:]},
        )
        self.assertTrue(all(
            row["reason"] == "exact_candidate_scope_needs_selection"
            and len(row["candidate_refs"]) == 1
            for row in issue_rows
        ))
        self.assertEqual(result, before)

    def test_partial_arguments_only_is_last_resort_when_issue_text_cannot_fit(self):
        module = self.module()
        arguments = {
            "action": "apply", "partial_apply": True,
            "menu_ref": {"menu_id": "menu", "revision": 1, "digest": "b" * 64},
            "candidate_approvals": [{
                "requirement_id": "req:selected", "candidate_refs": [10],
            }],
            "ingredient_decisions": [], "budget_ore": None, "price_mode": "exact",
            "partial_product_plan_digest": "c" * 64,
        }
        result = {
            "partial_apply_arguments": arguments,
            "product_plan": {
                "status": "needs_input", "product_plan_digest": "a" * 64,
                "partial_product_plan_digest": "c" * 64,
                "requirements": [{
                    "requirement_id": "req:selected", "item": "Selected",
                    "quantity": {"numerator": 1, "denominator": 1},
                    "unit": "piece", "status": "selected",
                }, {
                    "requirement_id": "req:remaining", "item": "z" * 60_000,
                    "quantity": {"numerator": 1, "denominator": 1},
                    "unit": "piece", "status": "needs_input",
                }],
                "unresolved_requirements": [{
                    "requirement_id": "req:remaining", "item": "z" * 60_000,
                    "reason": "exact_candidate_scope_needs_selection",
                }],
            },
        }
        projected = module._bounded_product_result(result)
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertEqual(projected["projection"], "partial_apply_arguments_only")
        self.assertEqual(projected["partial_apply_arguments"], arguments)
        self.assertNotIn("remaining_issues", projected)

    def test_malformed_partial_issue_list_does_not_crash_projection(self):
        module = self.module()
        arguments = {
            "action": "apply", "partial_apply": True,
            "menu_ref": {"menu_id": "menu", "revision": 1, "digest": "b" * 64},
            "candidate_approvals": [{
                "requirement_id": "req:selected", "candidate_refs": [10],
            }],
            "partial_product_plan_digest": "c" * 64,
        }
        projected = module._bounded_product_result({
            "partial_apply_arguments": arguments,
            "product_plan": {
                "status": "needs_input", "product_plan_digest": "a" * 64,
                "partial_product_plan_digest": "c" * 64,
                "requirements": [{
                    "requirement_id": "req:selected", "item": "x" * 60_000,
                    "status": "selected",
                }],
                "unresolved_requirements": None,
            },
        })
        self.assertEqual(projected["projection"], "partial_apply_arguments_only")
        self.assertEqual(projected["partial_apply_arguments"], arguments)
        self.assertNotIn("remaining_issue_count", projected)

    def test_apply_only_projection_rejects_inconsistent_or_authorizing_arguments(self):
        module = self.module()
        base = {
            "apply_arguments": {
                "action": "apply", "product_plan_digest": "a" * 64,
            },
            "product_plan": {
                "status": "prepared", "product_plan_digest": "a" * 64,
                "requirements": [{"item": "x" * 60_000}],
            },
        }
        cases = {
            "digest_mismatch": {
                **deepcopy(base),
                "apply_arguments": {
                    "action": "apply", "product_plan_digest": "b" * 64,
                },
            },
            "invalid_digest": {
                **deepcopy(base),
                "apply_arguments": {"action": "apply", "product_plan_digest": "not-a-digest"},
                "product_plan": {
                    **deepcopy(base["product_plan"]), "product_plan_digest": "not-a-digest",
                },
            },
            "authority_injected": {
                **deepcopy(base),
                "apply_arguments": {
                    **deepcopy(base["apply_arguments"]), "cart_change_requested": True,
                },
            },
            "malformed_drift": {
                **deepcopy(base),
                "observation_drift": {
                    "status": "unchanged",
                    "previous_product_plan_digest": "c" * 64,
                    "current_product_plan_digest": "b" * 64,
                },
            },
        }
        for name, result in cases.items():
            with self.subTest(name=name):
                projected = module._bounded_product_result(result)
                self.assertEqual(projected["reason"], "mcp_action_response_too_large")
                self.assertNotIn("apply_arguments", projected)

    def test_escape_heavy_candidate_name_fails_closed(self):
        module = self.module()
        result = {
            "apply_arguments": {
                "action": "apply", "product_plan_digest": "a" * 64,
            },
            "product_plan": {
                "status": "needs_input",
                "requirements": [{
                    "requirement_id": "req:hostile",
                    "item": "Hostile candidate",
                    "quantity": {"numerator": 1, "denominator": 1},
                    "unit": "piece",
                    "status": "needs_input",
                    "observation": {"products": [{
                        "product_ref": 1,
                        "name": '\\\"' * 30_000,
                        "availability": "available",
                        "purchase_options": [{
                            "package_count": 1,
                            "price_kind": "exact",
                            "eligibility": "confirmed",
                            "total_payable_ore": 100,
                        }],
                    }]},
                }],
                "unresolved_requirements": [{
                    "requirement_id": "req:hostile",
                    "item": "Hostile candidate",
                    "reason": "exact_candidate_scope_needs_selection",
                }],
            },
        }
        before = deepcopy(result)
        projected = module._bounded_product_result(result)
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(module._mcp_text_wire_chars(text), module.MCP_PRODUCT_WIRE_BUDGET)
        self.assertEqual(projected["projection"], "issues_only")
        self.assertNotIn("apply_arguments", projected)
        issue = projected["product_plan"]["requirements"][0]["issue"]
        self.assertEqual(issue["reason"], "exact_candidate_scope_needs_selection")
        self.assertNotIn("candidate_refs", issue)
        self.assertEqual(
            projected["product_plan"]["requirements"][0]["candidate_search"],
            {"query": "$item", "candidates": [], "omitted_products": 1},
        )
        self.assertNotIn('\\"', json.dumps(projected, ensure_ascii=False))
        self.assertEqual(result, before)


@unittest.skipUnless(MCP_AVAILABLE, "requires the pinned MCP 2.1.1 runtime")
class RecipeSelectionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertEqual(importlib.metadata.version("mcp"), "2.1.1")
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="rt-", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sock = self.root / "s.sock"
        self.process = None
        self.addCleanup(self.stop_service)
        self.empty = self._testMethodName == "test_empty_bank_and_unavailable_provider_never_authorize_ai"
        await self.start_service()

    async def start_service(self):
        log = (self.root / "service.log").open("a")
        self.addCleanup(log.close)
        args = [sys.executable, "-I", "-B", str(HERE), "--serve", str(self.root)]
        if self.empty:
            args.append("--empty")
        self.process = subprocess.Popen(args, stdout=log, stderr=log,
            env={"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root)})
        for _ in range(200):
            if self.process.poll() is not None:
                self.fail((self.root / "service.log").read_text())
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(.1)
                    connection.connect(str(self.sock))
                    connection.sendall(b'{"operation":"health","contract":1}\n')
                    response = json.loads(connection.recv(65536))
                    if response.get("ok"):
                        return
            except (OSError, ValueError):
                pass
            await asyncio.sleep(.05)
        self.fail("service health startup timeout")

    def stop_service(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    @asynccontextmanager
    async def client(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(command=sys.executable,
            args=["-I", "-B", str(HERE), "--relay-mcp", str(self.root)], cwd=str(self.root),
            env={"MEAL_CONCIERGE_SOCKET": str(self.sock), "HOME": str(self.root), "TMPDIR": str(self.root)})
        with (self.root / "bridge.log").open("a") as log:
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=120) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.server_info.name, "meal-concierge")
                    yield client

    async def call(self, client, tool, **arguments):
        result = await client.call_tool("meal_concierge_" + tool, arguments)
        self.assertFalse(result.is_error, result)
        text = json.loads(result.content[0].text)
        if tool in {"menu", "products"}:
            self.assertEqual(len(result.content), 1)
            self.assertEqual(result.content[0].type, "text")
            self.assertIsNone(result.structured_content)
            self.assertIsInstance(text, dict)
            self.assertEqual(result.content[0].text, json.dumps(text, ensure_ascii=False, separators=(",", ":")))
            if tool == "menu":
                self.last_menu_text = result.content[0].text
                lines = (self.root / "mcp-stdout.jsonl").read_text().splitlines()
                self.last_menu_wire = next(
                    line for line in reversed(lines)
                    if (raw := json.loads(line)).get("result", {}).get("content", [{}])[0].get("text")
                    == result.content[0].text
                )
                raw = json.loads(self.last_menu_wire)
                self.assertEqual(raw["result"]["content"][0]["text"], result.content[0].text)
                if isinstance(text.get("plan"), dict) and "selection" in text["plan"]:
                    self.assertIn('\\"plan\\"', self.last_menu_wire)
                    self.assertIn("blåbærmiddag", self.last_menu_wire)
        else:
            self.assertIsInstance(result.structured_content, dict)
            self.assertEqual(text, result.structured_content)
        return text

    async def cli(self, request):
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-B", str(SOURCE / "cli.py"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root),
                 "MEAL_CONCIERGE_SOCKET": str(self.sock)})
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), timeout=120)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        self.assertEqual(process.returncode, 0, stderr.decode() + stdout.decode())
        response = json.loads(stdout)
        self.assertTrue(response["ok"])
        return response["result"]

    def bank_counts(self):
        with sqlite3.connect(f"file:{self.root / 'state/recipes.sqlite3'}?mode=ro", uri=True) as db:
            return (db.execute("SELECT count(*) FROM recipes").fetchone()[0],
                    db.execute("SELECT count(*) FROM recipe_favorites WHERE is_favorite=1").fetchone()[0])

    @staticmethod
    def week():
        future = datetime.now(ZoneInfo("Europe/Oslo")).date() + timedelta(days=7)
        return future.strftime("%G-W%V")

    def assert_compact_selection(self, compact, full, *, alternative=False):
        def aggregate(reasons):
            result = {}
            for reason in reasons:
                item = result.setdefault(reason["code"], {"weight": 0, "count": 0})
                item["weight"] += reason["weight"]
                item["count"] += 1
            return result

        for field in ("selection_digest", "total_score", "soft_relaxations"):
            self.assertEqual(compact[field], full[field])
        slot_fields = ("date", "reference", "recipe_key", "name", "portions",
                       "source_date", "kind", "new_shopping_requirements")
        self.assertEqual(
            [{key: slot[key] for key in slot_fields if key in slot}
             for slot in compact["slots"]],
            [{key: slot[key] for key in slot_fields if key in slot}
             for slot in full["slots"]],
        )
        reason_slots = zip(compact["slots"], full["slots"], strict=True)
        if "source_slots" in full:
            self.assertTrue(all("reason_contributions" not in slot for slot in compact["slots"]))
            reason_slots = zip(compact["source_slots"], full["source_slots"], strict=True)
        for compact_slot, full_slot in reason_slots:
            self.assertEqual(
                {reason["code"]: {
                    "weight": reason["weight"], "count": reason.get("count", 1),
                } for reason in compact_slot["reason_contributions"]},
                aggregate(full_slot["reason_contributions"]),
            )
            if alternative:
                self.assertTrue(all(
                    "detail" not in reason and "details" not in reason
                    for reason in compact_slot["reason_contributions"]
                ))
        self.assertEqual(
            {reason["code"]: {
                "weight": reason["weight"], "count": reason.get("count", 1),
            } for reason in compact.get("plan_reason_contributions", [])},
            aggregate(full.get("plan_reason_contributions", [])),
        )
        if "source_slots" in full:
            self.assertEqual(
                [{key: slot[key] for key in ("date", "reference", "recipe_key", "name", "portions")}
                 for slot in compact["source_slots"]],
                [{key: slot[key] for key in ("date", "reference", "recipe_key", "name", "portions")}
                 for slot in full["source_slots"]],
            )
        if "batches" in full:
            batch_fields = ("source_date", "eating_dates", "batch", "prepared_portions",
                            "consumed_at_source", "recipe_key", "name")
            self.assertEqual(
                [{key: batch[key] for key in batch_fields if key in batch}
                 for batch in compact["batches"]],
                [{key: batch[key] for key in batch_fields if key in batch}
                 for batch in full["batches"]],
            )
            for compact_batch, full_batch in zip(compact["batches"], full["batches"], strict=True):
                self.assertEqual(compact_batch["guidance"], {
                    key: value for key, value in full_batch["guidance"].items()
                })

    async def test_compact_pages_and_no_ref_week_save_cli_restart(self):
        before = self.bank_counts()
        origins = json.loads((self.root / "manifest.json").read_text())["origins"]
        async with self.client() as client:
            self.assertIn("meal_concierge_menu", {tool.name for tool in (await client.list_tools()).tools})
            setup_gate = await self.call(client, "menu", action="plan", planner_input={"week": self.week()})
            self.assertNotIn("plan", setup_gate)
            self.assertTrue(setup_gate["configuration_required"])
            self.assertEqual(setup_gate, await self.cli({"operation": "menu", "action": "plan", "interactive": True,
                                                       "planner_input": {"week": self.week()}}))
            await self.call(client, "setup", action="apply", keep_current=True)
            page = await self.call(client, "recipe_discovery", projection="summary", source="internal", limit=3)
            self.assertIsNotNone(page["next_cursor"])
            following = await self.call(client, "recipe_discovery", projection="summary", source="internal",
                                        limit=3, cursor=page["next_cursor"])
            self.assertTrue({r["recipe_ref"]["id"] for r in page["recipes"]}.isdisjoint(
                r["recipe_ref"]["id"] for r in following["recipes"]))
            full = []
            for summary in page["recipes"]:
                self.assertNotIn("steps", summary)
                self.assertNotIn("ingredients", summary)
                self.assertEqual(summary["detail_fields"]["ingredients"], "not_loaded")
                ref = summary["recipe_ref"]
                detail = (await self.call(client, "recipes", action="get", recipe_id=ref["id"], revision=ref["revision"]))["recipe"]
                self.assertEqual((detail["id"], detail["revision"]), (ref["id"], ref["revision"]))
                full.append(detail)
            summary_bytes = len(json.dumps(page["recipes"]).encode())
            full_bytes = len(json.dumps(full).encode())
            self.assertLess(summary_bytes, full_bytes / 2)
            started = time.monotonic()
            planned = (await self.call(client, "menu", action="plan", planner_input={"week": self.week()}))["plan"]
            cold_seconds = time.monotonic() - started
            self.assertEqual(planned["status"], "planned")
            slots = planned["selection"]["slots"]
            self.assertEqual(len(slots), 7)
            self.assertEqual(len({slot["date"] for slot in slots}), 7)
            self.assertEqual({origins[slot["reference"]["recipe_ref"]["id"]] for slot in slots}, {"user", "bundled"})
            self.assertFalse(planned["discovery"]["ai_fallback_eligible"])
            self.assertEqual(next(s["status"] for s in planned["discovery"]["sources"] if s["source"] == "oda"), "unavailable")
            self.assertEqual(self.bank_counts(), before)
            started = time.monotonic()
            via_cli = (await self.cli({"operation": "menu", "action": "plan", "planner_input": {"week": self.week()}}))["plan"]
            warm_seconds = time.monotonic() - started
            self.assertEqual(via_cli["selection_digest"], planned["selection_digest"])
            self.assertEqual(
                {key: via_cli["save_handoff"][key] for key in planned["save_ref"]},
                planned["save_ref"],
            )
            self.assertEqual(
                [(slot["date"], slot["reference"], slot["name"], slot["portions"])
                 for slot in via_cli["selection"]["slots"]],
                [(slot["date"], slot["reference"], slot["name"], slot["portions"])
                 for slot in planned["selection"]["slots"]],
            )
            self.assertLess(len(json.dumps(planned["save_ref"])), len(json.dumps(via_cli["save_handoff"])) / 4)
            self.assertEqual(planned["alternatives"], [])
            print(json.dumps({"mc41_runtime": {"summary_bytes": summary_bytes, "full_bytes": full_bytes,
                "mcp_cold_seconds": round(cold_seconds, 3), "cli_warm_seconds": round(warm_seconds, 3),
                "explored_states": planned["explored_states"], "discovery_work": planned["discovery"]["work"]}}), flush=True)
            saved = (await self.call(client, "menu", action="save", planner_ref=planned["save_ref"]))["menu"]
            repeated = await self.call(client, "menu", action="save", planner_ref=planned["save_ref"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(repeated["menu"], saved)
        self.stop_service()
        await self.start_service()
        async with self.client() as client:
            restored = (await self.call(client, "menu"))["menu"]
            self.assertEqual(restored, saved)
            replay = await self.call(client, "menu", action="save", planner_ref=planned["save_ref"])
            self.assertTrue(replay["idempotent"])
            self.assertEqual(replay["menu"], saved)
        self.assertEqual(self.bank_counts(), before)
        calls = [json.loads(line) for line in (self.root / "provider.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertEqual({call["tool"] for call in calls}, {"recipe_search"})
        state = json.loads((self.root / "state/state.json").read_text())
        self.assertEqual(state["order_snapshots"], {})
        self.assertIsNone(state["cart_plan"])

    async def test_three_complete_mcp_alternatives_save_and_stale_rejection(self):
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            request = {"week": self.week(), "alternatives": 3}
            planned = (await self.call(client, "menu", action="plan", planner_input=request))["plan"]
            self.assertLess(len(self.last_menu_wire), 40000)
            print(json.dumps({"mc41_mcp_three_alternatives_wire_chars": len(self.last_menu_wire)}), flush=True)
            full = (await self.cli({"operation": "menu", "action": "plan", "planner_input": request}))["plan"]
            choices = [{"save_ref": planned["save_ref"], "selection": planned["selection"]}, *planned["alternatives"]]
            self.assertEqual(len(choices), 3)
            self.assertEqual(
                [choice["save_ref"] for choice in choices],
                [{key: handoff[key] for key in ("planner_version", "input_digest", "selection_digest", "request")}
                 for handoff in full["save_handoffs"]],
            )
            for index, (choice, full_selection) in enumerate(
                    zip(choices, full["selections"], strict=True)):
                self.assert_compact_selection(
                    choice["selection"], full_selection, alternative=index > 0)
            self.assertEqual(full["selection"], full["selections"][0])
            self.assertEqual(full["request"], full["canonical_input"]["request"])
            for field in ("input_digest", "selection_digest", "planner_version", "explored_states"):
                self.assertEqual(planned[field], full[field])
            for field in ("sources", "unknown"):
                if field == "sources":
                    self.assertEqual(planned["discovery"][field], full["discovery"][field])
            self.assertEqual(
                planned["discovery"]["rejected_summary"]["count"],
                len(full["discovery"]["rejected"]),
            )
            self.assertEqual(planned["candidate_summary"]["counts"], {"pass": 7, "unknown": 0, "fail": 0})
            self.assertTrue({
                "canonical_input", "candidate_evaluations", "cooking_experiences", "effective_profile",
                "effective_feedback", "request", "selections", "save_handoff", "save_handoffs", "work_limits",
            }.isdisjoint(planned))

            # Failed outcomes retain concise blockers without echoing the full request/evaluations.
            exact = json.loads((self.root / "manifest.json").read_text())["planner_candidates"]
            insufficient = {"week": self.week(), "candidates": [*exact[:6], *exact[7:9]]}
            no_plan = (await self.call(client, "menu", action="plan", planner_input=insufficient))["plan"]
            self.assertLess(len(self.last_menu_wire), 40000)
            no_plan_cli = (await self.cli({"operation": "menu", "action": "plan", "planner_input": insufficient}))["plan"]
            self.assertEqual(no_plan["status"], "no_plan")
            self.assertEqual(no_plan["issues"], no_plan_cli["issues"])
            self.assertEqual(no_plan["issues"][0]["code"], "insufficient_hard_constraint_candidates")
            self.assertEqual(no_plan["candidate_summary"]["counts"], {"pass": 6, "unknown": 0, "fail": 2})
            self.assertEqual(len(no_plan["candidate_summary"]["blockers"]), 2)
            self.assertTrue({"request", "candidate_evaluations"}.isdisjoint(no_plan))
            self.assertNotIn("save_ref", no_plan)

            await self.call(client, "profile", action="update",
                            changes={"diet": {"minimum_fish_portions": 1}})
            needs_input_request = {
                "week": self.week(), "candidates": full["request"]["candidates"][:7],
                "strict_targets": ["minimum_fish_portions"],
            }
            needs_input = (await self.call(
                client, "menu", action="plan", planner_input=needs_input_request))["plan"]
            needs_input_cli = (await self.cli({
                "operation": "menu", "action": "plan",
                "planner_input": needs_input_request,
            }))["plan"]
            self.assertEqual(needs_input["status"], "needs_input")
            self.assertEqual(needs_input["issues"], needs_input_cli["issues"])
            self.assertEqual(needs_input["issues"][0]["target"], "minimum_fish_portions")
            self.assertEqual(needs_input["candidate_summary"]["counts"],
                             {"pass": 7, "unknown": 0, "fail": 0})
            self.assertLess(len(self.last_menu_wire), 40000)

            complete_without_fish = [{
                **candidate,
                "facts": {"dietary_facets": {
                    "source": "explicit", "values": [], "complete": True,
                    "vegetable_types": [],
                }},
            } for candidate in full["request"]["candidates"][:7]]
            strict_no_plan = (await self.call(client, "menu", action="plan", planner_input={
                "week": self.week(), "candidates": complete_without_fish,
                "strict_targets": ["minimum_fish_portions"],
            }))["plan"]
            self.assertEqual(strict_no_plan["status"], "no_plan")
            self.assertEqual(strict_no_plan["issues"][0]["code"], "strict_targets_infeasible")
            self.assertEqual(strict_no_plan["issues"][0]["targets"], ["minimum_fish_portions"])
            self.assertLess(len(self.last_menu_wire), 40000)
            stale = await client.call_tool("meal_concierge_menu", {"action": "save", "planner_ref": choices[1]["save_ref"]})
            self.assertFalse(stale.is_error, stale)
            self.assertEqual(json.loads(stale.content[0].text)["status"], "rejected")
            self.assertFalse(json.loads(stale.content[0].text)["ok"])
            self.assertIn("stale", stale.content[0].text.lower())
            self.assertIsNone((await self.call(client, "menu"))["menu"])
            await self.call(client, "profile", action="update",
                            changes={"diet": {"minimum_fish_portions": 0}})
            fresh = (await self.call(client, "menu", action="plan", planner_input=request))["plan"]
            alternative = fresh["alternatives"][1]["save_ref"]
            saved = (await self.call(client, "menu", action="save", planner_ref=alternative))["menu"]
            self.assertEqual((await self.call(client, "menu"))["menu"], saved)
            self.assertEqual(saved["planner_selection"]["selection_digest"], alternative["selection_digest"])

    async def test_nine_exact_candidates_fit_wire_and_save_after_restart(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(len(manifest["planner_candidates"]), 9)
        request = {"week": self.week(), "candidates": manifest["planner_candidates"]}
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            planned = (await self.call(client, "menu", action="plan", planner_input=request))["plan"]
            self.assertEqual(planned["status"], "planned")
            self.assertEqual(planned["explored_states"], 5040)
            self.assertEqual(set(planned["save_ref"]), {
                "planner_version", "input_digest", "selection_digest", "request"})
            self.assertEqual(planned["candidate_summary"]["counts"], {"pass": 7, "unknown": 0, "fail": 2})
            self.assertEqual(len(planned["candidate_summary"]["blockers"]), 2)
            self.assertEqual(len(planned["selection"]["slots"]), 7)
            self.assertTrue(all(slot["reason_contributions"] for slot in planned["selection"]["slots"]))
            self.assertTrue(all("detail" in reason or "details" in reason
                                for slot in planned["selection"]["slots"]
                                for reason in slot["reason_contributions"]))
            self.assertLess(len(self.last_menu_wire), 40000)
            print(json.dumps({"mc41_mcp_nine_candidates_wire_chars": len(self.last_menu_wire)}), flush=True)
            full = (await self.cli({"operation": "menu", "action": "plan",
                                    "planner_input": request}))["plan"]
            self.assertEqual(len(full["candidate_evaluations"]), 9)
            self.assertIn("canonical_input", full)
            self.assertEqual(
                {key: full["save_handoff"][key] for key in planned["save_ref"]},
                planned["save_ref"],
            )
            saved = (await self.call(client, "menu", action="save",
                                     planner_ref=planned["save_ref"]))["menu"]
        self.stop_service()
        await self.start_service()
        async with self.client() as client:
            self.assertEqual((await self.call(client, "menu"))["menu"], saved)
            replay = await self.call(client, "menu", action="save", planner_ref=planned["save_ref"])
            self.assertTrue(replay["idempotent"])

    async def test_batch_plan_fits_wire_and_saves(self):
        all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        meals = {
            "meal_mode": "batch", "dinner_days": 7, "dishes": 3, "batch_dishes": 3,
            "portions": 2, "prepared_portion_range": [4, 8],
            "cook_days": ["Monday", "Wednesday", "Friday"], "eat_days": all_days,
            "recurring_batch_accepted": True,
        }
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            await self.call(client, "profile", action="update", changes={"meals": meals})
            manifest = json.loads((self.root / "manifest.json").read_text())
            guidance = {
                "basis": "synthetic explicit basis for planned leftovers",
                "suitability": "suitable",
                "storage": ("synthetic storage context " + "s" * 90
                            + "; discard rather than serve after the stated interval"),
                "reheating": "synthetic reheating guidance for the chosen dish",
            }
            candidates = [
                {**candidate, "facts": {"batch_guidance": guidance}}
                for candidate in manifest["planner_candidates"][:3]
            ]
            request = {"week": self.week(), "candidates": candidates, "alternatives": 3}
            planned = (await self.call(client, "menu", action="plan",
                                       planner_input=request))["plan"]
            self.assertEqual(planned["status"], "planned")
            self.assertEqual(len(planned["selection"]["slots"]), 7)
            self.assertEqual(len(planned["selection"]["source_slots"]), 3)
            self.assertEqual(len(planned["selection"]["batches"]), 3)
            self.assertEqual(len(planned["alternatives"]), 2)
            self.assertLess(len(self.last_menu_wire), 45000)
            print(json.dumps({"mc41_mcp_batch_wire_chars": len(self.last_menu_wire)}), flush=True)
            full = (await self.cli({"operation": "menu", "action": "plan",
                                    "planner_input": request}))["plan"]
            choices = [{"save_ref": planned["save_ref"], "selection": planned["selection"]},
                       *planned["alternatives"]]
            self.assertEqual(
                [choice["save_ref"] for choice in choices],
                [{key: handoff[key] for key in ("planner_version", "input_digest", "selection_digest", "request")}
                 for handoff in full["save_handoffs"]],
            )
            for index, (choice, full_selection) in enumerate(
                    zip(choices, full["selections"], strict=True)):
                self.assert_compact_selection(
                    choice["selection"], full_selection, alternative=index > 0)
            self.assertEqual(planned["selection"]["batches"][0]["guidance"], {
                key: value for key, value in guidance.items()
            })
            self.assertIn("discard rather than serve",
                          planned["selection"]["batches"][0]["guidance"]["storage"])
            saved = (await self.call(client, "menu", action="save",
                                     planner_ref=choices[1]["save_ref"]))["menu"]
            self.assertEqual(len(saved["slots"]), 7)
            self.assertEqual(len(saved["batches"]), 3)
            self.assertEqual(saved["planner_selection"]["selection_digest"],
                             choices[1]["save_ref"]["selection_digest"])

    async def test_oversized_exact_action_refs_return_compact_needs_input(self):
        all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        meals = {
            "meal_mode": "batch", "dinner_days": 7, "dishes": 3, "batch_dishes": 3,
            "portions": 2, "prepared_portion_range": [4, 8],
            "cook_days": ["Monday", "Wednesday", "Friday"], "eat_days": all_days,
            "recurring_batch_accepted": True,
        }
        guidance = {
            "basis": "synthetic explicit basis " + "b" * 970,
            "suitability": "suitable",
            "storage": "synthetic storage guidance " + "s" * 970,
            "reheating": "synthetic reheating guidance " + "r" * 968,
        }
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            await self.call(client, "profile", action="update", changes={"meals": meals})
            exact = []
            for index in range(200, 212):
                saved = (await self.cli({
                    "operation": "recipes", "action": "save",
                    "recipe": synthetic_recipe(index),
                    "idempotency_key": f"oversized-{index}",
                }))["recipe"]
                exact.append({"recipe_ref": {
                    "id": saved["id"], "revision": saved["revision"],
                }})
            candidates = [
                {**candidate, "facts": {"batch_guidance": guidance}}
                for candidate in exact
            ]
            planned = (await self.call(client, "menu", action="plan", planner_input={
                "week": self.week(), "candidates": candidates, "alternatives": 3,
            }))["plan"]
            self.assertEqual(planned["status"], "needs_input")
            self.assertEqual(planned["issues"][0]["code"], "mcp_action_response_too_large")
            self.assertGreater(planned["issues"][0]["projected_wire_chars"], 50000)
            self.assertEqual(planned["issues"][0]["maximum_wire_chars"], 45000)
            self.assertNotIn("save_ref", planned)
            self.assertLess(len(self.last_menu_wire), 4000)

    async def test_oversized_replan_returns_exact_durable_apply_handoff(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            planned = (await self.call(client, "menu", action="plan", planner_input={
                "week": self.week(), "candidates": manifest["planner_candidates"][:7],
            }))["plan"]
            current = (await self.call(
                client, "menu", action="save", planner_ref=planned["save_ref"]
            ))["menu"]

            oversized = synthetic_recipe(900)
            oversized["name"] = "Syntetisk stor erstatningsmiddag"
            oversized["steps"] = [
                f"Syntetisk trinn {index}: " + (chr(97 + index % 26) * 3_500)
                for index in range(45)
            ]
            saved = (await self.cli({
                "operation": "recipes", "action": "save", "recipe": oversized,
                "idempotency_key": "oversized-replan-candidate",
            }))["recipe"]
            candidate = {"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}}
            replacement_date = current["slots"][-1]["date"]
            request = {
                "operation": "menu", "action": "replan_prepare",
                "menu_ref": {key: current[key] for key in ("menu_id", "revision", "digest")},
                "remaining_dates": [replacement_date],
                "planner_input": {"candidates": [candidate]},
            }
            full = await self.cli(request)
            self.assertGreater(len(json.dumps(full, ensure_ascii=False).encode()), 132 * 1024)

            compact = await self.call(client, "menu", **{
                key: value for key, value in request.items() if key != "operation"
            })
            self.assertEqual(compact["replan"]["status"], "prepared")
            self.assertEqual(compact["replan"]["projection"], "apply_arguments_only")
            self.assertTrue(compact["replan"]["details_omitted"])
            self.assertEqual(compact["apply_arguments"], full["apply_arguments"])
            self.assertRegex(compact["apply_arguments"]["replan_ref"], r"^replan_[a-f0-9]{64}$")
            self.assertEqual(
                compact["replan"]["replan_digest"],
                compact["apply_arguments"]["replan_ref"].removeprefix("replan_"),
            )
            self.assertLess(len(self.last_menu_wire), 45_000)
            self.assertIn(candidate, [
                slot["reference"] for slot in compact["replan"]["successor_summary"]["slots"]
            ])

        self.stop_service()
        await self.start_service()
        async with self.client() as client:
            applied = await self.call(client, "menu", **compact["apply_arguments"])
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["projection"], "committed_menu_ref")
            self.assertLess(len(self.last_menu_wire), 45_000)
            self.assertEqual(
                next(slot for slot in applied["menu_summary"]["slots"] if slot["date"] == replacement_date)["reference"],
                candidate,
            )
            current = (await self.cli({"operation": "menu", "action": "get"}))["menu"]
            self.assertEqual(
                applied["menu_ref"],
                {key: current[key] for key in ("menu_id", "revision", "digest")},
            )
            replay = await self.call(client, "menu", **compact["apply_arguments"])
            self.assertTrue(replay["idempotent"])

    async def test_selected_dietary_warnings_remain_specific_and_compact(self):
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            await self.call(client, "profile", action="update", changes={
                "diet": {"rules": [{"kind": "preference", "term": "gulrot"}]},
            })
            planned = (await self.call(client, "menu", action="plan",
                                       planner_input={"week": self.week()}))["plan"]
            self.assertEqual(planned["status"], "planned")
            warnings = planned["candidate_summary"]["warnings"]
            self.assertEqual(len(warnings), 7)
            self.assertTrue(all(any(reason["code"] == "dietary_assessment"
                                    for reason in warning["reasons"])
                                for warning in warnings))
            self.assertIn("gulrot", json.dumps(warnings, ensure_ascii=False))
            self.assertTrue(all(any(reason["code"] == "dietary_preference"
                                    and "gulrot" in json.dumps(reason, ensure_ascii=False)
                                    for reason in slot["reason_contributions"])
                                for slot in planned["selection"]["slots"]))
            self.assertLess(len(self.last_menu_wire), 40000)

    async def test_blocker_heavy_no_plan_keeps_wire_bounded(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(len(manifest["blocker_candidates"]), 12)
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            await self.call(client, "profile", action="update", changes={
                "diet": {"rules": [{"kind": "allergy", "term": "melk"}]},
            })
            planned = (await self.call(client, "menu", action="plan", planner_input={
                "week": self.week(), "candidates": manifest["blocker_candidates"],
            }))["plan"]
            self.assertEqual(planned["status"], "no_plan")
            self.assertEqual(planned["candidate_summary"]["counts"],
                             {"pass": 0, "unknown": 0, "fail": 12})
            self.assertEqual(len(planned["candidate_summary"]["blockers"]), 12)
            self.assertIn("melk", json.dumps(planned["candidate_summary"], ensure_ascii=False))
            self.assertLess(len(self.last_menu_wire), 40000)
            print(json.dumps({"mc41_mcp_blocker_no_plan_wire_chars": len(self.last_menu_wire)}), flush=True)

    async def test_resolved_handoff_supports_unsaved_products_and_feedback(self):
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            planned = (await self.call(client, "menu", action="plan",
                                       planner_input={"week": self.week()}))["plan"]
            handoff = (await self.call(client, "menu", action="resolve_handoff",
                                      planner_ref=planned["save_ref"]))["planner_handoff"]
            self.assertEqual(
                {key: handoff[key] for key in planned["save_ref"]}, planned["save_ref"])
            self.assertEqual(
                [(slot["date"], slot["reference"], slot["name"], slot["portions"])
                 for slot in handoff["selection"]["slots"]],
                [(slot["date"], slot["reference"], slot["name"], slot["portions"])
                 for slot in planned["selection"]["slots"]],
            )
            self.assertIsNone((await self.call(client, "menu"))["menu"])
            # Explicit synthetic pantry coverage avoids any product-provider effects.
            decisions = [{"source": {"collection": "dishes", "recipe_index": i,
                                      "ingredient_index": 0}, "action": "have_all"}
                         for i in range(7)]
            prepared = await self.call(client, "products", action="prepare",
                                       planner_handoff=handoff, ingredient_decisions=decisions)
            self.assertEqual(prepared["apply_arguments"]["planner_selection_ref"], {
                key: handoff[key] for key in ("planner_version", "input_digest", "selection_digest")
            })
            self.assertEqual(prepared["product_plan"]["binding"]["planner_selection"], {
                key: handoff[key] for key in ("planner_version", "input_digest", "selection_digest")
            })
            self.assertEqual(prepared["product_plan"]["requirements"], [])
            accepted = await self.call(client, "feedback", action="accept",
                                      planner_handoff=handoff, idempotency_key="accept-resolved")
            replay = await self.call(client, "feedback", action="accept",
                                    planner_handoff=handoff, idempotency_key="accept-resolved")
            self.assertEqual(replay["event"], accepted["event"])
            fresh = (await self.call(client, "menu", action="plan",
                                     planner_input={"week": self.week()}))["plan"]
            current = (await self.call(client, "menu", action="resolve_handoff",
                                      planner_ref=fresh["save_ref"]))["planner_handoff"]
            slot = current["selection"]["slots"][0]
            await self.call(client, "feedback", action="reject", planner_handoff=current,
                            recipe_key=slot["recipe_key"], reference=slot["reference"],
                            idempotency_key="reject-resolved")
            stale = await client.call_tool("meal_concierge_menu", {
                "action": "resolve_handoff", "planner_ref": fresh["save_ref"]})
            self.assertFalse(stale.is_error, stale)
            self.assertEqual(json.loads(stale.content[0].text)["status"], "rejected")
            self.assertFalse(json.loads(stale.content[0].text)["ok"])
            self.assertIn("stale", stale.content[0].text.lower())
            self.assertIsNone((await self.call(client, "menu"))["menu"])
        calls = [json.loads(line) for line in (self.root / "provider.jsonl").read_text().splitlines()]
        self.assertEqual({row["tool"] for row in calls}, {"recipe_search"})

    async def test_empty_bank_and_unavailable_provider_never_authorize_ai(self):
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            result = (await self.call(client, "menu", action="plan", planner_input={"week": self.week()}))["plan"]
            self.assertNotEqual(result["status"], "planned")
            self.assertFalse(result["discovery"]["ai_fallback_eligible"])
            self.assertEqual(result["discovery"]["suitable_count"], 0)
            self.assertEqual(next(s["status"] for s in result["discovery"]["sources"] if s["source"] == "oda"), "unavailable")
        self.assertEqual(self.bank_counts(), (0, 0))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--relay-mcp":
        raise SystemExit(relay_mcp(Path(sys.argv[2])))
    elif len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]), "--empty" in sys.argv[3:])
    else:
        unittest.main()
