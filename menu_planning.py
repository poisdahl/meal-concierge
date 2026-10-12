"""Exact stable meal slots and structural shopping comparisons (no providers)."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
import json
from typing import Any, Mapping

from core import HouseholdError
from product_planner import menu_requirements

MAX_PLANNING_MENUS = 2000


def relative(menu):
    return (menu.get("planning_scope") or {}).get("planning_mode") == "relative"


def slot_day(slot):
    return slot.get("day", slot.get("date"))


def day_fields(day):
    return {"day" if type(day) is int else "date": day}


def past(slot, today):
    value = slot_day(slot)
    return isinstance(value, str) and value < today


def checked_day(value, *, relative=False):
    if relative:
        if type(value) is not int or not 1 <= value <= 31:
            raise HouseholdError("relative day must be an integer from one to 31")
    else:
        try:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError()
        except (TypeError, ValueError):
            raise HouseholdError("meal date must be a canonical ISO date") from None
    return value


def day_number(value):
    return value if type(value) is int else date.fromisoformat(value).toordinal()


def within_storage(source, target, storage):
    start, end = slot_day(source), slot_day(target)
    gap = day_number(end) - day_number(start)
    if gap <= 0 or gap > (storage.get("max_interval_days") if storage.get("max_interval_days") is not None else 3650000):
        return False
    if storage.get("use_by_date") is not None:
        if type(start) is int:
            raise HouseholdError("absolute use-by dates require a dated menu; use a verified storage interval for relative days")
        return end <= storage["use_by_date"]
    return True


def bind_calendar(menu, first_meal_date, delivery, *, timezone_name="Europe/Oslo"):
    """Derive a new revision; the original relative selection remains provenance."""
    from service_common import menu_digest
    if not relative(menu):
        raise HouseholdError("calendar binding requires an unbound relative menu")
    start = date.fromisoformat(checked_day(first_meal_date))
    result = deepcopy(menu)
    if menu.get("historical_slot_ids"):
        raise HouseholdError("historical meals cannot be rebound to a new calendar")
    first_day = min(checked_day(slot_day(s), relative=True) for s in result["slots"])
    def bind_slot(slot):
        day = checked_day(slot_day(slot), relative=True)
        slot.pop("day", None)
        slot["date"] = (start + timedelta(days=day - first_day)).isoformat()
    for slot in result["slots"]:
        bind_slot(slot)
    for batch in result.get("batches", []) or ([result["batch"]] if result.get("batch") else []):
        for slot in batch["leftovers"]:
            bind_slot(slot)
        batch["relative_spec_digest"] = batch["spec_digest"]
        batch["spec_digest"] = digest({k: v for k, v in batch.items() if k != "spec_digest"})
    scope = result["planning_scope"]
    scope["relative_days"] = scope.pop("days")
    scope["dates"] = sorted({s["date"] for s in result["slots"] if s["meal_type"] == "dinner"})
    scope["planning_mode"] = "ad_hoc"
    scope["relative_origin"] = True
    result["week"] = min(s["date"] for s in result["slots"])
    result["week"] = date.fromisoformat(result["week"]).strftime("%G-W%V")
    result["schedule"] = schedule(result)
    result["delivery_date"] = datetime.fromisoformat(delivery["slot"]["start_at"].replace("Z", "+00:00")).astimezone(ZoneInfo(timezone_name)).date().isoformat()
    result["calendar_binding"] = {"source_menu_ref": menu_ref(menu), "timezone": timezone_name,
        "first_meal_date": first_meal_date, "delivery": deepcopy(delivery)}
    result["revision"] += 1
    result["digest"] = menu_digest(result)
    return result


def require_calendar_eligible(state, menu):
    """A pending purchase may not move recorded cooking or mixed history."""
    if not relative(menu):
        return
    if menu.get("historical_slot_ids") or any(slot_outcome(state, menu, s) is not None for s in menu["slots"]):
        raise HouseholdError("recorded relative meals require a new menu before calendar binding")
    owners = {menu.get("slot_owners", {}).get(s["slot_id"], menu["menu_id"]) for s in menu["slots"]}
    for owner in owners:
        usage = state.get("recipe_usage", {}).get(owner, {})
        if (usage.get("order_id") or usage.get("cooked_keys") or usage.get("not_cooked_keys")
                or any(slot_outcome(state, {**menu, "menu_id": owner}, s) is not None for s in usage.get("slots", []))):
            raise HouseholdError("mixed_history_owner: start a distinct menu to preserve recorded meal history")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def slot_order(slot):
    from recipes import RECIPE_CATEGORIES
    return slot_day(slot), RECIPE_CATEGORIES.index(slot["meal_type"]), slot["slot_id"]


def source_slot(menu, slot):
    """A consumption slot names its one preparation, never a recipe family."""
    source_id = slot.get("source_slot_id", slot["slot_id"])
    return slot_by_id(menu, source_id)


def recipe_for_slot(menu, slot, *, allow_stale=False):
    source = source_slot(menu, slot)
    recipes = menu.get("dishes", []) + menu.get("salads", [])
    bound = [r for r in recipes if r.get("preparation_slot_id") == source["slot_id"]]
    if not bound:
        bound = [r for r in recipes if r.get("preparation_slot_id") is None
                 and r.get("recipe_key") == source.get("recipe_key")
                 and digest(r) == source.get("snapshot_digest")]
    if not bound and allow_stale:
        bound = [r for r in recipes if r.get("preparation_slot_id") is None
                 and r.get("recipe_key") == source.get("recipe_key")]
    if len(bound) != 1:
        raise HouseholdError("meal slot has no unique exact preparation snapshot")
    return bound[0]


def recipe_snapshot_digest(recipe):
    return digest({key: value for key, value in recipe.items() if key != "preparation_slot_id"})


def preparation_slots(menu):
    return [slot for slot in menu.get("slots", []) if slot.get("kind") != "leftover"]


def bind_preparations(menu):
    """Upgrade legacy unique-key snapshots when building a successor."""
    if not menu.get("slots"):
        return
    for slot in preparation_slots(menu):
        recipe = recipe_for_slot(menu, slot)
        recipe["preparation_slot_id"] = slot["slot_id"]


def schedule(menu):
    return [{"day": slot_day(s), "meal": recipe_for_slot(menu, s)["name"] + (" (rester)" if s.get("kind") == "leftover" else ""), "meal_type": s["meal_type"],
             "portions": deepcopy(s.get("portions", recipe_for_slot(menu, s).get("portions"))),
             "recipe_key": s["recipe_key"], "slot_id": s["slot_id"]} for s in menu["slots"]]


def initial_planning():
    return {"locks": {}, "history": {}, "retired": {}, "applied": {}, "outcomes": {}}


def menu_ref(menu):
    return {key: menu[key] for key in ("menu_id", "revision", "digest")}


def exact_menu(state, supplied):
    current = state.get("menu")
    if not isinstance(current, Mapping) or not isinstance(supplied, Mapping) or canonical(menu_ref(current)) != canonical(supplied):
        raise HouseholdError("replan requires the exact current menu ID, revision and digest")
    return current


def slots(menu):
    values = menu.get("slots")
    if not isinstance(values, list) or not values:
        raise HouseholdError("legacy schedule has no exact slots; create a new structured plan")
    return values


def slot_by_id(menu, slot_id):
    matches = [s for s in slots(menu) if s["slot_id"] == slot_id]
    if len(matches) != 1:
        raise HouseholdError("slot_id does not identify one exact current meal")
    return matches[0]


def lock_key(menu):
    return f'{menu["menu_id"]}:{menu["revision"]}'


def slot_outcome(state, menu, slot):
    leftover = state.get("batch_outcomes", {}).get("leftovers", {}).get(slot["slot_id"])
    if leftover is not None:
        return leftover["outcome"]
    outcome = state.get("menu_planning", {}).get("outcomes", {}).get(slot["slot_id"])
    if outcome is not None:
        return outcome["outcome"]
    owner = menu.get("slot_owners", {}).get(slot["slot_id"], menu["menu_id"])
    record = state.get("recipe_usage", {}).get(owner, {})
    if slot["slot_id"] in record.get("cooked_slot_ids", []):
        return "cooked"
    if slot["slot_id"] in record.get("not_cooked_slot_ids", []):
        return "not_cooked"
    return None


def shopping_menu(menu, historical_ids=None):
    import batch_planning
    if not menu.get("slots"):
        return batch_planning.shopping(menu)
    result = deepcopy(menu)
    historical = set(menu.get("historical_slot_ids", []) if historical_ids is None else historical_ids)
    result["dishes"], result["salads"] = [], []
    batches = {b["source_slot_id"]: b for b in batch_planning.sources(menu)}
    sources = {}
    for slot in preparation_slots(menu):
        recipe = recipe_for_slot(menu, slot)
        if id(recipe) in sources:
            raise HouseholdError("two preparations share one recipe snapshot; exact occurrence accounting is unavailable")
        sources[id(recipe)] = slot
    for collection in ("dishes", "salads"):
        for original in menu[collection]:
            slot = sources.get(id(original))
            if slot is None or slot["slot_id"] in historical:
                continue
            recipe = deepcopy(original)
            if batch := batches.get(slot["slot_id"]):
                batch_planning.scale_preparation(recipe, batch)
            result[collection].append(recipe)
    return result


def shopping_comparison(before, after):
    old, old_unresolved = menu_requirements(shopping_menu(before), maximum=None)
    new, new_unresolved = menu_requirements(shopping_menu(after), maximum=None)
    old = {r["requirement_id"]: {k: v for k, v in r.items() if k != "sources"} for r in old}
    new = {r["requirement_id"]: {k: v for k, v in r.items() if k != "sources"} for r in new}
    same = sorted(k for k in old.keys() & new.keys() if old[k] == new[k])
    return {"kind": "structural_recipe_requirements", "unchanged": [new[k] for k in same],
            "removed": [old[k] for k in sorted(old) if k not in same],
            "added": [new[k] for k in sorted(new) if k not in same],
            "unresolved": {"before": old_unresolved, "after": new_unresolved},
            "cart_action": "separate_explicit_sync_or_order_reconciliation_required"}


def retire_planned_slots(state, menu):
    """Cancel only active planned ownership; historical records stay untouched."""
    for slot in menu.get("slots", []):
        if slot.get("kind") == "leftover":
            continue
        if slot_outcome(state, menu, slot) == "cooked":
            continue
        owner = menu.get("slot_owners", {}).get(slot["slot_id"], menu["menu_id"])
        record = state.get("recipe_usage", {}).get(owner, {})
        if record.get("status") != "planned" or slot["recipe_key"] in record.get("cooldown_overrides", {}):
            continue
        retired = state["menu_planning"]["retired"]
        if owner not in retired and len(retired) >= MAX_PLANNING_MENUS:
            raise HouseholdError("planning retirement limit reached")
        values = retired.setdefault(owner, [])
        if slot["recipe_key"] not in values:
            values.append(slot["recipe_key"])
