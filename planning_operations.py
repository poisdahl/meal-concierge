"""Household menu, cooking feedback, product selection and cart operations.

Application owns shared state and locks; these methods run on that same instance.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import math
import re
import secrets
import time
from typing import Any, Mapping
from core import HouseholdError, cart_summary
from meny import MAX_CART_CLICKS, MENY_CART_TIMEOUT, MenyCartStoppedError
from recipes import RecipeError, normalize_recipe, prepare_recipe_input, validate_recipe_image, recipe_key, scale_recipe, validate_week
from recipes import recipe_provider_problem, RECIPE_CATEGORIES
from recipe_selection import history_source_index, family_history_usage, compact_candidate
from planner import _validate_request, normalize_candidate_facts
from planner import MAX_CANDIDATES, MAX_HISTORY_RECORDS, PLANNER_VERSION, PlannerError, plan_week, saved_menu_minimum_evaluation
from product_planner import normalize_available_ingredients
from product_planner import MAX_ALTERNATIVE_REQUIREMENTS, MAX_CANDIDATES_PER_REQUIREMENT, MAX_REQUIREMENTS, normalize_approvals, ingredient_search, build_product_plan, cart_requirements as prepared_cart_requirements, partial_cart_requirements, partial_product_plan_digest, menu_requirements as exact_menu_requirements, validate_product_plan, product_plan_digest
from product_observations import MAX_PRODUCTS
import menu_planning as mp
import planning_feedback as pf
from planning_assessment import assess_menu, feedback_targets
from service_common import oda_order_quantities
import batch_planning as bp
from recipe_libraries import MAX_LIBRARY_RECIPE_KEY, RecipeLibraryError, library_recipe_key, library_recipe_key_aliases, validate_library_recipe_ref
from service_common import (
    MAX_EMAIL_HTML_BYTES,
    MAX_MENU_BYTES,
    MAX_REQUEST,
    UNRESOLVED_CHECKOUT_STATUSES,
    bounded_limit,
    canonical,
    menu_digest,
    menu_email_html,
    scheduled_occurrence,
    scheduler_settings_digest,
    validate_schedule
)


PRODUCT_OPERATION_TIMEOUT = 240
PRODUCT_BUILD_RESERVE = 5
REPLAN_REF_PATTERN = re.compile(r"replan_[a-f0-9]{64}\Z")
PRODUCT_PLAN_REF_PATTERN = re.compile(r"productplan_[A-Za-z0-9_-]{16,32}\Z")
MAX_PREPARED_REPLANS = 32


class PlanningOperations:
    @staticmethod
    def _feedback_slot(state: Mapping[str, Any], value: Any, *, allow_predecessor: bool = False) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {"menu_ref", "slot_id", "recipe_key", "reference"}:
            raise HouseholdError("feedback target needs exact menu_ref, slot_id, recipe_key and reference")
        menu = state.get("menu")
        if not isinstance(menu, Mapping):
            raise HouseholdError("feedback has no active structured menu")
        if canonical(mp.menu_ref(menu)) != canonical(value["menu_ref"]):
            if not allow_predecessor or canonical(menu.get("supersedes")) != canonical(value["menu_ref"]):
                raise HouseholdError("feedback target is not the exact current menu or direct predecessor")
            source = value["menu_ref"]
            menu = state["menu_planning"]["history"].get(f'{source["menu_id"]}:{source["revision"]}')
            if not isinstance(menu, Mapping) or canonical(mp.menu_ref(menu)) != canonical(source):
                raise HouseholdError("feedback predecessor snapshot is unavailable")
        slot = mp.slot_by_id(menu, value["slot_id"])
        if value["recipe_key"] != slot["recipe_key"] or canonical(value["reference"]) != canonical(slot["reference"]):
            raise HouseholdError("feedback recipe reference does not match its exact slot")
        return deepcopy(slot)

    def _feedback(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "inspect")
        # The socket server validates the transport contract before dispatch.
        allowed = {"operation", "contract", "action", "planner_handoff", "target", "from_target", "to_target",
                   "recipe_key", "reference", "event_id", "scope", "reason", "idempotency_key", "view", "limit", "cursor", "experience"}
        if set(request).difference(allowed):
            raise HouseholdError("feedback request has unknown fields")
        snapshot = self.store.read()
        today = self._household_today(snapshot).isoformat()
        events = snapshot["planning_feedback"]
        if action == "inspect":
            view = request.get("view") or "events"
            if view not in {"events", "signals"}:
                raise HouseholdError("feedback inspect view must be events or signals")
            limit = request.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 25:
                raise HouseholdError("feedback inspect limit must be one to 25")
            effective = pf.effective(events, today)
            digest = mp.digest({"events": events, "as_of_date": today})
            keys = [e["event_id"] for e in events] if view == "events" else sorted(effective["signals"])
            start = 0
            cursor = request.get("cursor")
            if cursor is not None:
                if not isinstance(cursor, Mapping) or set(cursor) != {"digest", "view", "after"} or cursor["digest"] != digest or cursor["view"] != view or cursor["after"] not in keys:
                    raise HouseholdError("feedback cursor changed or expired; restart inspection")
                start = keys.index(cursor["after"]) + 1
            page_keys = keys[start:start+limit]
            page = events[start:start+limit] if view == "events" else []
            recipe_keys = {c["recipe_key"] for e in page for c in e["contributions"]} if view == "events" else set(page_keys)
            return {"events": deepcopy(page), "effective": {k: deepcopy(v) for k,v in effective.items() if k not in {"events", "signals"}},
                    "signals": {k:effective["signals"][k] for k in sorted(recipe_keys) if k in effective["signals"]},
                    "signal_scope": "page_recipe_keys", "view": view,
                    "cooking_experiences": [e for e in pf.experiences(events) if e["event_id"] in page_keys],
                    "next_cursor": {"digest":digest, "view":view, "after":page_keys[-1]} if start+limit < len(keys) else None,
                    "policy": {"version": pf.POLICY_VERSION, "maximum_events": pf.MAX_EVENTS,
                               "retention_days": pf.RETENTION_DAYS, "decay_days": [30, 60], "per_recipe_cap": 6}}
        if action not in {"accept", "reject", "swap", "experience", "undo", "reset"}:
            raise HouseholdError("feedback action must be explicit: accept, reject, swap, undo or reset")
        key = request.get("idempotency_key")
        reason = request.get("reason")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise HouseholdError("feedback writes require a bounded idempotency_key")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise HouseholdError("feedback reason must be at most 500 characters")
        signature = mp.digest({k:v for k,v in request.items() if v is not None})
        existing = pf.prior(events, key, signature)
        if existing is not None:
            return {"event": existing, "idempotent": True}
        contributions = []
        targets = None
        recipe_scope = None
        binding = {}
        if action in {"accept", "reject"}:
            handoff = request.get("planner_handoff")
            if handoff is not None:
                self._verify_planner_handoff(handoff)
                if handoff["request"]["as_of_date"] != today:
                    raise HouseholdError("feedback proposal is stale; prepare a current proposal")
                if request.get("target") is not None:
                    raise HouseholdError("feedback requires one exact proposal or saved-menu target")
                binding = {k:deepcopy(handoff[k]) for k in ("planner_version", "input_digest", "selection_digest")}
                if action == "reject":
                    matches = [s for s in handoff["selection"]["slots"] if s["recipe_key"] == request.get("recipe_key") and canonical(s["reference"]) == canonical(request.get("reference"))]
                    if len(matches) != 1:
                        raise HouseholdError("proposal rejection needs its exact recipe key and reference")
                    binding["recipe"] = {"recipe_key":matches[0]["recipe_key"], "reference":deepcopy(matches[0]["reference"])}
                    contributions = [{"recipe_key":matches[0]["recipe_key"], "direction":-1}]
            else:
                if action == "accept":
                    raise HouseholdError("plan acceptance needs the complete unchanged planner_handoff")
                slot = self._feedback_slot(snapshot, request.get("target"))
                binding = {"target":deepcopy(request["target"])}
                contributions = [{"recipe_key":slot["recipe_key"], "direction":-1}]
        elif action == "experience":
            target = request.get("target")
            menus = [snapshot.get("menu"), *snapshot.get("menu_planning", {}).get("history", {}).values(), *snapshot.get("order_snapshots", {}).values()]
            if not isinstance(target, Mapping) or not any(canonical(candidate) == canonical(target) for menu in menus if isinstance(menu, Mapping) for candidate in feedback_targets(menu)):
                raise HouseholdError("experience needs an exact returned feedback target from a retained menu")
            experience = request.get("experience")
            if not isinstance(experience, Mapping) or not experience or not set(experience).issubset({"actual_active_minutes", "portion_fit", "leftover_portions"}):
                raise HouseholdError("experience needs actual_active_minutes, portion_fit or leftover_portions")
            if "actual_active_minutes" in experience and (type(experience["actual_active_minutes"]) is not int or not 0 <= experience["actual_active_minutes"] <= 1440):
                raise HouseholdError("actual_active_minutes must be an integer from zero to 1440")
            if "portion_fit" in experience and experience["portion_fit"] not in {"too_small", "right", "too_large"}:
                raise HouseholdError("portion_fit must be too_small, right or too_large")
            if "leftover_portions" in experience and (type(experience["leftover_portions"]) not in {int, float} or not math.isfinite(experience["leftover_portions"]) or not 0 <= experience["leftover_portions"] <= 100):
                raise HouseholdError("leftover_portions must be a finite number from zero to 100")
            binding = {"target": deepcopy(target), "experience": deepcopy(experience)}
            contributions = [{"recipe_key": target["recipe_key"], "direction": 0}]
        elif action == "swap":
            former = self._feedback_slot(snapshot, request.get("from_target"), allow_predecessor=True)
            latter = self._feedback_slot(snapshot, request.get("to_target"))
            current = snapshot["menu"]
            if canonical(request["from_target"]["menu_ref"]) != canonical(current.get("supersedes")) or former["slot_id"] == latter["slot_id"] or former["recipe_key"] == latter["recipe_key"] or (former["date"], former["meal_type"]) != (latter["date"], latter["meal_type"]):
                raise HouseholdError("swap feedback requires one exact replaced predecessor slot and its successor on the same date/type")
            binding = {"from_target":deepcopy(request["from_target"]), "to_target":deepcopy(request["to_target"])}
            contributions = [{"recipe_key":former["recipe_key"], "direction":-1}, {"recipe_key":latter["recipe_key"], "direction":1}]
        elif action == "undo":
            event_id = request.get("event_id")
            if not isinstance(event_id, str) or not any(e["event_id"] == event_id for e in events):
                raise HouseholdError("undo requires one exact retained event_id")
            targets = [event_id]
        else:
            scope = request.get("scope")
            if scope not in {"recipe", "all"}:
                raise HouseholdError("reset requires explicit scope recipe or all")
            if scope == "recipe":
                recipe_scope = request.get("recipe_key")
                if not isinstance(recipe_scope, str) or not any(c["recipe_key"] == recipe_scope for e in events for c in e["contributions"]):
                    raise HouseholdError("recipe reset requires one exact feedback recipe_key")
            elif request.get("recipe_key") is not None:
                raise HouseholdError("all-feedback reset cannot also target one recipe")
            targets = [e["event_id"] for e in events if e["kind"] in {"accept", "reject", "swap", "experience"} and
                       (recipe_scope is None or any(c["recipe_key"] == recipe_scope for c in e["contributions"]))]
        with self.store.locked() as state:
            if canonical(state) != canonical(snapshot):
                raise HouseholdError("feedback target changed before write; inspect again")
            event = pf.append(state["planning_feedback"], kind=action, binding=binding,
                contributions=contributions, reason=reason, key=key, signature=signature, as_of_date=today,
                targets=targets, recipe_key=recipe_scope)
            effective = pf.effective(state["planning_feedback"], today)
            affected = {c["recipe_key"] for c in contributions}
            summary = {k: v for k, v in effective.items() if k not in {"events", "signals"}}
            summary["signals"] = {k: effective["signals"][k] for k in sorted(affected) if k in effective["signals"]}
            return {"event": event, "effective": summary, "signal_scope": "event_recipe_keys"}

    def _mark_slot(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with self.store.locked() as state:
            menu = state.get("menu")
            if not isinstance(menu, Mapping) or request.get("menu_id") != menu.get("menu_id") or type(request.get("expected_revision")) is not int or request["expected_revision"] != menu["revision"]:
                raise HouseholdError("cooking requires the exact current menu ID and revision")
            slot = mp.slot_by_id(menu, request.get("slot_id"))
            if request.get("recipe_key") not in {None, slot["recipe_key"]}:
                raise HouseholdError("recipe_key does not match slot_id")
            action = request["action"]
            key = request.get("idempotency_key")
            if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 200):
                raise HouseholdError("idempotency_key must be bounded text")
            signature = canonical({"menu": mp.menu_ref(menu), "slot_id": slot["slot_id"], "action": action, "actual_batch": request.get("actual_batch")})
            if key and (existing := self._usage_request(state, key, signature)):
                return existing
            owner = menu.get("slot_owners", {}).get(slot["slot_id"], menu["menu_id"])
            record = state["recipe_usage"].get(owner)
            if not isinstance(record, dict) or slot not in record.get("slots", []):
                raise HouseholdError("exact slot usage owner is unavailable")
            cooked = action == "mark_cooked"
            try:
                handled = bp.record_outcome(state, menu, slot, request)
            except HouseholdError as exc:
                return {"status":"needs_input", "reason":str(exc), "slot_id":slot["slot_id"]}
            if handled:
                result = {"menu_id":menu["menu_id"], "slot_id":slot["slot_id"], "cooked":cooked,
                          "kind":"leftover", "new_recipe_usage":False, "batch_dependencies":bp.dependency_status(state,menu)}
                if key:
                    self._store_usage_request(state,key,signature,result)
                return result
            if owner != menu["menu_id"]:
                outcomes = state["menu_planning"]["outcomes"]
                if slot["slot_id"] not in outcomes and len(outcomes) >= mp.MAX_PLANNING_MENUS:
                    raise HouseholdError("planning outcome limit reached")
                outcomes[slot["slot_id"]] = {"outcome": "cooked" if cooked else "not_cooked", "owner_menu_id": owner,
                    "recorded_in_menu_id": menu["menu_id"], "recipe_key": slot["recipe_key"]}
            else:
                for field, value in (("cooked_slot_ids", slot["slot_id"]), ("cooked_keys", slot["recipe_key"]),
                                     ("not_cooked_slot_ids", slot["slot_id"]), ("not_cooked_keys", slot["recipe_key"])):
                    values = record.setdefault(field, [])
                    wanted = cooked == field.startswith("cooked")
                    if wanted and value not in values:
                        values.append(value)
                    if not wanted and value in values:
                        values.remove(value)
            result = {"menu_id": menu["menu_id"], "slot_id": slot["slot_id"], "recipe_key": slot["recipe_key"], "cooked": cooked, "batch_dependencies": bp.dependency_status(state,menu)}
            if key:
                self._store_usage_request(state, key, signature, result)
            return result

    @staticmethod
    def _replan_state_digest(state: Mapping[str, Any]) -> str:
        planning = deepcopy(state.get("menu_planning"))
        if isinstance(planning, dict):
            # Prepared handoffs are durable transport state, not planning input.
            planning.pop("prepared", None)
        return mp.digest({
            "menu": state.get("menu"), "profile": state.get("profile"),
            "recipe_usage": state.get("recipe_usage"), "menu_planning": planning,
            "planning_feedback": state.get("planning_feedback"),
            "batch_outcomes": state.get("batch_outcomes"),
        })

    @staticmethod
    def _replan_ref(replan_digest: str) -> str:
        return "replan_" + replan_digest

    def _independent_menu_protection(self, state, candidate):
        """Return the frozen menu owners when a menu has no link to protected work.

        An addition's pending menu is merely the menu current at prepare time;
        the paid order snapshot, rather than that incidental copy, owns its slots.
        """
        if state.get("pending_cancellation") or not isinstance(candidate, Mapping):
            return None
        pending = state.get("pending_checkout")
        change = state.get("order_change")
        if pending is not None and not isinstance(pending, Mapping):
            return None
        if change is not None and not isinstance(change, Mapping):
            return None
        if not change and (not pending or pending.get("status") not in UNRESOLVED_CHECKOUT_STATUSES):
            return None
        if change:
            order_id = change.get("order_id")
            if (not isinstance(order_id, str) or not order_id
                    or pending and canonical(pending.get("order_change")) != canonical(change)):
                return None
            frozen = (state.get("order_snapshots") or {}).get(order_id)
            if (not isinstance(frozen, Mapping) or frozen.get("order_id") != order_id
                    or not isinstance(frozen.get("menu_id"), str) or not frozen["menu_id"]
                    or not isinstance(frozen.get("slots", []), list)):
                return None
            linked_usage = {
                menu_id: usage for menu_id, usage in (state.get("recipe_usage") or {}).items()
                if isinstance(usage, Mapping) and usage.get("order_id") == order_id
            }
            if frozen["menu_id"] not in linked_usage:
                return None
        else:
            frozen = pending.get("menu")
            attribution = (pending.get("summary") or {}).get("menu_attribution")
            if attribution is None:
                attribution = self._checkout_menu_attribution(frozen, pending.get("cart_plan"))
            if (attribution != "menu_bound" or not isinstance(frozen, Mapping)
                    or not isinstance(frozen.get("menu_id"), str) or not frozen["menu_id"]
                    or not isinstance(frozen.get("slots", []), list)):
                return None
            linked_usage = {}
        protected_ids = {frozen["menu_id"]}
        protected_slots = set()
        for record in [frozen, *linked_usage.values()]:
            slots = record.get("slots", [])
            if not isinstance(slots, list):
                return None
            owners = record.get("slot_owners") or {}
            if not isinstance(owners, Mapping) or any(not isinstance(owner, str) for owner in owners.values()):
                return None
            protected_ids.update(owners.values())
            for slot in slots:
                if not isinstance(slot, Mapping) or not isinstance(slot.get("slot_id"), str):
                    return None
                protected_slots.add(slot["slot_id"])
        protected_ids.update(linked_usage)
        if (candidate.get("menu_id") in protected_ids
                or change and candidate.get("order_id") == change["order_id"]):
            return None
        candidate_usage = (state.get("recipe_usage") or {}).get(candidate.get("menu_id"))
        if change and isinstance(candidate_usage, Mapping) and candidate_usage.get("order_id") == change["order_id"]:
            return None
        owners = candidate.get("slot_owners") or {}
        slots = candidate.get("slots") or []
        if not isinstance(owners, Mapping) or not isinstance(slots, list):
            return None
        if any(not isinstance(owner, str) or owner in protected_ids for owner in owners.values()):
            return None
        if any(not isinstance(slot, Mapping) or not isinstance(slot.get("slot_id"), str)
               or slot["slot_id"] in protected_slots for slot in slots):
            return None
        ancestor = candidate.get("supersedes")
        seen = set()
        while ancestor is not None:
            if not isinstance(ancestor, Mapping) or not isinstance(ancestor.get("menu_id"), str):
                return None
            ancestor_id = ancestor["menu_id"]
            if ancestor_id in protected_ids:
                return None
            key = f'{ancestor_id}:{ancestor.get("revision")}'
            if key in seen:
                return None
            seen.add(key)
            prior = (state.get("menu_planning") or {}).get("history", {}).get(key)
            if not isinstance(prior, Mapping) or canonical(mp.menu_ref(prior)) != canonical(ancestor):
                return None
            ancestor = prior.get("supersedes")
        return protected_ids

    def _store_prepared_replan(self, prepared: Mapping[str, Any]) -> str:
        replan_digest = str(prepared.get("replan_digest") or "")
        replan_ref = self._replan_ref(replan_digest)
        if REPLAN_REF_PATTERN.fullmatch(replan_ref) is None:
            raise HouseholdError("prepared replan digest is invalid")
        record = {key: deepcopy(prepared[key]) for key in (
            "source", "as_of_date", "remaining_dates", "locked_slot_ids",
            "planner_input", "state_digest", "replan_digest",
        )}
        with self.store.locked() as state:
            current = state.get("menu")
            if (
                not isinstance(current, Mapping)
                or self._household_today(state).isoformat() != record["as_of_date"]
                or self._replan_state_digest(state) != record["state_digest"]
                or canonical(mp.menu_ref(current)) != canonical(record["source"])
            ):
                raise HouseholdError("replan state changed while preparing; prepare again")
            records = state["menu_planning"]["prepared"]
            for key, value in list(records.items()):
                if isinstance(key, str) and key.startswith("productplan_"):
                    continue
                if (
                    not isinstance(value, Mapping)
                    or value.get("state_digest") != record["state_digest"]
                    or canonical(value.get("source")) != canonical(record["source"])
                    or value.get("as_of_date") != record["as_of_date"]
                ):
                    records.pop(key, None)
            replan_records = [key for key in records if isinstance(key, str) and key.startswith("replan_")]
            if replan_ref not in records and len(replan_records) >= MAX_PREPARED_REPLANS:
                raise HouseholdError("too many prepared replans; apply one or change the menu before preparing another")
            records[replan_ref] = record
        return replan_ref

    @staticmethod
    def _prepared_replan_record(state: Mapping[str, Any], replan_ref: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(replan_ref, str) or REPLAN_REF_PATTERN.fullmatch(replan_ref) is None:
            raise HouseholdError("replan_ref must be one exact server-returned replan reference")
        replan_digest = replan_ref.removeprefix("replan_")
        record = state.get("menu_planning", {}).get("prepared", {}).get(replan_ref)
        if not isinstance(record, Mapping) or set(record) != {
            "source", "as_of_date", "remaining_dates", "locked_slot_ids",
            "planner_input", "state_digest", "replan_digest",
        } or record.get("replan_digest") != replan_digest:
            raise HouseholdError("replan_ref is stale, missing or belongs to another menu")
        return replan_digest, deepcopy(dict(record))

    def _prepare_replan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        state = self.store.read()
        current = mp.exact_menu(state, request.get("menu_ref"))
        slots = mp.slots(current)
        today = self._household_today(state).isoformat()
        if request.get("as_of_date") not in {None, today}:
            raise HouseholdError("replan as_of_date changed; prepare again")
        dates = request.get("remaining_dates")
        if not isinstance(dates, list) or not 1 <= len(dates) <= 7 or any(not isinstance(day, str) for day in dates) or len(set(dates)) != len(dates):
            raise HouseholdError("remaining_dates must be one to seven exact distinct slot dates")
        dinners = [s for s in slots if s["meal_type"] == "dinner"]
        by_date = {s["date"]: s for s in dinners}
        if len(by_date) != len(dinners) or any(day not in by_date or day < today for day in dates):
            raise HouseholdError("remaining_dates must name exact current dinner slots on or after as_of_date")
        stored_locks = state["menu_planning"]["locks"].get(mp.lock_key(current), [])
        explicit = request.get("locked_slot_ids")
        if explicit is None:
            explicit = []
        if not isinstance(explicit, list) or len(explicit) > 31 or any(not isinstance(v, str) for v in explicit) or len(set(explicit)) != len(explicit):
            raise HouseholdError("locked_slot_ids must be exact distinct slot IDs")
        for value in explicit:
            mp.slot_by_id(current, value)
        locks = sorted(set(stored_locks) | set(explicit))
        historical = {s["slot_id"] for s in slots if s["date"] < today or mp.slot_outcome(state, current, s) == "cooked"}
        replacing = [s for s in dinners if s["date"] in dates and s["slot_id"] not in historical and s["slot_id"] not in locks]
        if not replacing:
            return {"status": "needs_input", "reason": "no requested unlocked future slots"}
        carried = [s for s in slots if s not in replacing]
        batches = bp.sources(current)
        for batch in batches:
            component = {batch["source_slot_id"]} | {s["slot_id"] for s in slots if s.get("source_slot_id") == batch["source_slot_id"] and s["slot_id"] not in historical}
            changed = {s["slot_id"] for s in replacing}
            invalid = {s["slot_id"] for s in bp.dependency_status(state,current) if s["status"]=="needs_replan" and s["slot_id"] not in historical}
            if invalid.difference(changed):
                return {"status":"needs_input", "reason":"all invalid future leftovers must be replanned together"}
            if changed & component and set(locks) & component:
                return {"status":"needs_input", "reason":"locked batch component cannot be partially replanned"}
            if batch["source_slot_id"] in changed and any(s.get("source_slot_id")==batch["source_slot_id"] and s["slot_id"] in historical for s in slots):
                return {"status":"needs_input", "reason":"a source with historical leftovers must retain its exact context; correct the conflicting source outcome first"}
            if batch["source_slot_id"] in changed and not component <= changed:
                return {"status":"needs_input", "reason":"source replacement requires every future dependent date in the same replan"}
        planning_state = deepcopy(state)
        for slot in replacing:
            owner = current.get("slot_owners", {}).get(slot["slot_id"], current["menu_id"])
            if slot.get("kind") != "leftover":
                if not any(s.get("kind") != "leftover" and s["recipe_key"] == slot["recipe_key"]
                           and current.get("slot_owners", {}).get(s["slot_id"], current["menu_id"]) == owner
                           for s in carried):
                    planning_state["menu_planning"]["retired"].setdefault(owner, []).append(slot["recipe_key"])
        planner_input = request.get("planner_input")
        if not isinstance(planner_input, Mapping):
            raise HouseholdError("replan requires bounded planner_input candidates")
        planner_input = deepcopy(dict(planner_input))
        if planner_input.get("week", current["week"]) != current["week"]:
            raise HouseholdError("replan must remain in the exact source week")
        replacement_dates = sorted(s["date"] for s in replacing)
        if planner_input.get("dates", replacement_dates) != replacement_dates:
            raise HouseholdError("planner dates must exactly match the unlocked remaining dates")
        planner_input.update({"week": current["week"], "dates": replacement_dates, "as_of_date": today, "alternatives": 1})
        # Replacements inherit complete frozen batch components, not the full
        # household week. Retain the accepted prepared amount for this menu.
        replacement_state = deepcopy(planning_state)
        replacement_state['profile']['meals']['meal_mode'] = 'fresh'
        effective = self._effective_planner_request(planner_input, replacement_state, anchor_current_date=True)
        if planner_input.get('meal_mode') != 'fresh':
            allocations = []
            batch_by_source = {b['source_slot_id']: b for b in batches}
            for slot in replacing:
                if slot.get('kind') == 'leftover':
                    continue
                batch = batch_by_source.get(slot['slot_id'])
                if batch:
                    amounts = [bp.fraction(batch[k]) for k in ('prepared_portions', 'consumed_at_source')]
                    dependents = [bp.fraction(s['portions']) for s in replacing if s.get('source_slot_id') == slot['slot_id']]
                    if any(a.denominator != 1 for a in amounts) or any(a != amounts[1] for a in dependents):
                        return {'status': 'needs_input', 'reason': 'This batch has fractional or unequal eating portions; retain it or explicitly request fresh replacements.',
                                'prepared_portions': batch['prepared_portions'], 'consumed_at_source': batch['consumed_at_source']}
                days = sorted([slot['date']] + [s['date'] for s in replacing if s.get('source_slot_id') == slot['slot_id']])
                allocations.append({'source_date': slot['date'], 'eating_dates': days, 'batch': bool(batch),
                    'prepared_portions': int(bp.fraction(batch['prepared_portions'])) if batch else slot['portions'],
                    'consumed_at_source': int(bp.fraction(batch['consumed_at_source'])) if batch else slot['portions']})
            covered = {d for a in allocations for d in a['eating_dates']}
            if any(a['batch'] for a in allocations) and covered != set(replacement_dates):
                return {'status': 'needs_input', 'reason': 'Replan the complete batch separately from leftover-only dates whose cooking source is retained.'}
            if any(a['batch'] for a in allocations) and covered == set(replacement_dates):
                consumption = {a['consumed_at_source'] for a in allocations}
                if len(consumption) != 1:
                    return {'status': 'needs_input', 'reason': 'These components have different eating portions; replan each component separately or explicitly choose fresh meals.'}
                effective['portions'] = next(iter(consumption))
                effective['recurring_batch'] = {'sources': allocations, 'shortages': [],
                    'required_portions': sum(len(a['eating_dates']) * a['consumed_at_source'] for a in allocations),
                    'available_portions': sum(a['prepared_portions'] for a in allocations),
                    'accepted_settings': {**deepcopy(state['profile']['meals']), 'portions': effective['portions']}}
        candidates = self._resolve_planner_candidates(effective, planning_state)
        if not candidates:
            return {"status": "needs_input", "reason": "no replacement candidates"}
        effective["candidates"] = [{**c["reference"], "facts": c["supplied_facts"]} for c in candidates]
        result = self._run_planner(effective, candidates, planning_state)
        if result["status"] != "planned":
            return {"status": "needs_input", "plan": result}
        replacement = self._materialize_planner_menu(result["save_handoff"], candidates)
        # Stable IDs are scoped to this exact predecessor; only carried slots keep IDs.
        remap = {slot['slot_id']: 'slot_' + mp.digest({'source': mp.menu_ref(current), 'replacement': slot})[:32]
                 for slot in replacement['slots']}
        for slot in replacement['slots']:
            slot['slot_id'] = remap[slot['slot_id']]
            if slot.get('source_slot_id') in remap:
                slot['source_slot_id'] = remap[slot['source_slot_id']]
        for batch in replacement.get('batches', []):
            batch['source_slot_id'] = remap[batch['source_slot_id']]
            for leftover in batch['leftovers']:
                leftover['slot_id'] = remap[leftover['slot_id']]
            batch['spec_digest'] = mp.digest({k: v for k, v in batch.items() if k != 'spec_digest'})
        successor = {"week": current["week"], "dishes": [], "salads": [],
                     "slots": sorted(deepcopy(carried) + replacement["slots"], key=mp.slot_order),
                     "historical_slot_ids": sorted(historical), "supersedes": mp.menu_ref(current),
                     "replan_selection": deepcopy(result["save_handoff"]),
                     "planning_scope": deepcopy(current.get("planning_scope") or (current.get("planner_selection") or {}).get("request") or {"dates": sorted({s["date"] for s in current["slots"]}), "portions": state["profile"]["meals"]["portions"]})}
        if not carried and not historical:
            successor["planning_scope"] = deepcopy(effective)
        if replacement.get("available_ingredients"):
            successor["available_ingredients"] = deepcopy(replacement["available_ingredients"])
        successor["slot_owners"] = {s["slot_id"]: current.get("slot_owners", {}).get(s["slot_id"], current["menu_id"]) for s in carried}
        successor["dishes"] = [deepcopy(mp.recipe_for_slot(current, slot)) for slot in mp.preparation_slots(current)
                               if slot in carried]
        for recipe, slot in zip(replacement["dishes"], mp.preparation_slots(replacement), strict=True):
            copied = deepcopy(recipe)
            copied["preparation_slot_id"] = slot["slot_id"]
            successor["dishes"].append(copied)
        retained_batches = [deepcopy(b) for b in batches if b["source_slot_id"] not in {s["slot_id"] for s in replacing}]
        retained_batches += deepcopy(replacement.get('batches', []))
        if retained_batches:
            successor["batches"] = retained_batches
            if current.get("batch") in retained_batches:
                successor["batch"] = deepcopy(current["batch"])
        successor["schedule"] = mp.schedule(successor)
        minimums = saved_menu_minimum_evaluation(successor, state.get("profile") or {})
        if minimums.get("complete_menu") and minimums.get("enforced_status", minimums.get("status")) != "pass":
            return {
                "status": "needs_input",
                "reason": "complete_weekly_successor_dietary_minimums_unsatisfied",
                "minimum_evaluation": minimums,
                "next": (
                    "Choose replacement candidates with sufficient positive structured dietary "
                    "facet evidence and run replan_prepare again. No apply reference was created."
                ),
            }
        before = deepcopy(current)
        before["historical_slot_ids"] = sorted(historical)
        prepared = {"status": "prepared", "source": mp.menu_ref(current), "as_of_date": today,
                    "remaining_dates": sorted(dates), "locked_slot_ids": sorted(explicit),
                    "planner_input": planner_input, "state_digest": self._replan_state_digest(state),
                    "successor": successor, "replaced_slot_ids": sorted(s["slot_id"] for s in replacing),
                    "shopping_comparison": mp.shopping_comparison(before, successor)}
        prepared["replan_digest"] = mp.digest(prepared)
        if len(canonical(prepared).encode()) > MAX_MENU_BYTES or len(json.dumps({"ok": True, "result": {"replan": prepared}}, ensure_ascii=True).encode()) > MAX_REQUEST - 4096:
            raise HouseholdError("replan exceeds bounded response size")
        return prepared

    def _replanning(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request["action"]
        if action in {"batch_prepare", "batch_apply"}:
            return self._batch(request)
        if action == "lock":
            desired = request.get("locked")
            if not isinstance(desired, bool):
                raise HouseholdError("locked must be an explicit desired boolean")
            with self.store.locked() as state:
                current = mp.exact_menu(state, request.get("menu_ref"))
                slot = mp.slot_by_id(current, request.get("slot_id"))
                usage = state["recipe_usage"].get(current["menu_id"], {})
                if current.get("phase") == "ordered" or usage.get("status") == "ordered":
                    raise HouseholdError("ordered/historical locks are immutable; pass exact preparation locks instead")
                values = state["menu_planning"]["locks"].setdefault(mp.lock_key(current), [])
                if desired and slot["slot_id"] not in values:
                    values.append(slot["slot_id"])
                    values.sort()
                elif not desired and slot["slot_id"] in values:
                    values.remove(slot["slot_id"])
                return {"menu_ref": mp.menu_ref(current), "slot_id": slot["slot_id"], "locked": desired}
        if action == "replan_prepare":
            prepared = self._prepare_replan(request)
            result = {"replan": prepared}
            if prepared.get("status") == "prepared":
                result["apply_arguments"] = {
                    "action": "replan_apply",
                    "replan_ref": self._store_prepared_replan(prepared),
                }
            return result
        supplied = request.get("replan")
        replan_ref = request.get("replan_ref")
        if supplied is not None and replan_ref is not None:
            raise HouseholdError("replan_apply accepts replan_ref or legacy replan, not both")
        state = self.store.read()
        if replan_ref is not None:
            if not isinstance(replan_ref, str) or REPLAN_REF_PATTERN.fullmatch(replan_ref) is None:
                raise HouseholdError("replan_ref must be one exact server-returned replan reference")
            replan_digest = replan_ref.removeprefix("replan_")
            applied = state["menu_planning"]["applied"].get(replan_digest)
            if applied is not None:
                return {"menu_ref": deepcopy(applied), "idempotent": True}
            replan_digest, record = self._prepared_replan_record(state, replan_ref)
            fresh = self._prepare_replan({
                "menu_ref": record["source"], "as_of_date": record["as_of_date"],
                "remaining_dates": record["remaining_dates"],
                "locked_slot_ids": record["locked_slot_ids"],
                "planner_input": record["planner_input"],
            })
            if fresh.get("replan_digest") != replan_digest or fresh.get("state_digest") != record["state_digest"]:
                raise HouseholdError("replan_ref is stale or its prepared plan changed; prepare again")
            supplied = fresh
        elif not isinstance(supplied, Mapping) or supplied.get("status") != "prepared" or supplied.get("replan_digest") != mp.digest({k: v for k, v in supplied.items() if k != "replan_digest"}):
            raise HouseholdError("replan_apply requires one exact server-returned replan_ref or the complete legacy replan")
        applied = state["menu_planning"]["applied"].get(supplied["replan_digest"])
        if applied is not None:
            return {"menu_ref": deepcopy(applied), "idempotent": True}
        if replan_ref is None:
            fresh = self._prepare_replan({"menu_ref": supplied["source"], "as_of_date": supplied["as_of_date"],
                "remaining_dates": supplied["remaining_dates"], "locked_slot_ids": supplied["locked_slot_ids"], "planner_input": supplied["planner_input"]})
            if canonical(fresh) != canonical(supplied):
                raise HouseholdError("replan is stale or altered; prepare again")
        with self.store.locked() as state:
            if self._household_today(state).isoformat() != supplied["as_of_date"] or self._replan_state_digest(state) != supplied["state_digest"]:
                raise HouseholdError("replan date or state changed; prepare again")
            if replan_ref is not None:
                _digest, current_record = self._prepared_replan_record(state, replan_ref)
                if canonical(current_record) != canonical(record):
                    raise HouseholdError("replan_ref changed before apply; prepare again")
            protected = bool(
                state.get("pending_cancellation") or state.get("order_change")
                or (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES
            )
            independent = self._independent_menu_protection(state, state.get("menu")) if protected else None
            if protected and independent is None:
                raise HouseholdError("reconcile the linked or unidentified protected operation before replan apply")
            if independent is None:
                self._abandon_predispatch(state, reason="menu replanned before checkout")
            result = self._commit_successor(state, supplied, preserve_cart_plan=independent is not None)
            state["menu_planning"]["prepared"].pop(
                self._replan_ref(supplied["replan_digest"]), None
            )
            return result

    def _prepare_batch(self, request):
        state = self.store.read()
        current = mp.exact_menu(state, request.get("menu_ref"))
        today = self._household_today(state).isoformat()
        if request.get("as_of_date") not in {None, today}:
            raise HouseholdError("batch preparation date changed")
        try:
            spec = bp.normalize(state, current, request.get("batch_spec"), today)
        except HouseholdError as exc:
            return {"status":"needs_input", "reason":str(exc)}
        source = mp.slot_by_id(current, spec["source_slot_id"])
        self._require_recipe_provider(mp.recipe_for_slot(current, source))
        replaced = {d["replaces_slot_id"] for d in spec["leftovers"]}
        carried = [deepcopy(s) for s in current["slots"] if s["slot_id"] not in replaced]
        leftover_slots = [{"slot_id":d["slot_id"], "date":d["date"], "meal_type":d["meal_type"], "kind":"leftover",
            "source_slot_id":source["slot_id"], "portions":deepcopy(d["portions"]), "recipe_key":source["recipe_key"],
            "reference":deepcopy(source["reference"]), "snapshot_digest":source["snapshot_digest"],
            **({"leafy_green": deepcopy(source["leafy_green"])} if "leafy_green" in source else {}),
            **({"served_with": mp.slot_by_id(current, d["replaces_slot_id"])["served_with"]}
               if "served_with" in mp.slot_by_id(current, d["replaces_slot_id"]) else {})} for d in spec["leftovers"]]
        successor = {"week":current["week"], "slots":sorted(carried+leftover_slots,key=mp.slot_order),
            "dishes":[], "salads":[], "batch":spec, "batches":deepcopy(bp.sources(current))+[spec], "supersedes":mp.menu_ref(current),
            "planner_selection":deepcopy(current.get("planner_selection")),
            "historical_slot_ids":[s["slot_id"] for s in carried if s["date"]<today or mp.slot_outcome(state,current,s)=="cooked"],
            "slot_owners":{s["slot_id"]:current.get("slot_owners",{}).get(s["slot_id"],current["menu_id"]) for s in carried}}
        successor["planning_scope"] = deepcopy(current.get("planning_scope") or (current.get("planner_selection") or {}).get("request") or {"dates": sorted({s["date"] for s in current["slots"]}), "portions": state["profile"]["meals"]["portions"]})
        if current.get("replan_selection"):
            successor["replan_selection"] = deepcopy(current["replan_selection"])
        successor["dishes"] = [deepcopy(mp.recipe_for_slot(current, slot)) for slot in mp.preparation_slots(current)
                               if slot["slot_id"] not in replaced]
        eligibility = bp.evaluate_plan(state, current, successor)
        if eligibility["status"] != "pass":
            return {"status":"needs_input", "reason":"batch arrangement does not satisfy current hard/strict constraints", "evaluation":eligibility}
        successor["schedule"] = mp.schedule(successor)
        prepared = {"status":"prepared", "source":mp.menu_ref(current), "state_digest":self._replan_state_digest(state),
            "as_of_date":today, "batch_spec":deepcopy(request["batch_spec"]), "successor":successor,
            "replaced_slot_ids":sorted(replaced), "locked_slot_ids":[], "planner_input":{},
            "shopping_comparison":mp.shopping_comparison(current,successor),
            "shopping_reasons":[{"slot_id":source["slot_id"],"reason":"source_scaled_once","prepared_portions":spec["prepared_portions"]}]+
                [{"slot_id":s["slot_id"],"source_slot_id":source["slot_id"],"reason":"leftover_zero_new_requirements","new_requirements":0} for s in leftover_slots],
            "confirmation_statement":bp.CONFIRMATION_STATEMENT}
        prepared["batch_digest"] = mp.digest(prepared)
        if len(canonical(prepared).encode())>MAX_MENU_BYTES or len(json.dumps({"ok":True,"result":{"batch_plan":prepared}},ensure_ascii=True).encode())>MAX_REQUEST-4096:
            raise HouseholdError("batch plan exceeds bounded response size")
        return prepared

    def _batch(self, request):
        if request["action"]=="batch_prepare":
            return {"batch_plan":self._prepare_batch(request)}
        supplied = request.get("batch_plan")
        if not isinstance(supplied,Mapping) or supplied.get("status")!="prepared" or supplied.get("batch_digest")!=mp.digest({k:v for k,v in supplied.items() if k!="batch_digest"}):
            raise HouseholdError("batch_apply requires the complete unchanged prepared batch_plan")
        confirmation = request.get("batch_confirmation")
        expected = {"batch_digest":supplied["batch_digest"],"statement":bp.CONFIRMATION_STATEMENT}
        if canonical(confirmation)!=canonical(expected):
            return {"status":"needs_input","reason":"clear current-user confirmation of the exact batch specification is required", "required_confirmation":expected}
        state = self.store.read()
        prior=state["menu_planning"]["applied"].get(supplied["batch_digest"])
        if prior is not None:
            return {"menu_ref":deepcopy(prior),"idempotent":True}
        fresh=self._prepare_batch({"menu_ref":supplied["source"],"as_of_date":supplied["as_of_date"],"batch_spec":supplied["batch_spec"]})
        if canonical(fresh)!=canonical(supplied):
            raise HouseholdError("batch source or specification is stale; prepare again")
        with self.store.locked() as state:
            if any(state.get(k) for k in ("pending_checkout","pending_cancellation","order_change")):
                raise HouseholdError("reconcile protected operations before batch apply")
            if self._household_today(state).isoformat()!=supplied["as_of_date"] or self._replan_state_digest(state)!=supplied["state_digest"]:
                raise HouseholdError("batch state/date changed before apply")
            commit=deepcopy(supplied); commit["replan_digest"]=supplied["batch_digest"]
            commit["successor"]["batch"]["confirmation"]=deepcopy(confirmation)
            commit["successor"]["batches"][-1]["confirmation"]=deepcopy(confirmation)
            return self._commit_successor(state,commit)

    def _commit_successor(self, state, supplied, *, preserve_cart_plan=False):
        planning = state["menu_planning"]
        if any(len(v) >= mp.MAX_PLANNING_MENUS for v in planning.values()):
            raise HouseholdError("planning history limit reached")
        current = state["menu"]
        successor = deepcopy(supplied["successor"])
        mp.bind_preparations(successor)
        self._require_menu_provider(successor)
        minimums = saved_menu_minimum_evaluation(successor, state.get("profile") or {})
        if minimums.get("complete_menu") and minimums.get("enforced_status", minimums.get("status")) != "pass":
            raise PlannerError(
                "complete weekly successor does not satisfy saved dietary minimums: "
                + canonical(minimums)
            )
        successor.update({"menu_id": "menu_" + secrets.token_hex(12), "revision": 1, "phase": "draft"})
        successor["digest"] = menu_digest(successor)
        if current is not None:
            planning["history"][mp.lock_key(current)] = deepcopy(current)
        for slot in current["slots"] if current else []:
            if slot["slot_id"] in supplied["replaced_slot_ids"] and slot.get("kind") != "leftover":
                owner = current.get("slot_owners", {}).get(slot["slot_id"], current["menu_id"])
                if any(s.get("kind") != "leftover" and s["recipe_key"] == slot["recipe_key"]
                       and successor["slot_owners"].get(s["slot_id"], successor["menu_id"]) == owner
                       for s in successor["slots"]):
                    continue
                values = planning["retired"].setdefault(owner, [])
                if slot["recipe_key"] not in values:
                    values.append(slot["recipe_key"])
        owned = [s for s in successor["slots"] if s["slot_id"] not in successor["slot_owners"]]
        state["recipe_usage"][successor["menu_id"]] = {"week": successor["week"], "status": "planned",
            "recipe_keys": [s["recipe_key"] for s in owned if s.get("kind") != "leftover"], "slots": deepcopy(owned),
            "cooked_keys": [], "not_cooked_keys": [], "cooked_slot_ids": [], "not_cooked_slot_ids": [],
            "cooldown_overrides": deepcopy(supplied["planner_input"].get("cooldown_overrides", {})), "order_id": None}
        previous_locks = planning["locks"].get(mp.lock_key(current), []) if current else []
        carried_locks = [s["slot_id"] for s in successor["slots"] if s["slot_id"] in set(previous_locks) | set(supplied["locked_slot_ids"])]
        planning["locks"][mp.lock_key(successor)] = carried_locks
        planning["applied"][supplied["replan_digest"]] = mp.menu_ref(successor)
        state["menu"] = successor
        if not preserve_cart_plan:
            self._clear_persisted_product_plan(state.get("cart_plan"))
        return {"menu": deepcopy(successor), "shopping_comparison": deepcopy(supplied["shopping_comparison"])}

    def _add_slot(self, request):
        """Append one explicitly requested meal, retaining the exact current plan."""
        raw = request.get("slot_input")
        if not isinstance(raw, Mapping) or set(raw) != {"date", "meal_type", "portions", "reference"}:
            raise HouseholdError("add_slot needs slot_input={date,meal_type,portions,reference}")
        try:
            day = date.fromisoformat(raw["date"])
            if day.isoformat() != raw["date"]:
                raise ValueError()
        except (ValueError, TypeError):
            raise HouseholdError("slot date must be a canonical ISO date") from None
        if not isinstance(raw["meal_type"], str) or raw["meal_type"] not in RECIPE_CATEGORIES:
            raise HouseholdError("meal_type must be one of: " + ", ".join(RECIPE_CATEGORIES))
        portions = raw["portions"]
        if type(portions) is not int or not 1 <= portions <= 100:
            raise HouseholdError("slot portions must be an integer from one to 100")
        reference, _ = self._planner_reference(raw["reference"])
        key = request.get("idempotency_key")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise HouseholdError("add_slot requires a stable idempotency_key")
        signature = mp.digest({"action": "add_slot", "menu_ref": request.get("menu_ref"), "slot_input": raw})
        snapshot = self.store.read()
        prior = self._usage_request(snapshot, key, signature)
        if prior is not None:
            return {**prior, "idempotent": True}
        current = snapshot.get("menu")
        if current is not None:
            mp.exact_menu(snapshot, request.get("menu_ref"))
            mp.slots(current)
            if current.get("phase") == "ordered" or snapshot.get("recipe_usage", {}).get(current["menu_id"], {}).get("status") == "ordered":
                raise HouseholdError("ordered menu slots are immutable; create a distinct new menu")
        elif request.get("menu_ref") is not None:
            raise HouseholdError("there is no current menu matching menu_ref")
        today = self._household_today(snapshot).isoformat()
        if raw["date"] < today:
            raise HouseholdError("new meals must be dated today or later")
        year, week_number, _ = day.isocalendar()
        week = f"{year}-W{week_number:02d}"
        if current and current["week"] != week:
            raise HouseholdError("the added meal must belong to the current menu week")
        if current and len(current["slots"]) >= 31:
            raise HouseholdError("a menu supports at most 31 meal slots")
        if current and raw["meal_type"] == "dinner" and any(
            slot["date"] == raw["date"] and slot["meal_type"] == "dinner" for slot in current["slots"]
        ):
            raise HouseholdError("that date already has a dinner; use replan to replace it, or add a side or another course")
        candidate = self._resolve_planner_candidates(
            {"week": week, "portions": portions, "candidates": [reference]}, snapshot)[0]
        if candidate["materialization_error"]:
            raise HouseholdError(candidate["materialization_error"])
        from dietary_assessment import assess
        conflicts = [finding for finding in assess(snapshot["profile"], candidate["recipe"], recipe=True) if finding["blocked"]]
        if conflicts:
            raise HouseholdError("household ingredient rules block this addition: " + canonical(conflicts))
        recipe = self._materialize_menu({"week": week, "dishes": [{**reference, "portions": portions}]})["dishes"][0]
        slot = {"slot_id": "slot_" + mp.digest({"addition": signature, "key": key})[:32],
                "date": raw["date"], "meal_type": raw["meal_type"], "portions": portions,
                "recipe_key": recipe["recipe_key"], "reference": reference, "snapshot_digest": mp.digest(recipe)}
        recipe["preparation_slot_id"] = slot["slot_id"]
        successor = {"week": week, "dishes": [*deepcopy(current["dishes"] if current else []), recipe],
                     "salads": deepcopy(current["salads"] if current else []),
                     "slots": sorted([*deepcopy(current["slots"] if current else []), slot], key=mp.slot_order),
                     "slot_owners": {}, "historical_slot_ids": []}
        if current:
            successor["supersedes"] = mp.menu_ref(current)
            successor["slot_owners"] = {s["slot_id"]: current.get("slot_owners", {}).get(s["slot_id"], current["menu_id"])
                                        for s in current["slots"]}
            successor["historical_slot_ids"] = sorted(s["slot_id"] for s in current["slots"]
                if s["date"] < today or mp.slot_outcome(snapshot, current, s) == "cooked")
            for field in ("batches", "batch", "available_ingredients", "notes"):
                if field in current:
                    successor[field] = deepcopy(current[field])
            if handoff := current.get("planner_selection") or current.get("replan_selection"):
                successor["replan_selection"] = deepcopy(handoff)
        scope = (current.get("planning_scope") or (current.get("planner_selection") or {}).get("request")) if current else None
        dinner_dates = sorted({s["date"] for s in successor["slots"] if s["meal_type"] == "dinner"})
        successor["planning_scope"] = deepcopy(scope) if scope is not None else {"dates": dinner_dates, "portions": portions}
        if raw["meal_type"] == "dinner":
            successor["planning_scope"]["dates"] = sorted(set(successor["planning_scope"].get("dates", [])) | {raw["date"]})
        successor["schedule"] = mp.schedule(successor)
        before = deepcopy(current) if current else {"week": week, "dishes": [], "salads": []}
        before["historical_slot_ids"] = successor["historical_slot_ids"]
        comparison = mp.shopping_comparison(before, successor)
        if len(canonical(successor).encode()) > MAX_MENU_BYTES or len(menu_email_html(successor).encode()) > MAX_EMAIL_HTML_BYTES:
            raise HouseholdError("menu addition exceeds the recipe delivery size limit")
        response = {"ok": True, "result": {"menu": successor, "shopping_comparison": comparison, "added_slot": slot}}
        if len(json.dumps(response, ensure_ascii=True).encode()) > MAX_REQUEST - 4096:
            raise HouseholdError("menu addition cannot fit the response transport")
        with self._menu_save_commit(current) as state:
            prior = self._usage_request(state, key, signature)
            if prior is not None:
                return {**prior, "idempotent": True}
            if self._household_today(state).isoformat() != today or self._replan_state_digest(state) != self._replan_state_digest(snapshot):
                raise HouseholdError("menu or household state changed; read the menu and try again")
            if state.get("pending_cart_change"):
                raise HouseholdError("reconcile the pending cart change before adding a meal")
            pending = state.get("pending_checkout") or state.get("order_change")
            if state.get("pending_cancellation") or pending and self._independent_menu_protection(state, current) is None:
                raise HouseholdError("reconcile the linked or unidentified protected operation before adding a meal")
            if pending is None:
                self._abandon_predispatch(state, reason="meal added")
            result = self._commit_successor(state, {"successor": successor, "replaced_slot_ids": [],
                "locked_slot_ids": [], "planner_input": {}, "replan_digest": signature,
                "shopping_comparison": comparison}, preserve_cart_plan=pending is not None)
            receipt = {"menu_ref": mp.menu_ref(result["menu"]), "added_slot": deepcopy(slot)}
            self._store_usage_request(state, key, signature, receipt)
            return {**result, **receipt}

    def _edit_slots(self, request):
        """Apply ordinary exact occurrence changes in one immutable successor."""
        edits = request.get("edits")
        if not isinstance(edits, list) or not 1 <= len(edits) <= 31 or any(not isinstance(e, Mapping) for e in edits):
            raise HouseholdError("edit_slots needs one to 31 structured edits")
        key = request.get("idempotency_key")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise HouseholdError("edit_slots requires a stable idempotency_key")
        signature = mp.digest({"action": "edit_slots", "menu_ref": request.get("menu_ref"), "edits": edits})
        snapshot = self.store.read()
        if prior := self._usage_request(snapshot, key, signature):
            return {**prior, "idempotent": True}
        current = mp.exact_menu(snapshot, request.get("menu_ref"))
        mp.slots(current)
        if current.get("phase") == "ordered" or snapshot.get("recipe_usage", {}).get(current["menu_id"], {}).get("status") == "ordered":
            raise HouseholdError("ordered menu slots are immutable; create a distinct new menu")
        today = self._household_today(snapshot).isoformat()
        successor = deepcopy(current)
        mp.bind_preparations(successor)
        successor.pop("menu_id", None)
        successor.pop("revision", None)
        successor.pop("digest", None)
        successor.pop("phase", None)
        successor.pop("order_id", None)
        successor["supersedes"] = mp.menu_ref(current)
        slots = successor["slots"]
        historical = {s["slot_id"] for s in current["slots"] if s["date"] < today or mp.slot_outcome(snapshot, current, s) == "cooked"}
        locks = set(snapshot["menu_planning"]["locks"].get(mp.lock_key(current), []))
        created = {}
        changed = set()
        added = []

        def checked_day(value):
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value or value < today or parsed.isocalendar()[:2] != date.fromisoformat(current["slots"][0]["date"]).isocalendar()[:2]:
                    raise ValueError()
            except (ValueError, TypeError):
                raise HouseholdError("edit date must be today or later in the current menu week") from None
            return value

        def checked_type(value):
            if not isinstance(value, str) or value not in RECIPE_CATEGORIES:
                raise HouseholdError("meal_type must be one of: " + ", ".join(RECIPE_CATEGORIES))
            return value

        def checked_served_with(value, meal_type):
            if value is None:
                return None
            if meal_type != "side" or value not in ("dinner", "lunch", "other"):
                raise HouseholdError("served_with is only for sides and must be dinner, lunch or other")
            return value

        def checked_portions(value):
            if type(value) is not int or not 1 <= value <= 100:
                raise HouseholdError("slot portions must be an integer from one to 100")
            return value

        def resolved_recipe(reference, portions):
            checked, _ = self._planner_reference(reference)
            recipe = self._materialize_menu({"week": current["week"], "dishes": [{**checked, "portions": portions}]})["dishes"][0]
            from dietary_assessment import assess
            conflicts = [finding for finding in assess(snapshot["profile"], recipe, recipe=True) if finding["blocked"]]
            if conflicts:
                raise HouseholdError("household ingredient rules block this meal: " + canonical(conflicts))
            return checked, recipe

        def fresh_slot(edit, index, *, old=None, frozen_recipe=None):
            day = checked_day(edit.get("date", old["date"] if old else None))
            meal_type = checked_type(edit.get("meal_type", old["meal_type"] if old else None))
            portions = checked_portions(edit.get("portions", old["portions"] if old else None))
            if frozen_recipe is None:
                reference, recipe = resolved_recipe(edit.get("reference", old["reference"] if old else None), portions)
            else:
                reference = deepcopy(old["reference"])
                recipe = deepcopy(frozen_recipe)
                recipe.pop("preparation_slot_id", None)
                if portions != old["portions"]:
                    recipe = scale_recipe(recipe, portions)
                self._require_recipe_provider(recipe)
                from dietary_assessment import assess
                if conflicts := [finding for finding in assess(snapshot["profile"], recipe, recipe=True) if finding["blocked"]]:
                    raise HouseholdError("household ingredient rules block this meal: " + canonical(conflicts))
            slot_id = "slot_" + mp.digest({"edit": signature, "index": index})[:32]
            slot = {"slot_id": slot_id, "date": day, "meal_type": meal_type, "portions": portions,
                    "recipe_key": recipe["recipe_key"], "reference": reference, "snapshot_digest": mp.digest(recipe)}
            served_with = checked_served_with(edit.get("served_with", old.get("served_with") if old else None), meal_type)
            if served_with is not None:
                slot["served_with"] = served_with
            fact = edit.get("leafy_green")
            if fact is not None:
                slot["leafy_green"] = normalize_candidate_facts({"leafy_green": fact})["leafy_green"]
            elif old is not None and old.get("snapshot_digest") == slot["snapshot_digest"] and "leafy_green" in old:
                slot["leafy_green"] = deepcopy(old["leafy_green"])
            recipe["preparation_slot_id"] = slot_id
            slots.append(slot)
            successor["dishes"].append(recipe)
            created[index] = slot_id
            added.append(slot)
            return slot

        def remove_slot(slot):
            if slot.get("kind") != "leftover":
                recipe = mp.recipe_for_slot(successor, slot)
                for collection in ("dishes", "salads"):
                    if recipe in successor[collection]:
                        successor[collection].remove(recipe)
                        break
            slots.remove(slot)

        for index, edit in enumerate(edits):
            action = edit.get("action")
            if not isinstance(action, str):
                raise HouseholdError("edit action must be add, replace, remove or move")
            if action == "add":
                if "reference" in edit:
                    if set(edit).difference({"action", "date", "meal_type", "portions", "reference", "leafy_green", "served_with"}):
                        raise HouseholdError("fresh add has unknown fields")
                    fresh_slot(edit, index)
                else:
                    if set(edit).difference({"action", "date", "meal_type", "portions", "source_slot_id", "source_edit_index", "served_with"}):
                        raise HouseholdError("linked add has unknown fields")
                    source_id = edit.get("source_slot_id")
                    if "source_edit_index" in edit:
                        source_index = edit["source_edit_index"]
                        if type(source_index) is not int or not 0 <= source_index < index:
                            raise HouseholdError("source_edit_index must name an earlier fresh add")
                        source_id = created.get(source_index)
                    if not isinstance(source_id, str):
                        raise HouseholdError("linked add needs an exact existing source_slot_id or earlier source_edit_index")
                    source = mp.slot_by_id(successor, source_id)
                    is_current_source = source_id in {s["slot_id"] for s in current["slots"]}
                    if (source.get("kind") == "leftover" or source_id in historical
                            or is_current_source and mp.slot_outcome(snapshot, current, source) is not None):
                        raise HouseholdError("linked source must be an unrecorded future preparation")
                    day = checked_day(edit.get("date"))
                    meal_type = checked_type(edit.get("meal_type", source["meal_type"]))
                    portions = checked_portions(edit.get("portions"))
                    if day <= source["date"] or meal_type != source["meal_type"]:
                        raise HouseholdError("linked serving must follow its preparation and have the same meal type")
                    slot = {"slot_id": "slot_" + mp.digest({"edit": signature, "index": index})[:32],
                            "date": day, "meal_type": meal_type, "portions": portions,
                            "kind": "leftover", "source_slot_id": source_id,
                            "recipe_key": source["recipe_key"], "reference": deepcopy(source["reference"]),
                            "snapshot_digest": source["snapshot_digest"]}
                    served_with = checked_served_with(edit.get("served_with"), meal_type)
                    if served_with is not None:
                        slot["served_with"] = served_with
                    if "leafy_green" in source:
                        slot["leafy_green"] = deepcopy(source["leafy_green"])
                    slots.append(slot)
                    added.append(slot)
            elif action in {"remove", "replace", "move"}:
                allowed = {"action", "slot_id"} if action == "remove" else (
                    {"action", "slot_id", "date", "meal_type", "portions", "reference", "leafy_green", "served_with"} if action == "replace" else
                    {"action", "slot_id", "date", "meal_type", "portions", "leafy_green", "served_with"})
                if set(edit).difference(allowed):
                    raise HouseholdError("slot edit has unknown fields")
                slot = mp.slot_by_id(successor, edit.get("slot_id"))
                if slot["slot_id"] in historical or slot["slot_id"] in locks or slot["date"] < today:
                    raise HouseholdError("historical or locked meal slots are immutable")
                if slot["slot_id"] in changed:
                    raise HouseholdError("one edit may change each existing slot only once")
                changed.add(slot["slot_id"])
                if action in {"replace", "move"} and slot.get("kind") == "leftover":
                    raise HouseholdError("replace or move a linked serving by removing it and adding an exact new serving")
                old = deepcopy(slot)
                if action == "move" and not any(field in edit for field in ("date", "meal_type", "portions", "leafy_green", "served_with")):
                    raise HouseholdError("move needs a changed date, type, portions or assessment")
                frozen_recipe = deepcopy(mp.recipe_for_slot(successor, slot)) if action == "move" else None
                remove_slot(slot)
                if action == "replace":
                    if "reference" not in edit:
                        raise HouseholdError("replace needs an exact recipe reference")
                    fresh_slot(edit, index, old=old)
                elif action == "move":
                    fresh_slot(edit, index, old=old, frozen_recipe=frozen_recipe)
            else:
                raise HouseholdError("edit action must be add, replace, remove or move")

        if not slots or len(slots) > 31:
            raise HouseholdError("edited menu needs one to 31 meal slots")
        dinner_dates = [s["date"] for s in slots if s["meal_type"] == "dinner"]
        if len(dinner_dates) != len(set(dinner_dates)):
            raise HouseholdError("a date already has a dinner; add a side or another course")
        if any(s.get("served_with") == "dinner" and s["date"] not in dinner_dates for s in slots):
            raise HouseholdError("a dinner side needs a dinner on the same date")
        slot_ids = {s["slot_id"] for s in slots}
        if any(s.get("source_slot_id") not in slot_ids for s in slots if s.get("kind") == "leftover"):
            raise HouseholdError("a preparation has linked servings; remove or replace the whole component together")
        successors = []
        for source in mp.preparation_slots(successor):
            dependents = sorted((s for s in slots if s.get("source_slot_id") == source["slot_id"]), key=mp.slot_order)
            old_batch = next((b for b in bp.sources(current) if b["source_slot_id"] == source["slot_id"]), None)
            if old_batch:
                old_source = mp.slot_by_id(current, source["slot_id"])
                old_dependents = sorted((s for s in current["slots"] if s.get("source_slot_id") == source["slot_id"]), key=mp.slot_order)
                if source == old_source and dependents == old_dependents:
                    successors.append(deepcopy(old_batch))
                    continue
                old_component = [old_source] + [s for s in current["slots"]
                                                if s.get("source_slot_id") == source["slot_id"]]
                if any(s["slot_id"] in historical or s["slot_id"] in locks
                       or mp.slot_outcome(snapshot, current, s) is not None for s in old_component):
                    raise HouseholdError("recorded batch servings are immutable; edit only an independent future component")
            if old_batch and not dependents:
                continue
            if not dependents:
                continue
            last = date.max
            storage = deepcopy(old_batch["storage"]) if old_batch else {"basis": "unknown"}
            if storage.get("max_interval_days") is not None:
                last = date.fromisoformat(source["date"]) + timedelta(days=storage["max_interval_days"])
            if storage.get("use_by_date") is not None:
                last = min(last, date.fromisoformat(storage["use_by_date"]))
            if any(s["date"] <= source["date"] or s["date"] > last.isoformat() for s in dependents):
                raise HouseholdError("linked serving dates exceed their preparation or recorded storage interval")
            total = bp.fraction(source["portions"]) + sum((bp.fraction(s["portions"]) for s in dependents), Fraction())
            batch = {"source_slot_id": source["slot_id"], "source_snapshot_digest": source["snapshot_digest"],
                     "prepared_portions": bp.rational(total), "consumed_at_source": bp.rational(bp.fraction(source["portions"])),
                     "unallocated_portions": bp.rational(Fraction()),
                     "suitability": {"source": "unassessed", "value": "unknown"},
                     "storage": storage,
                     "leftovers": [{"replaces_slot_id": s["slot_id"], "date": s["date"], "meal_type": s["meal_type"],
                                    "portions": bp.rational(bp.fraction(s["portions"])), "slot_id": s["slot_id"]} for s in dependents]}
            batch["spec_digest"] = mp.digest(batch)
            successors.append(batch)
        successor["batches"] = successors
        successor.pop("batch", None)
        successor["slots"] = sorted(slots, key=mp.slot_order)
        successor["historical_slot_ids"] = sorted(historical)
        successor["slot_owners"] = {s["slot_id"]: current.get("slot_owners", {}).get(s["slot_id"], current["menu_id"])
                                    for s in slots if s["slot_id"] in {v["slot_id"] for v in current["slots"]}}
        successor["schedule"] = mp.schedule(successor)
        successor["planning_scope"] = deepcopy(current.get("planning_scope") or (current.get("planner_selection") or {}).get("request") or {})
        successor["planning_scope"]["dates"] = sorted(set(dinner_dates))
        expected = (snapshot.get("profile") or {}).get("meals", {}).get("dinner_days")
        successor["weekly_plan_complete"] = type(expected) is int and expected > 0 and len(dinner_dates) == expected
        if len(canonical(successor).encode()) > MAX_MENU_BYTES or len(menu_email_html(successor).encode()) > MAX_EMAIL_HTML_BYTES:
            raise HouseholdError("edited menu exceeds the recipe delivery size limit")
        before = deepcopy(current)
        before["historical_slot_ids"] = sorted(historical)
        comparison = mp.shopping_comparison(before, successor)
        response_probe = {"ok": True, "result": {"menu": successor, "shopping_comparison": comparison, "added_slots": added}}
        if len(json.dumps(response_probe, ensure_ascii=True).encode()) > MAX_REQUEST - 4096:
            raise HouseholdError("menu edit cannot fit the response transport")
        commit = {"successor": successor, "replaced_slot_ids": sorted(changed), "locked_slot_ids": [],
                  "planner_input": {}, "replan_digest": signature, "shopping_comparison": comparison}
        with self._menu_save_commit(current) as state:
            if prior := self._usage_request(state, key, signature):
                return {**prior, "idempotent": True}
            if self._household_today(state).isoformat() != today or self._replan_state_digest(state) != self._replan_state_digest(snapshot):
                raise HouseholdError("menu or household state changed; read the menu and try again")
            if state.get("pending_cart_change"):
                raise HouseholdError("reconcile the pending cart change before editing this menu")
            pending = state.get("pending_checkout") or state.get("order_change")
            if state.get("pending_cancellation") or pending and self._independent_menu_protection(state, current) is None:
                raise HouseholdError("reconcile the linked or unidentified protected operation before editing this menu")
            if pending is None:
                self._abandon_predispatch(state, reason="menu edited")
            result = self._commit_successor(state, commit, preserve_cart_plan=pending is not None)
            receipt = {"menu_ref": mp.menu_ref(result["menu"]), "added_slots": deepcopy(added)}
            self._store_usage_request(state, key, signature, receipt)
            return {**result, **receipt}

    def _materialize_menu(self, value: Any, *, trusted_snapshots: bool = False) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise HouseholdError("menu must be an object")
        week = validate_week(value.get("week"))
        result: dict[str, Any] = {"week": week}
        profile_portions = self.store.read()["profile"]["meals"]["portions"]
        count = 0
        for collection in ("dishes", "salads"):
            values = value.get(collection, [])
            if not isinstance(values, list) or len(values) > 31:
                raise HouseholdError(f"menu {collection} must be a bounded list")
            materialized = []
            for raw in values:
                if not isinstance(raw, Mapping):
                    raise HouseholdError("menu recipes must be objects")
                reference = raw.get("recipe_ref")
                library_reference = raw.get("library_recipe_ref")
                discovery_reference = raw.get("discovery_ref") if "source" not in raw else None
                if sum(item is not None for item in (reference, library_reference, discovery_reference)) > 1:
                    raise RecipeLibraryError("menu recipe must use exactly one recipe reference type")
                if discovery_reference is not None:
                    stored = self.recipes.resolve_discovery(discovery_reference)["recipe"]
                    recipe = scale_recipe(stored, raw.get("portions") if raw.get("portions") is not None else profile_portions)
                    recipe["discovery_ref"] = discovery_reference
                elif library_reference is not None:
                    checked = validate_library_recipe_ref(library_reference)
                    if checked["library_id"] not in self.recipe_libraries:
                        raise RecipeLibraryError("library_recipe_ref names an unconfigured recipe library")
                    if checked["library_id"] == "builtin":
                        stored = self.recipes.get(checked["recipe_id"], checked.get("version"))
                        if stored.get("status") != "active" or stored.get("revision_status", stored.get("status")) != "active":
                            raise HouseholdError("only active recipes can be added to a new menu")
                    else:
                        stored = self._external_library_get(checked)
                    for field in ("library_id", "is_favorite", "favorite_revision", "entry_origin", "pack", "locally_modified"):
                        stored.pop(field, None)
                    recipe = scale_recipe(stored, raw.get("portions") if raw.get("portions") is not None else profile_portions)
                    recipe["library_recipe_ref"] = deepcopy(stored["library_recipe_ref"])
                    recipe["recipe_key"] = library_recipe_key(recipe["library_recipe_ref"])
                elif isinstance(reference, Mapping):
                    stored = self.recipes.get(reference.get("id"), reference.get("revision"))
                    if stored.get("status") != "active" or stored.get("revision_status", stored.get("status")) != "active":
                        raise HouseholdError("only active recipes can be added to a new menu")
                    for field in ("library_id", "is_favorite", "favorite_revision", "entry_origin", "pack", "locally_modified"):
                        stored.pop(field, None)
                    recipe = scale_recipe(stored, raw.get("portions") if raw.get("portions") is not None else profile_portions)
                else:
                    candidate = deepcopy(dict(raw))
                    if not isinstance(candidate.get("source"), Mapping) or not isinstance(candidate.get("rights"), Mapping):
                        raise HouseholdError("new menu recipes require explicit source, relationship and rights metadata")
                    document = (
                        normalize_recipe(candidate, trusted_store_product_hints=True)
                        if trusted_snapshots else self.recipes.prepare_input(candidate)
                    )
                    recipe = scale_recipe(document, candidate.get("portions"))
                self._require_recipe_provider(recipe)
                from planner import equipment_conflicts
                if missing := equipment_conflicts(self.store.read()["profile"], recipe):
                    raise HouseholdError("recipe requires unavailable equipment: " + ", ".join(missing))
                materialized.append(recipe)
                count += 1
            result[collection] = materialized
        if count < 1:
            raise HouseholdError("menu needs at least one complete recipe")
        schedule = value.get("schedule")
        if schedule is not None:
            if not isinstance(schedule, list) or len(schedule) > 31 or any(not isinstance(item, Mapping) for item in schedule):
                raise HouseholdError("menu schedule must be a bounded list of objects")
            result["schedule"] = deepcopy(schedule)
        if value.get("notes") is not None:
            if not isinstance(value["notes"], str) or len(value["notes"]) > 4_000:
                raise HouseholdError("menu notes are invalid")
            result["notes"] = value["notes"].strip()
        rendered_email = menu_email_html(result)
        if len(rendered_email.encode()) > MAX_EMAIL_HTML_BYTES:
            raise HouseholdError("menu recipes exceed the deliverable email size limit")
        if len(json.dumps({"ok": True, "result": {"html": rendered_email}}, ensure_ascii=True).encode()) > MAX_REQUEST - 4_096:
            raise HouseholdError("menu recipe email cannot fit the meal concierge response transport")
        if len(canonical(result).encode()) > MAX_MENU_BYTES:
            raise HouseholdError("menu is too large")
        response_probe = {"ok": True, "result": {"menu": result}}
        if len(json.dumps(response_probe, ensure_ascii=True).encode()) > MAX_REQUEST - 4_096:
            raise HouseholdError("menu cannot fit the meal concierge response transport")
        return result

    @staticmethod
    def _default_planner_dates(week: str, profile: Mapping[str, Any]) -> list[str]:
        match = re.fullmatch(r"(\d{4})-W(\d{2})", validate_week(week))
        monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
        meals = profile.get("meals")
        if not isinstance(meals, Mapping):
            raise PlannerError("profile meals are invalid")
        count = meals.get("dinner_days")
        dishes = meals.get("dishes")
        batch_dishes = meals.get("batch_dishes")
        eat_days = meals.get("eat_days")
        weekdays = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 7:
            raise PlannerError("profile dinner_days must be an integer from one to seven")
        if meals.get("meal_mode", "fresh") == "fresh" and (dishes != count or batch_dishes != 0):
            raise PlannerError(
                "default deterministic planning requires one different dish per dinner day and batch_dishes=0; supply exact dates only for a current explicit count"
            )
        if not isinstance(eat_days, list) or len(eat_days) > 7:
            raise PlannerError("profile eat_days must be a bounded list")
        selected_values = {weekdays.get(str(value).casefold()) for value in eat_days}
        if None in selected_values or len(selected_values) < count:
            raise PlannerError("profile eat_days do not cover dinner_days")
        selected = sorted(selected_values)
        return [(monday + timedelta(days=offset)).isoformat() for offset in selected[:count]]

    def _effective_planner_request(
        self, value: Any, state: Mapping[str, Any], *, anchor_current_date: bool
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value).difference({
            "week", "dates", "portions", "candidates", "strict_targets",
            "cooldown_overrides", "alternatives", "as_of_date", "available_ingredients", "recurring_batch", "prepared_portion_range", "meal_mode", "selection_mode",
        }):
            raise PlannerError("planner input has unknown fields")
        available = normalize_available_ingredients(value.get("available_ingredients"))
        week = validate_week(value.get("week"))
        supplied_as_of_date = value.get("as_of_date")
        if anchor_current_date:
            as_of_date = self._household_today(state).isoformat()
            if supplied_as_of_date is not None and supplied_as_of_date != as_of_date:
                raise PlannerError("planner as_of_date is not current in the household timezone")
        else:
            if not isinstance(supplied_as_of_date, str):
                raise PlannerError("planner as_of_date must be an ISO date")
            try:
                as_of_date = date.fromisoformat(supplied_as_of_date).isoformat()
            except ValueError as exc:
                raise PlannerError("planner as_of_date must be an ISO date") from exc
            if supplied_as_of_date != as_of_date:
                raise PlannerError("planner as_of_date must be a canonical ISO date")
        profile = state.get("profile")
        if not isinstance(profile, Mapping):
            raise PlannerError("household profile is invalid")
        meals = profile.get("meals")
        if not isinstance(meals, Mapping):
            raise PlannerError("profile meals are invalid")
        dates = deepcopy(value.get('dates')) if value.get('dates') is not None else self._default_planner_dates(week, profile)
        if not isinstance(dates, list) or not all(isinstance(d, str) for d in dates):
            raise PlannerError('planner dates must be ISO dates')
        selection_mode = value.get("selection_mode", "ranked")
        if not isinstance(selection_mode, str) or selection_mode not in {"agent", "ranked"}:
            raise PlannerError("selection_mode must be agent or ranked")
        if selection_mode == "agent":
            if dates != sorted(dates):
                raise PlannerError("agent dates must be in chronological order")
            if value.get("candidates") is None:
                raise PlannerError("agent selection requires explicit ordered candidates")
        dates = sorted(dates)
        mode = value.get('meal_mode')
        if mode is not None:
            if mode not in {'fresh', 'batch', 'mixed'}:
                raise PlannerError('meal_mode must be fresh, batch or mixed')
            profile = deepcopy(profile)
            profile['meals']['meal_mode'] = mode
        override = value.get('prepared_portion_range')
        if override is not None:
            if (not isinstance(override, list) or len(override) != 2
                    or any(type(v) is not int or not 1 <= v <= 100 for v in override) or override[0] > override[1]):
                raise PlannerError('prepared_portion_range must be two ordered positive portion counts')
            profile = deepcopy(profile)
            profile['meals']['prepared_portion_range'] = override
        layout = bp.recurring_layout(profile, dates)
        if layout and value.get('portions', meals['portions']) != meals['portions']:
            raise PlannerError('recurring batch consumption must match accepted profile portions; update that explicit setting or use fresh dates')
        if value.get('recurring_batch') is not None and canonical(layout) != canonical(value['recurring_batch']):
            raise PlannerError('recurring batch settings changed; plan again')
        return {
            **({'recurring_batch': layout} if layout else {}),
            **({'prepared_portion_range': deepcopy(override)} if override is not None else {}),
            **({'meal_mode': mode} if mode is not None else {}),
            **({"selection_mode": selection_mode} if "selection_mode" in value else {}),
            **({"available_ingredients": available} if available else {}),
            "week": week,
            "dates": dates,
            "portions": value.get("portions", meals.get("portions")),
            "candidates": deepcopy(value.get("candidates")),
            "strict_targets": deepcopy(value.get("strict_targets", [])),
            "cooldown_overrides": deepcopy(value.get("cooldown_overrides", {})),
            "alternatives": value.get("alternatives", 1),
            "as_of_date": as_of_date,
        }

    @staticmethod
    def _planner_reference(value: Any) -> tuple[dict[str, Any], str]:
        if not isinstance(value, Mapping) or set(value).difference(
            {"recipe_ref", "discovery_ref", "facts"}
        ):
            raise PlannerError("each planner candidate must contain one exact reference")
        recipe_ref = value.get("recipe_ref")
        discovery_ref = value.get("discovery_ref")
        if (recipe_ref is None) == (discovery_ref is None):
            raise PlannerError(
                "each planner candidate requires exactly one recipe_ref or discovery_ref"
            )
        if recipe_ref is not None:
            if (
                not isinstance(recipe_ref, Mapping) or set(recipe_ref) != {"id", "revision"}
                or not isinstance(recipe_ref.get("id"), str)
                or re.fullmatch(r"rec_[a-f0-9]{24}", recipe_ref["id"]) is None
                or isinstance(recipe_ref.get("revision"), bool)
                or not isinstance(recipe_ref.get("revision"), int)
                or recipe_ref["revision"] < 1
            ):
                raise PlannerError("planner recipe_ref must contain an exact id and revision")
            reference = {"recipe_ref": {"id": recipe_ref["id"], "revision": recipe_ref["revision"]}}
        else:
            if (
                not isinstance(discovery_ref, str)
                or re.fullmatch(
                    r"discovery:v1:[A-Za-z0-9_-]{16}:[A-Za-z0-9_-]{16,64}",
                    discovery_ref,
                ) is None
            ):
                raise PlannerError("planner discovery_ref must be bounded exact text")
            reference = {"discovery_ref": discovery_ref}
        return reference, canonical(reference)

    def _planner_history_index(self, state):
        def resolve(reference):
            if "recipe_ref" in reference:
                ref = reference["recipe_ref"]
                return self.recipes.get(ref["id"], ref["revision"])
            return self.recipes.resolve_discovery(reference["discovery_ref"])["recipe"]
        return history_source_index(state, resolve)

    def _planner_family_usage(self, candidate, state, week, index):
        return family_history_usage(candidate, index, lambda key: self._usage_summary(state, key, week))

    def _resolve_planner_candidates(
        self, request: Mapping[str, Any], state: Mapping[str, Any], *, history_index=None
    ) -> list[dict[str, Any]]:
        history = state.get("recipe_usage")
        if not isinstance(history, Mapping) or len(history) > MAX_HISTORY_RECORDS:
            raise PlannerError(
                f"planner history exceeds {MAX_HISTORY_RECORDS} records"
            )
        raw_candidates = request.get("candidates")
        if not isinstance(raw_candidates, list) or not 1 <= len(raw_candidates) <= MAX_CANDIDATES:
            raise PlannerError(
                f"planner candidates must contain one to {MAX_CANDIDATES} entries"
            )
        resolved = []
        history_index = history_index if history_index is not None else self._planner_history_index(state)
        seen = set()
        for raw in raw_candidates:
            reference, reference_key = self._planner_reference(raw)
            if reference_key in seen:
                raise PlannerError("planner candidates contain a duplicate exact reference")
            seen.add(reference_key)
            metadata = {}
            if "recipe_ref" in reference:
                exact = reference["recipe_ref"]
                stored = self.recipes.get(exact["id"], exact["revision"])
                materialization_error = None
                if (
                    stored.get("status") != "active"
                    or stored.get("revision_status", stored.get("status")) != "active"
                ):
                    materialization_error = "only active built-in recipe revisions can be planned"
                key = stored["recipe_key"]
                # Exact revisions read from the service-owned recipe bank may
                # carry retailer-detail evidence that ordinary client recipe
                # input is never allowed to assert.
                recipe = normalize_recipe(stored, trusted_store_product_hints=True)
                metadata = {field: deepcopy(stored[field]) for field in ("is_favorite", "entry_origin", "locally_modified") if field in stored}
            else:
                snapshot = self.recipes.resolve_discovery(reference["discovery_ref"])
                recipe = snapshot["recipe"]
                key = recipe_key(recipe)
                materialization_error = None
            try:
                scale_recipe(recipe, request.get("portions"))
            except RecipeError as exc:
                materialization_error = str(exc)
            if problem := recipe_provider_problem(recipe, self.provider):
                materialization_error = problem
            supplied_facts = deepcopy(raw.get("facts", {})) if isinstance(raw, Mapping) else {}
            resolved.append({
                **metadata,
                "reference": reference,
                "reference_key": reference_key,
                "recipe": recipe,
                "recipe_key": key,
                "dedupe_key": recipe_key(recipe),
                "content_digest": hashlib.sha256(canonical(recipe).encode()).hexdigest(),
                "usage": self._usage_summary(state, key, request["week"]),
                "facts": supplied_facts,
                "supplied_facts": supplied_facts,
                "materialization_error": materialization_error,
            })
            resolved[-1]["usage"] = self._planner_family_usage(resolved[-1], state, request["week"], history_index)
        return resolved

    @staticmethod
    def _planner_feedback(candidate, state, as_of_date, feedback=None):
        feedback = feedback if feedback is not None else pf.effective(state["planning_feedback"], as_of_date)
        keys = set(candidate.get("usage", {}).get("family_recipe_keys", [candidate["recipe_key"]]))
        signals = [feedback["signals"][key] for key in keys if key in feedback["signals"]]
        candidate.pop("planning_feedback", None)
        if signals:
            candidate["planning_feedback"] = {"weight": max(-6, min(6, sum(signals))),
                "policy_version": pf.POLICY_VERSION,
                "events": [event for event in feedback["events"] if event["recipe_key"] in keys]}

    def _run_planner(
        self, request: Mapping[str, Any], resolved: list[Mapping[str, Any]],
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        history = state.get("recipe_usage")
        if not isinstance(history, Mapping) or len(history) > MAX_HISTORY_RECORDS:
            raise PlannerError(
                f"planner history exceeds {MAX_HISTORY_RECORDS} records"
            )
        feedback = pf.effective(state["planning_feedback"], request["as_of_date"])
        refreshed = []
        index = self._planner_history_index(state)
        for candidate in resolved:
            item = deepcopy(dict(candidate))
            item["usage"] = self._planner_family_usage(item, state, request["week"], index)
            self._planner_feedback(item, state, request["as_of_date"], feedback)
            refreshed.append(item)
        profile = state.get("profile")
        if not isinstance(profile, Mapping):
            raise PlannerError("household profile is invalid")
        return plan_week(
            request, profile=profile, candidates=refreshed, history=history, feedback=feedback
        )

    def _plan_menu(
        self, value: Any, *, state: Mapping[str, Any] | None = None,
        anchor_current_date: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        snapshot = deepcopy(dict(state)) if isinstance(state, Mapping) else self.store.read()
        web_candidates, web_result = None, None
        if isinstance(value, Mapping):
            value = dict(value)
            web_candidates = value.pop("web_candidates", None)
            web_result = value.pop("web_search_result", None)
            if value.get("candidates") is not None and (web_candidates is not None or web_result is not None):
                raise PlannerError("web_candidates supplement automatic discovery; omit candidates")
        request = self._effective_planner_request(
            value, snapshot, anchor_current_date=anchor_current_date
        )
        collection = None
        if request["candidates"] is None:
            _validate_request(request, allow_discovery=True)
            collection = self._collect_planner_candidates(request, snapshot, web_candidates=web_candidates, web_result=web_result)
            request["candidates"] = [{**item["reference"], **({"facts": item["supplied_facts"]} if item.get("supplied_facts") else {})}
                                     for item in collection["candidates"]]
        resolved = self._resolve_planner_candidates(request, snapshot) if collection is None or request["candidates"] else []
        result = self._run_planner(request, resolved, snapshot) if resolved else {"status": "needs_input", "save_handoffs": []}
        if collection is not None:
            result["discovery"] = {key: deepcopy(value) for key, value in collection.items() if key not in {"candidates", "unknown", "rejected"}}
            for category in ("unknown", "rejected"):
                result["discovery"][category] = [{**compact_candidate(item["recipe"], item["reference"]), "recipe_digest": item["content_digest"], "hard_constraints": item["hard_constraints"]}
                                                  for item in collection[category]]
        result["cooking_experiences"] = pf.experiences(snapshot["planning_feedback"], {r["recipe_key"] for r in resolved})[-36:]
        if len(json.dumps({"ok": True, "result": {"plan": result}}, ensure_ascii=True).encode()) > MAX_REQUEST - 4_096:
            raise PlannerError("planner result cannot fit the response transport")
        return result, resolved, request

    @staticmethod
    def _planner_ref(handoff: Mapping[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(handoff[key]) for key in (
            "planner_version", "input_digest", "selection_digest", "request"
        )}

    def _resolve_planner_ref(
        self, value: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        if not isinstance(value, Mapping) or set(value) != {
            "planner_version", "input_digest", "selection_digest", "request"
        }:
            raise PlannerError("planner_ref must be one complete server-returned save_ref")
        if value.get("planner_version") != PLANNER_VERSION or any(
            not isinstance(value.get(field), str)
            or re.fullmatch(r"[a-f0-9]{64}", value[field]) is None
            for field in ("input_digest", "selection_digest")
        ):
            raise PlannerError("planner_ref version or digests are invalid")
        if not isinstance(value.get("request"), Mapping) or not isinstance(
            value["request"].get("candidates"), list
        ) or not value["request"]["candidates"]:
            raise PlannerError("planner_ref requires the exact resolved candidate request")
        result, resolved, request = self._plan_menu(
            value["request"], anchor_current_date=False
        )
        for handoff in result.get("save_handoffs", []):
            if canonical(self._planner_ref(handoff)) == canonical(value):
                return handoff, resolved, request
        raise PlannerError("planner_ref is stale, changed or fabricated; generate it again")

    @staticmethod
    def _matching_planner_handoff(
        result: Mapping[str, Any], handoff: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        values = result.get("save_handoffs")
        if not isinstance(values, list):
            return None
        supplied_digest = handoff.get("selection_digest")
        return next((
            value for value in values
            if isinstance(value, Mapping)
            and value.get("selection_digest") == supplied_digest
            and canonical(value) == canonical(handoff)
        ), None)

    def _verify_planner_handoff(
        self, value: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        if not isinstance(value, Mapping) or set(value) != {
            "planner_version", "input_digest", "selection_digest", "request", "selection"
        }:
            raise PlannerError("planner_handoff must be one complete server-returned handoff")
        if value.get("planner_version") != PLANNER_VERSION:
            raise PlannerError("planner_handoff uses an unsupported planner version")
        if any(
            re.fullmatch(r"[a-f0-9]{64}", str(value.get(field) or "")) is None
            for field in ("input_digest", "selection_digest")
        ):
            raise PlannerError("planner_handoff digests are invalid")
        result, resolved, request = self._plan_menu(
            value.get("request"), anchor_current_date=False
        )
        if result.get("status") != "planned" or self._matching_planner_handoff(result, value) is None:
            raise PlannerError("planner_handoff is stale, changed or fabricated; generate it again")
        return result, resolved, request

    def _materialize_planner_menu(
        self, handoff: Mapping[str, Any], resolved: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        selection = handoff["selection"]
        slots = selection.get("source_slots", selection.get("slots")) if isinstance(selection, Mapping) else None
        if not isinstance(slots, list) or not slots:
            raise PlannerError("planner_handoff selection is invalid")
        by_reference = {item["reference_key"]: item for item in resolved}
        dishes = []
        schedule = []
        for slot in slots:
            if not isinstance(slot, Mapping):
                raise PlannerError("planner_handoff selection is invalid")
            candidate = by_reference.get(slot.get("reference_key"))
            if candidate is None or canonical(candidate["reference"]) != canonical(slot.get("reference")):
                raise PlannerError("planner_handoff selection reference is invalid")
            portions = slot.get("portions")
            if "recipe_ref" in candidate["reference"]:
                dishes.append({
                    "recipe_ref": deepcopy(candidate["reference"]["recipe_ref"]),
                    "portions": portions,
                })
            else:
                dishes.append(scale_recipe(candidate["recipe"], portions))
            schedule.append({
                "day": slot.get("date"),
                "meal": slot.get("name"),
                "portions": portions,
                "meal_type": "dinner",
                "recipe_key": slot.get("recipe_key"),
                "reference": deepcopy(slot.get("reference")),
            })
        menu = self._materialize_menu({
            "week": handoff["request"]["week"],
            "dishes": dishes,
            "salads": [],
            "schedule": schedule,
        }, trusted_snapshots=True)
        menu["slots"] = [{
            "slot_id": "slot_" + mp.digest({"selection": handoff["selection_digest"], "date": slot["date"]})[:32],
            "date": slot["date"], "meal_type": "dinner", "portions": slot["portions"], "recipe_key": slot["recipe_key"],
            "reference": deepcopy(slot["reference"]), "snapshot_digest": mp.digest(recipe),
            "leafy_green": deepcopy(slot["leafy_green"]),
        } for slot, recipe in zip(slots, menu["dishes"], strict=True)]
        if handoff['request'].get('recurring_batch'):
            menu["planning_scope"] = {
                "selection_mode": handoff["request"].get("selection_mode", "ranked"),
                "strict_targets": deepcopy(handoff["request"].get("strict_targets", [])),
            }
            bp.attach_recurring(menu, handoff['request']['recurring_batch'], resolved)
        if handoff["request"].get("available_ingredients"):
            menu["available_ingredients"] = deepcopy(handoff["request"]["available_ingredients"])
        menu["planner_selection"] = {
            "planner_version": handoff["planner_version"],
            "input_digest": handoff["input_digest"],
            "selection_digest": handoff["selection_digest"],
            "request": deepcopy(handoff["request"]),
            "selection": deepcopy(selection),
        }
        if len(canonical(menu).encode()) > MAX_MENU_BYTES:
            raise PlannerError("planned menu is too large")
        if len(json.dumps({"ok": True, "result": {"menu": menu}}, ensure_ascii=True).encode()) > MAX_REQUEST - 4_096:
            raise PlannerError("planned menu cannot fit the response transport")
        return menu

    @staticmethod
    def _abandon_predispatch(state: dict[str, Any], *, reason: str) -> None:
        pending = state.get("pending_checkout")
        if not pending:
            return
        if pending.get("status") != "awaiting_confirmation":
            raise HouseholdError("checkout is pending and may have been dispatched; reconcile it before changing the menu")
        occurrence = pending.get("occurrence")
        if occurrence and isinstance(state.get("occurrences", {}).get(occurrence), dict):
            state["occurrences"][occurrence]["status"] = "abandoned"
            state["occurrences"][occurrence]["reason"] = reason
        state["pending_checkout"] = None

    @contextmanager
    def _menu_save_commit(self, candidate):
        # The state lock serializes this menu write with a payment handler's
        # journal writes. A held browser lock is safe only for a proven
        # independent menu; linked and unidentified work retains its fence.
        with self.product_plan_lock:
            acquired = self.browser_lock.acquire(blocking=False)
            try:
                with self.store.locked() as state:
                    if not acquired and self._independent_menu_protection(state, candidate) is None:
                        raise HouseholdError("finish the active provider operation before saving a menu")
                    yield state
            finally:
                if acquired:
                    self.browser_lock.release()

    def _menu_save_pending_payment(self, state, candidate):
        if state.get("pending_cancellation"):
            raise HouseholdError("finish the pending order cancellation before saving a menu")
        pending = state.get("pending_checkout")
        change = state.get("order_change")
        if not change and (not isinstance(pending, Mapping) or pending.get("status") == "awaiting_confirmation"):
            return None, set()
        independent = self._independent_menu_protection(state, candidate)
        if independent is not None:
            return pending or {"order_change": change}, independent
        if change:
            raise HouseholdError("reconcile the linked or unidentified order change before saving a menu")
        if (pending.get("status") not in UNRESOLVED_CHECKOUT_STATUSES
                or pending.get("order_change")):
            raise HouseholdError("reconcile the pending protected operation before saving a menu")
        attribution = (pending.get("summary") or {}).get("menu_attribution")
        if attribution is None:
            attribution = self._checkout_menu_attribution(pending.get("menu"), pending.get("cart_plan"))
        if attribution == "menu_bound":
            raise HouseholdError("reconcile the linked or unidentified pending purchase before saving a menu")
        child = pending.get("recovery")
        if isinstance(child, Mapping) and child.get("status") == "awaiting_confirmation":
            raise HouseholdError("resolve the prepared recovery review through checkout before saving a menu")
        attempt = child if isinstance(child, Mapping) else pending
        if attempt.get("status") == "clicking" and not (
                attempt.get("authentication_context") or self._checkout_payment_closed(pending)):
            raise HouseholdError("reconcile the active payment submission before saving a menu")
        return pending, set()

    def _menu(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "get")
        planner_ref = request.get("planner_ref")
        if planner_ref is not None and (
            action not in {"save", "resolve_handoff"} or request.get("planner_handoff") is not None
            or request.get("menu") is not None
        ):
            raise PlannerError("planner_ref requires save or resolve_handoff without planner_handoff or legacy menu")
        if action == "resolve_handoff":
            handoff, _resolved, _planner_request = self._resolve_planner_ref(planner_ref)
            return {"planner_handoff": handoff}
        if action == "assess":
            state = self.store.read()
            current = state.get("menu")
            return {"assessment": assess_menu(state),
                    **({"minimum_evaluation": saved_menu_minimum_evaluation(current, state.get("profile") or {})}
                       if isinstance(current, Mapping) else {})}
        if action == "get":
            state = self.store.read()
            current = state.get("menu")
            return {"menu": deepcopy(current), "assessment": assess_menu(state), "feedback_targets": feedback_targets(current), "slot_replan_available": bool(current and current.get("slots")),
                    **({"minimum_evaluation": saved_menu_minimum_evaluation(current, state.get("profile") or {})}
                       if isinstance(current, Mapping) else {}),
                    "batch_dependencies": bp.dependency_status(state, current) if current else [],
                    "locks": deepcopy(state["menu_planning"]["locks"].get(mp.lock_key(current), [])) if current else []}
        if action in {"add_slot", "edit_slots"}:
            setup_gate = self._setup_gate(request)
            if setup_gate is not None:
                return setup_gate
            return self._edit_slots(request) if action == "edit_slots" else self._add_slot(request)
        if action in {"lock", "replan_prepare", "replan_apply", "batch_prepare", "batch_apply"}:
            return self._replanning(request)
        if action == "plan":
            setup_gate = self._setup_gate(request)
            if setup_gate is not None:
                return setup_gate
            result, _resolved, _planner_request = self._plan_menu(
                request.get("planner_input")
            )
            if result.get("save_handoff") is not None:
                result["save_ref"] = self._planner_ref(result["save_handoff"])
                result["alternatives"] = [
                    {"save_ref": self._planner_ref(handoff), "selection": deepcopy(handoff["selection"])}
                    for handoff in result["save_handoffs"][1:]
                ]
            return {"plan": result}
        if action == "clear":
            with self.product_plan_lock, self.store.locked() as state:
                current = state.get("menu")
                if isinstance(current, Mapping) and (current.get("menu_id") or current.get("revision") is not None):
                    supplied_ref = request.get("menu_ref")
                    if not isinstance(supplied_ref, Mapping) or set(supplied_ref) != {"menu_id", "revision", "digest"}:
                        raise HouseholdError("menu clear requires the exact menu_ref from menu get")
                    if canonical(supplied_ref) != canonical(mp.menu_ref(current)):
                        raise HouseholdError("menu_ref does not match the current menu; call menu get and retry with its exact menu_ref")
                self._abandon_predispatch(state, reason="menu cleared")
                if isinstance(current, Mapping):
                    mp.retire_planned_slots(state, current)
                    usage = state.setdefault("recipe_usage", {}).get(current.get("menu_id"))
                    if isinstance(usage, dict) and usage.get("status") == "planned":
                        usage["status"] = "cancelled"
                state["menu"] = None
                state["cart_plan"] = None
                state.pop("managed_product_apply_fence", None)
                return {"menu": None}
        if action == "save":
            setup_gate = self._setup_gate(request)
            if setup_gate is not None:
                return setup_gate
            baseline_menu = deepcopy(self.store.read().get("menu"))
            planner_handoff = request.get("planner_handoff")
            planner_context = None
            if planner_handoff is not None or planner_ref is not None:
                if (
                    request.get("menu") is not None
                    or request.get("allow_repeat_keys") not in (None, [])
                    or request.get("override_reason") not in {None, ""}
                ):
                    raise PlannerError(
                        "planner save accepts planner_handoff without legacy menu or cooldown overrides"
                    )
                existing_planner = (
                    baseline_menu.get("planner_selection")
                    if isinstance(baseline_menu, Mapping) else None
                )
                if planner_ref is not None:
                    if isinstance(existing_planner, Mapping) and canonical(
                        self._planner_ref(existing_planner)
                    ) == canonical(planner_ref):
                        return {"menu": baseline_menu, "idempotent": True}
                    planner_handoff, resolved, planner_request = self._resolve_planner_ref(planner_ref)
                if (
                    isinstance(existing_planner, Mapping)
                    and canonical(existing_planner) == canonical(planner_handoff)
                ):
                    return {"menu": baseline_menu, "idempotent": True}
                if planner_ref is None:
                    _planner_result, resolved, planner_request = self._verify_planner_handoff(
                        planner_handoff
                    )
                menu = self._materialize_planner_menu(planner_handoff, resolved)
                planner_context = (deepcopy(planner_handoff), resolved, planner_request)
                override_map = dict(planner_request["cooldown_overrides"])
            else:
                menu = self._materialize_menu(request.get("menu"))
                repeat_keys = request.get("allow_repeat_keys", [])
                if not isinstance(repeat_keys, list) or len(repeat_keys) > 62 or not all(isinstance(key, str) and 1 <= len(key) <= MAX_LIBRARY_RECIPE_KEY for key in repeat_keys):
                    raise HouseholdError("allow_repeat_keys must be a list of recipe keys")
                override_reason = str(request.get("override_reason") or "").strip()
                if repeat_keys and not override_reason:
                    raise HouseholdError("a cooldown override reason is required")
                if len(override_reason) > 500:
                    raise HouseholdError("cooldown override reason is too long")
                override_map = {key: override_reason for key in repeat_keys}
            supplied_ref = request.get("menu_ref")
            if supplied_ref is not None and (
                not isinstance(supplied_ref, Mapping)
                or set(supplied_ref) != {"menu_id", "revision", "digest"}
            ):
                raise HouseholdError("menu update requires the exact menu_ref from menu get")
            supplied_menu_id = str(supplied_ref.get("menu_id") or "") if supplied_ref else None
            expected_revision = supplied_ref.get("revision") if supplied_ref else None
            def matched_override(key: str) -> str | None:
                aliases = library_recipe_key_aliases(key)
                return next((
                    reason for supplied_key, reason in override_map.items()
                    if aliases.intersection(library_recipe_key_aliases(supplied_key))
                ), None)

            digest = menu_digest(menu)
            keys = [recipe["recipe_key"] for collection in ("dishes", "salads") for recipe in menu[collection]]
            seen_keys: set[str] = set()
            duplicate_key = False
            for key in keys:
                aliases = library_recipe_key_aliases(key)
                if seen_keys.intersection(aliases):
                    duplicate_key = True
                    break
                seen_keys.update(aliases)
            if duplicate_key:
                raise HouseholdError("the same recipe cannot appear twice in one menu")
            with self._menu_save_commit(menu) as state:
                meals = (state.get("profile") or {}).get("meals") or {}
                expected_dinners = meals.get("dinner_days")
                has_explicit_slots = isinstance(menu.get("slots"), list)
                dinner_slots = [
                    slot for slot in menu.get("slots", [])
                    if isinstance(slot, Mapping) and slot.get("meal_type") == "dinner"
                ] if has_explicit_slots else []
                observed_dinners = len(dinner_slots) if has_explicit_slots else len(menu.get("dishes", []))
                menu["weekly_plan_complete"] = (
                    type(expected_dinners) is int and expected_dinners > 0
                    and observed_dinners == expected_dinners
                )
                digest = menu_digest(menu)
                current = state.get("menu")
                if isinstance(current, Mapping) and current.get("digest") == digest:
                    return {"menu": deepcopy(current), "idempotent": True}
                pending_payment, protected_menu_ids = self._menu_save_pending_payment(state, menu)
                if isinstance(current, Mapping) and supplied_ref is None:
                    raise HouseholdError(
                        "replacing the current menu requires its exact menu_ref from menu get"
                    )
                if (protected_menu_ids and isinstance(current, Mapping)
                        and current.get("menu_id") not in protected_menu_ids
                        and self._independent_menu_protection(state, current) is None):
                    raise HouseholdError(
                        "current menu shares protected order slots; reconcile the linked operation before replacing it"
                    )
                if planner_context is None:
                    for collection in ("dishes", "salads"):
                        for recipe in menu[collection]:
                            if "recipe_ref" not in recipe and "library_recipe_ref" not in recipe:
                                validate_recipe_image(recipe, self.recipes.assets)
                if planner_context is not None:
                    original_handoff, resolved, planner_request = planner_context
                    current_result = self._run_planner(
                        planner_request, resolved, state
                    )
                    if (
                        current_result.get("status") != "planned"
                        or self._matching_planner_handoff(
                            current_result, original_handoff
                        ) is None
                    ):
                        raise PlannerError(
                            "planner_handoff became stale before save; generate it again"
                        )
                minimums = saved_menu_minimum_evaluation(menu, state.get("profile") or {})
                if minimums.get("complete_menu") and minimums.get("enforced_status", minimums.get("status")) != "pass":
                    raise PlannerError(
                        "complete weekly menu does not satisfy saved dietary minimums: "
                        + canonical(minimums)
                    )
                if supplied_menu_id:
                    if not isinstance(current, Mapping) or canonical(supplied_ref) != canonical(mp.menu_ref(current)):
                        raise HouseholdError("menu_ref does not match the current menu; call menu get and retry with its exact menu_ref")
                    if pending_payment or current.get("week") != menu.get("week"):
                        menu_id = f"menu_{secrets.token_hex(12)}"
                        revision = 1
                    else:
                        if current.get("supersedes"):
                            raise HouseholdError("a successor preserves immutable lineage; use replan instead of revision edits")
                        current_usage = state.setdefault("recipe_usage", {}).get(supplied_menu_id)
                        if current.get("phase") == "ordered" or (isinstance(current_usage, Mapping) and current_usage.get("status") == "ordered"):
                            raise HouseholdError("an ordered menu is immutable; save a new menu instead")
                        if isinstance(current_usage, Mapping) and (
                            current_usage.get("cooked_keys")
                            or current_usage.get("not_cooked_keys")
                            or current_usage.get("cooldown_overrides")
                        ):
                            raise HouseholdError("a menu with explicit usage history is immutable; save a new menu instead")
                        menu_id = supplied_menu_id
                        revision = expected_revision + 1
                else:
                    if canonical(current) != canonical(baseline_menu):
                        raise HouseholdError("menu changed while saving; read it and try again")
                    menu_id = f"menu_{secrets.token_hex(12)}"
                    revision = 1
                if not pending_payment:
                    self._abandon_predispatch(state, reason="menu replaced")
                blocked = []
                cooldown_state = deepcopy(state)
                if isinstance(current, Mapping) and current.get("menu_id") not in protected_menu_ids:
                    mp.retire_planned_slots(cooldown_state, current)
                current_usage = state.setdefault("recipe_usage", {}).get(current.get("menu_id")) if isinstance(current, Mapping) else None
                for key in keys:
                    ignored_menu_id = (
                        current.get("menu_id")
                        if isinstance(current_usage, Mapping)
                        and current.get("menu_id") not in protected_menu_ids
                        and current_usage.get("status") == "planned"
                        and self._matching_recipe_key(key, current_usage.get("cooked_keys")) is None
                        and not library_recipe_key_aliases(key).intersection(
                            (current_usage.get("cooldown_overrides") or {}).keys()
                        )
                        else None
                    )
                    summary = self._usage_summary(cooldown_state, key, menu["week"], ignore_menu_id=ignored_menu_id)
                    if (not summary["eligible"] and matched_override(key) is None
                            and not (planner_context is not None and planner_request.get("selection_mode") == "agent")):
                        blocked.append({"recipe_key": key, "usage": summary})
                if blocked:
                    raise HouseholdError(f"recipe cooldown blocks this menu: {canonical(blocked)}")
                if isinstance(current, Mapping):
                    if current.get("menu_id") != menu_id and current.get("menu_id") not in protected_menu_ids:
                        mp.retire_planned_slots(state, current)
                    old_usage = state.setdefault("recipe_usage", {}).get(current.get("menu_id"))
                    if (isinstance(old_usage, dict) and old_usage.get("status") == "planned"
                            and current.get("menu_id") not in {menu_id, *protected_menu_ids}):
                        old_usage["status"] = "cancelled"
                menu.update({"menu_id": menu_id, "revision": revision, "digest": digest, "phase": "draft"})
                state["menu"] = deepcopy(menu)
                if not pending_payment:
                    self._clear_persisted_product_plan(state.get("cart_plan"))
                state.setdefault("recipe_usage", {})[menu_id] = {
                    "week": menu["week"], "status": "planned", "recipe_keys": keys,
                    "cooked_keys": [], "not_cooked_keys": [],
                    "cooldown_overrides": {
                        key: matched_override(key)
                        for key in keys
                        if matched_override(key) is not None
                    },
                    "order_id": None, "updated_at": self._now().isoformat(),
                    "slots": deepcopy(menu.get("slots", [])), "cooked_slot_ids": [], "not_cooked_slot_ids": [],
                }
                return {"menu": deepcopy(menu)}
        raise HouseholdError("unknown menu action")

    def _scheduler_owner(self, state):
        schedule = state["schedule"]
        if "scheduler_owner" not in schedule:
            return None
        owner = schedule["scheduler_owner"]
        if (not isinstance(owner, Mapping) or owner.get("state") not in {"active", "handover"}
                or not isinstance(owner.get("generation"), str) or not owner["generation"]):
            raise HouseholdError("invalid managed scheduler owner; inspect and reconcile it")
        self._scheduler_scope(owner.get("owner"))
        return owner

    def _scheduler_scope(self, value):
        if not isinstance(value, Mapping) or set(value) != {"platform", "scope"}:
            raise HouseholdError("scheduler owner requires exact platform and scope")
        self._scheduler_binding({**value, "job_id": "scope"})
        return dict(value)

    def _native_scheduler_record(self, record):
        if "scheduler" not in record:
            return None
        scheduler = record["scheduler"]
        if (not isinstance(scheduler, Mapping) or scheduler.get("state") not in {"active", "paused", "handover"}
                or not isinstance(scheduler.get("generation"), str) or not scheduler["generation"]):
            raise HouseholdError("invalid managed scheduler job; inspect and reconcile it")
        self._scheduler_binding(scheduler.get("binding"))
        if scheduler.get("previous_binding") is not None:
            self._scheduler_binding(scheduler["previous_binding"])
        return scheduler

    def _native_bindings(self, record):
        scheduler = self._native_scheduler_record(record)
        if scheduler is None:
            return []
        result = [] if scheduler.get("job_removed") is True else [scheduler["binding"]]
        if scheduler.get("previous_binding") and scheduler.get("previous_job_removed") is not True:
            result.append(scheduler["previous_binding"])
        return result

    def _check_native_collision(self, state, record, *bindings):
        for other in [state["schedule"], *state["email_jobs"]]:
            if other is record:
                continue
            reserved = self._native_bindings(other)
            if any(binding is not None and binding in reserved for binding in bindings):
                raise HouseholdError("native scheduler job is already bound to another weekly or email job")

    def _scheduler_target(self, state, binding):
        owner = self._scheduler_owner(state)
        if owner and {key: binding[key] for key in ("platform", "scope")} != owner["owner"]:
            raise HouseholdError("native job does not belong to the selected scheduler owner")
        return owner["generation"] if owner else None

    def _scheduler_dispatch_owner(self, state, scheduler):
        owner = self._scheduler_owner(state)
        if owner:
            if (owner["state"] != "active" or not scheduler
                    or scheduler.get("owner_generation") != owner["generation"]):
                raise HouseholdError("scheduler owner is awaiting verified adoption or handover")
            self._scheduler_target(state, scheduler["binding"])

    def _unresolved_scheduled_effects(self, state):
        return any(isinstance(row, Mapping) and isinstance(row.get("delivery_effect"), Mapping)
                   and row["delivery_effect"].get("state") in {"dispatching", "uncertain"}
                   for row in state["occurrences"].values())

    def _weekly_invocation(self, schedule):
        scheduler = self._native_scheduler_record(schedule)
        return {key: deepcopy(scheduler.get(key)) for key in
                ("binding", "generation", "owner_generation", "settings_digest")}

    def _require_weekly_scheduler(self, state, supplied):
        schedule = state["schedule"]
        scheduler = self._native_scheduler_record(schedule)
        self._scheduler_dispatch_owner(state, scheduler)
        if scheduler:
            if (scheduler["state"] != "active" or scheduler.get("job_removed") is True
                    or scheduler.get("settings_digest") != scheduler_settings_digest(schedule)
                    or canonical(supplied) != canonical(self._weekly_invocation(schedule))):
                raise HouseholdError("weekly scheduler identity, generation or settings is not active")
            return self._weekly_invocation(schedule)
        if supplied is not None:
            raise HouseholdError("weekly scheduler has not been adopted")
        return None

    def _weekly_plan_result(self, schedule):
        invocation = self._weekly_invocation(schedule)
        prompt = ("Run the saved weekly plan through the shared meal-concierge skill. "
                  "Call schedule due with this exact scheduler object: " + canonical(invocation)
                  + ". Carry its returned occurrence and scheduler unchanged into checkout auto. "
                  "Preserve the original occurrence on retries. Reconcile uncertain effects before retrying.")
        return {"scheduler": deepcopy(schedule["scheduler"]), "invocation": invocation,
                "cron_prompt": prompt, "automation_digest": hashlib.sha256(prompt.encode()).hexdigest(),
                "cron": {key: schedule[key] for key in ("weekday", "time", "timezone")}}

    def _owner_plan(self, state, supplied):
        target = self._scheduler_scope(supplied.get("owner"))
        old = self._scheduler_owner(state)
        if old and old["state"] == "handover":
            if target != old["owner"]:
                raise HouseholdError("reconcile the current owner plan before replacing it")
            return {"owner": deepcopy(old), "acknowledged": False, "idempotent": True}
        if old and supplied.get("generation") != old["generation"]:
            raise HouseholdError("scheduler owner generation is stale")
        if not old:
            inventory = supplied.get("inventory")
            if not isinstance(inventory, Mapping) or inventory.get("verified") is not True:
                raise HouseholdError("owner adoption requires authoritative native inventory")
            self._scheduler_scope({key: inventory.get(key) for key in ("platform", "scope")})
        for record in [state["schedule"], *state["email_jobs"]]:
            scheduler = self._native_scheduler_record(record)
            if scheduler and (scheduler["state"] == "handover" or
                              (scheduler.get("previous_binding") and not scheduler.get("previous_job_removed"))):
                raise HouseholdError("finish the existing native job handover before planning another owner")
        owner = {"owner": target, "state": "handover", "generation": secrets.token_urlsafe(18),
                 "original_owner": deepcopy(old["owner"]) if old else None,
                 "adoption_scope": deepcopy(old.get("adoption_scope", old["owner"])) if old else
                     {key: inventory[key] for key in ("platform", "scope")}}
        state["schedule"]["scheduler_owner"] = owner
        return {"owner": deepcopy(owner), "acknowledged": False}

    def _ack_owner(self, state, supplied):
        owner = self._scheduler_owner(state)
        if (not owner or supplied.get("owner") != owner["owner"]
                or supplied.get("generation") != owner["generation"]):
            raise HouseholdError("owner acknowledgment does not match the current plan")
        inventory = supplied.get("inventory")
        if (not isinstance(inventory, Mapping) or inventory.get("verified") is not True
                or {key: inventory.get(key) for key in ("platform", "scope")} != owner["owner"]
                or not isinstance(inventory.get("bindings"), list)):
            raise HouseholdError("owner activation requires verified target inventory")
        observed = [self._scheduler_binding(item) for item in inventory["bindings"]]
        if len({canonical(item) for item in observed}) != len(observed):
            raise HouseholdError("native owner inventory has duplicate bindings")
        if (self._unresolved_scheduled_effects(state)
                or (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES
                or any(job.get("status") == "sending" for job in state["email_jobs"])):
            raise HouseholdError("reconcile original dispatched effects before owner activation")
        expected = []
        for record in [state["schedule"], *state["email_jobs"]]:
            scheduler = self._native_scheduler_record(record)
            weekly = record is state["schedule"]
            terminal = not record.get("enabled") if weekly else record.get("status") in {"sent", "cancelled"}
            if scheduler is None:
                if terminal and record.get("native_cleanup"):
                    continue
                if weekly and not record.get("enabled") and not record.get("cron_job_id"):
                    continue
                raise HouseholdError("adopt or verify cleanup of every existing weekly and email job")
            bindings = self._native_bindings(record)
            if terminal:
                if bindings:
                    raise HouseholdError("terminal native jobs need verified cleanup before owner activation")
                continue
            if (scheduler.get("owner_generation") != owner["generation"]
                    or scheduler["state"] not in {"active", "paused"} or not scheduler.get("ack")
                    or scheduler.get("job_removed") is True
                    or (scheduler.get("previous_binding") and not scheduler.get("previous_job_removed"))):
                raise HouseholdError("every current job must acknowledge the target owner plan")
            self._scheduler_target(state, scheduler["binding"])
            if weekly and scheduler.get("settings_digest") != scheduler_settings_digest(record):
                raise HouseholdError("weekly scheduler settings changed during owner handover")
            if not weekly and scheduler.get("delivery_date") != record.get("delivery_date"):
                raise HouseholdError("email delivery changed during owner handover")
            expected.extend(bindings)
        if sorted(map(canonical, observed)) != sorted(map(canonical, expected)):
            raise HouseholdError("native inventory must exactly match the household's remaining jobs")
        owner["state"] = "active"
        return {"owner": deepcopy(owner), "acknowledged": True}

    def _weekly_scheduler(self, state, action, request):
        schedule = state["schedule"]
        supplied = request.get("scheduler")
        if not isinstance(supplied, Mapping):
            raise HouseholdError("scheduler action requires an exact scheduler object")
        if action == "owner_plan":
            return self._owner_plan(state, supplied)
        if action == "ack_owner":
            return self._ack_owner(state, supplied)
        scheduler = self._native_scheduler_record(schedule)
        if action == "due":
            self._require_weekly_scheduler(state, supplied)
            if not schedule.get("enabled"):
                raise HouseholdError("scheduled run is off")
            return {"occurrence": scheduled_occurrence(schedule, self._now()),
                    "scheduler": self._weekly_invocation(schedule)}
        if action == "scheduler_plan":
            owner = self._scheduler_owner(state)
            if not owner:
                raise HouseholdError("select the installation owner with owner_plan first")
            binding = self._scheduler_binding(supplied.get("binding"))
            if schedule.get("enabled"):
                self._scheduler_target(state, binding)
            if scheduler:
                if scheduler["state"] == "handover":
                    if (binding != scheduler["binding"] or scheduler.get("settings_digest") != scheduler_settings_digest(schedule)
                            or scheduler.get("owner_generation") != owner["generation"]):
                        raise HouseholdError("reconcile the existing weekly plan before replacing it")
                    return {"acknowledged": False, **self._weekly_plan_result(schedule)}
                if supplied.get("generation") != scheduler["generation"]:
                    raise HouseholdError("weekly scheduler plan generation is stale")
                if scheduler.get("previous_binding") and not scheduler.get("previous_job_removed"):
                    raise HouseholdError("verify the previous native job removal first")
                previous = None if scheduler.get("job_removed") else scheduler["binding"]
            else:
                inventory = supplied.get("inventory")
                if not isinstance(inventory, Mapping) or inventory.get("verified") is not True:
                    raise HouseholdError("weekly adoption requires authoritative old native inventory")
                scope = self._scheduler_scope({key: inventory.get(key) for key in ("platform", "scope")})
                if schedule.get("cron_job_id") and scope != owner.get("adoption_scope"):
                    raise HouseholdError("weekly adoption inventory does not match the original scheduler scope")
                previous = supplied.get("previous_binding")
                if previous is not None:
                    previous = self._scheduler_binding(previous)
                    if {key: previous[key] for key in scope} != scope:
                        raise HouseholdError("old weekly job differs from the inspected native scope")
                count = inventory.get("matching_jobs")
                if type(count) is not int or count != (1 if previous else 0):
                    raise HouseholdError("reconcile the exact old weekly native job before adoption")
                if schedule.get("cron_job_id") and (not previous or previous["job_id"] != schedule["cron_job_id"]):
                    raise HouseholdError("account for the legacy weekly cron job before adoption")
            self._check_native_collision(state, schedule, binding, previous)
            schedule["scheduler"] = {"binding": binding, "previous_binding": previous if previous != binding else None,
                                     "generation": secrets.token_urlsafe(18), "owner_generation": owner["generation"],
                                     "state": "handover", "settings_digest": scheduler_settings_digest(schedule)}
        elif action == "pause_scheduler":
            if not scheduler or canonical(supplied) != canonical(self._weekly_invocation(schedule)):
                raise HouseholdError("pause requires the exact current weekly invocation")
            self._pause_weekly(schedule)
        elif action == "ack_scheduler":
            if not scheduler or supplied.get("generation") != scheduler["generation"]:
                raise HouseholdError("weekly scheduler acknowledgement generation is stale")
            wanted = supplied.get("state")
            if wanted != "removed":
                self._scheduler_target(state, scheduler["binding"])
            if (supplied.get("binding") != scheduler["binding"] or supplied.get("verified") is not True
                    or wanted not in {"active", "paused", "removed"}
                    or supplied.get("previous_binding") != scheduler.get("previous_binding")
                    or (scheduler.get("previous_binding") and supplied.get("previous_job_removed") is not True)
                    or request.get("automation_digest") != self._weekly_plan_result(schedule)["automation_digest"]):
                raise HouseholdError("weekly acknowledgment requires exact native state and verified old removal")
            if wanted == "removed":
                if schedule.get("enabled"):
                    raise HouseholdError("disable weekly scheduling before acknowledging removal")
                scheduler["job_removed"] = True
            elif (scheduler.get("job_removed") or scheduler.get("settings_digest") != scheduler_settings_digest(schedule)
                  or scheduler.get("owner_generation") != self._scheduler_owner(state)["generation"]):
                raise HouseholdError("weekly settings or owner changed; replan before activation")
            if scheduler.get("ack") and canonical(scheduler["ack"]) != canonical(supplied):
                raise HouseholdError("weekly acknowledgment differs from the completed plan")
            scheduler["state"] = "paused" if wanted == "removed" else wanted
            scheduler["previous_job_removed"] = supplied.get("previous_job_removed") is True
            scheduler["ack"] = deepcopy(dict(supplied))
        else:
            raise HouseholdError("unknown scheduler action")
        return {"acknowledged": action == "ack_scheduler", **self._weekly_plan_result(schedule)}

    def _pause_weekly(self, schedule):
        scheduler = self._native_scheduler_record(schedule)
        if scheduler:
            scheduler["generation"] = secrets.token_urlsafe(18)
            scheduler["state"] = "paused"
            scheduler.pop("ack", None)

    def _schedule(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "show")
        if action == "reconcile":
            return self._reconcile_scheduled_delivery(request)
        with self.store.locked() as state:
            schedule = state["schedule"]
            if action in {"owner_plan", "ack_owner", "scheduler_plan", "ack_scheduler", "pause_scheduler", "due"}:
                return self._weekly_scheduler(state, action, request)
            if action == "show":
                return {"schedule": deepcopy(schedule)}
            if action == "disable":
                schedule["enabled"] = False
                schedule["auto_checkout"] = False
                if "scheduler" in schedule:
                    self._pause_weekly(schedule)
                    return {"schedule": deepcopy(schedule), "automation_cleanup": self._weekly_plan_result(schedule)}
                if self._scheduler_owner(state):
                    return {"schedule": deepcopy(schedule),
                            "next": "Adopt the exact legacy native binding before acknowledging its cleanup."}
                return {"schedule": deepcopy(schedule), "remove_cron_job_id": schedule.get("cron_job_id")}
            if action == "set_cron_job":
                if self._scheduler_owner(state) or "scheduler" in schedule:
                    raise HouseholdError("managed weekly scheduling requires scheduler_plan and ack_scheduler")
                schedule["cron_job_id"] = request.get("cron_job_id")
                return {"schedule": deepcopy(schedule)}
            if action == "update":
                changes = request.get("changes")
                if not isinstance(changes, Mapping):
                    raise HouseholdError("schedule changes must be an object")
                allowed = {"enabled", "weekday", "time", "timezone", "mode", "delivery", "maximum_total", "auto_checkout"}
                if not set(changes).issubset(allowed):
                    raise HouseholdError("schedule contains unknown fields")
                if "delivery" in changes:
                    replacement = changes["delivery"]
                    if not isinstance(replacement, Mapping):
                        raise HouseholdError("schedule delivery preference is invalid")
                    replacement = deepcopy(dict(replacement))
                    replacement.setdefault("strategy", "cheapest")
                    changes = {**changes, "delivery": replacement}
                before = scheduler_settings_digest(schedule)
                schedule.update(deepcopy(changes))
                validate_schedule(schedule, self.provider)
                if schedule.get("auto_checkout"):
                    schedule["mode"] = "auto_checkout"
                elif schedule.get("mode") == "auto_checkout":
                    schedule["mode"] = "cart_ready"
                if self._scheduler_owner(state) or "scheduler" in schedule:
                    if before != scheduler_settings_digest(schedule):
                        self._pause_weekly(schedule)
                    return {"schedule": deepcopy(schedule), "scheduler_update_required": True,
                            "next": "Plan and verify the native weekly job before acknowledging it."}
                return {
                    "schedule": deepcopy(schedule),
                    "cron": {
                        "name": f"{self.provider.upper()} ukesmeny ({state['household']})",
                        "weekday": schedule["weekday"],
                        "time": schedule["time"],
                        "timezone": schedule["timezone"],
                        "prompt": "Kjør den lagrede ukesplanen via den delte ukesmeny-skillen. Bruk forekomstnøkkel for denne lokale uken og stopp ved lagret modus.",
                    } if schedule.get("enabled") else None,
                }
        raise HouseholdError("unknown schedule action")

    def _catalog(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        kwargs = {
            "deadline": request.get("_deadline"),
            "allow_recovery": request.get("_allow_browser_recovery") is True,
        } if self.provider == "meny" else {}
        if action == "products":
            query_value = request.get("query", "")
            if not isinstance(query_value, str):
                raise HouseholdError("catalog query must be text")
            query = query_value.strip()
            return self.provider_client.call("product_search", {"queries": [query], "page": 1, "size": bounded_limit(request.get("limit"), default=5, maximum=MAX_PRODUCTS)}, **kwargs)
        if action == "recipes":
            query = request.get("query", "")
            if not isinstance(query, str):
                raise HouseholdError("catalog query must be text")
            return self.provider_client.call("recipe_search", {"query": query, "page": 1, "size": bounded_limit(request.get("limit"), default=5)}, **kwargs)
        if action == "usuals":
            return self.provider_client.call("likely_to_buy", {}, **kwargs)
        raise HouseholdError("unknown catalog action")

    def _product_binding(
        self, *, menu_ref: Any = None, planner_handoff: Any = None,
        planner_selection_ref: Any = None, planner_ref: Any = None,
        require_saved_planner: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        if sum(value is not None for value in (menu_ref, planner_handoff, planner_selection_ref, planner_ref)) != 1:
            raise HouseholdError("product preparation needs exactly one menu_ref, planner_ref, planner_handoff or planner_selection_ref")
        if planner_ref is not None:
            handoff, _resolved, _request = self._resolve_planner_ref(planner_ref)
            return self._product_binding(
                planner_handoff=handoff, require_saved_planner=require_saved_planner,
            )
        if menu_ref is not None:
            if not isinstance(menu_ref, Mapping) or set(menu_ref) != {"menu_id", "revision", "digest"}:
                raise HouseholdError("menu_ref must be the exact current menu identity")
            state = self.store.read()
            menu = state.get("menu")
            if not isinstance(menu, Mapping) or canonical(self._cart_menu_ref(menu)) != canonical(menu_ref):
                raise HouseholdError("menu_ref is stale or not the active menu")
            self._require_menu_provider(menu)
            return (
                {"kind": "saved_menu", "menu_ref": deepcopy(dict(menu_ref))},
                deepcopy(dict(menu)), deepcopy(dict(menu_ref)),
            )
        if planner_selection_ref is not None:
            if not isinstance(planner_selection_ref, Mapping) or set(planner_selection_ref) != {
                "planner_version", "input_digest", "selection_digest"
            }:
                raise HouseholdError("planner_selection_ref must be one exact returned selection identity")
            current = self.store.read().get("menu")
            selection = current.get("planner_selection") if isinstance(current, Mapping) else None
            actual = {
                key: selection.get(key) for key in (
                    "planner_version", "input_digest", "selection_digest"
                )
            } if isinstance(selection, Mapping) else None
            if canonical(actual) != canonical(planner_selection_ref):
                raise HouseholdError("planner_selection_ref is stale or not the active saved menu")
            self._require_menu_provider(current)
            return (
                {"kind": "planner_selection", "planner_handoff": deepcopy(dict(selection))},
                deepcopy(dict(current)), self._cart_menu_ref(current),
            )
        current = self.store.read().get("menu")
        if (isinstance(current, Mapping) and planner_handoff is not None
                and canonical(current.get("planner_selection")) == canonical(planner_handoff)):
            self._require_menu_provider(current)
            return ({"kind": "planner_selection", "planner_handoff": deepcopy(dict(planner_handoff))},
                    deepcopy(dict(current)), self._cart_menu_ref(current))
        _result, resolved, _request = self._verify_planner_handoff(planner_handoff)
        menu = self._materialize_planner_menu(planner_handoff, resolved)
        saved_ref = None
        if require_saved_planner:
            current = self.store.read().get("menu")
            current_selection = current.get("planner_selection") if isinstance(current, Mapping) else None
            if not isinstance(current, Mapping) or canonical(current_selection) != canonical(planner_handoff):
                raise HouseholdError("save this exact planner selection before applying its product plan")
            saved_ref = self._cart_menu_ref(current)
        return (
            {"kind": "planner_selection", "planner_handoff": deepcopy(dict(planner_handoff))},
            menu, saved_ref,
        )

    @staticmethod
    def _checked_product_observation(observation, provider, query):
        if not isinstance(observation, Mapping) or observation.get("provider") != provider:
            raise HouseholdError("provider product search is not normalized")
        if observation.get("query") not in {None, query}:
            raise HouseholdError("provider product search query changed")
        normalized = deepcopy(dict(observation))
        normalized["query"] = query
        scope = normalized.get("scope")
        products = normalized.get("products")
        if (not isinstance(scope, Mapping) or scope.get("semantics") != "bounded_relevance_ranked"
                or scope.get("kind") != "provider_search" or type(scope.get("page")) is not int
                or scope["page"] != 1 or type(scope.get("requested_size")) is not int
                or scope["requested_size"] != MAX_CANDIDATES_PER_REQUIREMENT
                or not isinstance(products, list) or len(products) > MAX_CANDIDATES_PER_REQUIREMENT
                or type(scope.get("returned")) is not int or scope["returned"] != len(products)):
            raise HouseholdError("provider product search exceeded its bounded candidate scope")
        return normalized

    def _product_observations(
        self, menu: Mapping[str, Any], *, deadline: float | None,
        search_cache: dict[str, dict[str, Any]] | None = None,
        ingredient_decisions: Any = None, candidate_approvals: Any = None,
        approved_only: bool = False, work: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        requirements, _unresolved = exact_menu_requirements(mp.shopping_menu(menu), ingredient_decisions=ingredient_decisions)
        approvals = normalize_approvals(candidate_approvals, {r["requirement_id"] for r in requirements})
        cache = work.setdefault("searches", {}) if work is not None else (search_cache if search_cache is not None else {})
        attempted = work.setdefault("attempted_searches", []) if work is not None else []
        requested = {}
        rows = []
        for requirement in requirements:
            if approved_only and requirement["requirement_id"] not in approvals:
                continue
            hints = [hint for hint in requirement.get("product_hints", [])
                     if isinstance(hint, Mapping) and hint.get("provider") == self.provider]
            query = (approvals.get(requirement["requirement_id"], {}).get("search_query")
                     or (hints[0].get("name") if hints else None)
                     or ingredient_search(requirement["identity"], self.provider))
            key = canonical({"provider": self.provider, "query": query, "page": 1,
                             "size": MAX_CANDIDATES_PER_REQUIREMENT})
            requested[key] = query
            rows.append((requirement, hints, key))
        # A failing query cannot starve the still-unseen tail on later calls.
        missing = sorted((key for key in requested if key not in cache), key=lambda key: attempted.index(key) if key in attempted else -1)
        failed = {}
        batch_reader = getattr(self.provider_client, "product_search_batch", None)
        native_batch = callable(batch_reader) and self.provider == "oda"
        while missing:
            remaining = MAX_REQUIREMENTS - work.get("reads", 0) if work is not None else len(missing)
            if remaining <= 0 or deadline is not None and time.monotonic() >= deadline:
                break
            keys = missing[:min(8 if native_batch else 1, remaining)]
            del missing[:len(keys)]
            queries = [requested[key] for key in keys]
            if work is not None:
                work["reads"] = work.get("reads", 0) + len(keys)
            for key in keys:
                if key in attempted:
                    attempted.remove(key)
                attempted.append(key)
            try:
                if native_batch:
                    result = batch_reader(queries, size=MAX_CANDIDATES_PER_REQUIREMENT, deadline=deadline)
                    if not isinstance(result, Mapping) or set(result) != set(queries):
                        raise HouseholdError("provider product batch query scope changed")
                else:
                    kwargs = {"deadline": deadline}
                    if self.provider == "meny":
                        kwargs["allow_recovery"] = False
                    result = {queries[0]: self.provider_client.call("product_search",
                        {"queries": queries, "page": 1, "size": MAX_CANDIDATES_PER_REQUIREMENT}, **kwargs)}
                checked = {key: self._checked_product_observation(result[requested[key]], self.provider, requested[key])
                           for key in keys}
                cache.update(checked)
            except HouseholdError:
                for key in keys:
                    failed[key] = "provider_search_deadline" if deadline is not None and time.monotonic() >= deadline else "provider_search_unavailable_or_scope_changed"
        observations = {}
        for requirement, hints, key in rows:
            if key not in cache:
                observations[requirement["requirement_id"]] = {"unavailable_reason": failed.get(key,
                    "provider_search_deadline" if deadline is not None and time.monotonic() >= deadline else "provider_search_pending")}
                continue
            normalized = self._checked_product_observation(cache[key], self.provider, requested[key])
            if hints:
                refs = [hint["product_ref"] for hint in hints]
                products = normalized["products"]
                normalized["products"] = ([p for p in products if p.get("product_ref") in refs]
                                         + [p for p in products if p.get("product_ref") not in refs])
                normalized["source_product_evidence"] = {"relationship": "source_recipe_association",
                    "candidate_refs": refs, "currently_observed_refs": [p["product_ref"] for p in products if p.get("product_ref") in refs],
                    "status": "currently_observed" if any(p.get("product_ref") in refs for p in products) else "not_in_current_search_scope"}
            observations[requirement["requirement_id"]] = normalized
        if work is not None:
            work["pending_search_count"] = sum(key not in cache for key in requested)
        return observations

    @staticmethod
    def _plan_approvals(plan: Mapping[str, Any], *, selected_only: bool = False) -> list[dict[str, Any]]:
        values = []
        for requirement in plan.get("requirements", []):
            if selected_only and (
                not isinstance(requirement, Mapping) or requirement.get("status") != "selected"
            ):
                continue
            approval = requirement.get("candidate_approval") if isinstance(requirement, Mapping) else None
            if isinstance(approval, Mapping):
                compact = {
                    key: deepcopy(approval[key])
                    for key in (
                        "requirement_id", "candidate_refs", "max_excess", "search_query",
                        "package_count", "quantity_basis", "selection_reason", "semantic_authorization", "shared_package",
                    )
                    if key in approval
                }
                if "shared_package" in compact:
                    compact.pop("package_count", None)
                    compact.pop("quantity_basis", None)
                values.append(compact)
        return values

    @classmethod
    def _continuation_approvals(cls, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Lock continued rows to the exact products selected by this plan."""
        approvals = {
            row["requirement_id"]: row
            for row in cls._plan_approvals(plan)
        }
        for requirement in plan.get("requirements", []):
            if not isinstance(requirement, Mapping) or requirement.get("status") != "selected":
                continue
            approval = approvals.get(requirement.get("requirement_id"))
            selection = requirement.get("selection")
            products = selection.get("products") if isinstance(selection, Mapping) else None
            if not isinstance(approval, dict) or not isinstance(products, list):
                continue
            selected_refs = {
                product.get("product_ref")
                for product in products if isinstance(product, Mapping)
            }
            approval["candidate_refs"] = [
                reference for reference in approval["candidate_refs"]
                if reference in selected_refs
            ]
            authority = approval.get("semantic_authorization")
            if (
                isinstance(authority, Mapping)
                and authority.get("candidate_ref") not in selected_refs
            ):
                approval.pop("semantic_authorization", None)
        return [approvals[key] for key in sorted(approvals)]

    @classmethod
    def _partial_seed_approvals(cls, cart_plan: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Recover only the exact refs already authorized by a partial apply."""
        stored = cart_plan.get("partial_product_plan_approvals")
        authority = cart_plan.get("partial_product_plan_authority")
        selected = authority.get("selected_requirements") if isinstance(authority, Mapping) else None
        if (
            not isinstance(stored, list) or not stored
            or not isinstance(selected, list) or not selected
            or any(not isinstance(row, Mapping) for row in stored + selected)
        ):
            raise HouseholdError(
                "persisted partial product selections lack exact digest-bound authority"
            )
        stored_by_id = {
            row.get("requirement_id"): deepcopy(dict(row)) for row in stored
            if isinstance(row.get("requirement_id"), str)
        }
        selected_by_id = {
            row.get("requirement_id"): row for row in selected
            if isinstance(row.get("requirement_id"), str)
        }
        if (
            len(stored_by_id) != len(stored)
            or len(selected_by_id) != len(selected)
            or set(stored_by_id) != set(selected_by_id)
        ):
            raise HouseholdError(
                "persisted partial product approvals do not match their recorded authority"
            )
        selected_plan = {
            "requirements": [{**dict(row), "status": "selected"} for row in selected]
        }
        authority_approvals = {
            row["requirement_id"]: row
            for row in cls._plan_approvals(
                selected_plan, selected_only=True,
            )
        }
        if canonical(stored_by_id) != canonical(authority_approvals):
            raise HouseholdError(
                "persisted partial product approvals do not match their recorded authority"
            )
        narrowed = cls._continuation_approvals(selected_plan)
        narrowed_by_id = {row["requirement_id"]: row for row in narrowed}
        for requirement_id, row in selected_by_id.items():
            selection = row.get("selection")
            products = selection.get("products") if isinstance(selection, Mapping) else None
            selected_refs = {
                product.get("product_ref")
                for product in products or [] if isinstance(product, Mapping)
            }
            approval = narrowed_by_id.get(requirement_id)
            if (
                not selected_refs or not isinstance(approval, Mapping)
                or set(approval.get("candidate_refs", [])) != selected_refs
            ):
                raise HouseholdError(
                    "persisted partial product selection differs from its recorded approval"
                )
        return narrowed

    @staticmethod
    def _continuation_dependent_groups(plan: Mapping[str, Any]) -> list[list[str]]:
        groups = set()
        for requirement in plan.get("requirements", []):
            selection = requirement.get("selection") if isinstance(requirement, Mapping) else None
            allocation = (
                selection.get("shared_package_allocation")
                if isinstance(selection, Mapping) else None
            )
            members = allocation.get("requirement_ids") if isinstance(allocation, Mapping) else None
            if (
                isinstance(members, list) and len(members) > 1
                and all(isinstance(member, str) for member in members)
            ):
                groups.add(tuple(sorted(set(members))))
        return [list(group) for group in sorted(groups)]

    @staticmethod
    def _product_requirement_scope_digest(plan: Mapping[str, Any]) -> str:
        scope = [{
            key: deepcopy(row.get(key))
            for key in ("requirement_id", "identity", "item", "quantity", "unit", "sources")
        } for row in plan.get("requirements", []) if isinstance(row, Mapping)]
        return mp.digest(scope)

    @classmethod
    def _full_plan_authority(
        cls, plan: Mapping[str, Any], menu_ref: Mapping[str, Any],
        cart_plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind the exact selections applied by one complete product plan."""
        selected = cls._partial_plan_authority(plan)
        authority = {
            "menu_ref": deepcopy(dict(menu_ref)),
            "product_plan_digest": plan.get("product_plan_digest"),
            "cart_digest": cart_plan.get("last_synced_digest"),
            "cart_requirements_digest": mp.digest(
                cart_plan.get("menu_required_quantities")
            ),
            "requirement_scope_digest": cls._product_requirement_scope_digest(plan),
            "dependent_groups": cls._continuation_dependent_groups(plan),
            **selected,
        }
        authority["authority_digest"] = mp.digest(authority)
        return authority

    @classmethod
    def _full_seed_authority(
        cls, cart_plan: Mapping[str, Any], menu_ref: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Recover exact full-apply selections only from unchanged state."""
        authority = cart_plan.get("product_plan_authority")
        required = {
            "menu_ref", "product_plan_digest", "cart_digest",
            "cart_requirements_digest", "requirement_scope_digest",
            "dependent_groups", "context", "selected_requirements",
            "authority_digest",
        }
        if not isinstance(authority, Mapping) or set(authority) != required:
            raise HouseholdError(
                "persisted full product selections lack exact digest-bound authority"
            )
        unsigned = {
            key: deepcopy(value) for key, value in authority.items()
            if key != "authority_digest"
        }
        if authority.get("authority_digest") != mp.digest(unsigned):
            raise HouseholdError("persisted full product selection authority is invalid")
        cart_digest = authority.get("cart_digest")
        if (
            canonical(authority.get("menu_ref")) != canonical(menu_ref)
            or canonical(cart_plan.get("menu_ref")) != canonical(menu_ref)
            or authority.get("product_plan_digest") != cart_plan.get("product_plan_digest")
            or not isinstance(cart_digest, str)
            or cart_plan.get("last_synced_digest") != cart_digest
            or cart_plan.get("status") != "active"
            or cart_plan.get("pending_cart_digest") is not None
            or (
                cart_plan.get("approved_cart_digest") is not None
                and cart_plan.get("approved_cart_digest") != cart_digest
            )
            or authority.get("cart_requirements_digest") != mp.digest(
                cart_plan.get("menu_required_quantities")
            )
        ):
            raise HouseholdError(
                "persisted full product selection authority no longer matches its menu, plan or cart"
            )
        selected = authority.get("selected_requirements")
        if (
            not isinstance(selected, list)
            or any(not isinstance(row, Mapping) for row in selected)
        ):
            raise HouseholdError("persisted full product selection authority is invalid")
        selected_plan = {
            "requirements": [{**dict(row), "status": "selected"} for row in selected]
        }
        narrowed = cls._continuation_approvals(selected_plan)
        narrowed_by_id = {
            row.get("requirement_id"): row for row in narrowed
            if isinstance(row.get("requirement_id"), str)
        }
        if len(narrowed_by_id) != len(selected):
            raise HouseholdError("persisted full product selection authority is invalid")
        for row in selected:
            requirement_id = row.get("requirement_id")
            selection = row.get("selection")
            products = selection.get("products") if isinstance(selection, Mapping) else None
            selected_refs = {
                product.get("product_ref")
                for product in products or [] if isinstance(product, Mapping)
            }
            approval = narrowed_by_id.get(requirement_id)
            if (
                not isinstance(requirement_id, str) or not selected_refs
                or not isinstance(approval, Mapping)
                or set(approval.get("candidate_refs", [])) != selected_refs
            ):
                raise HouseholdError(
                    "persisted full product selection differs from its recorded approval"
                )
        return {
            **deepcopy(dict(authority)),
            "candidate_approvals": narrowed,
        }

    def _verify_full_seed_cart(
        self, authority: Mapping[str, Any], *, menu_ref: Mapping[str, Any],
        expected_cart_plan: Mapping[str, Any], deadline: float | None,
    ) -> None:
        cart = self._cart_provider_call("get_cart", {}, deadline=deadline)
        summary = cart_summary(cart)
        live, names = self._cart_lines(summary)
        drifted = self._cart_digest(live) != authority.get("cart_digest")
        with self.store.locked() as state:
            cart_plan = state.get("cart_plan")
            try:
                current_authority = self._full_seed_authority(cart_plan, menu_ref)
            except (AttributeError, HouseholdError) as exc:
                raise HouseholdError(
                    "persisted full product selection changed during cart verification; prepare again"
                ) from exc
            if (
                canonical(cart_plan) != canonical(expected_cart_plan)
                or current_authority.get("authority_digest") != authority.get("authority_digest")
            ):
                raise HouseholdError(
                    "persisted full product selection changed during cart verification; prepare again"
                )
            if drifted:
                self._set_cart_needs_input(cart_plan, live, names)
        if not drifted:
            return
        raise HouseholdError(
            "retailer cart changed since the full product apply; reconcile the current cart before preparing again"
        )

    @staticmethod
    def _merge_product_plan_approvals(
        previous: Any, delta: Any, mode: Any, *, dependent_groups: Any = None,
    ) -> list[dict[str, Any]]:
        if mode not in {"extend", "replace", "reset"}:
            raise HouseholdError("continuation_mode must be extend, replace or reset")
        if not isinstance(delta, list) or len(delta) > MAX_REQUIREMENTS:
            raise HouseholdError("candidate_approvals must be a bounded list")
        if mode == "reset" and delta:
            raise HouseholdError("reset requires empty candidate_approvals; use replace for a new selection")
        prior = previous if isinstance(previous, list) else []
        if any(not isinstance(row, Mapping) for row in prior):
            raise HouseholdError("stored product continuation approvals are invalid")
        changed_ids = set()
        for row in delta:
            if not isinstance(row, Mapping) or not isinstance(row.get("requirement_id"), str):
                raise HouseholdError("each candidate approval needs one requirement_id")
            changed_ids.add(row["requirement_id"])
        combined = {
            row["requirement_id"]: deepcopy(dict(row))
            for row in prior
            if isinstance(row.get("requirement_id"), str)
        } if mode == "extend" else {}
        groups = []
        if dependent_groups is not None:
            if (
                not isinstance(dependent_groups, list)
                or any(
                    not isinstance(group, list) or len(group) < 2
                    or any(not isinstance(member, str) for member in group)
                    or len(set(group)) != len(group)
                    for group in dependent_groups
                )
            ):
                raise HouseholdError("stored product continuation dependencies are invalid")
            groups.extend(tuple(group) for group in dependent_groups)
        # A shared selection is one dependency unit. Changing any member removes
        # the complete prior unit before the replacement delta is considered.
        invalidated = set()
        for row in combined.values():
            shared = row.get("shared_package")
            members = shared.get("requirement_ids") if isinstance(shared, Mapping) else None
            if isinstance(members, list):
                groups.append(tuple(member for member in members if isinstance(member, str)))
        invalidated.update(changed_ids)
        changed = True
        while changed:
            changed = False
            for group in groups:
                if invalidated.intersection(group) and not set(group).issubset(invalidated):
                    invalidated.update(group)
                    changed = True
        for requirement_id in invalidated:
            combined.pop(requirement_id, None)
        for row in delta:
            combined[row["requirement_id"]] = deepcopy(dict(row))
        return [combined[key] for key in sorted(combined)]

    def _product_plan_record(self, state: Mapping[str, Any], value: Any) -> dict[str, Any]:
        if not isinstance(value, str) or PRODUCT_PLAN_REF_PATTERN.fullmatch(value) is None:
            raise HouseholdError("product_plan_ref must be one exact server-returned reference")
        record = state.get("menu_planning", {}).get("prepared", {}).get(value)
        required = {
            "kind", "menu_ref", "candidate_approvals", "ingredient_decisions",
            "budget_ore", "price_mode", "selection_digest", "product_plan_digest",
            "requirement_scope_digest", "dependent_groups",
        }
        additions = {"binding_arguments", "snapshot", "apply_arguments", "partial_apply_arguments"}
        if (not isinstance(record, Mapping) or not required.issubset(record)
                or set(record).difference(required | additions | {"observation_work", "validation_work"})
                or record.get("kind") != "product_plan"):
            raise HouseholdError("product_plan_ref is stale, unknown or belongs to another menu")
        selection = {
            "candidate_approvals": record.get("candidate_approvals"),
            "dependent_groups": record.get("dependent_groups"),
        }
        if record.get("selection_digest") != mp.digest(selection):
            raise HouseholdError("stored product_plan_ref selection is invalid")
        if record.get("menu_ref") is not None:
            menu = state.get("menu")
            if not isinstance(menu, Mapping) or canonical(mp.menu_ref(menu)) != canonical(record["menu_ref"]):
                raise HouseholdError("product_plan_ref is stale or belongs to a changed menu")
        else:
            arguments = record.get("binding_arguments")
            if not isinstance(arguments, Mapping) or set(arguments) != {"planner_ref"}:
                raise HouseholdError("product preview has no exact planner reference")
            self._resolve_planner_ref(arguments["planner_ref"])
        return deepcopy(dict(record))

    def _store_product_plan_record(
        self, *, menu_ref: Mapping[str, Any] | None, plan: Mapping[str, Any],
        approvals: list[dict[str, Any]], ingredient_decisions: Any,
        budget_ore: Any, price_mode: str, replaced_ref: str | None = None,
        expected_record: Mapping[str, Any] | None = None,
        binding_arguments: Mapping[str, Any], apply_arguments: Any,
        partial_apply_arguments: Any,
        observation_work: Any = None, validation_work: Any = None,
    ) -> tuple[str, str]:
        record = {
            "kind": "product_plan",
            "menu_ref": deepcopy(dict(menu_ref)) if menu_ref is not None else None,
            "candidate_approvals": deepcopy(approvals),
            "dependent_groups": self._continuation_dependent_groups(plan),
            "ingredient_decisions": deepcopy(ingredient_decisions),
            "budget_ore": budget_ore,
            "price_mode": price_mode,
            "product_plan_digest": plan.get("product_plan_digest"),
            "requirement_scope_digest": self._product_requirement_scope_digest(plan),
            "binding_arguments": deepcopy(dict(binding_arguments)),
            "snapshot": self._product_review_snapshot(plan),
            "apply_arguments": deepcopy(apply_arguments) if menu_ref is not None else None,
            "partial_apply_arguments": deepcopy(partial_apply_arguments) if menu_ref is not None else None,
            **({"observation_work": deepcopy(observation_work)} if observation_work is not None else {}),
            **({"validation_work": deepcopy(validation_work)} if validation_work is not None else {}),
        }
        record["selection_digest"] = mp.digest({
            "candidate_approvals": record["candidate_approvals"],
            "dependent_groups": record["dependent_groups"],
        })
        with self.store.locked() as state:
            menu = state.get("menu")
            if menu_ref is not None and (not isinstance(menu, Mapping) or canonical(mp.menu_ref(menu)) != canonical(menu_ref)):
                raise HouseholdError("menu changed while preparing product continuation")
            records = state["menu_planning"]["prepared"]
            if replaced_ref is not None:
                if canonical(records.get(replaced_ref)) != canonical(expected_record):
                    raise HouseholdError("product_plan_ref changed while preparing; retry from the latest reference")
                records.pop(replaced_ref, None)
            for key in list(records):
                if isinstance(key, str) and key.startswith("productplan_"):
                    # A fresh prepare or continuation rotates the one active
                    # chain. Abandoned refs cannot exhaust the shared store.
                    records.pop(key, None)
            while True:
                reference = "productplan_" + secrets.token_urlsafe(12)
                if reference not in records:
                    break
            records[reference] = record
        return reference, record["selection_digest"]

    @staticmethod
    def _product_review_snapshot(plan: Mapping[str, Any]) -> dict[str, Any]:
        """Retain reviewable facts once, without the internal plan's repeated evidence."""
        snapshot = {key: deepcopy(plan[key]) for key in (
            "provider", "status", "product_plan_digest", "partial_product_plan_digest",
            "coverage_status", "cost_status", "budget_status", "budget_ore", "price_mode",
            "totals", "excluded_costs",
        ) if key in plan}
        rules = []

        def findings(values):
            grouped = {}
            for finding in values or []:
                rule = {key: finding.get(key) for key in ("kind", "term")}
                if rule not in rules:
                    rules.append(rule)
                condition = finding.get("condition", "unknown")
                grouped.setdefault(condition, []).append(rules.index(rule))
            return {key: sorted(set(value)) for key, value in grouped.items()}

        def product(value, dietary=None):
            result = {key: deepcopy(value[key]) for key in (
                "product_ref", "name", "availability", "package", "package_limit",
                "quantity", "merchandise_ore", "mandatory_deposit_ore", "total_payable_ore",
                "price_status",
            ) if key in value}
            description = (value.get("display") or {}).get("package")
            if description:
                result["package_description"] = description
            if "purchase_options" in value:
                result["purchase_options"] = [{key: deepcopy(option[key]) for key in (
                    "option_index", "package_count", "price_kind", "eligibility", "offer_kind",
                    "merchandise_ore", "estimated_merchandise_ore", "mandatory_deposit_ore", "total_payable_ore",
                ) if key in option} for option in value["purchase_options"]]
            dietary = dietary if dietary is not None else value.get("dietary_assessments", value.get("dietary_findings", []))
            if dietary:
                result["dietary_findings"] = findings(dietary)
            return result

        issues = deepcopy(plan.get("unresolved_requirements", []))
        rows = []
        for requirement in plan.get("requirements", []):
            row = {key: deepcopy(requirement[key]) for key in (
                "requirement_id", "item", "quantity", "unit", "status", "sources",
                "gross_quantity", "confirmed_pantry_quantity", "candidate_approval",
            ) if key in requirement}
            observation = requirement.get("observation") or {}
            row["search_query"] = observation.get("query", requirement.get("search", requirement["item"]))
            row["observed_at"] = observation.get("observed_at")
            if observation.get("unavailable_reason"):
                row["unavailable_reason"] = observation["unavailable_reason"]
            by_product = {}
            for finding in requirement.get("dietary_assessments", []):
                by_product.setdefault(str(finding.get("product_ref")), []).append(finding)
            row["candidates"] = [product(value, by_product.get(str(value.get("product_ref")), []))
                                 for value in observation.get("products", [])]
            if "selection" in requirement:
                selection = requirement["selection"]
                row["selection"] = {key: deepcopy(selection[key]) for key in (
                    "coverage", "required", "unit", "coverage_status", "quantity_basis",
                    "expected_coverage", "minimum_coverage", "observed_package",
                    "observed_package_description", "surplus_quantity", "package_count",
                    "merchandise_ore", "mandatory_deposit_ore", "total_payable_ore",
                    "shared_package_allocation", "counts_toward_cart_and_totals",
                ) if key in selection}
                row["selection"]["products"] = [product(value) for value in selection.get("products", [])]
            row["issues"] = [deepcopy(issue) for issue in issues
                             if issue.get("requirement_id") == row["requirement_id"]]
            rows.append(row)
        snapshot.update(requirements=rows, issues=issues, dietary_rules=rules)
        return snapshot

    @staticmethod
    def _product_page_options(request: Mapping[str, Any]) -> tuple[int, int, str]:
        offset = request.get("offset") if request.get("offset") is not None else 0
        limit = request.get("limit") if request.get("limit") is not None else 8
        section = request.get("section") if request.get("section") is not None else "requirements"
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise HouseholdError("products pages require offset >= 0 and limit between 1 and 20")
        if section not in {"requirements", "issues"}:
            raise HouseholdError("products section must be requirements or issues")
        if request.get("requirement_id") is not None and (
            not isinstance(request["requirement_id"], str) or section != "requirements" or offset
        ):
            raise HouseholdError("an exact requirement_id requires section=requirements and offset=0")
        return offset, limit, section

    @staticmethod
    def _product_agent_wire_size(value: Mapping[str, Any]) -> int:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return len(json.dumps({"content": [{"type": "text", "text": payload}], "isError": False},
                              ensure_ascii=False, separators=(",", ":"))) + 256

    def _product_plan_page(
        self, reference: str, record: Mapping[str, Any], request: Mapping[str, Any],
    ) -> dict[str, Any]:
        offset, limit, section = self._product_page_options(request)
        snapshot = record.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise HouseholdError("this older product_plan_ref has no review snapshot; prepare the exact menu again")
        rows = snapshot[section]
        requested_id = request.get("requirement_id")
        if requested_id is not None:
            rows = [row for row in rows if row.get("requirement_id") == requested_id]
            if not rows:
                raise HouseholdError("requirement_id does not belong to this exact product_plan_ref")
        page = {key: deepcopy(value) for key, value in snapshot.items()
                if key not in {"requirements", "issues", "dietary_rules"}}
        page.update(
            projection="agent", product_plan_ref=reference,
            product_selection_digest=record["selection_digest"],
            menu_ref=deepcopy(record["menu_ref"]), preview=record["menu_ref"] is None,
            progress={"requirement_count": len(snapshot["requirements"]),
                      "selected_count": sum(row["status"] == "selected" for row in snapshot["requirements"]),
                      "issue_count": len(snapshot["issues"])},
            section=section, offset=offset, total=len(rows), next_offset=None,
        )
        work = record.get("observation_work") or {}
        page["progress"].update(self._product_work_progress(work))
        if self._product_work_pending(work):
            page["continue_arguments"] = {"action": "prepare", "product_plan_ref": reference}
        if record.get("validation_work") is not None:
            page["validation_progress"] = {**self._product_work_progress(record["validation_work"]),
                "restarted": record["validation_work"].get("restarted", False)}
            page["continue_arguments"] = {"action": "apply", "product_plan_ref": reference,
                "product_plan_digest": record["apply_arguments"]["product_plan_digest"], "cart_change_requested": True}
        for key, digest_key in (("apply_arguments", "product_plan_digest"),
                                ("partial_apply_arguments", "partial_product_plan_digest")):
            arguments = record.get(key)
            if arguments is not None:
                page[key] = {"action": "apply", "product_plan_ref": reference,
                             digest_key: arguments[digest_key],
                             **({"partial_apply": True} if key == "partial_apply_arguments" else {})}
        page["next"] = (
            "Read further pages with products get and this product_plan_ref; continue prepare with only new or changed candidate_approvals. "
            + ("Save the exact planner selection, then prepare its menu_ref before cart apply."
               if page["preview"] else "Apply only the reviewed returned arguments with an explicit cart-change request."
              )
        )
        page[section] = []
        # Size the actual text wrapper, not just the internal JSON. Never cut a
        # requirement or remove a candidate to squeeze in another row.
        for row in rows[offset:offset + limit]:
            proposed = deepcopy(page)
            proposed[section].append(deepcopy(row))
            rule_indexes = set()
            for displayed in proposed.get("requirements", []):
                products = displayed.get("candidates", []) + displayed.get("selection", {}).get("products", [])
                for candidate in products:
                    for indexes in candidate.get("dietary_findings", {}).values():
                        rule_indexes.update(indexes)
            proposed["dietary_rules"] = [{"index": index, **deepcopy(snapshot["dietary_rules"][index])}
                                         for index in sorted(rule_indexes)]
            if self._product_agent_wire_size(proposed) >= 44_000:
                if not page[section]:
                    raise HouseholdError("one product review item exceeds its page budget; prepare a narrower candidate scope")
                break
            page = proposed
        end = offset + len(page[section])
        page["next_offset"] = end if end < len(rows) else None
        if section == "requirements" and snapshot["issues"]:
            page["issues_arguments"] = {"action": "get", "product_plan_ref": reference, "section": "issues"}
        return page

    def _get_product_plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._product_page_options(request)
        keys = [key for key in ("product_plan_ref", "menu_ref", "planner_ref") if request.get(key) is not None]
        if len(keys) != 1:
            raise HouseholdError("products get requires one product_plan_ref, exact menu_ref or planner_ref")
        state = self.store.read()
        reference = request.get("product_plan_ref")
        if reference is None:
            key = keys[0]
            # Verify the supplied identity, but never re-observe retail products.
            self._product_binding(**{key: request[key]})
            matches = []
            for ref, record in state.get("menu_planning", {}).get("prepared", {}).items():
                if not isinstance(record, Mapping) or record.get("kind") != "product_plan":
                    continue
                actual = record.get("menu_ref") if key == "menu_ref" else record.get("binding_arguments", {}).get("planner_ref")
                if actual is not None and canonical(actual) == canonical(request[key]):
                    matches.append(ref)
            if not matches:
                raise HouseholdError("no prepared product snapshot for this exact binding; prepare its products")
            reference = matches[-1]
        record = self._product_plan_record(state, reference)
        return self._product_plan_page(reference, record, request)

    def _product_apply_view(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """Keep write outcomes and reconciliation identities ahead of diagnostics."""
        fields = (
            "applied", "partial_applied", "completed_from_partial", "synced", "idempotent",
            "outcome_unknown", "cart_write_pending", "cart_reconciliation_required", "reason",
            "menu_ref", "product_plan_digest", "partial_product_plan_digest", "remaining_issue_count",
            "cart_changed", "nothing_to_buy", "nothing_to_buy_for_menu", "price_verification",
            "price_locked", "final_price_authority", "product_plan_stale", "menu_binding_stale",
            "default_suggestion", "next",
            "continue_arguments", "validation_progress", "product_plan_ref",
        )
        compact = {key: deepcopy(result[key]) for key in fields if key in result}
        compact["projection"] = "agent"
        compact["status"] = (
            "outcome_unknown" if result.get("outcome_unknown") else
            "partial_applied" if result.get("partial_applied") else
            "applied" if result.get("applied") else result.get("status", "needs_input")
        )
        cart_result = result.get("cart")
        if isinstance(cart_result, Mapping):
            compact["cart"] = {key: deepcopy(cart_result[key]) for key in fields if key in cart_result}
            if cart_result.get("outcome_unknown"):
                compact.update(status="outcome_unknown", outcome_unknown=True)
            plan = cart_result.get("cart_plan")
        else:
            plan = None
        plan = result.get("cart_plan", plan)
        if isinstance(plan, Mapping):
            compact["cart_plan"] = {key: deepcopy(plan[key]) for key in (
                "provider", "menu_ref", "status", "cart_digest", "approved",
            ) if key in plan}
            compact["cart_plan"]["item_count"] = len(plan.get("items", []))
            compact["cart_read_arguments"] = {"action": "get"}
        fresh = result.get("fresh_product_plan")
        if isinstance(fresh, Mapping):
            # A refreshed diagnostic is a new review, never authority to retry
            # the write. Store its bounded facts so every row remains readable.
            binding = fresh.get("binding") or {}
            if binding.get("kind") == "saved_menu":
                arguments = {"menu_ref": binding.get("menu_ref")}
            else:
                handoff = binding.get("planner_handoff") or {}
                arguments = {"planner_selection_ref": {key: handoff.get(key) for key in (
                    "planner_version", "input_digest", "selection_digest")}}
            menu_ref = result.get("menu_ref")
            common = {**arguments, "candidate_approvals": self._plan_approvals(fresh),
                      "ingredient_decisions": fresh.get("ingredient_decisions"),
                      "budget_ore": fresh.get("budget_ore"), "price_mode": fresh.get("price_mode", "exact")}
            try:
                reference, _selection = self._store_product_plan_record(
                    menu_ref=menu_ref, plan=fresh, approvals=self._continuation_approvals(fresh),
                    ingredient_decisions=fresh.get("ingredient_decisions"), budget_ore=fresh.get("budget_ore"),
                    price_mode=fresh.get("price_mode", "exact"), binding_arguments=arguments,
                    apply_arguments={"action": "apply", **common, "product_plan_digest": fresh["product_plan_digest"]}
                    if fresh.get("status") == "prepared" else None,
                    partial_apply_arguments={"action": "apply", **common, "partial_apply": True,
                                             "partial_product_plan_digest": fresh["partial_product_plan_digest"]}
                    if fresh.get("partial_product_plan_digest") else None,
                )
                compact["fresh_product_plan"] = {
                    "status": fresh.get("status"), "product_plan_ref": reference,
                    "product_plan_digest": fresh.get("product_plan_digest"),
                    "next": "Read products get with this reference and review the changed facts before any authorized retry.",
                }
            except HouseholdError:
                compact["fresh_product_plan"] = {"status": "needs_input",
                    "next": "The menu changed while recording diagnostics; read the current menu and prepare its products."}
        if "next" not in compact:
            compact["next"] = ("Read the exact cart and reconcile before any retry."
                               if compact.get("cart_reconciliation_required") or compact.get("outcome_unknown")
                               else "Read the cart for its exact current lines; checkout remains a separate operation.")
        return compact

    @staticmethod
    def _partial_plan_authority(plan: Mapping[str, Any]) -> dict[str, Any]:
        """Facts whose composition is authorized by reviewed partial digests."""
        selected = []
        for requirement in plan.get("requirements", []):
            if not isinstance(requirement, Mapping) or requirement.get("status") != "selected":
                continue
            selected.append({
                key: deepcopy(requirement[key])
                for key in (
                    "requirement_id", "identity", "item", "quantity", "unit",
                    "candidate_approval", "selection",
                )
                if key in requirement
            })
        return {
            "context": {
                key: deepcopy(plan.get(key))
                for key in (
                    "product_plan_version", "provider", "binding", "hard_product_constraints",
                    "ingredient_decisions", "budget_ore", "price_mode",
                )
            },
            "selected_requirements": selected,
        }

    @staticmethod
    def _with_partial_apply_authority(plan: Mapping[str, Any]) -> dict[str, Any]:
        """Expose selected-line authority while completing an existing partial plan."""
        result = deepcopy(dict(plan))
        digest = partial_product_plan_digest(result)
        if digest is not None:
            result["partial_product_plan_digest"] = digest
        return result

    @staticmethod
    def _cart_amount_ore(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            return None
        ore = amount * 100
        if not amount.is_finite() or amount < 0 or ore != ore.to_integral_value():
            return None
        integer = int(ore)
        return integer if integer <= 100_000_000 else None

    def _verified_cart_product_amounts(
        self, plan: Mapping[str, Any], cart: Mapping[str, Any],
    ) -> str:
        # The current MENY cart fixture does not establish a semantic
        # product-line-total node. Its cart prices remain presentation only.
        if self.provider == "meny":
            return "unavailable_after_cart_write"
        if not isinstance(cart, Mapping):
            return "unavailable_after_cart_write"
        expected: dict[str, dict[str, int]] = {}
        for requirement in plan.get("requirements", []):
            selection = requirement.get("selection") if isinstance(requirement, Mapping) else None
            if not isinstance(selection, Mapping) or not isinstance(selection.get("products"), list):
                return "unavailable_after_cart_write"
            for product in selection["products"]:
                if not isinstance(product, Mapping):
                    return "unavailable_after_cart_write"
                try:
                    product_id = self._product_id(product.get("product_ref"))
                except HouseholdError:
                    return "unavailable_after_cart_write"
                quantity = product.get("quantity")
                payable = product.get("total_payable_ore")
                if (
                    isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1
                    or isinstance(payable, bool) or not isinstance(payable, int) or payable < 0
                ):
                    return "unavailable_after_cart_write"
                current = expected.setdefault(product_id, {"quantity": 0, "total_payable_ore": 0})
                current["quantity"] += quantity
                current["total_payable_ore"] += payable
        try:
            summary = cart_summary(cart)
        except HouseholdError:
            return "unavailable_after_cart_write"
        lines = {}
        for line in summary.get("items", []):
            if not isinstance(line, Mapping):
                return "unavailable_after_cart_write"
            try:
                product_id = self._product_id(line.get("product_id"))
            except HouseholdError:
                return "unavailable_after_cart_write"
            if product_id in lines:
                return "unavailable_after_cart_write"
            lines[product_id] = line
        for product_id, prepared in expected.items():
            line = lines.get(product_id)
            if not isinstance(line, Mapping):
                return "unavailable_after_cart_write"
            live_quantity = line.get("quantity")
            line_ore = self._cart_amount_ore(line.get("price"))
            if (
                isinstance(live_quantity, bool) or not isinstance(live_quantity, int)
                or live_quantity < 1 or line_ore is None
            ):
                return "unavailable_after_cart_write"
            if line_ore * prepared["quantity"] != prepared["total_payable_ore"] * live_quantity:
                return "changed_after_cart_write"
        return "unchanged"

    @staticmethod
    def _product_work_progress(work):
        return {"pending_search_count": work.get("pending_search_count", 0),
                "pending_detail_count": work.get("pending_detail_count", 0)}

    @classmethod
    def _product_work_pending(cls, work):
        return any(cls._product_work_progress(work).values())

    @staticmethod
    def _product_current_context(state):
        return mp.digest({key: state.get(key) for key in
            ("provider", "profile", "menu_ingredient_decisions", "product_selection_generation")})

    def _prepare_products(
        self, *, binding: Mapping[str, Any], menu: Mapping[str, Any],
        candidate_approvals: Any, deadline: float | None,
        ingredient_decisions: Any = None, budget_ore: int | None = None, price_mode: str = "exact",
        observe_selected_only: bool = False, work: dict[str, Any] | None = None,
        selected_refs: Mapping[str, list[Any]] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.store.read()
        if snapshot.get("product_selection_generation"):
            binding = {**binding, "selection_generation": snapshot["product_selection_generation"]}
        minimums = saved_menu_minimum_evaluation(menu, snapshot.get("profile") or {})
        if (
            minimums.get("enforced_status", minimums.get("status")) != "pass"
            and (
                minimums.get("complete_menu")
                or menu.get("weekly_plan_complete") is True
            )
        ):
            raise HouseholdError(
                "product preparation requires a complete weekly menu that satisfies saved dietary minimums: "
                + canonical(minimums)
            )
        saved = snapshot.get("menu_ingredient_decisions") or {}
        reference = binding.get("menu_ref")
        if (binding.get("kind") == "planner_selection" and snapshot.get("menu")
                and canonical(snapshot["menu"].get("planner_selection")) == canonical(binding.get("planner_handoff"))):
            reference = self._cart_menu_ref(snapshot["menu"])
        if reference is not None and saved.get("menu_ref") == reference:
            binding = {**binding, "ingredient_decisions_digest": hashlib.sha256(canonical(saved["decisions"]).encode()).hexdigest()}
            exact_menu_requirements(mp.shopping_menu(menu), ingredient_decisions=ingredient_decisions)
            merged = {canonical(d["source"]): d for d in ingredient_decisions or []}
            # A returned stale plan must never resurrect an older stock assertion.
            # Explicit changes go through record_ingredients; current saved facts
            # also win when returning a newly refreshed plan after stale apply.
            merged.update({canonical(d["source"]): deepcopy(d) for d in saved["decisions"]})
            ingredient_decisions = list(merged.values())
        requirements, _issues = exact_menu_requirements(mp.shopping_menu(menu), ingredient_decisions=ingredient_decisions)
        requirement_ids = {row["requirement_id"] for row in requirements}
        approvals = normalize_approvals(candidate_approvals, requirement_ids)
        if work is not None:
            scope = mp.digest({"provider": self.provider, "binding": binding,
                "menu": mp.shopping_menu(menu), "ingredient_decisions": ingredient_decisions or []})
            if work.get("scope_digest") is not None and work["scope_digest"] != scope:
                raise HouseholdError("product observation requirements changed with the menu or ingredient scope; prepare the current menu again")
            work["scope_digest"] = scope
            work["reads"] = 0
        read_deadline = deadline - PRODUCT_BUILD_RESERVE if work is not None and deadline is not None else deadline
        observations = self._product_observations(
            menu, deadline=read_deadline, ingredient_decisions=ingredient_decisions,
            candidate_approvals=candidate_approvals, approved_only=observe_selected_only, work=work,
        )
        if selected_refs is not None:
            for requirement_id, observation in observations.items():
                if "products" in observation:
                    observation["products"] = [p for p in observation["products"]
                        if p["product_ref"] in selected_refs.get(requirement_id, [])]
                    observation["scope"]["returned"] = len(observation["products"])
        stable_approvals = []
        for requirement_id, approval in approvals.items():
            value = {key: deepcopy(item) for key, item in approval.items() if key != "source"}
            if "shared_package" in value:
                value.pop("package_count", None)
                value.pop("quantity_basis", None)
            if "search_query" not in value and len(value["candidate_refs"]) == 1:
                chosen = next((product for product in observations.get(requirement_id, {}).get("products", [])
                               if product.get("product_ref") in value["candidate_refs"]), None)
                if isinstance(chosen, Mapping) and isinstance(chosen.get("name"), str):
                    value["search_query"] = chosen["name"][:150]
            stable_approvals.append(value)
        profile = self.store.read().get("profile")
        diet = profile.get("diet") if isinstance(profile, Mapping) else None
        hard_constraints = {
            key: deepcopy(diet.get(key, []))
            for key in ("allergies_or_sensitivities", "avoid")
            if isinstance(diet, Mapping) and diet.get(key)
        }
        from dietary_assessment import rules
        reader = getattr(self.provider_client, 'product_dietary_evidence', None)
        details = work.setdefault("details", {}) if work is not None else {}
        pending_details = set()
        if reader is not None and rules(profile) and stable_approvals:
            approved_refs = {str(ref) for approval in stable_approvals for ref in approval.get('candidate_refs', [])}
            occurrences = {}
            for observation in observations.values():
                for product in observation.get('products', []):
                    if str(product['product_ref']) in approved_refs:
                        occurrences.setdefault(str(product['product_ref']), []).append(product)
            attempted = work.setdefault("attempted_details", []) if work is not None else []
            for ref in sorted(occurrences, key=lambda ref: attempted.index(ref) if ref in attempted else -1):
                if ref not in details:
                    if ((work is not None and work["reads"] >= MAX_REQUIREMENTS)
                            or read_deadline is not None and time.monotonic() + 20 > read_deadline):
                        pending_details.add(ref)
                        continue
                    if work is not None:
                        work["reads"] += 1
                    if ref in attempted:
                        attempted.remove(ref)
                    attempted.append(ref)
                    try:
                        observed = reader(ref, deadline=read_deadline)
                    except HouseholdError:
                        observed = {"unavailable": "exact_public_product_detail_unavailable"}
                    if not isinstance(observed, Mapping):
                        raise HouseholdError("product dietary evidence returned an invalid result")
                    if observed.get("unavailable") == "detail_time_budget_exhausted":
                        pending_details.add(ref)
                        continue
                    # A completed unavailable/empty public detail is unknown
                    # evidence under the existing dietary policy. Only a read
                    # that did not fit the budget remains pending work.
                    details[ref] = deepcopy(dict(observed))
                for product in occurrences[ref]:
                    product['dietary_evidence'] = {**product.get('dietary_evidence', {}), **details[ref]}
            if work is not None:
                for requirement_id, observation in observations.items():
                    if any(str(p['product_ref']) in pending_details for p in observation.get('products', [])):
                        observation['unavailable_reason'] = 'product_detail_pending'
        if work is not None:
            work["pending_detail_count"] = len(pending_details)
        return build_product_plan(
            provider=self.provider,
            binding=binding,
            menu=mp.shopping_menu(menu),
            observations=observations,
            candidate_approvals=stable_approvals,
            hard_product_constraints=hard_constraints,
            dietary_profile=profile,
            ingredient_decisions=ingredient_decisions, budget_ore=budget_ore, price_mode=price_mode, deadline=deadline,
            selected_refs=selected_refs,
        )

    def _compare_menu_costs(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = request.get("planner_input")
        if not isinstance(value, Mapping):
            raise PlannerError("lowest_cost requires one canonical planner_input")
        # Explicit comparison defaults to three; the normal planner stays at one.
        value = {**value, "alternatives": value.get("alternatives", 3)}
        result, resolved, _ = self._plan_menu(value)
        comparison = {
            "mode": "lowest_cost", "status": "unavailable", "comparison_claim": None,
            "planner_version": result["planner_version"], "input_digest": result["input_digest"],
            "work_limits": {"maximum_alternatives": 3, "maximum_requirements_per_menu": MAX_REQUIREMENTS,
                            "maximum_unique_requirements": MAX_ALTERNATIVE_REQUIREMENTS,
                            "maximum_unique_searches": MAX_ALTERNATIVE_REQUIREMENTS,
                            "operation_timeout_seconds": PRODUCT_OPERATION_TIMEOUT,
                            "maximum_candidates_per_search": MAX_CANDIDATES_PER_REQUIREMENT,
                            "maximum_product_plans": 3},
            "alternatives": [], "unavailable": [],
        }
        if result.get("status") != "planned":
            comparison["unavailable"] = deepcopy(result.get("issues", []))
            return {"plan": result, "cost_comparison": comparison}
        menus = [self._materialize_planner_menu(h, resolved) for h in result["save_handoffs"]]
        requirement_error = None
        try:
            requirements = [exact_menu_requirements(mp.shopping_menu(menu), maximum=MAX_REQUIREMENTS)[0] for menu in menus]
        except HouseholdError as exc:
            requirements = []
            requirement_error = str(exc)
        ids = {r["requirement_id"] for rows in requirements for r in rows}
        queries = {r["identity"] for rows in requirements for r in rows}
        if requirement_error or len(ids) > MAX_ALTERNATIVE_REQUIREMENTS or len(queries) > MAX_ALTERNATIVE_REQUIREMENTS:
            comparison["alternatives"] = [{"original_rank": rank, "selection_digest": h["selection_digest"],
                "non_price_selection": deepcopy(h["selection"]), "save_handoff": deepcopy(h),
                "product_plan": None, "cost_status": "comparison_work_budget_exceeded"}
                for rank, h in enumerate(result["save_handoffs"], 1)]
            comparison["selected_handoff"] = deepcopy(result["save_handoff"])
            comparison["unavailable"] = [{"reason": "comparison_work_budget_exceeded", "detail": requirement_error}]
            return {"plan": result, "cost_comparison": comparison}
        approvals = normalize_approvals(request.get("candidate_approvals"), ids)
        profile = self.store.read()["profile"]
        diet = profile.get("diet", {})
        hard = {key: deepcopy(diet[key]) for key in ("allergies_or_sensitivities", "avoid") if diet.get(key)}
        cache = {}
        menu_observations = []
        for menu, rows in zip(menus, requirements, strict=True):
            selected = [{key: value for key, value in approvals[r['requirement_id']].items() if key != 'source'}
                        for r in rows if r['requirement_id'] in approvals]
            menu_observations.append(self._product_observations(menu, deadline=request.get('_deadline'),
                search_cache=cache, candidate_approvals=selected))
        from dietary_assessment import rules
        reader = getattr(self.provider_client, 'product_dietary_evidence', None)
        if reader is not None and rules(profile):
            refs = {str(ref) for approval in approvals.values() for ref in approval['candidate_refs']}
            for observation in cache.values():
                for product in observation.get('products', []):
                    if str(product['product_ref']) in refs:
                        product['dietary_evidence'] = {**product.get('dietary_evidence', {}), **reader(product['product_ref'], deadline=request.get('_deadline'))}
        for rank, (handoff, menu, rows) in enumerate(zip(result["save_handoffs"], menus, requirements, strict=True), 1):
            observations = menu_observations[rank - 1]
            # Reuse enriched observations for the exact translated/overridden query.
            for row in rows:
                query = approvals.get(row['requirement_id'], {}).get('search_query') or ingredient_search(row['identity'], self.provider)
                if query in cache:
                    observations[row['requirement_id']] = deepcopy(cache[query])
            selected_approvals = [{key: value for key, value in approvals[r["requirement_id"]].items() if key != "source"}
                                  for r in rows if r["requirement_id"] in approvals]
            product_plan = build_product_plan(
                provider=self.provider, binding={"kind": "planner_selection", "planner_handoff": handoff},
                menu=mp.shopping_menu(menu), observations=observations, candidate_approvals=selected_approvals, hard_product_constraints=hard, dietary_profile=profile,
                deadline=request.get("_deadline"),
            )
            comparison["alternatives"].append({
                "original_rank": rank, "selection_digest": handoff["selection_digest"],
                "non_price_selection": deepcopy(handoff["selection"]), "save_handoff": deepcopy(handoff),
                "product_plan": product_plan,
            })
            if product_plan.get("coverage_status") == "practical_estimate":
                comparison["unavailable"].append({"reason": "practical_package_estimate_not_exact_comparison"})
            if product_plan["status"] != "prepared":
                comparison["unavailable"].append({"selection_digest": handoff["selection_digest"],
                                                   "requirements": product_plan["unresolved_requirements"]})
        alternatives = comparison["alternatives"]
        scopes = {canonical(a["product_plan"]["scope"]) for a in alternatives}
        if len(scopes) != 1:
            comparison["unavailable"].append({"reason": "inconsistent_candidate_scope"})
        if not comparison["unavailable"]:
            def cost_key(alternative):
                totals = alternative["product_plan"]["totals"]
                excess = totals["excess_score"]
                return (totals["total_payable_ore"], Fraction(excess["numerator"], excess["denominator"]),
                        totals["package_count"], alternative["original_rank"], alternative["selection_digest"])
            alternatives.sort(key=cost_key)
            comparison["status"] = "compared"
            comparison["comparison_claim"] = f"lowest verified product cost among these {len(alternatives)} exact menu alternatives and their declared provider candidate scopes"
        comparison["selected_handoff"] = deepcopy(alternatives[0]["save_handoff"])
        # Timestamps/display never carry comparison or apply authority.
        comparison["fact_digest"] = hashlib.sha256(canonical({
            "input_digest": result["input_digest"], "status": comparison["status"],
            "alternatives": [{"selection_digest": a["selection_digest"],
                              "product_plan_digest": a["product_plan"]["product_plan_digest"]} for a in alternatives],
        }).encode()).hexdigest()
        if len(json.dumps({"ok": True, "result": comparison}, ensure_ascii=True).encode()) > MAX_REQUEST - 4096:
            raise PlannerError("cost comparison cannot fit the response transport; reduce candidate scope")
        return {"cost_comparison": comparison}

    def _start_managed_product_apply_fence(self) -> None:
        with self.store.locked() as state:
            state["managed_product_apply_fence"] = {
                "menu_ref": deepcopy(self._cart_menu_ref(state.get("menu"))),
                "started_at": self._now().isoformat(),
            }

    @staticmethod
    def _clear_persisted_product_plan(cart_plan: Any) -> None:
        if not isinstance(cart_plan, dict):
            return
        for key in (
            "product_plan_digest", "product_plan_authority", "product_plan_summary",
            "partial_product_plan_digest", "partial_product_plan_digests",
            "partial_product_plan_lines", "partial_product_plan_approvals",
            "partial_product_plan_authority", "partial_product_plan_summary",
        ):
            cart_plan.pop(key, None)

    def _products(self, request: Mapping[str, Any]) -> dict[str, Any]:
        agent = request.get("response_view") == "agent"
        if agent and request.get("action", "prepare") in {"prepare", "get"}:
            self._product_page_options(request)
        result = self._products_operation(request)
        if not agent or result.get("projection") == "agent":
            return result
        if request.get("action", "prepare") == "prepare" and result.get("product_plan_ref"):
            reference = result["product_plan_ref"]
            return self._product_plan_page(reference, self._product_plan_record(self.store.read(), reference), request)
        if request.get("action") == "apply":
            return self._product_apply_view(result)
        return result

    def _products_operation(self, request: Mapping[str, Any], *, validation_record=None, validation_ref=None) -> dict[str, Any]:
        action = request.get("action", "prepare")
        if action == "get":
            return self._get_product_plan(request)
        if request.get("planner_ref") is not None and action != "prepare":
            raise HouseholdError("planner_ref is only for product preparation; save the menu before apply")
        if request.get("planner_ref") is not None and request.get("planner_input") is not None:
            raise HouseholdError("planner_ref replaces planner_input for product preparation")
        if action == "record_ingredients":
            binding, menu, reference = self._product_binding(menu_ref=request.get("menu_ref"))
            decisions = request.get("ingredient_decisions")
            if not isinstance(decisions, list) or not decisions:
                raise HouseholdError("record_ingredients requires the user's explicit ingredient decisions")
            # Validate the exact source positions against this frozen menu, without
            # requiring a provider connection or a complete shopping plan.
            exact_menu_requirements(mp.shopping_menu(menu), ingredient_decisions=decisions)
            with self.store.locked() as state:
                if self._cart_menu_ref(state.get("menu")) != reference:
                    raise HouseholdError("menu changed before recording ingredients")
                if state.get("pending_checkout") or state.get("order_change"):
                    raise HouseholdError("finish the existing checkout before changing menu shopping requirements")
                previous = state.get("menu_ingredient_decisions") or {}
                merged = {canonical(d["source"]): d for d in previous.get("decisions", [])} if previous.get("menu_ref") == reference else {}
                merged.update({canonical(d["source"]): deepcopy(d) for d in decisions})
                state["menu_ingredient_decisions"] = {"menu_ref": deepcopy(reference), "decisions": list(merged.values())}
                state.pop("product_plan_completion", None)
                self._clear_persisted_product_plan(state.get("cart_plan"))
            return {"recorded": True, "menu_ref": reference, "ingredient_decisions": list(merged.values()),
                    "cart_changed": False, "next": "Use these recorded ingredients for this menu; prepare/apply its updated products for an authorized shop."}
        deadline = time.monotonic() + PRODUCT_OPERATION_TIMEOUT
        if request.get("_deadline") is not None:
            deadline = min(deadline, request["_deadline"])
        request = {**request, "_deadline": deadline}
        if action == "lowest_cost":
            return self._compare_menu_costs(request)
        if action == "apply" and request.get("product_plan_ref") is not None:
            if any(request.get(key) is not None for key in (
                "menu_ref", "planner_ref", "planner_handoff", "planner_selection_ref", "product_plan",
                "candidate_approvals", "ingredient_decisions", "budget_ore", "price_mode",
            )):
                raise HouseholdError("product_plan_ref apply replaces binding, candidate and product-plan arguments")
            record = self._product_plan_record(self.store.read(), request["product_plan_ref"])
            if record.get("menu_ref") is None:
                raise HouseholdError("product preview cannot apply; save the selection and prepare its exact menu_ref first")
            partial = request.get("partial_apply") is True
            arguments = record.get("partial_apply_arguments" if partial else "apply_arguments")
            digest_key = "partial_product_plan_digest" if partial else "product_plan_digest"
            if not isinstance(arguments, Mapping) or request.get(digest_key) != arguments.get(digest_key):
                raise HouseholdError("product_plan_ref apply requires its exact reviewed digest and available apply arguments")
            return self._products_operation({
                **deepcopy(dict(arguments)), "cart_change_requested": request.get("cart_change_requested"),
                "_deadline": deadline,
            }, validation_record=record if not partial else None,
               validation_ref=request["product_plan_ref"] if not partial else None)
        if action == "prepare":
            continuation_ref = request.get("product_plan_ref")
            continuation_record = None
            persisted_seed = []
            persisted_authority = None
            persisted_dependencies = None
            mode = request.get("continuation_mode", "extend")
            if continuation_ref is not None:
                if any(request.get(key) is not None for key in (
                    "menu_ref", "planner_ref", "planner_handoff", "planner_selection_ref",
                )):
                    raise HouseholdError("product_plan_ref replaces menu/planner binding arguments")
                continuation_record = self._product_plan_record(self.store.read(), continuation_ref)
                binding, menu, saved_ref = self._product_binding(**continuation_record.get(
                    "binding_arguments", {"menu_ref": continuation_record["menu_ref"]}))
                # A preview reference never silently acquires cart authority
                # merely because its selection was subsequently saved.
                if continuation_record["menu_ref"] is None:
                    saved_ref = None
                approvals = self._merge_product_plan_approvals(
                    continuation_record["candidate_approvals"],
                    request.get("candidate_approvals") or [], mode,
                    dependent_groups=continuation_record["dependent_groups"],
                )
                ingredient_decisions = continuation_record["ingredient_decisions"]
                budget_ore = continuation_record["budget_ore"]
                price_mode = continuation_record["price_mode"]
            else:
                if mode not in {"extend", "reset"}:
                    raise HouseholdError("continuation_mode replace requires product_plan_ref")
                binding, menu, saved_ref = self._product_binding(
                    menu_ref=request.get("menu_ref"),
                    planner_ref=request.get("planner_ref"),
                    planner_handoff=request.get("planner_handoff"),
                    planner_selection_ref=request.get("planner_selection_ref"),
                )
                state = self.store.read()
                cart_plan = (state.get("cart_plan") or {}) if mode != "reset" else {}
                partial_state = any(key in cart_plan for key in (
                        "partial_product_plan_digest", "partial_product_plan_approvals",
                        "partial_product_plan_authority",
                    ))
                full_state = any(key in cart_plan for key in (
                    "product_plan_digest", "product_plan_authority",
                ))
                if partial_state and full_state:
                    raise HouseholdError(
                        "persisted product selections mix partial and full authority"
                    )
                if saved_ref is not None and partial_state:
                    if canonical(cart_plan.get("menu_ref")) != canonical(saved_ref):
                        raise HouseholdError(
                            "persisted partial product selections belong to a different menu"
                        )
                    persisted_seed = self._partial_seed_approvals(cart_plan)
                elif saved_ref is not None and full_state:
                    stored_authority = cart_plan.get("product_plan_authority")
                    stored_context = (
                        stored_authority.get("context")
                        if isinstance(stored_authority, Mapping) else None
                    )
                    explicit_context_change = isinstance(stored_context, Mapping) and any(
                        key in request and request.get(key) is not None
                        and (key != "ingredient_decisions" or bool(request.get(key)))
                        and canonical(request.get(key)) != canonical(stored_context.get(key))
                        for key in ("ingredient_decisions", "budget_ore", "price_mode")
                    )
                    if not explicit_context_change:
                        persisted_authority = self._full_seed_authority(cart_plan, saved_ref)
                        self._verify_full_seed_cart(
                            persisted_authority, menu_ref=saved_ref,
                            expected_cart_plan=cart_plan, deadline=deadline,
                        )
                        persisted_seed = persisted_authority["candidate_approvals"]
                        persisted_dependencies = persisted_authority["dependent_groups"]
                approvals = self._merge_product_plan_approvals(
                    persisted_seed, request.get("candidate_approvals") or [], "extend",
                    dependent_groups=persisted_dependencies,
                )
                if persisted_authority is not None:
                    context = persisted_authority["context"]
                    ingredient_decisions = deepcopy(context.get("ingredient_decisions"))
                    budget_ore = context.get("budget_ore")
                    price_mode = context.get("price_mode") or "exact"
                else:
                    ingredient_decisions = request.get("ingredient_decisions")
                    budget_ore = request.get("budget_ore")
                    price_mode = request.get("price_mode") or "exact"
            observation_work = (deepcopy(continuation_record.get("observation_work") or {})
                                if continuation_record is not None and mode != "reset" else {})
            plan = self._prepare_products(
                work=observation_work,
                binding=binding,
                menu=menu,
                candidate_approvals=approvals,
                ingredient_decisions=ingredient_decisions,
                budget_ore=budget_ore, price_mode=price_mode,
                deadline=deadline,
            )
            if persisted_authority is not None:
                if (
                    self._product_requirement_scope_digest(plan)
                    != persisted_authority["requirement_scope_digest"]
                    or canonical(self._partial_plan_authority(plan).get("context"))
                    != canonical(persisted_authority.get("context"))
                ):
                    raise HouseholdError(
                        "persisted full product selection scope or context changed"
                    )
            partial_apply_digest = plan.get("partial_product_plan_digest")
            if persisted_seed and plan.get("status") == "prepared":
                # The returned full plan remains digest-valid, while callers
                # continuing an existing partial cart can still use selected-
                # line authority for the cumulative apply path.
                partial_apply_digest = partial_product_plan_digest(plan)
            # Store only selections regenerated from the just-refreshed
            # provider observations. Persisted partial approvals are inputs to
            # that refresh above, never unverified output authority.
            retained_approvals = self._continuation_approvals(plan)
            if continuation_record is not None:
                if self._product_requirement_scope_digest(plan) != continuation_record["requirement_scope_digest"]:
                    raise HouseholdError("product_plan_ref requirements changed; reset from the exact current menu")
            selection_ref = None
            if binding.get("kind") == "planner_selection":
                handoff = binding["planner_handoff"]
                selection_ref = {key: deepcopy(handoff[key]) for key in (
                    "planner_version", "input_digest", "selection_digest"
                )}
            binding_arguments = (
                {"menu_ref": deepcopy(binding["menu_ref"])}
                if binding.get("kind") == "saved_menu"
                else {"planner_selection_ref": selection_ref}
            )
            common_arguments = {
                **binding_arguments,
                "candidate_approvals": self._plan_approvals(plan),
                "ingredient_decisions": deepcopy(plan.get("ingredient_decisions")),
                "budget_ore": plan.get("budget_ore"), "price_mode": plan.get("price_mode") or "exact",
            }
            selected_approvals = self._plan_approvals(plan, selected_only=True)
            if persisted_seed and continuation_record is None:
                explicit_ids = {
                    row.get("requirement_id") for row in request.get("candidate_approvals") or []
                    if isinstance(row, Mapping) and isinstance(row.get("requirement_id"), str)
                }
                selected_approvals = [
                    row for row in selected_approvals if row.get("requirement_id") in explicit_ids
                ]
            result = {
                "apply_arguments": {
                "action": "apply", **common_arguments,
                "product_plan_digest": plan["product_plan_digest"],
            } if plan.get("status") == "prepared" else None,
                "partial_apply_arguments": {
                    "action": "apply", "partial_apply": True,
                    **{**common_arguments, "candidate_approvals": selected_approvals},
                    "partial_product_plan_digest": partial_apply_digest,
                } if partial_apply_digest else None,
                "product_plan": plan}
            if saved_ref is None and (request.get("planner_ref") is not None
                                      or continuation_ref is not None
                                      or request.get("response_view") == "agent"):
                result.pop("apply_arguments", None)
                result.pop("partial_apply_arguments", None)
                result["next"] = (
                    "Save this exact menu selection, then prepare products with its menu_ref before cart apply."
                )
            previous = request.get("previous_product_plan")
            if previous is not None:
                previous = validate_product_plan(previous, previous.get("product_plan_digest") if isinstance(previous, Mapping) else None)
                old_binding = previous["binding"]
                same_selection = (
                    old_binding.get("kind") == "planner_selection"
                    and canonical(old_binding.get("planner_handoff")) == canonical(menu.get("planner_selection"))
                )
                identity_binding = {k: v for k, v in old_binding.items() if k != "ingredient_decisions_digest"}
                if not same_selection and canonical(identity_binding) != canonical(binding):
                    raise HouseholdError("previous product plan does not bind this exact menu selection")
                old_facts = {k: v for k, v in previous.items() if k != "binding"}
                new_facts = {k: v for k, v in plan.items() if k != "binding"}
                result["observation_drift"] = {
                    "status": "unchanged" if product_plan_digest(old_facts) == product_plan_digest(new_facts) else "changed",
                    "previous_product_plan_digest": previous["product_plan_digest"],
                    "current_product_plan_digest": plan["product_plan_digest"],
                }
            stored_binding = binding_arguments if saved_ref is not None else {
                "planner_ref": self._planner_ref(binding["planner_handoff"])}
            product_plan_ref, selection_digest = self._store_product_plan_record(
                menu_ref=saved_ref, plan=plan, approvals=retained_approvals,
                ingredient_decisions=plan.get("ingredient_decisions"),
                budget_ore=plan.get("budget_ore"), price_mode=plan.get("price_mode") or price_mode,
                replaced_ref=continuation_ref, expected_record=continuation_record,
                binding_arguments=stored_binding, apply_arguments=result.get("apply_arguments"),
                partial_apply_arguments=result.get("partial_apply_arguments"),
                observation_work=observation_work,
            )
            if self._product_work_pending(observation_work):
                result["continue_arguments"] = {"action": "prepare", "product_plan_ref": product_plan_ref}
            result.update(product_plan_ref=product_plan_ref, product_selection_digest=selection_digest)
            return result
        if action == "apply":
            if request.get("cart_change_requested") is not True:
                return {
                    "applied": False,
                    "reason": "a clear current user request to change the cart is required",
                }
            if request.get("partial_apply") is True:
                if request.get("product_plan") is not None:
                    raise HouseholdError("partial apply uses only the compact selected-line arguments")
                expected_digest = request.get("partial_product_plan_digest")
                if not isinstance(expected_digest, str) or re.fullmatch(r"[a-f0-9]{64}", expected_digest) is None:
                    raise HouseholdError("partial apply needs the exact reviewed partial_product_plan_digest")
                existing_cart_plan = self.store.read().get("cart_plan") or {}
                if existing_cart_plan.get("product_plan_digest"):
                    raise HouseholdError("a complete product plan is already applied; do not replace it with a partial selection")
                self._start_managed_product_apply_fence()
                fresh_binding, menu, expected_menu_ref = self._product_binding(
                    menu_ref=request.get("menu_ref"),
                    planner_handoff=request.get("planner_handoff"),
                    planner_selection_ref=request.get("planner_selection_ref"),
                    require_saved_planner=True,
                )
                supplied_approvals = request.get("candidate_approvals") or []
                explicit_ids = {
                    approval.get("requirement_id")
                    for approval in supplied_approvals
                    if isinstance(approval, Mapping)
                    and isinstance(approval.get("requirement_id"), str)
                }
                prior_approvals = existing_cart_plan.get("partial_product_plan_approvals", [])
                if existing_cart_plan.get("partial_product_plan_digest") and not prior_approvals:
                    return {
                        "applied": False, "status": "needs_input",
                        "menu_ref": deepcopy(expected_menu_ref),
                        "partial_product_plan_digest": expected_digest,
                        "reason": "previous partial selections lack refreshable authority; prepare the menu products again",
                    }
                combined_approvals = {
                    approval["requirement_id"]: deepcopy(approval)
                    for approval in prior_approvals
                    if isinstance(approval, Mapping)
                    and isinstance(approval.get("requirement_id"), str)
                }
                combined_approvals.update({
                    approval["requirement_id"]: deepcopy(approval)
                    for approval in supplied_approvals
                    if isinstance(approval, Mapping)
                    and isinstance(approval.get("requirement_id"), str)
                })
                fresh = self._prepare_products(
                    binding=fresh_binding,
                    menu=menu,
                    candidate_approvals=list(combined_approvals.values()),
                    ingredient_decisions=request.get("ingredient_decisions"),
                    budget_ore=request.get("budget_ore"),
                    price_mode=request.get("price_mode") or "exact",
                    observe_selected_only=True,
                    deadline=deadline,
                )
                if existing_cart_plan.get("partial_product_plan_digest") and fresh.get("status") == "prepared":
                    fresh = self._with_partial_apply_authority(fresh)
                if fresh.get("partial_product_plan_digest") != expected_digest:
                    return {
                        "applied": False, "status": "needs_input",
                        "menu_ref": deepcopy(expected_menu_ref),
                        "partial_product_plan_digest": expected_digest,
                        "reason": "selected product, quantity, availability, eligibility, offer or price facts changed",
                        "fresh_product_plan": fresh,
                    }
                current_approvals = self._plan_approvals(fresh, selected_only=True)
                current_ids = explicit_ids
                existing_plan = self.store.read().get("cart_plan") or {}
                combined_approvals.update({
                    approval["requirement_id"]: deepcopy(approval)
                    for approval in current_approvals
                })
                revalidated = self._prepare_products(
                    binding=fresh_binding,
                    menu=menu,
                    candidate_approvals=list(combined_approvals.values()),
                    ingredient_decisions=fresh.get("ingredient_decisions"),
                    budget_ore=fresh.get("budget_ore"),
                    price_mode=fresh.get("price_mode") or "exact",
                    observe_selected_only=True,
                    deadline=deadline,
                )
                if existing_plan.get("partial_product_plan_digest") and revalidated.get("status") == "prepared":
                    revalidated = self._with_partial_apply_authority(revalidated)
                selected_ids = {
                    row.get("requirement_id") for row in revalidated.get("requirements", [])
                    if row.get("status") == "selected"
                }
                if selected_ids != set(combined_approvals):
                    return {
                        "applied": False, "status": "needs_input",
                        "menu_ref": deepcopy(expected_menu_ref),
                        "partial_product_plan_digest": expected_digest,
                        "reason": "a previously or newly selected product is no longer current and eligible",
                        "fresh_product_plan": revalidated,
                    }
                revalidated_authority = self._partial_plan_authority(revalidated)
                prior_authority = existing_plan.get("partial_product_plan_authority")
                if prior_approvals:
                    if not isinstance(prior_authority, Mapping):
                        return {
                            "applied": False, "status": "needs_input",
                            "menu_ref": deepcopy(expected_menu_ref),
                            "partial_product_plan_digest": expected_digest,
                            "reason": "previous partial selections lack digest-bound facts; prepare the menu products again",
                        }
                    prior_rows = {
                        row.get("requirement_id"): row
                        for row in prior_authority.get("selected_requirements", [])
                        if isinstance(row, Mapping) and isinstance(row.get("requirement_id"), str)
                    }
                    current_rows = {
                        row.get("requirement_id"): row
                        for row in revalidated_authority["selected_requirements"]
                    }
                    unreviewed_prior_ids = set(prior_rows) - current_ids
                    context_changed = canonical(prior_authority.get("context")) != canonical(revalidated_authority["context"])
                    facts_changed = any(
                        requirement_id not in current_rows
                        or canonical(prior_rows[requirement_id]) != canonical(current_rows[requirement_id])
                        for requirement_id in unreviewed_prior_ids
                    )
                    if (context_changed and unreviewed_prior_ids) or facts_changed:
                        return {
                            "applied": False, "status": "needs_input",
                            "menu_ref": deepcopy(expected_menu_ref),
                            "partial_product_plan_digest": expected_digest,
                            "reason": "a previously selected product or its authority context changed; review a fresh partial plan",
                            "fresh_product_plan": revalidated,
                        }
                if revalidated.get("status") == "prepared":
                    completed_plan = deepcopy(revalidated)
                    completed_plan.pop("partial_product_plan_digest", None)
                    completed = self._products({
                        "action": "apply",
                        "product_plan": completed_plan,
                        "product_plan_digest": completed_plan["product_plan_digest"],
                        "cart_change_requested": True,
                        "_deadline": deadline,
                    })
                    if completed.get("applied") is True:
                        completed["completed_from_partial"] = True
                    return completed
                combined_digest = revalidated.get("partial_product_plan_digest")
                requirements = partial_cart_requirements(revalidated, combined_digest)
                selected_lines = [{
                    "requirement_id": row["requirement_id"],
                    "products": prepared_cart_requirements({"requirements": [row]}),
                } for row in revalidated["requirements"] if row.get("status") == "selected"]
                combined_lines = {line["requirement_id"]: line["products"] for line in selected_lines}
                remaining_issue_count = sum(
                    row.get("requirement_id") not in combined_lines
                    for row in revalidated.get("unresolved_requirements", [])
                )
                product_owners = {}
                requirements = []
                for requirement_id in sorted(combined_lines):
                    for product in combined_lines[requirement_id]:
                        product_id = self._product_id(product.get("product_id"))
                        owner = product_owners.setdefault(product_id, requirement_id)
                        if owner != requirement_id:
                            raise HouseholdError(
                                "one product cannot satisfy different requirements across partial applies"
                            )
                        requirements.append(deepcopy(product))

                def final_partial_prewrite_check() -> dict[str, Any]:
                    try:
                        final = self._prepare_products(
                            binding=fresh_binding,
                            menu=menu,
                            candidate_approvals=list(combined_approvals.values()),
                            ingredient_decisions=fresh.get("ingredient_decisions"),
                            budget_ore=fresh.get("budget_ore"),
                            price_mode=fresh.get("price_mode") or "exact",
                            observe_selected_only=True,
                            deadline=deadline,
                        )
                    except HouseholdError:
                        return {"ok": False, "reason": "selected product facts became unavailable immediately before cart sync", "fresh_product_plan": None}
                    final_selected_ids = {
                        row.get("requirement_id") for row in final.get("requirements", [])
                        if row.get("status") == "selected"
                    }
                    return {
                        "ok": final.get("partial_product_plan_digest") == combined_digest
                        and final_selected_ids == set(combined_approvals),
                        "reason": "selected product facts changed immediately before cart sync",
                        "fresh_product_plan": final,
                    }

                cart_result = self._cart_sync({
                    "requirements": requirements,
                    "_expected_menu_ref": expected_menu_ref,
                    "_before_cart_write": final_partial_prewrite_check,
                    "_include_recurring": False,
                }, deadline)
                if cart_result.get("synced") is not True:
                    return {
                        "applied": False, "status": "needs_input",
                        "menu_ref": deepcopy(expected_menu_ref),
                        "partial_product_plan_digest": combined_digest,
                        "reason": "cart reconciliation required", **cart_result,
                    }
                with self.store.locked() as state:
                    if canonical(self._cart_menu_ref(state.get("menu"))) != canonical(expected_menu_ref):
                        raise HouseholdError("menu changed before recording the partial product selection")
                    state.pop("product_plan_completion", None)
                    state.pop("managed_product_apply_fence", None)
                    cart_plan = state["cart_plan"]
                    cart_plan.pop("product_plan_digest", None)
                    cart_plan.pop("product_plan_authority", None)
                    cart_plan.pop("product_plan_summary", None)
                    cart_plan["partial_product_plan_digest"] = combined_digest
                    digests = cart_plan.setdefault("partial_product_plan_digests", [])
                    for digest in (expected_digest, combined_digest):
                        if digest not in digests:
                            digests.append(digest)
                    cart_plan["partial_product_plan_lines"] = [
                        {"requirement_id": requirement_id, "products": deepcopy(combined_lines[requirement_id])}
                        for requirement_id in sorted(combined_lines)
                    ]
                    cart_plan["partial_product_plan_approvals"] = [
                        deepcopy(combined_approvals[requirement_id])
                        for requirement_id in sorted(combined_approvals)
                    ]
                    cart_plan["partial_product_plan_authority"] = deepcopy(revalidated_authority)
                    cart_plan["partial_product_plan_summary"] = {
                        "selected_requirement_ids": sorted(combined_lines),
                        "remaining_issue_count": remaining_issue_count,
                    }
                return {
                    "applied": True, "partial_applied": True, "cart": cart_result,
                    "menu_ref": deepcopy(expected_menu_ref),
                    "partial_product_plan_digest": combined_digest,
                    "remaining_issue_count": remaining_issue_count,
                    "next": "Continue product preparation for the remaining requirements; checkout stays blocked until one complete product plan is applied.",
                }
            supplied = request.get("product_plan")
            if supplied is None:
                expected_digest = request.get("product_plan_digest")
                if not isinstance(expected_digest, str) or re.fullmatch(r"[a-f0-9]{64}", expected_digest) is None:
                    raise HouseholdError("apply needs the exact reviewed product_plan_digest")
                fresh_binding, menu, expected_menu_ref = self._product_binding(
                    menu_ref=request.get("menu_ref"), planner_handoff=request.get("planner_handoff"),
                    planner_selection_ref=request.get("planner_selection_ref"),
                    require_saved_planner=True,
                )
                supplied = {"binding": fresh_binding, "product_plan_digest": expected_digest,
                            "ingredient_decisions": request.get("ingredient_decisions"),
                            "budget_ore": request.get("budget_ore"), "price_mode": request.get("price_mode") or "exact"}
                approvals = request.get("candidate_approvals")
            else:
                supplied = validate_product_plan(supplied, request.get("product_plan_digest"))
                approvals = self._plan_approvals(supplied)
                binding = supplied.get("binding")
                if not isinstance(binding, Mapping):
                    raise HouseholdError("prepared product plan binding is invalid")
                if binding.get("kind") == "saved_menu":
                    fresh_binding, menu, expected_menu_ref = self._product_binding(
                        menu_ref=binding.get("menu_ref")
                    )
                elif binding.get("kind") == "planner_selection":
                    fresh_binding, menu, expected_menu_ref = self._product_binding(
                        planner_handoff=binding.get("planner_handoff"),
                        require_saved_planner=True,
                    )
                else:
                    raise HouseholdError("prepared product plan binding is invalid")
            self._start_managed_product_apply_fence()
            work = deepcopy((validation_record or {}).get("validation_work") or {})
            restarted = False
            now = self._now()
            if work:
                try:
                    age = (now - datetime.fromisoformat(work["started_at"])).total_seconds()
                except (KeyError, TypeError, ValueError):
                    age = -1
                if not 0 <= age <= 3600:
                    work = {}
                    restarted = True
            work.setdefault("started_at", now.isoformat())
            work["restarted"] = restarted
            reviewed = (validation_record or {}).get("snapshot") or supplied
            selected_refs = ({row["requirement_id"]: [p["product_ref"] for p in row.get("selection", {}).get("products", [])]
                              for row in reviewed.get("requirements", [])}
                             if reviewed.get("status") == "prepared" else None)
            context = self._product_current_context(self.store.read())
            fresh = self._prepare_products(
                binding=fresh_binding, menu=menu, candidate_approvals=approvals,
                ingredient_decisions=supplied.get("ingredient_decisions"),
                budget_ore=supplied.get("budget_ore"), price_mode=supplied.get("price_mode") or "exact",
                deadline=deadline, work=work, selected_refs=selected_refs,
            )
            if not 0 <= (self._now() - datetime.fromisoformat(work["started_at"])).total_seconds() <= 3600:
                # Reads that straddled the cycle limit cannot authorize a write.
                # Preserve the review/ref and begin a fresh cycle on continuation.
                restarted = True
                work = {"started_at": self._now().isoformat(), "restarted": True,
                        "pending_search_count": max(1, len(fresh["requirements"])), "pending_detail_count": 0}
            if self._product_work_pending(work):
                if validation_ref is None:
                    common = {"action": "apply", "menu_ref": expected_menu_ref,
                        "candidate_approvals": deepcopy(approvals),
                        "ingredient_decisions": supplied.get("ingredient_decisions"),
                        "budget_ore": supplied.get("budget_ore"), "price_mode": supplied.get("price_mode") or "exact",
                        "product_plan_digest": supplied["product_plan_digest"]}
                    validation_ref, _ = self._store_product_plan_record(
                        menu_ref=expected_menu_ref, plan=supplied if supplied.get("requirements") is not None else fresh,
                        approvals=deepcopy(approvals), ingredient_decisions=supplied.get("ingredient_decisions"),
                        budget_ore=supplied.get("budget_ore"), price_mode=supplied.get("price_mode") or "exact",
                        binding_arguments={"menu_ref": expected_menu_ref}, apply_arguments=common,
                        partial_apply_arguments=None, validation_work=work)
                else:
                    with self.store.locked() as state:
                        records = state["menu_planning"]["prepared"]
                        if (canonical(records.get(validation_ref)) != canonical(validation_record)
                                or self._cart_menu_ref(state.get("menu")) != expected_menu_ref
                                or self._product_current_context(state) != context):
                            raise HouseholdError("product validation context changed while reading; prepare again")
                        records[validation_ref]["validation_work"] = deepcopy(work)
                return {"applied": False, "cart_changed": False, "status": "validating",
                    "menu_ref": expected_menu_ref, "product_plan_ref": validation_ref,
                    "validation_progress": {**self._product_work_progress(work), "restarted": restarted},
                    "continue_arguments": {"action": "apply", "product_plan_ref": validation_ref,
                        "product_plan_digest": supplied["product_plan_digest"], "cart_change_requested": True},
                    "next": "Selected-product reads remain. Continue these exact arguments; no cart write has occurred."}
            # A completed cycle is consumed before either drift review or cart
            # dispatch. Replaying a completed apply starts a fresh read cycle.
            if validation_ref is not None:
                with self.store.locked() as state:
                    records = state["menu_planning"]["prepared"]
                    if canonical(records.get(validation_ref)) != canonical(validation_record):
                        raise HouseholdError("product validation changed while reading; get its latest reference")
                    records[validation_ref].pop("validation_work", None)
            if fresh.get("product_plan_digest") != supplied.get("product_plan_digest"):
                return {"applied": False, "status": "needs_input", "menu_ref": deepcopy(expected_menu_ref),
                    "product_plan_digest": supplied.get("product_plan_digest"),
                    "reason": "menu, selected product, availability, eligibility, offer or price facts changed",
                    "fresh_product_plan": fresh}
            supplied = validate_product_plan(fresh, supplied["product_plan_digest"])

            def final_product_prewrite_check() -> dict[str, Any]:
                current = self.store.read()
                if not 0 <= (self._now() - datetime.fromisoformat(work["started_at"])).total_seconds() <= 3600:
                    return {"ok": False, "reason": "selected-product validation expired before cart sync",
                            "next": "Repeat the same reviewed apply to refresh validation; no cart write occurred.",
                            **({"continue_arguments": {"action": "apply", "product_plan_ref": validation_ref,
                                "product_plan_digest": supplied["product_plan_digest"], "cart_change_requested": True}}
                               if validation_ref is not None else {})}
                return {"ok": self._cart_menu_ref(current.get("menu")) == expected_menu_ref
                        and self._product_current_context(current) == context,
                        "reason": "menu or dietary context changed before cart sync"}

            if (not prepared_cart_requirements(supplied) and not self.store.read()["recurring_items"]
                    and not (self.store.read().get("cart_plan") or {}).get("supplemental_quantities")):
                cart_result = self._cart_sync({"requirements": [], "_expected_menu_ref": expected_menu_ref,
                    "_allow_empty_requirements": True, "_before_cart_write": final_product_prewrite_check,
                    "_expected_product_context": context}, deadline)
                if not cart_result.get("synced"):
                    return {
                        "applied": False,
                        "menu_ref": deepcopy(expected_menu_ref),
                        "product_plan_digest": supplied["product_plan_digest"],
                        **cart_result,
                    }
                summary = cart_summary(cart_result["cart"])
                live, names = self._cart_lines(summary)
                if live:
                    with self.store.locked() as state:
                        self._set_cart_needs_input(state["cart_plan"], live, names)
                        question = self._cart_question(state["cart_plan"], summary, reason="menu_fully_covered_review_existing_cart")
                    return {
                        "applied": False, "nothing_to_buy_for_menu": True,
                        "menu_ref": deepcopy(expected_menu_ref),
                        "product_plan_digest": supplied["product_plan_digest"],
                        **question,
                    }
                with self.store.locked() as state:
                    if (self._cart_menu_ref(state.get("menu")) != expected_menu_ref
                            or self._product_current_context(state) != context):
                        raise HouseholdError("menu or dietary context changed before recording pantry coverage")
                    state["product_plan_completion"] = {"menu_ref": deepcopy(expected_menu_ref), "nothing_to_buy": True, "product_plan_digest": supplied["product_plan_digest"]}
                    state.pop("managed_product_apply_fence", None)
                    for key in (
                        "partial_product_plan_digest", "partial_product_plan_digests",
                        "partial_product_plan_lines", "partial_product_plan_approvals",
                        "partial_product_plan_authority", "partial_product_plan_summary",
                    ):
                        state["cart_plan"].pop(key, None)
                    state["cart_plan"]["product_plan_digest"] = supplied["product_plan_digest"]
                    state["cart_plan"]["product_plan_authority"] = self._full_plan_authority(
                        supplied, expected_menu_ref, state["cart_plan"]
                    )
                    state["cart_plan"]["product_plan_summary"] = {
                        key: deepcopy(supplied.get(key))
                        for key in (
                            "totals", "cost_status", "budget_status", "budget_ore",
                            "ingredient_decisions", "coverage_status",
                        )
                    }
                    state["cart_plan"]["product_plan_summary"]["quantity_estimates"] = []
                return {
                    "applied": True,
                    "cart_changed": bool(cart_result.get("applied_operations")),
                    "nothing_to_buy": True,
                    "menu_ref": deepcopy(expected_menu_ref),
                    "product_plan_digest": supplied["product_plan_digest"],
                    "cart": cart_result,
                    "product_plan": supplied,
                }
            cart_result = self._cart_sync({
                "requirements": prepared_cart_requirements(supplied),
                "_allow_empty_requirements": True,
                "_expected_menu_ref": expected_menu_ref,
                "_before_cart_write": final_product_prewrite_check,
                "_expected_product_context": context,
            }, deadline)
            if cart_result.get("synced") is not True:
                return {
                    "applied": False,
                    "menu_ref": deepcopy(expected_menu_ref),
                    "product_plan_digest": supplied["product_plan_digest"],
                    **({"status": "needs_input"} if cart_result.get("product_plan_stale") else {}),
                    "reason": "cart reconciliation required",
                    **cart_result,
                }
            with self.store.locked() as state:
                if (canonical(self._cart_menu_ref(state.get("menu"))) != canonical(expected_menu_ref)
                        or self._product_current_context(state) != context):
                    raise HouseholdError("menu or dietary context changed before recording the applied product plan")
                state.pop("product_plan_completion", None)
                state.pop("managed_product_apply_fence", None)
                state["cart_plan"].pop("partial_product_plan_digest", None)
                state["cart_plan"].pop("partial_product_plan_digests", None)
                state["cart_plan"].pop("partial_product_plan_lines", None)
                state["cart_plan"].pop("partial_product_plan_approvals", None)
                state["cart_plan"].pop("partial_product_plan_authority", None)
                state["cart_plan"].pop("partial_product_plan_summary", None)
                state["cart_plan"]["product_plan_digest"] = supplied["product_plan_digest"]
                state["cart_plan"]["product_plan_authority"] = self._full_plan_authority(
                    supplied, expected_menu_ref, state["cart_plan"]
                )
                state["cart_plan"]["product_plan_summary"] = {
                    key: deepcopy(supplied.get(key)) for key in ("totals", "cost_status", "budget_status", "budget_ore", "ingredient_decisions", "coverage_status")
                }
                meals = (state.get("profile") or {}).get("meals") or {}
                expected_dinners = meals.get("dinner_days")
                has_explicit_slots = isinstance(menu.get("slots"), list)
                dinner_slots = [
                    slot for slot in menu.get("slots", [])
                    if isinstance(slot, Mapping) and slot.get("meal_type") == "dinner"
                ] if has_explicit_slots else []
                observed_dinners = len(dinner_slots) if has_explicit_slots else len(menu.get("dishes", []))
                state["cart_plan"]["weekly_minimums_enforced"] = (
                    menu.get("weekly_plan_complete") is True
                    or (
                    type(expected_dinners) is int and expected_dinners > 0
                    and observed_dinners == expected_dinners
                    )
                )
                requirements_by_id = {
                    row.get("requirement_id"): row for row in supplied["requirements"]
                    if isinstance(row, Mapping)
                }
                quantity_estimates = []
                for row in supplied["requirements"]:
                    selection = row.get("selection", {})
                    if (
                        selection.get("coverage_status") != "practical_estimate"
                        or selection.get("counts_toward_cart_and_totals") is False
                    ):
                        continue
                    estimate = {
                        "item": row["item"], "quantity": deepcopy(row["quantity"]),
                        "unit": row["unit"], "quantity_basis": selection["quantity_basis"],
                        "packages": [
                            {"name": product["name"], "quantity": product["quantity"]}
                            for product in selection["products"]
                        ],
                    }
                    allocation = selection.get("shared_package_allocation")
                    if isinstance(allocation, Mapping):
                        estimate["shared_requirements"] = [
                            {
                                "item": requirements_by_id[member]["item"],
                                "quantity": deepcopy(requirements_by_id[member]["quantity"]),
                                "unit": requirements_by_id[member]["unit"],
                            }
                            for member in allocation.get("requirement_ids", [])
                            if member in requirements_by_id
                        ]
                    quantity_estimates.append(estimate)
                state["cart_plan"]["product_plan_summary"]["quantity_estimates"] = quantity_estimates
            price_verification = self._verified_cart_product_amounts(
                supplied, cart_result.get("cart")
            )
            return {
                "applied": True,
                "menu_ref": deepcopy(expected_menu_ref),
                "product_plan_digest": supplied["product_plan_digest"],
                "cart": cart_result,
                "price_verification": price_verification,
                "price_locked": False,
                "final_price_authority": "provider checkout summary",
            }
        raise HouseholdError("unknown products action")

    @staticmethod
    def _cart_menu_ref(menu: Any) -> dict[str, Any] | None:
        if not isinstance(menu, Mapping):
            return None
        return {
            "menu_id": menu.get("menu_id"),
            "revision": menu.get("revision"),
            "digest": menu.get("digest"),
        }

    def _cart_lines(self, summary: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, str]]:
        quantities: dict[str, int] = {}
        names: dict[str, str] = {}
        for item in summary.get("items", []):
            if not isinstance(item, Mapping):
                raise HouseholdError("provider cart item is invalid")
            product_id = self._product_id(item.get("product_id"))
            quantity = item.get("quantity")
            name = str(item.get("name") or "").strip()
            if product_id in quantities or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or not name:
                raise HouseholdError("provider cart product identity is ambiguous")
            quantities[product_id] = quantity
            names[product_id] = name
        return quantities, names

    @staticmethod
    def _cart_digest(quantities: Mapping[str, int]) -> str:
        return hashlib.sha256(canonical(dict(sorted(quantities.items()))).encode()).hexdigest()

    def _cart_requirements(self, value: Any, *, allow_empty: bool = False) -> tuple[dict[str, int], dict[str, str]]:
        if allow_empty and value == []:
            return {}, {}
        if not isinstance(value, list) or not value:
            raise HouseholdError("cart sync needs one or more exact product requirements")
        quantities: dict[str, int] = {}
        names: dict[str, str] = {}
        for item in value:
            if not isinstance(item, Mapping):
                raise HouseholdError("cart requirements must be objects")
            product_id = self._product_id(item.get("product_id"))
            name = str(item.get("product_name") or "").strip()
            quantity = item.get("quantity")
            if not name or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise HouseholdError("cart requirements need exact product_id, name and positive integer quantity")
            if product_id in names and names[product_id] != name:
                raise HouseholdError("one product_id cannot have conflicting requirement names")
            names[product_id] = name
            quantities[product_id] = quantities.get(product_id, 0) + quantity
            if quantities[product_id] > 1_000_000:
                raise HouseholdError("cart requirement quantity is too large")
        return quantities, names

    @staticmethod
    def _cart_target(plan: Mapping[str, Any]) -> dict[str, int]:
        baseline = plan["baseline_quantities"]
        requirements = plan["required_quantities"]
        supplements = plan.get("supplemental_quantities", {})
        extra = set(plan["start_as_extra_product_ids"])
        target = {}
        for product_id in set(baseline) | set(requirements) | set(supplements):
            quantity = (
                baseline.get(product_id, 0) + requirements.get(product_id, 0)
                if product_id in extra
                else max(baseline.get(product_id, 0), requirements.get(product_id, 0))
            )
            quantity += supplements.get(product_id, 0)
            if quantity:
                target[product_id] = quantity
        return target

    @staticmethod
    def _meny_cart_batches(operations: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        batch: list[dict[str, Any]] = []
        remaining_capacity = MAX_CART_CLICKS
        for operation in operations:
            remaining_quantity = abs(operation["quantity"])
            direction = 1 if operation["quantity"] > 0 else -1
            while remaining_quantity:
                amount = min(remaining_quantity, remaining_capacity)
                batch.append({**operation, "quantity": direction * amount})
                remaining_quantity -= amount
                remaining_capacity -= amount
                if remaining_capacity == 0:
                    batches.append(batch)
                    batch = []
                    remaining_capacity = MAX_CART_CLICKS
        if batch:
            batches.append(batch)
        return batches

    def _cart_provider_call(self, tool: str, arguments: Mapping[str, Any], *, deadline: float | None) -> dict[str, Any]:
        if deadline is not None and time.monotonic() >= deadline:
            raise HouseholdError("cart operation deadline reached; reconcile any unverified changes")
        kwargs = {"deadline": deadline} if deadline is not None else {}
        return self.provider_client.call(tool, arguments, **kwargs)

    def _apply_meny_cart_batches(
        self, operations: list[dict[str, Any]], acknowledged: dict[str, int],
        *, deadline: float | None,
    ) -> dict[str, int]:
        for batch in self._meny_cart_batches(operations):
            before = self._cart_provider_call("get_cart", {}, deadline=deadline)
            before_live, _before_names = self._cart_lines(cart_summary(before))
            if before_live != acknowledged:
                raise HouseholdError("MENY cart changed between bounded batches")
            changed_cart = self._cart_provider_call(
                "manipulate_cart", {"operations": batch}, deadline=deadline
            )
            for operation in batch:
                product_id = str(operation["productId"])
                quantity = acknowledged.get(product_id, 0) + operation["quantity"]
                if quantity > 0:
                    acknowledged[product_id] = quantity
                else:
                    acknowledged.pop(product_id, None)
            changed_live, _changed_names = self._cart_lines(cart_summary(changed_cart))
            if changed_live != acknowledged:
                raise HouseholdError("MENY cart changed between bounded batches")
        return acknowledged

    def _cart_plan_view(self, plan: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
        live, live_names = self._cart_lines(summary)
        requirements = plan["required_quantities"]
        names = {**plan["product_names"], **live_names}
        target = self._cart_target(plan)
        approved = plan.get("approved_cart_digest") == self._cart_digest(live)
        items = []
        for product_id in sorted(set(live) | set(requirements) | set(plan["baseline_quantities"]) | set(plan.get("supplemental_quantities", {}))):
            current = live.get(product_id, 0)
            required = requirements.get(product_id, 0)
            items.append({
                "product_id": product_id,
                "name": names.get(product_id, product_id),
                "start_quantity": plan["baseline_quantities"].get(product_id, 0),
                "required_quantity": required,
                "target_quantity": target.get(product_id, 0),
                "confirmed_added_quantity": plan["added_quantities"].get(product_id, 0),
                "supplemental_quantity": plan.get("supplemental_quantities", {}).get(product_id, 0),
                "live_quantity": current,
                "extra_quantity": max(current - required, 0),
                "missing_quantity": max(target.get(product_id, 0) - current, 0),
                "unresolved_start_quantity": bool(plan["baseline_quantities"].get(product_id, 0) and not approved),
            })
        return {
            "provider": plan["provider"],
            "menu_ref": deepcopy(plan["menu_ref"]),
            "status": plan["status"],
            "cart_digest": self._cart_digest(live),
            "approved": approved,
            "items": items,
        }

    def _new_cart_plan(
        self,
        menu_ref: Mapping[str, Any],
        live: Mapping[str, int],
        names: Mapping[str, str],
        requirements: Mapping[str, int],
        requirement_names: Mapping[str, str],
        start_as_extra: set[str],
        previous: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        retained_added = {}
        supplements = {}
        baseline = dict(live)
        status = "active"
        if isinstance(previous, Mapping) and previous.get("provider") == self.provider:
            for product_id, quantity in previous.get("added_quantities", {}).items():
                retained = min(quantity, max(0, live.get(product_id, 0) - previous.get("supplemental_quantities", {}).get(product_id, 0)))
                if retained:
                    retained_added[product_id] = retained
                    baseline[product_id] = live.get(product_id, 0) - retained
                    if baseline[product_id] == 0:
                        baseline.pop(product_id)
            for product_id, quantity in previous.get("supplemental_quantities", {}).items():
                retained = min(quantity, baseline.get(product_id, 0))
                if retained:
                    supplements[product_id] = retained
                    baseline[product_id] -= retained
                    if baseline[product_id] == 0:
                        baseline.pop(product_id)
            # Replacing a menu is not external cart drift. Only the quantities
            # attributable to our previous sync may be removed automatically.
            status = "active" if previous.get("status") == "active" and previous.get("last_synced_digest") == self._cart_digest(live) else "needs_input"
        digest = self._cart_digest(live)
        return {
            "provider": self.provider,
            "menu_ref": deepcopy(dict(menu_ref)),
            "status": status,
            "baseline_quantities": baseline,
            "required_quantities": dict(requirements),
            "added_quantities": retained_added,
            "supplemental_quantities": supplements,
            "start_as_extra_product_ids": sorted(start_as_extra.intersection(baseline)),
            "product_names": {**dict(names), **dict(requirement_names)},
            "last_synced_quantities": dict(live),
            "last_synced_digest": digest,
            "approved_cart_digest": None,
            "pending_cart_digest": digest if status == "needs_input" else None,
            "updated_at": self._now().isoformat(),
        }

    def _cart_question(self, plan: Mapping[str, Any], summary: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
        return {
            "cart_reconciliation_required": True,
            "reason": reason,
            "default_suggestion": "keep_current",
            "cart_plan": self._cart_plan_view(plan, summary),
            "next": (
                "Ask one combined question for this exact cart digest. Suggest keeping the current cart, "
                "but require an explicit answer. The owner may name exact exclusions, restore missing menu products, "
                "or explicitly accept the current missing quantities."
            ),
        }

    def _set_cart_needs_input(self, plan: dict[str, Any], live: Mapping[str, int], names: Mapping[str, str]) -> None:
        digest = self._cart_digest(live)
        plan["status"] = "needs_input"
        plan["last_synced_quantities"] = dict(live)
        plan["last_synced_digest"] = digest
        plan["pending_cart_digest"] = digest
        plan["approved_cart_digest"] = None
        plan["product_names"].update(names)
        plan["updated_at"] = self._now().isoformat()

    @staticmethod
    def _retain_acknowledged_cart_additions(
        plan: dict[str, Any], before: Mapping[str, int],
        acknowledged: Mapping[str, int], live: Mapping[str, int],
    ) -> None:
        capacity = {product_id: max(0, quantity - plan.get("supplemental_quantities", {}).get(product_id, 0))
                    for product_id, quantity in live.items()}
        retained = {
            product_id: min(quantity, capacity.get(product_id, 0))
            for product_id, quantity in plan["added_quantities"].items()
            if min(quantity, capacity.get(product_id, 0)) > 0
        }
        for product_id in set(before) | set(acknowledged):
            confirmed = acknowledged.get(product_id, 0) - before.get(product_id, 0)
            if confirmed > 0:
                retained[product_id] = min(
                    retained.get(product_id, 0) + confirmed,
                    capacity.get(product_id, 0),
                )
        plan["added_quantities"] = retained

    def _cart_sync(self, request: Mapping[str, Any], deadline: float | None) -> dict[str, Any]:
        expected_menu_ref = self._require_cart_menu(request)
        with self.store.locked() as state:
            state.pop("product_plan_completion", None)
        requirements, requirement_names = self._cart_requirements(request.get("requirements"), allow_empty=request.get("_allow_empty_requirements") is True)
        menu_requirements = dict(requirements)
        snapshot = self.store.read()
        menu = snapshot.get('menu') or {}
        recurring = []
        if request.get("_include_recurring", True) and snapshot['recurring_items']:
            year, week = map(int, menu['week'].split('-W'))
            recurring = self._due_recurring(snapshot, date.fromisocalendar(year, week, 1))
        for item in recurring:
            key = self._product_id(item['product_id'])
            requirements[key] = requirements.get(key, 0) + item['quantity']
            requirement_names.setdefault(key, item['product_name'])
        extra_values = request.get("start_as_extra_product_ids")
        if extra_values is None:
            prior_plan = snapshot.get("cart_plan") or {}
            extra_values = prior_plan.get("start_as_extra_product_ids", []) if prior_plan.get("provider") == self.provider else []
        if not isinstance(extra_values, list):
            raise HouseholdError("start_as_extra_product_ids must be a list")
        start_as_extra = {self._product_id(value) for value in extra_values}
        first_cart = self._cart_provider_call("get_cart", {}, deadline=deadline)
        first_summary = cart_summary(first_cart)
        first_live, first_names = self._cart_lines(first_summary)
        if request.get("start_as_extra_product_ids") is not None and not start_as_extra.issubset(first_live):
            raise HouseholdError("starting quantities can be extra only for exact products already in the cart")
        approved_idempotent = False
        with self.store.locked() as state:
            if state.get("pending_checkout") or state.get("order_change"):
                raise HouseholdError("finish the pending checkout or order change before syncing a menu cart")
            menu_ref = self._cart_menu_ref(state.get("menu"))
            if menu_ref is None:
                raise HouseholdError("save the active menu before syncing its cart")
            if canonical(expected_menu_ref) != canonical(menu_ref):
                raise HouseholdError("prepared product plan menu binding is stale")
            current = deepcopy(state.get("cart_plan"))
            same_plan = (
                isinstance(current, Mapping)
                and current.get("provider") == self.provider
                and canonical(current.get("menu_ref")) == canonical(menu_ref)
            )
            if not same_plan:
                current = self._new_cart_plan(
                    menu_ref, first_live, first_names, requirements, requirement_names,
                    start_as_extra, previous=current if isinstance(current, Mapping) else None,
                )
                state["cart_plan"] = deepcopy(current)
            else:
                requirements_changed = (
                    canonical(current.get("required_quantities")) != canonical(requirements)
                    or set(current.get("start_as_extra_product_ids", [])) != start_as_extra
                )
                if (current.get("menu_required_quantities", current.get("required_quantities")) != menu_requirements
                        or set(current.get("start_as_extra_product_ids", [])) != start_as_extra):
                    self._clear_persisted_product_plan(current)
                current["required_quantities"] = dict(requirements)
                current["start_as_extra_product_ids"] = sorted(start_as_extra)
                current["product_names"].update(requirement_names)
                first_digest = self._cart_digest(first_live)
                if not requirements_changed and current.get("approved_cart_digest") == first_digest:
                    current["status"] = "active"
                    current["pending_cart_digest"] = None
                    current["last_synced_quantities"] = dict(first_live)
                    current["last_synced_digest"] = first_digest
                    current["product_names"].update(first_names)
                    current["updated_at"] = self._now().isoformat()
                    approved_idempotent = True
                elif current.get("status") == "needs_input" and current.get("pending_cart_digest") != first_digest:
                    # A failed post-write read can leave the plan fenced without
                    # a digest. The next managed sync must bind the observed cart
                    # so the owner can reconcile it instead of reaching for a raw
                    # cart write to escape the fence.
                    self._set_cart_needs_input(current, first_live, first_names)
                elif current.get("last_synced_digest") != first_digest:
                    self._set_cart_needs_input(current, first_live, first_names)
                elif requirements_changed:
                    current["approved_cart_digest"] = None
                state["cart_plan"] = deepcopy(current)
            current['menu_required_quantities'] = menu_requirements
            current['recurring_items'] = recurring
            state['cart_plan'] = deepcopy(current)
        if current["status"] == "needs_input":
            return {"synced": False, **self._cart_question(current, first_summary, reason="cart_or_menu_changed_before_sync")}
        before_cart_write = request.get("_before_cart_write")
        if before_cart_write is not None:
            if not callable(before_cart_write):
                raise HouseholdError("cart prewrite check is invalid")
            guard = before_cart_write()
            if not isinstance(guard, Mapping) or guard.get("ok") is not True:
                return {
                    "synced": False,
                    "product_plan_stale": True,
                    "reason": (
                        str(guard.get("reason"))
                        if isinstance(guard, Mapping) and guard.get("reason")
                        else "product facts changed immediately before cart sync"
                    ),
                    "fresh_product_plan": (
                        deepcopy(guard.get("fresh_product_plan"))
                        if isinstance(guard, Mapping) else None
                    ),
                    **({key: deepcopy(guard[key]) for key in ("continue_arguments", "next") if key in guard}
                       if isinstance(guard, Mapping) else {}),
                }
        if approved_idempotent:
            return {
                "synced": True,
                "idempotent": True,
                "applied_operations": [],
                "cart": first_cart,
                "cart_plan": self._cart_plan_view(current, first_summary),
            }
        target = self._cart_target(current)
        operations = []
        for product_id in sorted(set(target) | set(first_live)):
            missing = target.get(product_id, 0) - first_live.get(product_id, 0)
            if missing:
                operations.append({
                    "productId": int(product_id) if self.provider in {"oda", "mathem"} else product_id,
                    "quantity": missing,
                })
        mutation_error = None
        acknowledged_live = dict(first_live)
        if operations:
            prewrite_cart = self._cart_provider_call("get_cart", {}, deadline=deadline)
            prewrite_summary = cart_summary(prewrite_cart)
            prewrite_live, prewrite_names = self._cart_lines(prewrite_summary)
            if self._cart_digest(prewrite_live) != self._cart_digest(first_live):
                with self.store.locked() as state:
                    plan = state["cart_plan"]
                    self._set_cart_needs_input(plan, prewrite_live, prewrite_names)
                    current = deepcopy(plan)
                return {"synced": False, **self._cart_question(current, prewrite_summary, reason="cart_changed_immediately_before_sync")}
            if expected_menu_ref is not None:
                with self.store.locked() as state:
                    if canonical(self._cart_menu_ref(state.get("menu"))) != canonical(expected_menu_ref):
                        return {
                            "synced": False,
                            "menu_binding_stale": True,
                            "reason": "prepared product plan menu binding changed immediately before sync",
                        }
            if request.get("_expected_product_context") is not None:
                guard = before_cart_write()
                if guard.get("ok") is not True:
                    return {"synced": False, "product_plan_stale": True,
                            **{key: deepcopy(guard[key]) for key in ("reason", "continue_arguments", "next") if key in guard}}
            if deadline is not None and time.monotonic() >= deadline:
                raise HouseholdError("cart operation deadline reached before dispatch")
            try:
                if self.provider == "meny":
                    self._apply_meny_cart_batches(
                        operations, acknowledged_live, deadline=deadline
                    )
                else:
                    self._cart_provider_call("manipulate_cart", {"operations": operations}, deadline=deadline)
            except HouseholdError as exc:
                mutation_error = exc
        try:
            verified_cart = self._cart_provider_call("get_cart", {}, deadline=deadline)
        except HouseholdError:
            if operations:
                with self.store.locked() as state:
                    plan = state["cart_plan"]
                    plan["status"] = "needs_input"
                    plan["approved_cart_digest"] = None
                    plan["pending_cart_digest"] = None
                    plan["updated_at"] = self._now().isoformat()
                    current = deepcopy(plan)
                return {
                    "synced": None,
                    "outcome_unknown": True,
                    "cart_write_pending": True,
                    "cart_reconciliation_required": True,
                    "reason": "cart_write_verification_unavailable",
                    # This is the exact last verified pre-write cart identity;
                    # parity after the dispatched mutation remains unknown.
                    "cart_plan": self._cart_plan_view(current, prewrite_summary),
                    "next": (
                        "Read and reconcile this exact cart plan before any retry; "
                        "the dispatched write may already have committed."
                    ),
                }
            raise
        verified_summary = cart_summary(verified_cart)
        verified_live, verified_names = self._cart_lines(verified_summary)
        expected = dict(first_live)
        for operation in operations:
            product_id = str(operation["productId"])
            expected[product_id] = expected.get(product_id, 0) + operation["quantity"]
            if expected[product_id] == 0:
                expected.pop(product_id)
        if mutation_error is not None or verified_live != expected:
            with self.store.locked() as state:
                plan = state["cart_plan"]
                self._retain_acknowledged_cart_additions(
                    plan, first_live, acknowledged_live, verified_live
                )
                self._set_cart_needs_input(plan, verified_live, verified_names)
                current = deepcopy(plan)
            return {
                "synced": False,
                **self._cart_question(
                    current,
                    verified_summary,
                    reason="cart_write_result_uncertain" if mutation_error is not None else "cart_changed_during_sync",
                ),
            }
        with self.store.locked() as state:
            plan = state.get("cart_plan")
            if not isinstance(plan, dict) or canonical(plan.get("menu_ref")) != canonical(current.get("menu_ref")):
                raise HouseholdError("cart plan changed while syncing")
            for operation in operations:
                product_id = str(operation["productId"])
                remaining = max(0, plan["added_quantities"].get(product_id, 0) + operation["quantity"])
                if remaining:
                    plan["added_quantities"][product_id] = remaining
                else:
                    plan["added_quantities"].pop(product_id, None)
            plan["required_quantities"] = dict(requirements)
            plan["product_names"].update({**verified_names, **requirement_names})
            plan["last_synced_quantities"] = dict(verified_live)
            plan["last_synced_digest"] = self._cart_digest(verified_live)
            plan["approved_cart_digest"] = None
            plan["pending_cart_digest"] = None
            plan["status"] = "active"
            plan["updated_at"] = self._now().isoformat()
            current = deepcopy(plan)
        return {
            "synced": True,
            "idempotent": not operations,
            "applied_operations": operations,
            "cart": verified_cart,
            "cart_plan": self._cart_plan_view(current, verified_summary),
        }

    def _cart_reconcile(self, request: Mapping[str, Any], deadline: float | None) -> dict[str, Any]:
        expected_menu_ref = self._require_cart_menu(request)
        with self.store.locked() as state:
            state.pop("product_plan_completion", None)
        decision = request.get("decision")
        if decision not in {"keep_current", "restore_missing"}:
            raise HouseholdError("cart reconciliation decision must be keep_current or restore_missing")
        supplied_digest = request.get("cart_digest")
        if not isinstance(supplied_digest, str) or re.fullmatch(r"[a-f0-9]{64}", supplied_digest) is None:
            raise HouseholdError("cart reconciliation needs the exact returned cart_digest")
        excluded_values = request.get("exclude_product_ids") or []
        accepted_missing_values = request.get("accept_missing_product_ids") or []
        if not isinstance(excluded_values, list) or not isinstance(accepted_missing_values, list):
            raise HouseholdError("cart exclusions and accepted missing products must be lists")
        excluded = {self._product_id(value) for value in excluded_values}
        accepted_missing = {self._product_id(value) for value in accepted_missing_values}
        first_cart = self._cart_provider_call("get_cart", {}, deadline=deadline)
        first_summary = cart_summary(first_cart)
        first_live, first_names = self._cart_lines(first_summary)
        first_digest = self._cart_digest(first_live)
        with self.store.locked() as state:
            plan = deepcopy(state.get("cart_plan"))
        if not isinstance(plan, dict) or canonical(plan.get("menu_ref")) != canonical(expected_menu_ref) or plan.get("status") != "needs_input" or plan.get("pending_cart_digest") != supplied_digest:
            raise HouseholdError("cart reconciliation is not bound to the pending cart")
        if first_digest != supplied_digest:
            with self.store.locked() as state:
                current = state["cart_plan"]
                self._set_cart_needs_input(current, first_live, first_names)
                plan = deepcopy(current)
            return {"reconciled": False, **self._cart_question(plan, first_summary, reason="cart_changed_after_question")}
        requirements = plan["required_quantities"]
        unknown = (excluded | accepted_missing) - (set(first_live) | set(requirements) | set(plan.get("supplemental_quantities", {})))
        if unknown:
            raise HouseholdError("cart decisions must use exact products from the current plan or cart")
        target = dict(first_live)
        if decision == "restore_missing":
            for product_id, required in self._cart_target(plan).items():
                if first_live.get(product_id, 0) < required and product_id not in accepted_missing:
                    target[product_id] = required
        for product_id in excluded:
            current = target.get(product_id, 0)
            required = requirements.get(product_id, 0)
            if current <= required:
                if product_id not in accepted_missing:
                    raise HouseholdError("an exclusion cannot reduce below the menu requirement unless that missing product is explicitly accepted")
                target.pop(product_id, None)
            elif required:
                target[product_id] = required
            else:
                target.pop(product_id, None)
        operations = []
        for product_id in sorted(set(first_live) | set(target)):
            delta = target.get(product_id, 0) - first_live.get(product_id, 0)
            if delta:
                operations.append({"productId": int(product_id) if self.provider in {"oda", "mathem"} else product_id, "quantity": delta})
        mutation_error = None
        acknowledged_live = dict(first_live)
        if operations:
            prewrite_cart = self._cart_provider_call("get_cart", {}, deadline=deadline)
            prewrite_summary = cart_summary(prewrite_cart)
            prewrite_live, prewrite_names = self._cart_lines(prewrite_summary)
            if self._cart_digest(prewrite_live) != supplied_digest:
                with self.store.locked() as state:
                    current = state["cart_plan"]
                    self._set_cart_needs_input(current, prewrite_live, prewrite_names)
                    plan = deepcopy(current)
                return {"reconciled": False, **self._cart_question(plan, prewrite_summary, reason="cart_changed_immediately_before_decision")}
            try:
                if self.provider == "meny":
                    self._apply_meny_cart_batches(
                        operations, acknowledged_live, deadline=deadline
                    )
                else:
                    self._cart_provider_call("manipulate_cart", {"operations": operations}, deadline=deadline)
            except HouseholdError as exc:
                mutation_error = exc
        try:
            verified_cart = self._cart_provider_call("get_cart", {}, deadline=deadline)
        except HouseholdError:
            if operations:
                with self.store.locked() as state:
                    plan = state["cart_plan"]
                    plan["status"] = "needs_input"
                    plan["approved_cart_digest"] = None
                    plan["pending_cart_digest"] = None
            raise
        verified_summary = cart_summary(verified_cart)
        verified_live, verified_names = self._cart_lines(verified_summary)
        if mutation_error is not None or verified_live != target:
            with self.store.locked() as state:
                current = state["cart_plan"]
                self._reduce_supplements(current, first_live, {key: verified_live.get(key, 0) if key in excluded else value for key, value in first_live.items()})
                self._retain_acknowledged_cart_additions(
                    current, first_live, acknowledged_live, verified_live
                )
                self._set_cart_needs_input(current, verified_live, verified_names)
                plan = deepcopy(current)
            return {
                "reconciled": False,
                **self._cart_question(
                    plan,
                    verified_summary,
                    reason="cart_write_result_uncertain" if mutation_error is not None else "cart_changed_while_applying_decision",
                ),
            }
        approved_digest = self._cart_digest(verified_live)
        with self.store.locked() as state:
            current = state.get("cart_plan")
            if not isinstance(current, dict) or current.get("pending_cart_digest") != supplied_digest:
                raise HouseholdError("cart plan changed while applying the decision")
            self._reduce_supplements(current, first_live, verified_live)
            # Excluding a surplus also withdraws its extra-purpose allocation.
            # The same physical units may now cover the new menu, with no
            # quantity decrease (for example two existing fish packages).
            for product_id in excluded:
                supplements = current.get("supplemental_quantities", {})
                remaining = min(supplements.get(product_id, 0), max(0, verified_live.get(product_id, 0) - requirements.get(product_id, 0)))
                if remaining:
                    supplements[product_id] = remaining
                else:
                    supplements.pop(product_id, None)
            current["added_quantities"] = {
                product_id: min(quantity, verified_live.get(product_id, 0))
                for product_id, quantity in current["added_quantities"].items()
                if min(quantity, verified_live.get(product_id, 0)) > 0
            }
            for operation in operations:
                if operation["quantity"] > 0:
                    product_id = str(operation["productId"])
                    current["added_quantities"][product_id] = current["added_quantities"].get(product_id, 0) + operation["quantity"]
            current["added_quantities"] = {
                key: min(value, max(0, verified_live.get(key, 0) - current.get("supplemental_quantities", {}).get(key, 0)))
                for key, value in current["added_quantities"].items()
            }
            current["product_names"].update(verified_names)
            current["last_synced_quantities"] = dict(verified_live)
            current["last_synced_digest"] = approved_digest
            current["approved_cart_digest"] = approved_digest
            current["pending_cart_digest"] = None
            current["status"] = "active"
            current["updated_at"] = self._now().isoformat()
            authority = current.get("product_plan_authority")
            if (
                isinstance(authority, Mapping)
                and authority.get("cart_digest") != approved_digest
            ):
                current.pop("product_plan_digest", None)
                current.pop("product_plan_authority", None)
                current.pop("product_plan_summary", None)
            plan = deepcopy(current)
        return {
            "reconciled": True,
            "decision": decision,
            "excluded_product_ids": sorted(excluded),
            "accepted_missing_product_ids": sorted(accepted_missing),
            "cart": verified_cart,
            "cart_plan": self._cart_plan_view(plan, verified_summary),
        }

    def _cart_checkout_gate(self, summary: Mapping[str, Any], menu: Mapping[str, Any]) -> dict[str, Any] | None:
        self._require_menu_provider(menu)
        live, names = self._cart_lines(summary)
        digest = self._cart_digest(live)
        menu_ref = self._cart_menu_ref(menu)
        with self.store.locked() as state:
            if state.get("managed_product_apply_fence"):
                return {
                    "confirmed": False,
                    "status": "needs_input",
                    "reason": "managed_product_apply_incomplete",
                    "next": "Reconcile the cart and complete a fresh saved-menu product apply before checkout.",
                }
            minimums = saved_menu_minimum_evaluation(menu, state.get("profile") or {})
            plan = state.get("cart_plan")
            if (
                minimums.get("enforced_status", minimums.get("status")) != "pass"
                and (
                    minimums.get("complete_menu")
                    or isinstance(plan, Mapping) and plan.get("weekly_minimums_enforced") is True
                )
            ):
                return {
                    "confirmed": False,
                    "status": "needs_input",
                    "reason": "weekly_menu_minimums_unsatisfied",
                    "minimum_evaluation": minimums,
                    "next": "Replan and save a complete weekly menu that satisfies the current profile minimums.",
                }
            bound = (
                isinstance(plan, dict)
                and plan.get("provider") == self.provider
                and canonical(plan.get("menu_ref")) == canonical(menu_ref)
            )
            if not bound:
                previous = deepcopy(plan) if isinstance(plan, Mapping) else None
                plan = self._new_cart_plan(menu_ref or {}, live, names, {}, {}, set(), previous=previous)
                self._set_cart_needs_input(plan, live, names)
                state["cart_plan"] = plan
                result = deepcopy(plan)
                reason = "missing_or_stale_cart_plan"
            elif plan.get("partial_product_plan_digest"):
                return {
                    "confirmed": False,
                    "status": "needs_input",
                    "reason": "weekly_menu_products_incomplete",
                    "remaining_issue_count": (plan.get("partial_product_plan_summary") or {}).get("remaining_issue_count"),
                    "next": "Complete and apply one full saved-menu product plan before checkout.",
                }
            elif plan.get("approved_cart_digest") == digest:
                plan["status"] = "active"
                plan["pending_cart_digest"] = None
                plan["last_synced_quantities"] = dict(live)
                plan["last_synced_digest"] = digest
                plan["product_names"].update(names)
                plan["updated_at"] = self._now().isoformat()
                return None
            else:
                view = self._cart_plan_view(plan, summary)
                has_extra = any(item["extra_quantity"] > item["supplemental_quantity"] for item in view["items"])
                has_missing = any(item["missing_quantity"] > 0 for item in view["items"])
                has_unresolved_start = any(item["unresolved_start_quantity"] for item in view["items"])
                if not has_extra and not has_missing and not has_unresolved_start:
                    plan["approved_cart_digest"] = digest
                    plan["pending_cart_digest"] = None
                    plan["status"] = "active"
                    plan["last_synced_quantities"] = dict(live)
                    plan["last_synced_digest"] = digest
                    plan["product_names"].update(names)
                    plan["updated_at"] = self._now().isoformat()
                    return None
                self._set_cart_needs_input(plan, live, names)
                result = deepcopy(plan)
                reason = "cart_requires_owner_decision"
        return self._cart_question(result, summary, reason=reason)

    def _require_cart_menu(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._require_menu_provider(self.store.read().get("menu"))
        supplied = request.get("menu_ref", request.get("_expected_menu_ref"))
        if not isinstance(supplied, Mapping) or set(supplied) != {"menu_id", "revision", "digest"}:
            raise HouseholdError("cart sync and reconcile require the exact menu_ref from the menu or question")
        current = self._cart_menu_ref(self.store.read().get("menu"))
        if current is None or canonical(current) != canonical(supplied):
            raise HouseholdError("cart menu_ref is stale; read the menu and rebuild its requirements")
        return deepcopy(dict(supplied))

    @staticmethod
    def _reduce_supplements(plan: dict[str, Any], before: Mapping[str, int], after: Mapping[str, int]) -> None:
        supplements = plan.setdefault("supplemental_quantities", {})
        for product_id, old in before.items():
            delta = after.get(product_id, 0) - old
            if delta >= 0:
                continue
            supplements[product_id] = max(0, supplements.get(product_id, 0) + delta)
            retained = max(0, after.get(product_id, 0) - supplements[product_id])
            plan["added_quantities"][product_id] = min(plan["added_quantities"].get(product_id, 0), retained)
            plan["baseline_quantities"][product_id] = min(plan["baseline_quantities"].get(product_id, 0), retained - plan["added_quantities"][product_id])
        plan["supplemental_quantities"] = {key: value for key, value in supplements.items() if value}

    def _record_supplemental_cart_change(
        self, state: dict[str, Any], before: Mapping[str, int],
        after: Mapping[str, int], names: Mapping[str, str],
    ) -> None:
        menu_ref = self._cart_menu_ref(state.get("menu"))
        if menu_ref is None:
            return
        plan = state.get("cart_plan")
        if not isinstance(plan, dict) or plan.get("menu_ref") != menu_ref:
            plan = self._new_cart_plan(menu_ref, before, names, {}, {}, set(), previous=plan)
            plan["status"] = "needs_input"
            state["cart_plan"] = plan
        approved = plan.get("approved_cart_digest") == self._cart_digest(before)
        drift = plan.get("last_synced_digest") != self._cart_digest(before)
        self._reduce_supplements(plan, before, after)
        supplements = plan.setdefault("supplemental_quantities", {})
        for product_id in set(before) | set(after):
            delta = after.get(product_id, 0) - before.get(product_id, 0)
            if delta > 0:
                supplements[product_id] = supplements.get(product_id, 0) + delta
        plan["supplemental_quantities"] = {key: value for key, value in supplements.items() if value}
        missing = any(after.get(key, 0) < value for key, value in plan["required_quantities"].items())
        if drift or missing or plan["status"] == "needs_input":
            self._set_cart_needs_input(plan, after, names)
        else:
            plan["last_synced_quantities"] = dict(after)
            plan["last_synced_digest"] = self._cart_digest(after)
            plan["approved_cart_digest"] = self._cart_digest(after) if approved else None
            plan["product_names"].update(names)
            plan["updated_at"] = self._now().isoformat()
        state.pop("product_plan_completion", None)

    @staticmethod
    def _require_raw_cart_write_allowed(state: Mapping[str, Any]) -> None:
        if state.get("managed_product_apply_fence"):
            raise HouseholdError(
                "complete a fresh products prepare/apply before raw cart changes; "
                "reconcile its pending menu cart question first when required"
            )

    def _complete_cart_write(self, pending: Mapping[str, Any], cart: Mapping[str, Any]) -> None:
        quantities, names = self._cart_lines(cart_summary(cart))
        if quantities != pending["expected"] and not pending.get("dispatch_finished"):
            raise HouseholdError("cart write is still uncertain; reconcile_change only reads and never resends")
        with self.store.locked() as state:
            if canonical(state.get("pending_cart_change")) != canonical(pending):
                raise HouseholdError("pending cart change changed during readback")
            if canonical(state.get("order_change")) != canonical(pending.get("order_change")):
                raise HouseholdError("cart order binding changed during readback")
            if pending.get("order_change"):
                state["order_change"]["kind"] = "full_order" if self.provider == "meny" else "addition"
                if self.provider in {"oda", "mathem"}:
                    state["order_change"]["expected_cart_quantities"] = dict(quantities)
            else:
                self._record_supplemental_cart_change(state, pending["before"], pending["expected"], names)
                if quantities != pending["expected"] and isinstance(state.get("cart_plan"), dict):
                    self._set_cart_needs_input(state["cart_plan"], quantities, names)
            if pending.get("clear_requested"):
                self._clear_persisted_product_plan(state.get("cart_plan"))
                state.pop("product_plan_completion", None)
                # A partial MENY clear still requires a fresh product apply.
                if quantities:
                    state["managed_product_apply_fence"] = {
                        "menu_ref": deepcopy(self._cart_menu_ref(state.get("menu"))),
                        "started_at": self._now().isoformat(),
                    }
                    if isinstance(state.get("cart_plan"), dict):
                        self._set_cart_needs_input(state["cart_plan"], quantities, names)
                else:
                    state.pop("cart_plan", None)
                    state.pop("managed_product_apply_fence", None)
            state.pop("pending_cart_change", None)

    def _cart(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "get")
        if action in {"change", "apply", "set", "update", "ensure", "sync", "weekly"}:
            self._require_raw_cart_write_allowed(self.store.read())
        if action == 'weekly':
            state = self.store.read()
            plan = state.get('cart_plan') or {}
            reference = self._cart_menu_ref(state.get('menu'))
            if (not reference or canonical(request.get('menu_ref')) != canonical(reference)
                    or canonical(plan.get('menu_ref')) != canonical(reference)
                    or not plan.get('product_plan_digest')):
                return {'synced': False, 'status': 'needs_input', 'reason': 'Prepare and apply the menu product plan first; weekly goods are included there.'}
            required = plan.get('menu_required_quantities', plan['required_quantities'])
            return self._cart({**request, 'action': 'sync', 'requirements': [
                {'product_id': key, 'product_name': plan['product_names'][key], 'quantity': value}
                for key, value in required.items()], '_allow_empty_requirements': True})
        if action in {"sync", "reconcile"}:
            deadline = time.monotonic() + MENY_CART_TIMEOUT if self.provider == "meny" else request.get("_deadline")
            with self._browser_operation(deadline):
                if self.store.read().get("pending_cart_change"):
                    raise HouseholdError("reconcile_change before another cart write")
                return self._cart_sync(request, deadline) if action == "sync" else self._cart_reconcile(request, deadline)
        if action == "get":
            cart = self.provider_client.call("get_cart", {}, deadline=request.get("_deadline"), allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.provider_client.call("get_cart", {}, deadline=request.get("_deadline"))
            state = self.store.read()
            plan = state.get("cart_plan")
            try:
                observed_digest = {"cart_digest": self._cart_digest(self._cart_lines(cart_summary(cart))[0])}
            except HouseholdError:
                # Keep incomplete retailer reads readable; never invent a writable identity.
                observed_digest = {}
            return {**cart, **observed_digest, **({"cart_write_pending": True} if state.get("pending_cart_change") else {}), **({"meal_concierge_cart_plan": self._cart_plan_view(plan, cart_summary(cart))} if isinstance(plan, Mapping) else {})}
        if action not in {"change", "apply", "set", "update", "clear", "ensure", "reconcile_change"}:
            raise HouseholdError("unknown cart action")
        deadline = time.monotonic() + MENY_CART_TIMEOUT if self.provider == "meny" else request.get("_deadline")
        with self._browser_operation(deadline, allow_pending_cart=action == "reconcile_change"):
            state = self.store.read()
            if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                raise HouseholdError("reconcile the pending checkout before changing the cart")
            if (state.get("pending_cancellation") or {}).get("status") in {"clicking", "uncertain"}:
                raise HouseholdError("reconcile the pending cancellation before changing the cart")
            pending = state.get("pending_cart_change")
            if action == "reconcile_change":
                if not pending:
                    return {"reconciled": True, "cart_write_pending": False}
                if pending["provider"] != self.provider:
                    raise HouseholdError("pending cart provider changed")
                if self.provider == "meny":
                    change = pending.get("order_change") or {}
                    self.browser.verify_order_change(change.get("order_id"), change.get("code"), deadline=deadline)
                cart = self.provider_client.call("get_cart", {}, deadline=deadline)
                self._complete_cart_write(pending, cart)
                return {"reconciled": True, "cart_write_pending": False, "cart": cart,
                        "next": "The saved write is verified. Rerun ensure for the original minimum if a multi-batch request was interrupted; never repeat a delta."}
            if pending:
                raise HouseholdError("reconcile_change before another cart write; do not repeat an uncertain delta")
            change = deepcopy(state.get("order_change"))
            if action == "clear" and change:
                raise HouseholdError("finish or abort the active order edit before clearing the cart")
            if change and change.get("kind") == "reduction":
                raise HouseholdError("finish or reconcile the order removal before changing the addition cart")
            if change and change.get("status") != "editing":
                raise HouseholdError("the order change is still starting")
            if self.provider in {"oda", "mathem"} and change and change.get("requested_delivery"):
                raise HouseholdError(f"finish or abort the staged {self.provider.title()} delivery change before adding items")
            if self.provider == "meny":
                self.browser.verify_order_change(change.get("order_id") if change else None, change.get("code") if change else None, deadline=deadline)
            desired = self._cart_requirements(request.get("requirements"))[0] if action == "ensure" else None
            ordered = {}
            if change and self.provider in {"oda", "mathem"}:
                current = self._orders({"action": "get", "order_id": change["order_id"], "_deadline": deadline})
                if current["tracking"].get("status") != "paid_and_modifiable":
                    raise HouseholdError(f"{self.provider.title()} no longer allows additions to this order; retain the staged goods for review")
                if canonical(current) != canonical(change["before"]):
                    raise HouseholdError(f"the target {self.provider.title()} order changed; read it again before continuing")
                if desired is not None:
                    ordered = oda_order_quantities(current["order"])
                    if ordered is None:
                        raise HouseholdError(f"{self.provider.title()} ordered quantities cannot be verified")
            cart = self.provider_client.call("get_cart", {}, deadline=deadline)
            before, _names = self._cart_lines(cart_summary(cart))
            if change and self.provider in {"oda", "mathem"} and before != change.get("expected_cart_quantities", {}):
                raise HouseholdError(f"{self.provider.title()} addition cart changed outside this edit; abort with retain_cart=true, then review its destination again")
            if action == "clear":
                if request.get("operations") or request.get("requirements"):
                    raise HouseholdError("clear takes only the observed cart_digest, not operations or requirements")
                if request.get("cart_digest") != self._cart_digest(before):
                    raise HouseholdError("cart changed or cart_digest missing; get the cart before clearing it")
                operations = [{"productId": key, "quantity": -quantity} for key, quantity in before.items()]
                if not operations:
                    with self.store.locked() as locked:
                        if (locked.get("pending_cart_change") or locked.get("order_change")
                                or (locked.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES
                                or (locked.get("pending_cancellation") or {}).get("status") in {"clicking", "uncertain"}):
                            raise HouseholdError("cart operation changed before clearing local selections")
                        locked["product_selection_generation"] = secrets.token_hex(16)
                        locked.pop("cart_plan", None)
                        locked.pop("product_plan_completion", None)
                        locked.pop("managed_product_apply_fence", None)
                    return {"cleared": True, "idempotent": True, "cart": cart}
            elif desired is not None:
                operations = [{"productId": key, "quantity": quantity - before.get(key, 0) - ordered.get(key, 0)}
                              for key, quantity in desired.items() if quantity > before.get(key, 0) + ordered.get(key, 0)]
            else:
                operations = request.get("operations")
                if not isinstance(operations, list) or not operations:
                    raise HouseholdError("cart change needs operations")
            normalized = []
            for item in operations:
                if not isinstance(item, Mapping):
                    raise HouseholdError("cart operations must be objects")
                if set(item).difference({"product_id", "productId", "quantity"}):
                    raise HouseholdError("cart operations use product_id and signed quantity; empty a cart with action=clear and its cart_digest")
                key = self._product_id(item.get("product_id", item.get("productId")))
                if "product_id" in item and "productId" in item and str(item["product_id"]) != str(item["productId"]):
                    raise HouseholdError("cart operation product identifiers disagree")
                quantity = item.get("quantity")
                if isinstance(quantity, bool) or not isinstance(quantity, int) or not quantity or abs(quantity) > 1_000_000:
                    raise HouseholdError("cart changes require bounded nonzero integer quantity deltas")
                normalized.append({"productId": int(key) if self.provider in {"oda", "mathem"} else key, "quantity": quantity})
            # All deltas are journalled before dispatch, including each small MENY batch.
            batches = self._meny_cart_batches(normalized) if self.provider == "meny" else ([normalized] if normalized else [])
            for batch in batches:
                expected = dict(before)
                for item in batch:
                    key = str(item["productId"])
                    expected[key] = expected.get(key, 0) + item["quantity"]
                    if expected[key] < 0:
                        raise HouseholdError("cart quantity cannot become negative")
                expected = {key: value for key, value in expected.items() if value}
                latest = self.provider_client.call("get_cart", {}, deadline=deadline)
                if self._cart_lines(cart_summary(latest))[0] != before:
                    raise HouseholdError("cart changed before the update; read it again without repeating a delta")
                with self.store.locked() as locked:
                    if locked.get("pending_cart_change") or canonical(locked.get("order_change")) != canonical(change):
                        raise HouseholdError("cart operation changed before dispatch")
                    if action != "clear":
                        self._require_raw_cart_write_allowed(locked)
                    if (locked.get("pending_cancellation") or {}).get("status") in {"clicking", "uncertain"}:
                        raise HouseholdError("reconcile the pending cancellation before changing the cart")
                    if (locked.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                        raise HouseholdError("reconcile the pending checkout before changing the cart")
                    pending = {"provider": self.provider, "before": before, "expected": expected,
                               "order_change": deepcopy(change), "operations": batch,
                               **({"clear_requested": True} if action == "clear" else {})}
                    if action == "clear":
                        locked["product_selection_generation"] = secrets.token_hex(16)
                    locked["pending_cart_change"] = deepcopy(pending)
                arguments = {"operations": batch}
                if self.provider == "meny" and change:
                    arguments["order_change_code"] = change["code"]
                try:
                    self.provider_client.call("manipulate_cart", arguments, deadline=deadline)
                except MenyCartStoppedError as exc:
                    acknowledged = dict(before)
                    for item in exc.applied_operations:
                        key = str(item["productId"])
                        acknowledged[key] = acknowledged.get(key, 0) + item["quantity"]
                    pending = {**pending, "expected": {key: value for key, value in acknowledged.items() if value}, "dispatch_finished": not exc.applied_operations}
                    with self.store.locked() as locked:
                        locked["pending_cart_change"] = deepcopy(pending)
                    if self.provider == "meny":
                        self.browser.verify_order_change(change.get("order_id") if change else None, change.get("code") if change else None, deadline=deadline)
                    cart = self.provider_client.call("get_cart", {}, deadline=deadline)
                    self._complete_cart_write(pending, cart)
                    return {"ensured": False, "stopped": True, "reason": str(exc), "cart": cart,
                            "next": "The adapter stopped before the next click. Earlier additions are retained; resolve product availability before a new ensure."}
                if self.provider == "meny":
                    self.browser.verify_order_change(change.get("order_id") if change else None, change.get("code") if change else None, deadline=deadline)
                cart = self.provider_client.call("get_cart", {}, deadline=deadline)
                self._complete_cart_write(pending, cart)
                before = expected
                change = deepcopy(self.store.read().get("order_change"))
            if action == "clear":
                return {"cleared": True, "idempotent": False, "cart": cart}
            if desired is not None:
                return {"ensured": True, "idempotent": not normalized, "applied_operations": normalized,
                        "cart": cart, "order_id": change.get("order_id") if change else None}
            return cart
