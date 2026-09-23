"""Read-only menu coverage and household workflow, derived from existing state."""
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata


def _text(value):
    return " ".join(unicodedata.normalize("NFC", str(value)).casefold().split())


def feedback_targets(menu):
    if not isinstance(menu, Mapping):
        return []
    reference = {k: menu[k] for k in ("menu_id", "revision", "digest")}
    if menu.get("slots"):
        return [{"menu_ref": reference, "slot_id": slot["slot_id"], "recipe_key": slot["recipe_key"], "reference": deepcopy(slot["reference"])} for slot in menu["slots"]]
    return [{"menu_ref": reference, "recipe_key": recipe["recipe_key"],
             "reference": deepcopy(recipe.get("library_recipe_ref") or recipe.get("recipe_ref") or {"recipe_key": recipe["recipe_key"]})}
            for recipe in [*menu.get("dishes", []), *menu.get("salads", [])]]


def assess_menu(state):
    menu = state.get("menu")
    if not isinstance(menu, Mapping):
        return {"ready": False, "status": "missing", "issues": [{"code": "menu_missing"}]}
    profile = state["profile"]
    meals = profile["meals"]
    selection = menu.get("planner_selection") or {}
    requested = menu.get("planning_scope") or selection.get("request") or {}
    expected_days = len(requested["dates"]) if "dates" in requested else meals["dinner_days"]
    expected_portions = requested.get("portions") or meals["portions"]
    dishes = menu.get("dishes", [])
    recipes = [*dishes, *menu.get("salads", [])]
    slots = menu.get("slots") or []
    covered_days = len({slot.get("date") for slot in slots if slot.get("meal_type") == "dinner"}) if slots else None
    issues = []
    if slots and covered_days != expected_days:
        issues.append({"code": "dinner_day_coverage", "expected": expected_days, "actual": covered_days})
    if not slots and len(dishes) != meals["dishes"]:
        issues.append({"code": "dish_coverage", "expected": meals["dishes"], "actual": len(dishes)})
    if not slots:
        issues.append({"code": "meal_dates_unverified", "detail": "Legacy recipe lists do not establish which dates are covered."})
    unknown_quantities = []
    from dietary_assessment import assess
    findings = []
    conflicts = []
    diet = profile["diet"]
    rules = [*diet["allergies_or_sensitivities"], *diet["avoid"]]
    for recipe in recipes:
        key = recipe.get("recipe_key")
        from planner import equipment_conflicts
        if missing := equipment_conflicts(profile, recipe):
            issues.append({"code": "equipment_unavailable", "recipe_key": key, "equipment": missing})
        findings.extend({**f, "recipe_key": key} for f in assess(profile, recipe, recipe=True))
        if not slots and recipe.get("portions") != expected_portions:
            issues.append({"code": "portion_mismatch", "recipe_key": key, "expected": expected_portions, "actual": recipe.get("portions")})
        for index, ingredient in enumerate(recipe.get("ingredients", [])):
            if ingredient.get("quantity") is None or not ingredient.get("unit"):
                unknown_quantities.append({"recipe_key": key, "ingredient_index": index, "item": ingredient.get("item"), "pantry": ingredient.get("pantry", False)})
    conflicts = [f for f in findings if f['blocked']]
    if conflicts:
        issues.append({'code': 'explicit_ingredient_conflict', 'conflicts': conflicts})
    result = {"menu_ref": {k: menu[k] for k in ("menu_id", "revision", "digest")},
              "ready": not issues, "status": "ready" if not issues else "needs_input",
              "dinner_days": {"expected": expected_days, "verified": covered_days},
              "dietary_assessments": findings, "portions": expected_portions, "issues": issues[:25], "issue_count": len(issues),
              "ingredients_needing_quantity_or_pantry_decision": unknown_quantities[:25],
              "ingredients_needing_decision_count": len(unknown_quantities),
              "scope": "Menu coverage and explicit conflicts only; nutrition and allergy safety are not certified."}
    result["assessment_digest"] = hashlib.sha256(json.dumps({"menu": result["menu_ref"], "profile": profile, "assessment": result}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def workflow_status(state):
    assessment = assess_menu(state)
    menu = state.get("menu") or {}
    cart = state.get("cart_plan") or {}
    current_ref = {k: menu.get(k) for k in ("menu_id", "revision", "digest")}
    bound_cart = cart if cart.get("menu_ref") == current_ref else {}
    pending = state.get("pending_checkout") or {}
    cancellation = state.get("pending_cancellation") or {}
    change = state.get("order_change") or {}
    order_id = menu.get("order_id")
    active_checkout_status = pending.get("status", "confirmed" if order_id else "not_started")
    jobs = [j for j in state.get("email_jobs", []) if j.get("order_id") == order_id and j.get("provider") == state["provider"]] if order_id else []
    next_action = {"operation": "menu", "action": "plan", "reason": "Prepare a menu."}
    if state.get("pending_cart_change"):
        next_action = {"operation": "cart", "action": "reconcile_change", "reason": "Read back the pending grocery top-up before any further write; never repeat an uncertain delta."}
    elif pending:
        child = pending.get("recovery")
        attempt = child if isinstance(child, Mapping) else pending
        status = attempt.get("status")
        active_checkout_status = status or active_checkout_status
        method = ((attempt.get("browser_review") or {}).get("payment_choice") or {}).get("method") if attempt is child else (pending.get("checkout_payment") or {}).get("method")
        expired_child = False
        if attempt is child and status == "awaiting_confirmation" and isinstance(attempt.get("expires_at"), str):
            try:
                expiry = datetime.fromisoformat(attempt["expires_at"])
                expired_child = expiry.tzinfo is not None and expiry <= datetime.now(timezone.utc)
            except ValueError:
                pass
        action = "prepare" if expired_child else "confirm" if status == "awaiting_confirmation" else "reconcile"
        if expired_child:
            legacy_child = (attempt.get("owner_reported_no_vipps_request") is True
                            and attempt.get("original_confirmation_id") == pending.get("confirmation_id")
                            and pending.get("unpaid_order_binding_source") == "oda_retry_available_page"
                            and isinstance(attempt.get("order_id"), str) and bool(attempt["order_id"]))
            reason = ("This same-order recovery review expired without payment. If the owner currently confirms no Vipps request or manual payment, "
                      "prepare a fresh review for the exact order with the original confirmation_id, vipps_request_not_received=true "
                      "and the same checkout_payment; the service must reverify the order. Preserve the original journal."
                      if legacy_child else "This same-order recovery review expired without payment. Prepare a fresh review with the same checkout_payment; preserve the original journal.")
        elif action == "confirm":
            reason = "Review and confirm this exact prepared recovery under the existing confirmation policy." if attempt is child else "Continue the exact prepared checkout under the existing confirmation policy."
        elif (attempt.get("payment_abort") or {}).get("status") == "closed":
            reason = ("The exact payment is closed. For an explicit order cancellation, "
                      "use orders cancel_prepare for this same order; a different payment method needs a fresh reviewed switch.")
        elif (attempt.get("payment_abort") or {}).get("status") in {"observing", "closing", "unknown"}:
            action = "abort_payment"
            reason = ("Observe the already started exact payment abort with this confirmation. "
                      "An unknown result remains pending; do not cancel or pay again.")
        elif (method == "vipps" and attempt.get("vipps_request_status") == "sent"
              and isinstance(attempt.get("vipps_request_context"), Mapping)
              and bool(attempt["vipps_request_context"])
              and isinstance(attempt.get("payment_requested_at"), str)
              and bool(attempt["payment_requested_at"])):
            reason = "A Vipps request was sent for this attempt. Approve that exact request on your phone if still pending, then reconcile; do not send another payment."
        elif (state.get("provider") == "meny" and status == "awaiting_user_payment"
              and isinstance(attempt.get("payment_requested_at"), str) and bool(attempt["payment_requested_at"])
              and isinstance(attempt.get("payment_expires_at"), str) and bool(attempt["payment_expires_at"])):
            reason = "A MENY Vipps request was sent for this attempt. Approve that exact request on your phone if still pending, then reconcile; do not send another payment."
            method = "vipps"
        elif (method == "vipps" and attempt is pending and state.get("provider") == "oda"
              and not pending.get("order_change") and not pending.get("automatic_checkout")
              and not pending.get("authentication_unresolved") and not pending.get("authentication_context")
              and pending.get("unpaid_order_binding_source") in {None, "oda_retry_available_page"}
              and all(attempt.get(key) is None for key in (
                  "vipps_request_status", "vipps_request_context", "vipps_request_attempted_at",
                  "payment_requested_at", "owner_vipps_approval_completed_at", "payment_failure", "payment_switch"))):
            reason = ("No sent Vipps request is proven. Reconcile this original attempt. If the owner confirms no Vipps request or manual payment and identifies the exact same order, "
                      "checkout prepare with recovery=true, that order_id, this original confirmation_id and vipps_request_not_received=true can review a same-order payment; "
                      "choose the requested existing saved_card or vipps method with checkout_payment. "
                      "The service must independently verify it before any confirmation. Do not erase the journal or retry from the report alone.")
        else:
            reason = "Reconcile this exact payment attempt before any further payment; its outcome is not established."
            if (state.get("provider") == "oda" and method == "saved_card"
                    and isinstance(attempt.get("authentication_context"), Mapping)):
                reason += (" For an explicit cancellation or method change, abort_payment with this active "
                           "confirmation can close the retained 3D Secure payment; an unknown closure stays pending.")
        next_action = {"operation": "checkout", "action": action, "reason": reason}
        if expired_child:
            next_action["recovery"] = True
            payment_choice = (attempt.get("browser_review") or {}).get("payment_choice")
            if isinstance(payment_choice, Mapping):
                next_action["checkout_payment"] = deepcopy(payment_choice)
            if legacy_child:
                next_action["order_id"] = attempt["order_id"]
                next_action["confirmation_id"] = pending["confirmation_id"]
        if method in {"vipps", "saved_card"}:
            next_action["payment_method"] = method
        if not expired_child and isinstance(attempt.get("confirmation_id"), str) and attempt["confirmation_id"]:
            next_action["confirmation_id"] = attempt["confirmation_id"]
        if (state.get("provider") == "oda" and method == "saved_card"
                and isinstance(attempt.get("authentication_context"), Mapping)
                and action == "reconcile" and not cancellation):
            next_action["on_explicit_cancellation"] = {
                "operation": "checkout", "action": "abort_payment",
                "confirmation_id": attempt["confirmation_id"]}
        if cancellation:
            next_action = {"operation": "orders", "action": "cancel_reconcile" if cancellation.get("status") in {"clicking", "uncertain"} else "cancel_confirm",
                           "confirmation_id": cancellation.get("confirmation_id"), "order_id": cancellation.get("order_id"),
                           "reason": "Finish the exact prepared order cancellation; do not start another payment."}
    elif cancellation:
        next_action = {"operation": "orders", "action": "cancel_reconcile" if cancellation.get("status") in {"clicking", "uncertain"} else "cancel_confirm", "reason": "Finish the existing cancellation."}
    elif change:
        next_action = {"operation": "orders", "action": "get", "reason": "Review the exact order being changed before continuing."}
    elif menu.get("phase") == "ordered" and not jobs and state.get("email_recipient"):
        next_action = {"operation": "email", "action": "schedule", "reason": "Schedule recipes for the exact confirmed order and delivery date."}
    elif menu.get("phase") == "ordered" and any(j.get("status") in {"claimed", "sending", "uncertain"} for j in jobs):
        next_action = {"operation": "email", "action": "status", "reason": "Resolve the existing email dispatch before sending again."}
    elif menu.get("phase") == "ordered":
        next_action = {"operation": "email", "action": "automation_plan", "reason": "Apply any pending recipe-email scheduling changes."} if any(j.get("status") == "pending" and j.get("automation_protocol") != 4 for j in jobs) else {"operation": "feedback", "action": "experience", "reason": "Record cooking experience only when the household reports it."}
    elif menu:
        completion = state.get("product_plan_completion") or {}
        if completion.get("menu_ref") == current_ref and completion.get("nothing_to_buy"):
            next_action = {"operation": "menu", "action": "get", "reason": "All ingredient needs were explicitly covered at home; no grocery purchase is required. Continue cooking and record reported experience."}
        elif not assessment["ready"]:
            next_action = {"operation": "menu", "action": "assess", "reason": "Resolve the listed menu coverage or ingredient issues."}
        elif bound_cart.get("status") == "needs_input":
            next_action = {"operation": "cart", "action": "get", "reason": "Read the current cart and resolve its exact reconciliation question."}
        elif not bound_cart.get("product_plan_digest"):
            next_action = {"operation": "products", "action": "prepare", "reason": "Map ingredient needs to observed packages and resolve pantry assumptions."}
        elif not state.get("delivery_selection"):
            next_action = {"operation": "delivery", "action": "list", "reason": "Choose a delivery window."}
        else:
            next_action = {"operation": "checkout", "action": "prepare", "reason": "Read the final provider total and prepare checkout."}
    return {"menu": assessment, "cart_status": bound_cart.get("status", "not_prepared"),
            "product_plan": deepcopy(bound_cart.get("product_plan_summary")),
            "delivery_status": "observed_requires_revalidation" if state.get("delivery_selection") else "not_observed",
            "checkout_status": active_checkout_status,
            "email_status": [j.get("status") for j in jobs], "next_action": next_action}
