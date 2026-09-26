"""Bounded service-side views for agent clients; raw API results remain available."""

import hashlib
import json
import re
from typing import Any

MCP_MENU_WIRE_BUDGET = 45_000

def _compact_plan_reason(reason: Any) -> dict[str, Any] | None:
    """Return the stable, concise part of a planner scoring reason."""
    if not isinstance(reason, dict) or not isinstance(reason.get("code"), str):
        return None
    compact = {"code": reason["code"]}
    if isinstance(reason.get("weight"), int) and not isinstance(reason.get("weight"), bool):
        compact["weight"] = reason["weight"]
    if "detail" in reason:
        compact["detail"] = _concise_detail(reason["detail"])
    return compact


def _compact_plan_reasons(reasons: Any, *, details: bool = True) -> list[dict[str, Any]]:
    """Aggregate repeated scoring codes while retaining bounded examples."""
    if not isinstance(reasons, list):
        return []
    groups: dict[str, dict[str, Any]] = {}
    signatures: dict[str, set[str]] = {}
    distinct: dict[str, int] = {}
    for reason in reasons:
        compact = _compact_plan_reason(reason)
        if compact is None:
            continue
        code = compact["code"]
        group = groups.setdefault(code, {"code": code, "weight": 0, "count": 0})
        group["weight"] += compact.get("weight", 0)
        group["count"] += 1
        if details and "detail" in compact:
            signature = json.dumps(
                compact["detail"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            seen = signatures.setdefault(code, set())
            if signature not in seen:
                seen.add(signature)
                distinct[code] = distinct.get(code, 0) + 1
                group.setdefault("details", [])
                if len(group["details"]) < 3:
                    group["details"].append(compact["detail"])
    for code, group in groups.items():
        details = group.pop("details", [])
        if group["count"] == 1 and details:
            group["detail"] = details[0]
        elif details:
            group["details"] = details
        omitted = distinct.get(code, 0) - len(details)
        if omitted > 0:
            group["omitted_details"] = omitted
        if group["count"] == 1:
            group.pop("count")
    return list(groups.values())


def _bounded_detail(value: Any, depth: int = 0) -> Any:
    """Bound trusted diagnostic values while preserving their useful leading facts."""
    if isinstance(value, str):
        if len(value) <= 80:
            return value
        return {
            "excerpt": value[:79] + "…",
            "sha256": hashlib.sha256(value.encode()).hexdigest()[:16],
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 2:
        return "additional nested detail omitted"
    if isinstance(value, list):
        items = [_bounded_detail(item, depth + 1) for item in value[:6]]
        if len(value) > 6:
            items.append({"omitted_items": len(value) - 6})
        return items
    if isinstance(value, dict):
        priority = (
            "kind", "term", "condition", "target", "status", "blocked", "basis",
            "value", "values", "minimum", "observed", "finding_id", "detail",
        )
        ordered = [key for key in priority if key in value]
        ordered.extend(key for key in value if key not in ordered)
        keys = ordered[:8]
        compact = {str(key): _bounded_detail(value[key], depth + 1) for key in keys}
        if len(value) > len(keys):
            compact["omitted_fields"] = len(value) - len(keys)
        return compact
    return _bounded_detail(str(value), depth)


def _concise_detail(value: Any) -> Any:
    projected = _bounded_detail(value)
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= 400:
        return projected
    return {"summary": encoded[:399] + "…", "truncated": True}


def _compact_leafy_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Show the count and recipe arithmetic without echoing culinary prose."""
    compact = {
        field: detail[field] for field in ("target_range", "counted_dinners", "unknown_dinners", "status")
        if field in detail
    }
    compact["dinner_assessments"] = [
        {"dinner_index": index, **{
            field: row[field] for field in (
                "date", "source", "assessment", "counts", "ingredient_indices",
                "listed_grams_per_serving", "quantity_evidence", "detail",
            ) if field in row}}
        for index, row in enumerate(detail.get("dinner_assessments", [])[:7])
        if isinstance(row, dict)
    ]
    return compact


def _compact_issue(issue: Any, strict_targets: Any = None) -> Any:
    if not isinstance(issue, dict):
        return _bounded_detail(issue)
    compact = {
        key: issue[key]
        for key in ("code", "status", "target", "required", "eligible", "evaluated")
        if key in issue
    }
    if issue.get("code") == "strict_targets_infeasible" and isinstance(strict_targets, list):
        compact["targets"] = strict_targets
    for key in ("unknown", "detail", "shortages"):
        if key in issue:
            if key == "detail" and issue.get("target") == "leafy_green_days" and isinstance(issue[key], dict):
                compact[key] = _compact_leafy_detail(issue[key])
            else:
                compact[key] = _bounded_detail(issue[key])
    for key in ("required_portions", "available_portions"):
        if key in issue:
            compact[key] = issue[key]
    return compact


def _compact_batch(batch: Any) -> dict[str, Any]:
    if not isinstance(batch, dict):
        return {}
    compact = {
        key: batch[key]
        for key in (
            "source_date", "eating_dates", "batch", "prepared_portions",
            "consumed_at_source", "recipe_key", "name",
        )
        if key in batch
    }
    guidance_value = batch.get("guidance")
    if isinstance(guidance_value, dict):
        compact["guidance"] = {
            key: guidance_value[key]
            for key in ("basis", "suitability", "storage", "reheating")
            if key in guidance_value
        }
    return compact


def _compact_plan_slot(
    slot: Any, *, reasons: bool = True, reason_details: bool = True
) -> dict[str, Any]:
    if not isinstance(slot, dict):
        return {}
    compact = {
        key: slot[key]
        for key in (
            "date", "reference", "recipe_key", "name", "portions", "source_date",
            "kind", "new_shopping_requirements",
        )
        if key in slot
    }
    if reasons:
        compact_reasons = _compact_plan_reasons(
            slot.get("reason_contributions", []), details=reason_details
        )
        if compact_reasons:
            compact["reason_contributions"] = compact_reasons
    return compact


def _compact_plan_selection(selection: Any, *, alternative: bool = False) -> dict[str, Any]:
    """Keep display and warning fields; the save_ref carries exact action state."""
    if not isinstance(selection, dict):
        return {}
    compact = {
        key: selection[key]
        for key in ("selection_digest", "total_score", "soft_relaxations")
        if key in selection
    }
    if "batches" in selection:
        compact["batches"] = [
            _compact_batch(batch)
            for batch in selection.get("batches", [])
        ]
    recurring = "source_slots" in selection
    if recurring:
        compact["slots"] = [
            _compact_plan_slot(slot, reasons=False)
            for slot in selection.get("slots", [])
        ]
    else:
        compact["slots"] = [
            _compact_plan_slot(slot, reason_details=not alternative)
            for slot in selection.get("slots", [])
        ]
    if recurring:
        compact["source_slots"] = [
            _compact_plan_slot(slot, reason_details=not alternative)
            for slot in selection.get("source_slots", [])
        ]
    plan_reasons = _compact_plan_reasons(
        selection.get("plan_reason_contributions", []), details=not alternative
    )
    if plan_reasons:
        compact["plan_reason_contributions"] = plan_reasons
    strict = selection.get("strict_targets")
    if isinstance(strict, dict):
        issues = [
            item for item in strict.get("results", [])
            if isinstance(item, dict) and item.get("status") != "pass"
        ]
        if issues:
            compact["strict_target_issues"] = [_compact_issue(item) for item in issues]
    return compact


def _compact_constraint_reasons(constraints: Any) -> list[dict[str, Any]]:
    if not isinstance(constraints, dict):
        return []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for reason in constraints.get("reasons", []):
        if not isinstance(reason, dict) or reason.get("status") == "pass":
            continue
        key = (str(reason.get("code", "unknown")), str(reason.get("status", "unknown")))
        group = groups.setdefault(key, {
            "code": key[0], "status": key[1], "count": 0, "details": [],
        })
        group["count"] += 1
        if "detail" in reason:
            detail = _concise_detail(reason["detail"])
            signature = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            existing = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for item in group["details"]
            }
            if signature not in existing:
                group["distinct_details"] = group.get("distinct_details", 0) + 1
                if len(group["details"]) < 3:
                    group["details"].append(detail)
    for group in groups.values():
        if not group["details"]:
            group.pop("details")
        distinct = group.pop("distinct_details", 0)
        if distinct > len(group.get("details", [])):
            group["omitted_details"] = distinct - len(group.get("details", []))
    return list(groups.values())


def _candidate_summary(plan: dict[str, Any]) -> dict[str, Any] | None:
    evaluations = plan.get("candidate_evaluations")
    if not isinstance(evaluations, list):
        return None
    counts = {"pass": 0, "unknown": 0, "fail": 0}
    blockers = []
    warnings = []
    selections = [plan.get("selection")]
    selections.extend(
        item.get("selection") for item in plan.get("alternatives", [])
        if isinstance(item, dict)
    )
    selected = set()
    for selection in selections:
        if not isinstance(selection, dict):
            continue
        for field in ("slots", "source_slots"):
            selected.update(
                json.dumps(slot.get("reference"), sort_keys=True, separators=(",", ":"))
                for slot in selection.get(field, [])
                if isinstance(slot, dict) and isinstance(slot.get("reference"), dict)
            )
    for evaluation in evaluations:
        if not isinstance(evaluation, dict):
            continue
        constraints = evaluation.get("hard_constraints")
        status = constraints.get("status") if isinstance(constraints, dict) else None
        if status in counts:
            counts[status] += 1
        reasons = _compact_constraint_reasons(constraints)
        item = {
                **({"reference": evaluation["reference"]} if "reference" in evaluation else {}),
                "status": status or "unknown",
                "reasons": reasons,
        }
        if status != "pass":
            blockers.append(item)
        elif reasons and json.dumps(
            evaluation.get("reference"), sort_keys=True, separators=(",", ":")
        ) in selected:
            warnings.append(item)
    return {
        "total": sum(counts.values()), "counts": counts, "blockers": blockers,
        **({"warnings": warnings} if warnings else {}),
    }


def _compact_discovery(discovery: Any) -> dict[str, Any] | None:
    if not isinstance(discovery, dict):
        return None
    compact = {
        key: value for key, value in discovery.items()
        if key not in {"unknown", "rejected"}
    }
    unknown = []
    for recipe in discovery.get("unknown", []):
        if not isinstance(recipe, dict):
            continue
        unknown.append({
            **{key: recipe[key] for key in ("name", "recipe_ref", "discovery_ref") if key in recipe},
            "reasons": _compact_constraint_reasons(recipe.get("hard_constraints")),
        })
    compact["unknown_summary"] = {
        "count": len(discovery.get("unknown", [])),
        "examples": unknown[:6],
        **({"omitted": len(unknown) - 6} if len(unknown) > 6 else {}),
    }

    reason_counts: dict[tuple[str, str], int] = {}
    rejected = discovery.get("rejected", [])
    for recipe in rejected:
        constraints = recipe.get("hard_constraints") if isinstance(recipe, dict) else None
        for reason in _compact_constraint_reasons(constraints):
            key = (str(reason.get("code", "unknown")), str(reason.get("status", "unknown")))
            reason_counts[key] = reason_counts.get(key, 0) + int(reason.get("count", 1))
    compact["rejected_summary"] = {
        "count": len(rejected) if isinstance(rejected, list) else 0,
        "reasons": [
            {"code": code, "status": status, "count": count}
            for (code, status), count in sorted(reason_counts.items())
        ],
    }
    return compact


def _menu_plan_projection(plan: dict[str, Any]) -> dict[str, Any]:
    """Project full planner evidence into the bounded MCP presentation contract."""
    omitted = {
        "canonical_input", "candidate_evaluations", "cooking_experiences", "request",
        "selection", "selections", "save_handoff", "save_handoffs", "alternatives",
        "work_limits", "discovery", "issues",
    }
    projected = {
        key: plan[key] for key in ("status", "save_ref")
        if key in plan
    }
    if "selection" in plan:
        projected["selection"] = _compact_plan_selection(plan["selection"])
    if "alternatives" in plan:
        projected["alternatives"] = [
            {
                **({"save_ref": item["save_ref"]} if isinstance(item, dict) and "save_ref" in item else {}),
                **({"selection": _compact_plan_selection(item["selection"], alternative=True)}
                   if isinstance(item, dict) and "selection" in item else {}),
            }
            for item in plan["alternatives"]
        ]
    projected.update({
        key: value for key, value in plan.items()
        if key not in omitted and key not in projected
    })
    request = plan.get("request")
    if isinstance(plan.get("issues"), list):
        strict_targets = request.get("strict_targets") if isinstance(request, dict) else None
        projected["issues"] = [
            _compact_issue(issue, strict_targets)
            for issue in plan["issues"]
        ]
    summary = _candidate_summary(plan)
    if summary is not None:
        projected["candidate_summary"] = summary
    work_limits = plan.get("work_limits")
    if isinstance(work_limits, dict):
        projected["work_summary"] = {
            **({"explored_states": plan["explored_states"]} if "explored_states" in plan else {}),
            **{key: work_limits[key] for key in ("maximum_candidates", "maximum_explored_states")
               if key in work_limits},
        }
    discovery = _compact_discovery(plan.get("discovery"))
    if discovery is not None:
        projected["discovery"] = discovery
    return projected


def _mcp_text_wire_chars(text: str) -> int:
    """Conservatively measure the newline-framed JSON-RPC tool result."""
    wrapper = {
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }
    return len(json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"))) + 1


def _bounded_menu_plan_result(result: dict[str, Any]) -> dict[str, Any]:
    projected = {**result, "plan": _menu_plan_projection(result["plan"])}
    text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    wire_chars = _mcp_text_wire_chars(text)
    if wire_chars < MCP_MENU_WIRE_BUDGET:
        return projected
    return {
        **{key: value for key, value in result.items() if key != "plan"},
        "plan": {
            "status": "needs_input",
            "issues": [{
                "code": "mcp_action_response_too_large",
                "projected_wire_chars": wire_chars,
                "maximum_wire_chars": MCP_MENU_WIRE_BUDGET,
                "suggestions": [
                    "request fewer alternatives",
                    "remove nonessential candidate facts or candidates",
                    "omit candidates to use bounded automatic discovery",
                ],
            }],
        },
    }


def _menu_successor_summary(successor: Any) -> dict[str, Any]:
    if not isinstance(successor, dict):
        return {"week": None, "slots": []}
    import menu_planning as mp
    slots = [{
        **{key: slot[key] for key in (
            "date", "meal_type", "portions", "recipe_key", "reference", "kind", "source_slot_id"
        ) if key in slot},
        **({"name": mp.recipe_for_slot(successor, slot, allow_stale=True).get("name")}
           if successor.get("dishes") else {}),
    } for slot in successor.get("slots", []) if isinstance(slot, dict)]
    return {"week": successor.get("week"), "slots": slots}


def _nonprepared_replan_projection(
    result: dict[str, Any], *, wire_chars: int,
) -> dict[str, Any] | None:
    replan = result.get("replan")
    if not isinstance(replan, dict) or replan.get("status") == "prepared":
        return None
    compact = {
        key: replan[key]
        for key in (
            "status", "reason", "slot_id", "prepared_portions",
            "consumed_at_source", "required_portions", "available_portions",
        )
        if key in replan
    }
    if "reason" in compact:
        compact["reason"] = _bounded_detail(compact["reason"])
    plan = replan.get("plan")
    if isinstance(plan, dict):
        compact["plan"] = _menu_plan_projection(plan)
    minimums = replan.get("minimum_evaluation")
    if isinstance(minimums, dict):
        compact["minimum_evaluation"] = {
            key: _bounded_detail(minimums[key])
            for key in ("status", "complete_menu", "targets", "unknown", "failures")
            if key in minimums
        }
        results = minimums.get("results")
        if isinstance(results, list):
            compact["minimum_evaluation"]["results"] = [{
                key: (_bounded_detail(row[key]) if key == "detail" else row[key])
                for key in ("target", "status", "detail") if key in row
            } for row in results[:8] if isinstance(row, dict)]
    if "next" in replan:
        compact["next"] = _bounded_detail(replan["next"])
    compact.update({
        "projection": "nonprepared_summary",
        "details_omitted": True,
        "projected_wire_chars": wire_chars,
        "maximum_wire_chars": MCP_MENU_WIRE_BUDGET,
    })
    projected = {
        "replan": compact,
        **({key: _bounded_detail(value) for key, value in result.items()
            if key not in {"replan", "apply_arguments"} and key in {"status", "reason"}}),
    }
    text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    if _mcp_text_wire_chars(text) < MCP_MENU_WIRE_BUDGET:
        return projected
    issues = plan.get("issues") if isinstance(plan, dict) else None
    compact.pop("plan", None)
    if isinstance(plan, dict):
        compact["plan"] = {
            "status": plan.get("status"),
            "issues": [_compact_issue(issue, plan.get("strict_targets")) for issue in issues[:24]]
            if isinstance(issues, list) else [],
            **({"issue_count": len(issues)} if isinstance(issues, list) else {}),
            **({"candidate_summary": _candidate_summary(plan)} if _candidate_summary(plan) is not None else {}),
        }
    return {"replan": compact}


def _bounded_menu_replan_result(result: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    wire_chars = _mcp_text_wire_chars(text)
    if wire_chars < MCP_MENU_WIRE_BUDGET:
        return result
    nonprepared = _nonprepared_replan_projection(result, wire_chars=wire_chars)
    if nonprepared is not None:
        return nonprepared
    prepared = result.get("replan")
    arguments = result.get("apply_arguments")
    if (
        not isinstance(prepared, dict) or prepared.get("status") != "prepared"
        or not isinstance(arguments, dict) or set(arguments) != {"action", "replan_ref"}
        or arguments.get("action") != "replan_apply"
        or not isinstance(arguments.get("replan_ref"), str)
        or re.fullmatch(r"replan_[a-f0-9]{64}", arguments["replan_ref"]) is None
        or prepared.get("replan_digest") != arguments["replan_ref"].removeprefix("replan_")
    ):
        return {
            "replan": {
                "status": "rejected", "reason": "invalid_prepared_replan_continuation",
                "projected_wire_chars": wire_chars,
                "maximum_wire_chars": MCP_MENU_WIRE_BUDGET,
            },
        }
    successor = prepared.get("successor")
    comparison = prepared.get("shopping_comparison")
    projected = {
        "replan": {
            "status": "prepared", "projection": "apply_arguments_only",
            "details_omitted": True, "replan_digest": prepared["replan_digest"],
            "source": prepared.get("source"),
            "remaining_dates": prepared.get("remaining_dates"),
            "successor_summary": _menu_successor_summary(successor),
            **({"shopping_comparison_counts": {
                key: len(value) for key, value in comparison.items() if isinstance(value, list)
            }} if isinstance(comparison, dict) else {}),
            "projected_wire_chars": wire_chars,
            "maximum_wire_chars": MCP_MENU_WIRE_BUDGET,
        },
        "apply_arguments": arguments,
        "next": (
            "Call meal_concierge_menu with these unchanged apply_arguments. The service will "
            "resolve the durable handoff, regenerate the full replan, and require the same "
            "menu state and digest before saving the successor."
        ),
    }
    projected_text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    if _mcp_text_wire_chars(projected_text) < MCP_MENU_WIRE_BUDGET:
        return projected
    return {
        "replan": {
            "status": "prepared", "projection": "apply_arguments_only",
            "details_omitted": True, "replan_digest": prepared["replan_digest"],
            "projected_wire_chars": wire_chars,
            "maximum_wire_chars": MCP_MENU_WIRE_BUDGET,
        },
        "apply_arguments": arguments,
        "next": "Call meal_concierge_menu with these unchanged apply_arguments.",
    }


def _bounded_menu_replan_apply_result(result: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    wire_chars = _mcp_text_wire_chars(text)
    if wire_chars < MCP_MENU_WIRE_BUDGET:
        return result
    menu = result.get("menu")
    if not isinstance(menu, dict) or any(key not in menu for key in ("menu_id", "revision", "digest")):
        return {
            "status": "applied_response_too_large",
            "details_omitted": True,
            "projected_wire_chars": wire_chars,
            "maximum_wire_chars": MCP_MENU_WIRE_BUDGET,
        }
    comparison = result.get("shopping_comparison")
    return {
        "status": "applied",
        "projection": "committed_menu_ref",
        "details_omitted": True,
        "menu_ref": {key: menu[key] for key in ("menu_id", "revision", "digest")},
        "menu_summary": _menu_successor_summary(menu),
        **({"supersedes": menu["supersedes"]} if "supersedes" in menu else {}),
        **({"shopping_comparison_counts": {
            key: len(value) for key, value in comparison.items() if isinstance(value, list)
        }} if isinstance(comparison, dict) else {}),
        "projected_wire_chars": wire_chars,
        "maximum_wire_chars": MCP_MENU_WIRE_BUDGET,
        "next": "Use menu_ref for product preparation or other exact follow-up operations.",
    }


# Ordinary service-side views

def _fields(value: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: value[name] for name in names if isinstance(value, dict) and name in value}


def _page(rows: Any, offset: int, limit: int, name: str) -> dict[str, Any]:
    values = rows if isinstance(rows, list) else []
    end = offset
    size = 0
    page = []
    while end < len(values) and end < offset + limit:
        item_size = len(json.dumps(values[end], ensure_ascii=False, separators=(",", ":")))
        if size + item_size > 9_000:
            if end == offset:
                page.append(values[end])
                end += 1
            break
        size += item_size
        page.append(values[end])
        end += 1
    return {name: page, "total": len(values), "offset": offset,
            **({"next_offset": end} if end < len(values) else {})}


def _finding(finding: Any) -> dict[str, Any]:
    return _fields(finding, ("finding_id", "kind", "term", "condition", "blocked",
                             "unknown", "recipe_key", "product_ref", "item"))


def _display_text(value: Any, maximum: int = 1_000) -> Any:
    if not isinstance(value, str) or len(value) <= maximum:
        return value
    return {"excerpt": value[:maximum], "omitted_characters": len(value) - maximum}


def _assessment_view(assessment: Any, offset: int, limit: int, section: str) -> dict[str, Any]:
    if not isinstance(assessment, dict):
        return {}
    view = _fields(assessment, ("menu_ref", "ready", "status", "dinner_days", "portions",
                                "issue_count", "ingredients_needing_decision_count", "assessment_digest", "scope"))
    findings = assessment.get("dietary_assessments")
    findings = [_finding(item) for item in findings] if isinstance(findings, list) else []
    deviations = [item for item in findings if item.get("condition") not in {None, "compatible_label"}]
    view["dietary_findings_count"] = len(findings)
    view["dietary_deviations_or_unknown_count"] = len(deviations)
    if section in {"summary", "issues"}:
        issues = [_fields(item, ("code", "expected", "actual", "recipe_key", "slot_id",
                                 "ingredient_index", "item"))
                  for item in (assessment.get("issues") or [])]
        view["issues"] = _page(issues, offset, limit, "items")
        view["dietary_deviations_or_unknown"] = _page(deviations, offset, limit, "items")
        if section == "issues":
            view["ingredients_needing_quantity_or_pantry_decision"] = _page(
                assessment.get("ingredients_needing_quantity_or_pantry_decision"), offset, limit, "items")
    return view


def _menu_view(action: str, result: dict[str, Any], offset: int, limit: int, section: str) -> dict[str, Any]:
    view = {"projection": "agent", "operation": "menu", "action": action}
    view.update(_fields(result, ("status", "reason", "next", "idempotent", "locked", "slot_id",
                                 "slot_replan_available", "menu_ref", "added_slot")))
    if isinstance(result.get("added_slots"), list):
        view["added_slots"] = _page(result["added_slots"], offset, limit, "items")
    menu = result.get("menu")
    if isinstance(menu, dict):
        view["menu_ref"] = _fields(menu, ("menu_id", "revision", "digest"))
        view.update(_fields(menu, ("week", "phase", "weekly_plan_complete", "order_id", "supersedes")))
        view["menu"] = {**view["menu_ref"], **_fields(menu, ("week", "phase"))}
        if menu.get("output_language"):
            view["output_language"] = menu["output_language"]
            view["recipe_languages"] = _page([
                {"collection": group, "index": index, **_fields(recipe.get("presentation"), ("name", "requested_language", "resolved_language", "fallback"))}
                for group in ("dishes", "salads") for index, recipe in enumerate(menu.get(group, []))
            ], offset, limit, "items")
        summary = _menu_successor_summary(menu)
        raw_slots = [slot for slot in menu.get("slots", []) if isinstance(slot, dict)]
        slots = [{**_fields(slot, ("slot_id",)), **item}
                 for slot, item in zip(raw_slots, summary["slots"])]
        if not slots:
            slots = [{"collection": collection, "index": index,
                      **_fields(recipe, ("name", "portions", "recipe_key", "recipe_ref", "library_recipe_ref", "discovery_ref"))}
                     for collection in ("dishes", "salads")
                     for index, recipe in enumerate(menu.get(collection, [])) if isinstance(recipe, dict)]
        view["slots"] = _page(slots, offset, limit, "items")
        handoff = menu.get("planner_selection") or menu.get("replan_selection") or {}
        selection = handoff.get("selection") if isinstance(handoff, dict) else None
        preference_findings = []
        if isinstance(selection, dict):
            for slot in selection.get("slots", []):
                if not isinstance(slot, dict):
                    continue
                for reason in slot.get("reason_contributions", []):
                    if isinstance(reason, dict) and reason.get("code") == "dietary_preference":
                        preference_findings.append({"date": slot.get("date"),
                                                    **_finding(reason.get("detail"))})
        if preference_findings:
            view["preference_deviations"] = _page(preference_findings, offset, limit, "items")
    elif "menu" in result:
        view["menu"] = None
    assessment = result.get("assessment")
    if isinstance(assessment, dict):
        view["assessment"] = _assessment_view(assessment, offset, limit, section)
    for key in ("minimum_evaluation", "evaluation"):
        if isinstance(result.get(key), dict):
            view[key] = _fields(result[key], ("status", "enforced_status", "complete_menu", "strict_targets", "reason"))
            if key == "minimum_evaluation":
                leafy = next((row for row in result[key].get("results", [])
                              if isinstance(row, dict) and row.get("target") == "leafy_green_days"), None)
                if leafy and isinstance(leafy.get("detail"), dict):
                    view[key]["leafy_green_days"] = {
                        "status": leafy.get("status"),
                        "detail": _compact_leafy_detail(leafy["detail"]),
                    }
    comparison = result.get("shopping_comparison")
    if isinstance(comparison, dict):
        view["shopping_comparison_counts"] = {key: len(value) for key, value in comparison.items()
                                               if isinstance(value, list)}
    if isinstance(result.get("locks"), list):
        view["locks"] = result["locks"][:20]
        view["lock_count"] = len(result["locks"])
    if isinstance(result.get("batch_dependencies"), list):
        view["batch_dependency_count"] = len(result["batch_dependencies"])
        if section == "issues":
            view["batch_dependencies"] = _page(result["batch_dependencies"], offset, limit, "items")
    if isinstance(result.get("feedback_targets"), list):
        view["feedback_targets"] = _page(result["feedback_targets"], offset, limit, "items")
    return view


def _status_view(result: dict[str, Any], offset: int, limit: int, section: str) -> dict[str, Any]:
    view = {"projection": "agent", "operation": "status"}
    view.update(_fields(result, ("household", "configuration_status", "menu_phase", "menu_id",
                                 "cart_plan_status", "pending_checkout_status", "pending_cancellation_status",
                                 "order_change_status", "confirmation_policy", "auto_checkout", "currency")))
    view["integration"] = _fields(result.get("integration"), ("status", "provider", "reason"))
    server = (result.get("integration") or {}).get("server")
    if isinstance(server, dict):
        view["integration"]["server"] = _fields(server, ("name", "version"))
    workflow = result.get("workflow")
    if isinstance(workflow, dict):
        view["workflow"] = _fields(workflow, ("cart_status", "delivery_status", "checkout_status",
                                              "email_status", "next_action"))
        view["workflow"]["menu"] = _assessment_view(workflow.get("menu"), offset, limit, section)
    if isinstance(result.get("store_readiness"), dict):
        view["store_readiness"] = _fields(result["store_readiness"], ("status", "payment", "reason", "next"))
    return view


def _cart_plan_items(plan: dict[str, Any], offset: int, limit: int) -> dict[str, Any]:
    rows = []
    for item in plan.get("items", []):
        if not isinstance(item, dict):
            continue
        row = _fields(item, ("product_id", "name", "start_quantity", "required_quantity",
                             "target_quantity", "confirmed_added_quantity", "supplemental_quantity",
                             "live_quantity", "extra_quantity", "missing_quantity", "unresolved_start_quantity"))
        row["name"] = _display_text(row.get("name"), 500)
        rows.append(row)
    return _page(rows, offset, limit, "items")


def _cart_view(action: str, result: dict[str, Any], offset: int, limit: int, section: str) -> dict[str, Any]:
    view = {"projection": "agent", "operation": "cart", "action": action}
    view.update(_fields(result, ("status", "reason", "next", "cart_digest", "cart_write_pending",
                                 "cleared", "ensured", "reconciled", "synced", "stopped", "idempotent",
                                 "changed", "outcome_unknown", "cart_reconciliation_required", "decision",
                                 "product_plan_stale", "menu_binding_stale", "order_id", "applied_operations",
                                 "excluded_product_ids", "accepted_missing_product_ids", "default_suggestion")))
    if isinstance(result.get("cart_plan"), dict):
        view["cart_plan"] = _fields(result["cart_plan"], ("provider", "status", "menu_ref", "cart_digest", "approved"))
        view["cart_plan"]["items"] = _cart_plan_items(result["cart_plan"], offset, limit)
    cart = result.get("cart") if isinstance(result.get("cart"), dict) else result
    try:
        from core import HouseholdError, cart_summary
        summary = cart_summary(cart)
    except HouseholdError:
        # A provider can omit its total. Preserve the write outcome without
        # exposing raw provider internals or minting a writable cart digest.
        view["cart_normalization"] = "unavailable"
        return view
    view.update(_fields(summary, ("count", "total", "delivery", "amounts")))
    lines = [_fields(item, ("product_id", "name", "quantity", "price"))
             for item in summary.get("items", []) if isinstance(item, dict)]
    for line in lines:
        line["name"] = _display_text(line.get("name"), 500)
    if section in {"summary", "items"}:
        view["lines"] = _page(lines, offset, limit, "items")
    else:
        view["line_count"] = len(lines)
    if isinstance(result.get("meal_concierge_cart_plan"), dict):
        view["meal_concierge_cart_plan"] = _fields(result["meal_concierge_cart_plan"],
                                                   ("provider", "status", "menu_ref", "cart_digest", "approved"))
        view["meal_concierge_cart_plan"]["items"] = _cart_plan_items(
            result["meal_concierge_cart_plan"], offset, limit)
    return view


def _order_row(order: Any, *, exact: bool, tracking: Any = None) -> dict[str, Any]:
    if not isinstance(order, dict):
        return {"observed_status": "unknown", "payment_status": "unknown"}
    row = _fields(order, ("id", "order_id", "order_number", "orderNumber", "currency", "delivery_date",
                          "deliveryDate", "delivery_window", "deliverySlot", "deliverySlotDisplay",
                          "subtotal", "total", "grossAmount"))
    row["normalized_order_id"] = next((str(order[key]) for key in ("order_id", "order_number", "orderNumber", "id")
                                       if order.get(key) is not None), None)
    if isinstance(row.get("deliverySlot"), dict):
        row["deliverySlot"] = _fields(row["deliverySlot"], ("id", "name", "date", "start_at", "end_at"))
    observed = order.get("status") or order.get("orderStatus") or "unknown"
    row["observed_status"] = observed
    row["tracking_status"] = (tracking.get("status") or "unknown") if isinstance(tracking, dict) else "not_read"
    row["payment_status"] = (order.get("payment_status") or order.get("paymentStatus") or "unknown") if exact else "unknown"
    if isinstance(tracking, dict):
        row["tracking"] = _fields(tracking, ("order_id", "orderNumber", "status", "delivery_date",
                                              "deliveryDate", "delivery_window"))
    row["cancelled"] = any(str(value).casefold().strip() in {
        "cancelled", "canceled", "kansellert", "cancelled_by_customer", "canceled_by_customer"}
                           for value in (observed, row["tracking_status"]))
    if not exact:
        row["exact_read_required"] = True
    return row


def _order_items(order: Any, offset: int, limit: int) -> dict[str, Any]:
    if not isinstance(order, dict):
        return {"available": False, "total": None}
    source = next((key for key in ("products", "items") if isinstance(order.get(key), list)), None)
    if source is None:
        return {"available": False, "total": None}
    rows = []
    for index, item in enumerate(order[source]):
        if not isinstance(item, dict):
            rows.append({"index": index, "details_unavailable": True})
            continue
        product = item.get("product") if isinstance(item.get("product"), dict) else item
        product_id = next((product[key] for key in ("id", "product_id", "productId")
                           if product.get(key) is not None), None)
        name = product.get("name") or product.get("product_name") or item.get("name") or item.get("identity")
        rows.append({"index": index, "product_id": product_id,
                     "name": _display_text(name, 500), "quantity": item.get("quantity"),
                     "price": item.get("totalGrossAmount", item.get("total", item.get("price", product.get("price"))))})
    return {"available": True, "source": source, **_page(rows, offset, limit, "items")}


def _orders_view(action: str, result: dict[str, Any], offset: int, limit: int, section: str) -> dict[str, Any]:
    view = {"projection": "agent", "operation": "orders", "action": action}
    if action == "get":
        view["order"] = _order_row(result.get("order"), exact=True, tracking=result.get("tracking"))
        view["order_items"] = _order_items(result.get("order"), offset, limit)
        if isinstance(result.get("payment"), dict):
            view["payment"] = _fields(result["payment"], ("provider_status", "authorization", "charge", "source"))
        return view
    orders = result.get("orders")
    rows = [_order_row(item, exact=False) for item in orders] if isinstance(orders, list) else []
    view["orders"] = _page(rows, offset, limit, "items") if section in {"summary", "items"} else {"total": len(rows)}
    view["next"] = "Use orders get with the exact order ID before treating delivery or payment as current."
    return view


def _recipe_ingredient(item: Any, index: int, *, provenance: bool = False) -> dict[str, Any]:
    value = {"index": index, **_fields(item, ("item", "quantity", "unit", "amount", "original_text",
                                              "optional", "pantry", "notes"))}
    evidence = item.get("evidence") if isinstance(item, dict) else None
    if isinstance(evidence, dict):
        fields = ("basis", "input", "assumptions", "conversion", "calculation", "acceptance", "project_review") if provenance else ("basis", "assumptions")
        value["evidence"] = {field: _fields(detail, fields)
                             for field, detail in evidence.items() if field in {"quantity", "unit"}}
        value["estimate_or_unknown"] = any(detail.get("basis") in {"estimate", "unknown"}
                                           for detail in evidence.values() if isinstance(detail, dict))
    return value


def _recipe_times(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    known = ("active_minutes", "prep_minutes", "cook_minutes", "total_minutes",
             "passive_minutes", "preparation_minutes", "cooking_minutes")
    result = {key: value[key] for key in known if key in value and
              (type(value[key]) in {int, float} or isinstance(value[key], str) and len(value[key]) <= 100)}
    if len(result) != len(value):
        result["other_fields_omitted"] = len(value) - len(result)
    return result


def _recipe_view(action: str, result: dict[str, Any], offset: int, limit: int, section: str) -> dict[str, Any]:
    view = {"projection": "agent", "operation": "recipes", "action": action}
    view.update(_fields(result, ("discovery_ref", "recipe_digest", "source_identity", "suggested_status",
                                 "personal_entry_created", "persisted", "cache", "readiness")))
    recipe = result.get("recipe")
    if not isinstance(recipe, dict):
        return view
    view.update(_fields(recipe, ("name", "schema_version", "portions", "scaled_from_portions",
                                 "recipe_ref", "library_recipe_ref", "recipe_digest", "recipe_key",
                                 "status", "revision", "notes", "storage", "reheating", "language", "available_languages", "source_text_digest")))
    view["times"] = _recipe_times(recipe.get("times"))
    if "recipe_ref" not in view and isinstance(recipe.get("id"), str) and type(recipe.get("revision")) is int:
        view["recipe_ref"] = {"id": recipe["id"], "revision": recipe["revision"]}
    view["source_schema_version"] = recipe.get("schema_version")
    view["display_portions"] = recipe.get("portions")
    view["original_portions"] = recipe.get("scaled_from_portions", recipe.get("portions"))
    view["source"] = _fields(recipe.get("source"), ("kind", "publisher", "title", "author", "url",
                                                   "external_id", "relationship", "original"))
    view["source_provider"] = recipe.get("source_provider")
    view["rights"] = _fields(recipe.get("rights"), ("storage", "credit", "license", "license_url"))
    if "recipe_digest" not in view:
        view["recipe_digest"] = recipe.get("recipe_digest")
    if action != "adapt":
        view["original_digest"] = view["recipe_digest"]
    ingredients = [_recipe_ingredient(item, index, provenance=section == "provenance")
                   for index, item in enumerate(recipe.get("ingredients", []))]
    steps = [{"index": index, "text": step}
             for index, step in enumerate(recipe.get("steps", []))]
    presentation = recipe.get("presentation")
    if isinstance(presentation, dict):
        view["presentation"] = _fields(presentation, ("name", "notes", "storage", "reheating", "requested_language", "resolved_language", "fallback"))
        if section in {"summary", "ingredients"}:
            view["presentation"]["ingredients"] = _page(presentation.get("ingredients"), offset, limit, "items")
        if section in {"summary", "steps"}:
            view["presentation"]["steps"] = _page(presentation.get("steps"), offset, limit, "items")
    view["ingredient_count"] = len(ingredients)
    view["step_count"] = len(steps)
    if section in {"summary", "ingredients"}:
        view["ingredients"] = _page(ingredients, offset, limit, "items")
    if section in {"summary", "steps"}:
        view["steps"] = _page(steps, offset, limit, "items")
    if section == "provenance":
        view["portions_evidence"] = _fields(recipe.get("portions_evidence"),
                                            ("basis", "input", "assumptions", "calculation"))
        view["yield"] = _fields(recipe.get("yield"), ("original_text", "quantity", "unit", "evidence"))
        view["ingredient_provenance"] = _page(ingredients, offset, limit, "items")
    if section == "issues":
        issues = [item for item in ingredients if item.get("estimate_or_unknown")]
        view["ingredient_estimates_or_unknowns"] = _page(issues, offset, limit, "items")
        view["readiness"] = result.get("readiness", recipe.get("readiness"))
    return view


def _project_agent_result_once(operation: str, action: str | None, result: dict[str, Any], *,
                               offset: int, limit: int, section: str) -> dict[str, Any]:
    if result.get("status") == "configuration_required" or ("question" in result and "current" in result):
        return {"projection": "agent", **result}
    if result.get("ok") is False or result.get("status") in {"rejected", "unavailable"}:
        return {"projection": "agent", **_fields(result, ("ok", "status", "error", "reason", "next",
                                                    "provider", "retryable"))}
    if operation == "menu" and action == "plan" and isinstance(result.get("plan"), dict):
        return {"projection": "agent", **_bounded_menu_plan_result(result)}
    if operation == "menu" and action == "replan_prepare" and isinstance(result.get("replan"), dict):
        return {"projection": "agent", **_bounded_menu_replan_result(result)}
    if operation == "status":
        return _status_view(result, offset, limit, section)
    if operation == "menu" and action in {None, "get", "assess", "save", "replan_apply", "add_slot", "edit_slots", "lock", "batch_apply"}:
        return _menu_view(action or "get", result, offset, limit, section)
    if operation == "cart":
        return _cart_view(action or "get", result, offset, limit, section)
    if operation == "orders" and action in {None, "list", "get"}:
        return _orders_view(action or "list", result, offset, limit, section)
    if operation == "recipes" and action in {"get", "resolve", "detail", "adapt"}:
        return _recipe_view(action, result, offset, limit, section)
    return result


def project_agent_result(operation: str, action: str | None, result: dict[str, Any], *,
                         offset: int = 0, limit: int = 10, section: str = "summary") -> dict[str, Any]:
    """Return an operation-specific view below the measured MCP wire budget."""
    page_limit = limit
    while True:
        view = _project_agent_result_once(operation, action, result, offset=offset,
                                          limit=page_limit, section=section)
        if (page_limit == 1 or _mcp_text_wire_chars(json.dumps(view, ensure_ascii=False, separators=(",", ":")))
                < MCP_MENU_WIRE_BUDGET):
            return view
        page_limit = max(1, page_limit // 2)
