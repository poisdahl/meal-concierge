"""Delivery and protected checkout/order state transitions.

Application owns shared state and locks; these methods run on that same instance.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import math
import re
import secrets
import time
from typing import Any, Mapping
import unicodedata
from zoneinfo import ZoneInfo
from oda_browser import require_order_binding, OdaCheckoutMismatchError, delivery_signature as oda_delivery_signature, _oda_checkout_amounts_minor
from core import CancellationPreconditionError, CheckoutPreconditionError, HouseholdError, cart_summary, checkout_payment_settings, cheapest_delivery_slot, delivery_candidate_digest, delivery_price_display, validate_delivery_slot
from retail_mcp import retail_cart_delivery_matches_slot, oda_cart_delivery_window, retail_delivery_slot_date
from meny import MENY_ORDER_TIMEOUT, MenyOrderChangeDispatchError, meny_checkout_reviews_match
from planning_assessment import assess_menu
from service_common import (
    CANCELLATION_OPERATION_TIMEOUT,
    MENY_CHECKOUT_OPERATION_TIMEOUT,
    MENY_VIPPS_EXPIRY_BUFFER,
    SCHEDULE_OCCURRENCE_LEASE,
    SCHEDULE_WEEKDAYS,
    UNRESOLVED_CHECKOUT_STATUSES,
    bounded_limit,
    canonical,
    checkout_intent_signature,
    delivery_matches,
    email_job_provider,
    expired_awaiting_confirmation,
    meny_order_matches_checkout,
    money_cents,
    oda_order_address_identity,
    oda_order_matches_addition,
    oda_order_quantities,
    order_matches_checkout,
    require_provider_identity,
    safe_order_id,
    scheduled_occurrence,
    scheduler_settings_digest,
    validate_schedule
)


class OrderOperations:
    def _delivery_price_review(self, summary, change):
        """One authorization rule; adapters supply actual full-order totals."""
        requested = change.get("requested_delivery") if change else None
        if not requested:
            return None
        original = change["before"]["order"]
        currency = "SEK" if self.provider == "mathem" else "NOK"
        before = money_cents(original.get("order_total") if self.provider == "meny" else original.get("grossAmount"))
        payable = money_cents(summary.get("total"))
        if self.provider == "meny":
            after = payable
            # MENY reviews show the full new order total; this is not evidence
            # of a new charge. Match the original goods independently of it.
            unchanged = {**summary, "total": original.get("grossAmount"),
                         "delivery": {"display": original.get("deliverySlotDisplay")}}
            if not meny_order_matches_checkout(original, unchanged):
                raise HouseholdError("A delivery-only change must preserve the original MENY goods")
        else:
            amounts = summary.get("order_amounts")
            keys = ("original_minor", "original_count", "combined_minor", "combined_count", "payable_minor")
            if not isinstance(amounts, Mapping) or any(type(amounts.get(k)) is not int or (k != "payable_minor" and amounts[k] < 0) for k in keys):
                raise HouseholdError("Delivery change requires verified original, final and payable order totals")
            payable = amounts["payable_minor"]
            if (original.get("currency") != currency or amounts["original_minor"] != before
                    or isinstance(summary.get("total"), bool) or summary.get("total") != payable / 100
                    or amounts["original_count"] != amounts["combined_count"]):
                raise HouseholdError("Delivery change amounts or original goods do not match")
            after = amounts["combined_minor"]
        if before is None or after is None or payable is None:
            raise HouseholdError("Delivery change requires exact full order totals, including fees and discounts")
        maximum = requested.get("max_total_ore")
        covered = after <= before or (maximum is not None and after <= maximum)
        return {"currency": currency, "original_total_ore": before, "new_total_ore": after,
                "difference_ore": after - before, "payable_ore": payable,
                "max_total_ore": maximum, "confirmation_required": not covered,
                "authorization": "requested_window" if after <= before else "price_limit" if covered else "required"}

    @staticmethod
    def _payment_evidence(tracking_status=None):
        # Merchant fulfillment status is not bank authorization/capture evidence.
        return {'provider_status': tracking_status, 'source': 'order_tracking' if tracking_status else None,
                'authorization': 'unknown', 'charge': 'unknown'}

    @staticmethod
    def _protected_result_view(result, kind):
        value = deepcopy(dict(result))
        if kind == 'checkout' and value.get('confirmed'):
            value.setdefault('payment', OrderOperations._payment_evidence(value.get('tracking_status')))
        elif kind == 'cancellation' and value.get('cancelled'):
            value.setdefault('payment_resolution', {'authorization_release': 'unknown', 'refund': 'unknown'})
        return value

    def _guard_scheduled_context(self, state, context):
        if context is None:
            return
        row = state["occurrences"].get(context["occurrence"])
        if not isinstance(row, Mapping) or row.get("attempt_id") != context["attempt_id"]:
            raise HouseholdError("scheduled worker attempt is stale")
        if context.get("manual") is True:
            return
        self._require_weekly_scheduler(state, context["scheduler"])
        if (not state["schedule"].get("enabled")
                or scheduler_settings_digest(state["schedule"]) != context["settings_digest"]):
            raise HouseholdError("scheduled settings changed while the worker was running")

    def _pending_scheduler_guard(self, state, pending):
        if (pending.get("scheduler_context") or {}).get("manual") is True:
            self._guard_scheduled_context(state, pending["scheduler_context"])
            return
        if pending.get("automatic_checkout", bool(pending.get("occurrence"))):
            context = pending.get("scheduler_context")
            if context is None and self._scheduler_owner(state):
                raise HouseholdError("original automatic checkout has no managed scheduler identity")
            self._guard_scheduled_context(state, context)

    def _weekly_attempt_status(self, state, context, status):
        try:
            self._guard_scheduled_context(state, context)
        except HouseholdError:
            return False
        row = state["occurrences"][context["occurrence"]]
        if row.get("status") != "completed":
            row["status"] = status
        return True

    def _scheduled_select(self, request, arguments, deadline):
        context = request.get("_scheduler_context")
        with self.store.locked() as state:
            if self._unresolved_scheduled_effects(state):
                raise HouseholdError("reconcile the original scheduled delivery selection before another selection")
            if context is not None:
                self._guard_scheduled_context(state, context)
                state["occurrences"][context["occurrence"]]["delivery_effect"] = {
                    "attempt_id": context["attempt_id"], "provider": self.provider,
                    "slot_ref": request["slot_ref"], "state": "dispatching",
                }
        try:
            return self.provider_client.call("select_delivery_slot", arguments, deadline=deadline)
        except Exception:
            if context is not None:
                self._record_scheduled_effect(context, "uncertain")
            raise

    def _record_scheduled_effect(self, context, outcome, selected=None):
        if context is None:
            return
        with self.store.locked() as state:
            row = state["occurrences"].get(context["occurrence"])
            effect = row.get("delivery_effect") if isinstance(row, Mapping) else None
            if not isinstance(effect, dict) or effect.get("attempt_id") != context["attempt_id"]:
                raise HouseholdError("original scheduled delivery effect is unavailable")
            if selected is not None and selected["slot_ref"] != effect["slot_ref"]:
                raise HouseholdError("selected delivery does not match the original scheduled effect")
            if effect.get("state") != "resolved":
                effect["state"] = outcome
                if selected is not None:
                    effect["selected"] = deepcopy(selected)

    def _reconcile_scheduled_delivery(self, request):
        occurrence = request.get("occurrence")
        if not isinstance(occurrence, str) or not occurrence:
            raise HouseholdError("reconciliation requires the original scheduled occurrence")
        deadline = request.get("_deadline") or time.monotonic() + 240
        with self._browser_operation(deadline):
            with self.store.locked() as state:
                row = state["occurrences"].get(occurrence)
                effect = deepcopy(row.get("delivery_effect")) if isinstance(row, Mapping) else None
                if ((state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES
                        and isinstance(effect, Mapping) and effect.get("state") != "resolved"):
                    raise HouseholdError("reconcile the protected checkout before reading delivery selection")
            if not isinstance(effect, Mapping) or effect.get("provider") != self.provider:
                raise HouseholdError("no original delivery effect for this provider occurrence")
            if effect.get("state") == "resolved":
                return {"resolved": True, "occurrence": occurrence, "retry_allowed": False}
            dates = None
            reference = effect["slot_ref"]
            if self.provider in {"oda", "mathem"} and reference.startswith(f"{self.provider}:"):
                dates = [retail_delivery_slot_date(reference, provider=self.provider)]
            selected = [slot for slot in self._normalized_provider_slots(dates, deadline=deadline) if slot["selected"]]
            if len(selected) != 1 or selected[0]["slot_ref"] != reference:
                return {"resolved": False, "occurrence": occurrence, "retry_allowed": False}
            self._record_scheduled_effect({"occurrence": occurrence, "attempt_id": effect["attempt_id"]}, "resolved", selected[0])
            return {"resolved": True, "occurrence": occurrence, "retry_allowed": False, "selected": selected[0]}

    @staticmethod
    def _find_delivery_slot(value: Any, slot_id: Any) -> dict[str, Any] | None:
        if isinstance(value, Mapping):
            candidate_id = value.get("id", value.get("slot_id", value.get("deliverySlotId")))
            if candidate_id is not None and str(candidate_id) == str(slot_id):
                display = value.get("name", value.get("display", value.get("description")))
                if isinstance(display, str) and display.strip():
                    return {"slot_id": candidate_id, "display": display.strip()}
            for child in value.values():
                if found := OrderOperations._find_delivery_slot(child, slot_id):
                    return found
        elif isinstance(value, list):
            for child in value:
                if found := OrderOperations._find_delivery_slot(child, slot_id):
                    return found
        return None

    @staticmethod
    def _delivery_scope(state: Mapping[str, Any], cart: Mapping[str, Any] | None = None) -> dict[str, str | None]:
        raw_cart_id = None
        if isinstance(cart, Mapping):
            raw_cart_id = cart.get("id", cart.get("cartId", cart.get("cart_id")))
        order_change = state.get("order_change")
        pending = state.get("pending_checkout")
        return {
            "cart_id": str(raw_cart_id)[:128] if raw_cart_id not in {None, ""} else None,
            "order_id": str(order_change.get("order_id"))[:128] if isinstance(order_change, Mapping) and order_change.get("order_id") else None,
            "occurrence": str(pending.get("occurrence"))[:128] if isinstance(pending, Mapping) and pending.get("occurrence") else None,
        }

    def _record_delivery_selection(
        self,
        slot: Mapping[str, Any],
        *,
        origin: str,
        candidate_digest: str | None,
        baseline: Mapping[str, Any],
        cart: Mapping[str, Any] | None = None,
        occurrence: str | None = None,
        scheduler_context: Mapping[str, Any] | None = None,
    ) -> None:
        normalized = validate_delivery_slot(slot)
        if origin not in {"explicit", "cheapest"}:
            raise HouseholdError("delivery selection origin is invalid")
        if candidate_digest is not None and (
            not isinstance(candidate_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", candidate_digest) is None
        ):
            raise HouseholdError("delivery candidate digest is invalid")
        observation = {
            "provider": self.provider,
            "scope": {
                **self._delivery_scope(baseline, cart),
                **({"occurrence": occurrence} if occurrence else {}),
            },
            "origin": origin,
            "slot": normalized,
            "candidate_digest": candidate_digest,
            "observed_at": self._now().isoformat(),
        }
        with self.store.locked() as state:
            self._guard_scheduled_context(state, scheduler_context)
            if canonical(state.get("order_change")) != canonical(baseline.get("order_change")):
                raise HouseholdError("order change state changed while recording delivery")
            state["delivery_selection"] = observation

    def _delivery_observation_applies(
        self,
        observation: Any,
        state: Mapping[str, Any],
        *,
        cart: Mapping[str, Any] | None,
        occurrence: str | None,
    ) -> bool:
        if not isinstance(observation, Mapping) or observation.get("provider") != self.provider:
            return False
        scope = observation.get("scope")
        if not isinstance(scope, Mapping):
            return False
        current = self._delivery_scope(state, cart)
        if scope.get("cart_id") != current["cart_id"] or scope.get("order_id") != current["order_id"]:
            return False
        return scope.get("occurrence") in {None, occurrence}

    @staticmethod
    def _same_delivery_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        keys = ("slot_ref", "provider_slot_id", "start_at", "end_at")
        return all(left.get(key) == right.get(key) for key in keys)

    @staticmethod
    def _delivery_slot_date(slot: Mapping[str, Any]) -> str:
        normalized = validate_delivery_slot(slot)
        return datetime.fromisoformat(
            normalized["start_at"].replace("Z", "+00:00")
        ).astimezone(ZoneInfo("Europe/Oslo")).date().isoformat()

    def _normalized_provider_slots(
        self,
        dates: list[str] | None = None,
        *,
        deadline: float | None = None,
        address_id: Any = None,
        allow_recovery: bool = False,
        display_metadata: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        calls = dates or [None]
        slots: list[dict[str, Any]] = []
        for delivery_date in calls:
            arguments = {"delivery_date": delivery_date} if delivery_date else {}
            if address_id is not None:
                arguments["delivery_address_id"] = address_id
            if self.provider == "meny":
                result = self.provider_client.call(
                    "get_delivery_slots", arguments, deadline=deadline, allow_recovery=allow_recovery,
                )
            else:
                result = self.provider_client.call("get_delivery_slots", arguments, deadline=deadline)
            raw_slots = result.get("slots") if isinstance(result, Mapping) else None
            if not isinstance(raw_slots, list):
                raise HouseholdError(f"{self.provider.upper()} delivery slots are not normalized")
            normalized_call = [validate_delivery_slot(slot) for slot in raw_slots]
            if display_metadata is not None:
                raw_display = result.get("display") if isinstance(result, Mapping) else None
                if self.provider == "meny" and (
                    not isinstance(raw_display, Mapping)
                    or set(raw_display) != {slot["slot_ref"] for slot in normalized_call}
                ):
                    raise HouseholdError("MENY delivery display metadata is unavailable")
                if isinstance(raw_display, Mapping):
                    for slot in normalized_call:
                        reference = slot["slot_ref"]
                        label = raw_display.get(reference)
                        if (
                            not isinstance(label, str)
                            or not label.strip()
                            or len(label.encode("utf-8")) > 500
                        ):
                            raise HouseholdError("provider delivery display metadata is invalid")
                        normalized_label = " ".join(label.split())
                        previous = display_metadata.get(reference)
                        if previous is not None and previous != normalized_label:
                            raise HouseholdError("provider returned conflicting delivery display metadata")
                        display_metadata[reference] = normalized_label
            slots.extend(normalized_call)
        unique: dict[str, dict[str, Any]] = {}
        for slot in slots:
            reference = slot["slot_ref"]
            if reference in unique and canonical(unique[reference]) != canonical(slot):
                raise HouseholdError("provider returned conflicting delivery slot references")
            unique[reference] = slot
        return list(unique.values())

    @staticmethod
    def _scheduled_delivery_dates(schedule: Mapping[str, Any], instant: datetime) -> list[str]:
        preference = schedule["delivery"]
        local_day = instant.astimezone(ZoneInfo(str(schedule["timezone"]))).date()
        weekday = str(preference.get("weekday") or "").casefold()
        if weekday:
            offset = (SCHEDULE_WEEKDAYS[weekday] - local_day.weekday()) % 7
            return [(local_day + timedelta(days=offset)).isoformat()]
        return [(local_day + timedelta(days=offset)).isoformat() for offset in range(7)]

    def _scheduled_delivery_choice(
        self,
        schedule: Mapping[str, Any],
        *,
        occurrence: str,
        deadline: float | None,
        scheduler_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        preference = schedule["delivery"]
        state = self.store.read()
        scope_cart = (
            self.provider_client.call("get_cart", {}, deadline=deadline)
            if self.provider == "meny"
            else self.provider_client.call("get_cart", {}, deadline=deadline)
        )
        observation = state.get("delivery_selection")
        applicable = self._delivery_observation_applies(
            observation, state, cart=scope_cart, occurrence=occurrence,
        )
        cart_delivery = cart_summary(scope_cart).get("delivery") if self.provider in {"oda", "mathem"} else None
        current_dates = None
        if applicable and isinstance(observation.get("slot"), Mapping):
            current_dates = [self._delivery_slot_date(observation["slot"])]
        elif self.provider == "oda" and isinstance(cart_delivery, Mapping):
            oda_today = self._now().astimezone(ZoneInfo("Europe/Oslo")).date()
            current_dates = [oda_cart_delivery_window(cart_delivery, today=oda_today)["date"]]
        current_slots = self._normalized_provider_slots(current_dates, deadline=deadline)
        selected = [slot for slot in current_slots if slot["selected"]]
        if len(selected) > 1:
            raise HouseholdError("provider-selected delivery is ambiguous")
        selected_slot = selected[0] if selected else None
        if self.provider in {"oda", "mathem"}:
            if isinstance(cart_delivery, Mapping):
                if (
                    selected_slot is None
                    or not retail_cart_delivery_matches_slot(cart_delivery, selected_slot, provider=self.provider)
                ):
                    raise HouseholdError("Cart and selected delivery listing disagree")
            elif selected_slot is not None:
                raise HouseholdError("Cart and selected delivery listing disagree")
        if selected_slot is not None and not applicable:
            return {
                "ready": True,
                "origin": "external",
                "selected": selected_slot,
                "price_display": delivery_price_display(selected_slot),
            }
        if applicable:
            observed_slot = observation.get("slot")
            if (
                selected_slot is None
                or not isinstance(observed_slot, Mapping)
                or not self._same_delivery_identity(
                    selected_slot, validate_delivery_slot(observed_slot),
                )
            ):
                raise HouseholdError("provider delivery and local selection provenance disagree")
            if observation.get("origin") == "explicit" or preference["strategy"] == "keep_selected":
                self._record_delivery_selection(
                    selected_slot,
                    origin=str(observation["origin"]),
                    candidate_digest=observation.get("candidate_digest"),
                    baseline=state,
                    cart=scope_cart,
                    occurrence=occurrence,
                    scheduler_context=scheduler_context,
                )
                return {
                    "ready": True,
                    "origin": observation["origin"],
                    "selected": selected_slot,
                    "price_display": delivery_price_display(selected_slot),
                }
        elif preference["strategy"] == "keep_selected":
            return {"ready": False, "reason": "select a delivery slot", "candidates": []}

        dates = self._scheduled_delivery_dates(schedule, self._now())
        candidates = [
            slot for slot in self._normalized_provider_slots(dates, deadline=deadline)
            if delivery_matches(
                preference, slot, timezone_name=str(schedule["timezone"]),
            )
        ]
        rendered = [
            {"slot": slot, "price_display": delivery_price_display(slot)}
            for slot in candidates
        ]
        if not candidates:
            return {"ready": False, "reason": "no delivery slot satisfies the hard constraints", "candidates": []}
        if any(slot["price_kind"] != "exact" for slot in candidates):
            return {
                "ready": False,
                "reason": "eligible delivery prices are not all exact",
                "candidates": rendered,
            }
        digest = delivery_candidate_digest(candidates)
        winner = cheapest_delivery_slot(
            candidates,
            preferred_end=preference.get("preferred_end"),
            timezone_name=str(schedule["timezone"]),
        )
        if selected_slot is not None and self._same_delivery_identity(selected_slot, winner) and selected_slot["price_ore"] == winner["price_ore"]:
            self._record_delivery_selection(
                selected_slot,
                origin="cheapest",
                candidate_digest=digest,
                baseline=state,
                cart=scope_cart,
                occurrence=occurrence,
                scheduler_context=scheduler_context,
            )
            return {
                "ready": True,
                "origin": "cheapest",
                "selected": selected_slot,
                "candidate_digest": digest,
                "price_display": delivery_price_display(selected_slot),
            }
        selection_error = None
        try:
            result = self._delivery({
                "action": "select",
                "slot_ref": winner["slot_ref"],
                "_deadline": deadline,
                "_origin": "cheapest",
                "_candidate_digest": digest,
                "_occurrence": occurrence,
                "_defer_record": True,
                "_scheduler_context": scheduler_context,
            })
        except HouseholdError as exc:
            selection_error = exc
            result = None
            if scheduler_context is not None and scheduler_context.get("scheduler") is not None:
                # An attempted mutation with a lost outcome is reconciled
                # separately; a fresh read here must not silently retry it.
                raise
        verified = result.get("selected") if isinstance(result, Mapping) else None
        fresh_slots = self._normalized_provider_slots(dates, deadline=deadline)
        fresh_selected = [slot for slot in fresh_slots if slot["selected"]]
        fresh_candidates = [
            slot for slot in fresh_slots
            if delivery_matches(
                preference, slot, timezone_name=str(schedule["timezone"]),
            )
        ]
        if (
            len(fresh_selected) != 1
            or (selection_error is None and not isinstance(verified, Mapping))
            or (
                isinstance(verified, Mapping)
                and not self._same_delivery_identity(
                    fresh_selected[0], validate_delivery_slot(verified),
                )
            )
            or not fresh_candidates
            or any(slot["price_kind"] != "exact" for slot in fresh_candidates)
        ):
            raise HouseholdError(
                "automatic delivery selection failed and fresh provider state is not the exact winner"
                if selection_error is not None
                else "automatic delivery selection changed; inspect the provider selection"
            ) from selection_error
        fresh_digest = delivery_candidate_digest(fresh_candidates)
        fresh_winner = cheapest_delivery_slot(
            fresh_candidates,
            preferred_end=preference.get("preferred_end"),
            timezone_name=str(schedule["timezone"]),
        )
        if fresh_digest != digest or canonical(fresh_selected[0]) != canonical(fresh_winner):
            raise HouseholdError(
                "automatic delivery selection failed and fresh candidates do not prove the winner"
                if selection_error is not None
                else "automatic delivery candidates changed; inspect the provider selection"
            ) from selection_error
        verified = fresh_selected[0]
        fresh_scope_cart = (
            self.provider_client.call("get_cart", {}, deadline=deadline)
            if self.provider == "meny"
            else self.provider_client.call("get_cart", {}, deadline=deadline)
        )
        if self.provider in {"oda", "mathem"}:
            cart_delivery = cart_summary(fresh_scope_cart).get("delivery")
            if (
                not isinstance(cart_delivery, Mapping)
                or not retail_cart_delivery_matches_slot(cart_delivery, verified, provider=self.provider)
            ):
                raise HouseholdError(
                    "automatic delivery selection is uncertain; provider cart and slots disagree"
                ) from selection_error
        self._record_delivery_selection(
            verified,
            origin="cheapest",
            candidate_digest=fresh_digest,
            baseline=self.store.read(),
            cart=fresh_scope_cart,
            occurrence=occurrence,
            scheduler_context=scheduler_context,
        )
        if scheduler_context is not None:
            self._record_scheduled_effect(scheduler_context, "resolved", verified)
        return {
            "ready": True,
            "origin": "cheapest",
            "selected": validate_delivery_slot(verified),
            "candidate_digest": digest,
            "price_display": delivery_price_display(verified),
        }

    def _current_delivery_choice(
        self,
        *,
        occurrence: str | None,
        deadline: float | None,
        allow_recovery: bool = False,
        expected_slot: Mapping[str, Any] | None = None,
        scope_cart: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.store.read()
        if scope_cart is None:
            scope_cart = (
                self.provider_client.call(
                    "get_cart", {}, deadline=deadline, allow_recovery=allow_recovery,
                )
                if self.provider == "meny"
                else self.provider_client.call("get_cart", {}, deadline=deadline)
            )
        observation = state.get("delivery_selection")
        applicable = self._delivery_observation_applies(
            observation, state, cart=scope_cart, occurrence=occurrence,
        )
        target = expected_slot
        if target is None and applicable and isinstance(observation.get("slot"), Mapping):
            target = observation["slot"]
        cart_delivery = cart_summary(scope_cart).get("delivery") if self.provider in {"oda", "mathem"} else None
        dates = [self._delivery_slot_date(target)] if isinstance(target, Mapping) else None
        if dates is None and self.provider == "oda" and isinstance(cart_delivery, Mapping):
            oda_today = self._now().astimezone(ZoneInfo("Europe/Oslo")).date()
            dates = [oda_cart_delivery_window(cart_delivery, today=oda_today)["date"]]
        selected = [
            slot for slot in self._normalized_provider_slots(
                dates, deadline=deadline, allow_recovery=allow_recovery,
            )
            if slot["selected"]
        ]
        if self.provider in {"oda", "mathem"}:
            if isinstance(cart_delivery, Mapping):
                if (
                    len(selected) != 1
                    or not retail_cart_delivery_matches_slot(cart_delivery, selected[0], provider=self.provider)
                ):
                    raise HouseholdError("Cart and selected delivery listing disagree")
            elif selected:
                raise HouseholdError("Cart and selected delivery listing disagree")
        if len(selected) != 1:
            raise HouseholdError("select one unambiguous provider delivery slot before checkout")
        slot = selected[0]
        if applicable:
            observed_slot = observation.get("slot")
            if (
                not isinstance(observed_slot, Mapping)
                or not self._same_delivery_identity(
                    slot, validate_delivery_slot(observed_slot),
                )
            ):
                raise HouseholdError("provider delivery and local selection provenance disagree")
            origin = str(observation["origin"])
            digest = observation.get("candidate_digest")
            self._record_delivery_selection(
                slot,
                origin=origin,
                candidate_digest=digest,
                baseline=state,
                cart=scope_cart,
                occurrence=occurrence,
            )
        else:
            origin = "external"
            digest = None
        return {
            "ready": True,
            "origin": origin,
            "selected": slot,
            "candidate_digest": digest,
            "price_display": delivery_price_display(slot),
        }

    @staticmethod
    def _bind_delivery_summary(summary: Mapping[str, Any], binding: Mapping[str, Any], *, provider: str = "oda", discount_breakdown: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = deepcopy(dict(summary))
        slot = validate_delivery_slot(binding["selected"])
        existing = result.get("delivery")
        delivery = deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
        delivery.update({
            "slot": slot,
            "price_display": str(binding["price_display"]),
            "candidate_digest": binding.get("candidate_digest"),
            "selection_origin": binding["origin"],
        })
        result["delivery"] = delivery
        if result.get("delivery_change"):
            # The slot quote is displayed above; it is not a missing fee row in
            # the merchant's final original/new order overview.
            return result
        if provider == "mathem" and discount_breakdown is not None:
            result["discount_breakdown"] = deepcopy(dict(discount_breakdown))
        amounts = result.get("amounts")
        if slot["price_kind"] == "exact" and isinstance(amounts, Mapping):
            amounts = deepcopy(dict(amounts))
            exact_price = slot["price_ore"] / 100
            supplied = amounts.get("delivery_price")
            # Mathem exposes gross delivery and an observed full delivery credit
            # as separate checkout rows; the selected slot reports the net price.
            credit = discount_breakdown.get("delivery_discount") if provider == "mathem" and discount_breakdown is not None else None
            free_mathem_delivery = supplied is not None and supplied > 0 and credit == -supplied and exact_price == 0
            if supplied is not None and supplied != exact_price and not free_mathem_delivery:
                raise HouseholdError("provider checkout delivery price disagrees with the selected slot")
            if not free_mathem_delivery:
                amounts["delivery_price"] = exact_price
            result["amounts"] = amounts
        return result

    def _unchanged_delivery_binding(
        self,
        binding: Mapping[str, Any],
        *,
        occurrence: str | None,
        deadline: float | None,
        allow_recovery: bool = False,
        scope_cart: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        expected = binding.get("selected", binding.get("slot"))
        if not isinstance(expected, Mapping):
            raise HouseholdError("checkout delivery binding is invalid")
        expected_slot = validate_delivery_slot(expected)
        expected_origin = str(binding.get("origin", binding.get("selection_origin")) or "")
        current = self._current_delivery_choice(
            occurrence=occurrence,
            deadline=deadline,
            allow_recovery=allow_recovery,
            expected_slot=expected_slot,
            scope_cart=scope_cart,
        )
        current_slot = validate_delivery_slot(current["selected"])
        if canonical(current_slot) != canonical(expected_slot):
            raise HouseholdError("delivery changed while preparing the protected checkout summary")
        if expected_origin in {"explicit", "cheapest"} and current.get("origin") != expected_origin:
            raise HouseholdError("delivery selection provenance changed")
        if expected_origin != "cheapest":
            return current
        if not occurrence:
            raise HouseholdError("automatic cheapest delivery is not bound to a scheduled occurrence")
        state = self.store.read()
        schedule = state.get("schedule")
        if not isinstance(schedule, Mapping):
            raise HouseholdError("scheduled delivery configuration is unavailable")
        preference = schedule["delivery"]
        candidates = [
            slot
            for slot in self._normalized_provider_slots(
                self._scheduled_delivery_dates(schedule, self._now()),
                deadline=deadline,
                allow_recovery=allow_recovery,
            )
            if delivery_matches(
                preference, slot, timezone_name=str(schedule["timezone"]),
            )
        ]
        if not candidates or any(slot["price_kind"] != "exact" for slot in candidates):
            raise HouseholdError("delivery candidates changed and are no longer all exact")
        digest = delivery_candidate_digest(candidates)
        winner = cheapest_delivery_slot(
            candidates,
            preferred_end=preference.get("preferred_end"),
            timezone_name=str(schedule["timezone"]),
        )
        if digest != binding.get("candidate_digest") or canonical(winner) != canonical(expected_slot):
            raise HouseholdError("cheapest delivery candidates changed")
        return {
            "ready": True,
            "origin": "cheapest",
            "selected": current_slot,
            "candidate_digest": digest,
            "price_display": delivery_price_display(current_slot),
        }

    def _scheduled_checkout_problem(
        self,
        summary: Mapping[str, Any],
        occurrence: str | None,
        *, automatic: bool = True,
    ) -> str | None:
        if not occurrence or not automatic:
            return None
        state = self.store.read()
        schedule = state.get("schedule")
        if not isinstance(schedule, Mapping):
            return "scheduled checkout configuration changed"
        maximum_total = validate_schedule(schedule, self.provider)
        if state.get("menu"):
            assessment = assess_menu(state)
            if not assessment["ready"]:
                return "menu coverage or explicit ingredient constraints need review"
            plan = state.get("cart_plan") or {}
            if plan.get("menu_ref") != assessment["menu_ref"] or not plan.get("product_plan_digest"):
                return "menu ingredients need an applied, exact product plan before automatic checkout"
            if self._menu_shortfall(plan, summary):
                return "required menu products are missing; review or restore the incomplete cart before automatic checkout"
        if not schedule.get("enabled") or not schedule.get("auto_checkout"):
            return "scheduled checkout is no longer enabled"
        total = summary.get("total")
        if (
            isinstance(total, bool)
            or not isinstance(total, (int, float))
            or not math.isfinite(float(total))
            or maximum_total is None
            or total > maximum_total
        ):
            return "total exceeds maximum"
        if not delivery_matches(
            schedule["delivery"],
            summary.get("delivery"),
            timezone_name=str(schedule["timezone"]),
        ):
            return "delivery does not match preference"
        return None

    def _checkout_menu_attribution(self, menu, plan):
        # A supplemental-only cart proves nothing about the saved menu. Use the
        # frozen plan, including during recovery of older pending checkouts.
        return "menu_bound" if (
            isinstance(menu, Mapping) and isinstance(plan, Mapping)
            and plan.get("provider") == self.provider
            and plan.get("menu_ref") == self._cart_menu_ref(menu)
            and bool(plan.get("required_quantities"))
        ) else "cart_only"

    def _menu_shortfall(self, plan, summary):
        if not isinstance(plan, Mapping):
            return []
        return [{"product_id": row["product_id"], "name": row["name"],
                 "required_quantity": row["required_quantity"], "live_quantity": row["live_quantity"],
                 "missing_quantity": row["required_quantity"] - row["live_quantity"]}
                for row in self._cart_plan_view(plan, summary)["items"]
                if row["required_quantity"] > row["live_quantity"]]

    def _revalidate_checkout_delivery(
        self,
        pending: Mapping[str, Any],
        *,
        deadline: float | None,
    ) -> dict[str, Any] | None:
        with self.store.locked() as state:
            self._pending_scheduler_guard(state, pending)
        change = pending.get("order_change")
        if self.provider in {"oda", "mathem"} and change and not change.get("requested_delivery"):
            # Additions inherit the existing order's delivery, not a new cart
            # reservation. Revalidate that original order without selecting one.
            current = self._orders({"action": "get", "order_id": change["order_id"], "_deadline": deadline})
            if canonical(current) != canonical(change["before"]):
                raise HouseholdError("the target retailer order changed; prepare a new review")
            return None
        delivery = (pending.get("summary") or {}).get("delivery")
        expected = delivery.get("slot") if isinstance(delivery, Mapping) else None
        if not isinstance(expected, Mapping):
            raise HouseholdError("protected checkout summary has no normalized delivery binding")
        expected_slot = validate_delivery_slot(expected)
        origin = delivery.get("selection_origin")
        occurrence = str(pending.get("occurrence") or "")
        reselections = pending.get("delivery_reselections", 0)
        if isinstance(reselections, bool) or not isinstance(reselections, int) or reselections not in {0, 1}:
            raise HouseholdError("checkout delivery reselection state is invalid")
        if origin == "cheapest":
            state = self.store.read()
            current = [
                slot for slot in self._normalized_provider_slots(
                    [self._delivery_slot_date(expected_slot)], deadline=deadline,
                )
                if slot["selected"]
            ]
            if len(current) != 1 or not self._same_delivery_identity(current[0], expected_slot):
                raise HouseholdError("provider delivery and local selection provenance disagree")
            schedule = state["schedule"]
            preference = schedule["delivery"]
            candidates = [
                slot for slot in self._normalized_provider_slots(
                    self._scheduled_delivery_dates(schedule, self._now()), deadline=deadline,
                )
                if delivery_matches(
                    preference, slot, timezone_name=str(schedule["timezone"]),
                )
            ]
            if not candidates or any(slot["price_kind"] != "exact" for slot in candidates):
                raise HouseholdError("delivery candidates changed and no exact cheapest window is available")
            digest = delivery_candidate_digest(candidates)
            winner = cheapest_delivery_slot(
                candidates,
                preferred_end=preference.get("preferred_end"),
                timezone_name=str(schedule["timezone"]),
            )
            changed = (
                canonical(winner) != canonical(expected_slot)
                or digest != delivery.get("candidate_digest")
            )
            if changed and reselections >= 1:
                raise HouseholdError("delivery changed a second time; checkout stopped before payment")
            choice = (
                self._scheduled_delivery_choice(
                    schedule, occurrence=occurrence, deadline=deadline, scheduler_context=pending.get("scheduler_context"),
                )
                if changed
                else {
                    "ready": True,
                    "origin": "cheapest",
                    "selected": current[0],
                    "candidate_digest": digest,
                    "price_display": delivery_price_display(current[0]),
                }
            )
        else:
            choice = self._current_delivery_choice(
                occurrence=occurrence or None, deadline=deadline, expected_slot=expected_slot,
            )
            current = validate_delivery_slot(choice["selected"])
            if not self._same_delivery_identity(current, expected_slot):
                raise HouseholdError("explicit or external delivery selection changed; inspect it before checkout")
            changed = canonical(current) != canonical(expected_slot)
        if not changed:
            return None
        with self.store.locked() as state:
            if canonical(state.get("pending_checkout")) != canonical(pending):
                raise HouseholdError("checkout state changed while revalidating delivery")
            state["pending_checkout"] = None
        prepared = self._checkout_prepare(
            deadline,
            occurrence=occurrence or None,
            delivery_binding=choice,
            delivery_reselections=reselections + 1,
            automatic_checkout=pending.get("automatic_checkout", bool(occurrence)),
            scheduler_context=pending.get("scheduler_context"),
        )
        problem = self._scheduled_checkout_problem(prepared["summary"], occurrence or None, automatic=pending.get("automatic_checkout", bool(occurrence)))
        if problem is not None:
            with self.store.locked() as state:
                replacement = state.get("pending_checkout")
                if (
                    isinstance(replacement, Mapping)
                    and replacement.get("confirmation_id") == prepared.get("confirmation_id")
                    and replacement.get("status") == "awaiting_confirmation"
                ):
                    state["pending_checkout"] = None
            raise HouseholdError(f"scheduled checkout stopped: {problem}")
        return {
            "confirmed": False,
            "reprepared": True,
            "reason": "delivery price or cheapest candidate set changed",
            **prepared,
        }

    def _delivery(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "list")
        if action == "list":
            dates = request.get("dates")
            if dates is not None and (
                not isinstance(dates, list)
                or not dates
                or len(dates) > 7
                or not all(isinstance(item, str) for item in dates)
            ):
                raise HouseholdError("delivery dates must be one to seven ISO dates")
            if dates is not None:
                for item in dates:
                    try:
                        if date.fromisoformat(item).isoformat() != item:
                            raise ValueError
                    except ValueError as exc:
                        raise HouseholdError("delivery dates must be one to seven ISO dates") from exc
            display: dict[str, str] = {}
            slots = self._normalized_provider_slots(
                dates,
                deadline=request.get("_deadline"),
                address_id=request.get("address_id"),
                allow_recovery=request.get("_allow_browser_recovery") is True,
                display_metadata=display,
            )
            return {
                "provider": self.provider,
                "slots": slots,
                "price_display": {
                    slot["slot_ref"]: delivery_price_display(slot)
                    for slot in slots
                },
                "display": display,
            }
        if action == "select":
            slot_ref = request.get("slot_ref")
            if slot_ref is None:
                raise HouseholdError("delivery select requires the exact slot_ref returned by delivery list")
            arguments = {"delivery_slot_id": slot_ref}
            if request.get("address_id") is not None:
                arguments["delivery_address_id"] = request["address_id"]
            if request.get("unattended") is not None:
                arguments["is_unattended_delivery"] = bool(request["unattended"])
            deadline = request.get("_deadline")
            if deadline is None and self.provider == "meny":
                deadline = time.monotonic() + MENY_ORDER_TIMEOUT
            with self._browser_operation(deadline):
                state = self.store.read()
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending checkout before changing delivery")
                change = deepcopy(state.get("order_change"))
                if change and change.get("status") != "editing":
                    raise HouseholdError("the order change is still starting")
                maximum = request.get("max_total_ore")
                if maximum is not None and (not change or type(maximum) is not int or not 0 <= maximum <= 100_000_000):
                    raise HouseholdError("max_total_ore requires an existing-order change and an explicit nonnegative integer price limit in the provider currency")
                if maximum is not None and self.provider == "meny" and not change.get("delivery_only"):
                    raise HouseholdError("Begin the exact MENY change with delivery_only=true before authorizing a delivery price limit")
                if self.provider in {"oda", "mathem"}:
                    if change:
                        cart = cart_summary(self.provider_client.call("get_cart", {}, deadline=deadline))
                        if cart["items"]:
                            raise HouseholdError("an Oda delivery-window change must be prepared without staged item additions")
                    requested_dates = None
                    if isinstance(slot_ref, str) and slot_ref.startswith(f"{self.provider}:"):
                        requested_dates = [retail_delivery_slot_date(slot_ref, provider=self.provider)]
                    available = self._normalized_provider_slots(requested_dates, deadline=deadline)
                    candidates = [slot for slot in available if slot["slot_ref"] == slot_ref]
                    if len(candidates) != 1:
                        raise HouseholdError("the requested provider delivery slot is no longer available")
                    candidate = candidates[0]
                    provider_slot_id = candidate["provider_slot_id"]
                    if provider_slot_id is None:
                        raise HouseholdError("the requested provider delivery slot has no provider id")
                    arguments["delivery_slot_id"] = provider_slot_id
                    self._scheduled_select(request, arguments, deadline)
                    selected_date = self._delivery_slot_date(candidate)
                    fresh = [
                        slot for slot in self._normalized_provider_slots([selected_date], deadline=deadline)
                        if slot["selected"]
                    ]
                    if (
                        len(fresh) != 1
                        or fresh[0]["slot_ref"] != slot_ref
                        or not self._same_delivery_identity(fresh[0], candidate)
                    ):
                        raise HouseholdError(f"{self.provider.upper()} delivery selection is uncertain; inspect the provider selection")
                    normalized = fresh[0]
                    self._record_scheduled_effect(request.get("_scheduler_context"), "resolved", normalized)
                    raw_cart = self.provider_client.call("get_cart", {}, deadline=deadline)
                    cart = cart_summary(raw_cart)
                    delivery = cart.get("delivery")
                    if (
                        not isinstance(delivery, Mapping)
                        or not retail_cart_delivery_matches_slot(delivery, normalized, provider=self.provider)
                    ):
                        raise HouseholdError(f"{self.provider.upper()} delivery selection is uncertain; inspect the provider selection")
                    if change:
                        display = delivery.get("display")
                        if not isinstance(display, str) or not display.strip() or len(display) > 500:
                            raise HouseholdError(f"{self.provider.upper()} delivery selection returned no verified display")
                        if self.provider == "mathem":
                            # Cart labels such as "imorgon" change meaning after
                            # midnight. Freeze the freshly verified slot instead.
                            start, end = [datetime.fromisoformat(normalized[key].replace("Z", "+00:00")).astimezone(
                                ZoneInfo("Europe/Stockholm")) for key in ("start_at", "end_at")]
                            if start.date() != end.date() or any(value.second or value.microsecond for value in (start, end)):
                                raise HouseholdError("Mathem delivery window cannot be represented exactly in checkout")
                            month = ("jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec")[start.month - 1]
                            display = f"{start.day} {month} {start:%H:%M} - {end:%H:%M}"
                        requested = {
                            "slot_id": normalized["provider_slot_id"],
                            "display": display.strip(),
                            "slot": normalized,
                            "max_total_ore": maximum,
                        }
                        with self.store.locked() as locked:
                            if canonical(locked.get("order_change")) != canonical(change):
                                raise HouseholdError("order change state changed while selecting delivery")
                            locked["order_change"]["kind"] = "delivery"
                            locked["order_change"]["requested_delivery"] = requested
                    with self.store.locked() as current_state:
                        self._guard_scheduled_context(current_state, request.get("_scheduler_context"))
                    if request.get("_defer_record") is not True:
                        self._record_delivery_selection(
                            normalized,
                            origin=str(request.get("_origin") or "explicit"),
                            candidate_digest=request.get("_candidate_digest"),
                            baseline=self.store.read(),
                            cart=raw_cart,
                            occurrence=str(request.get("_occurrence") or "") or None,
                        )
                    response = {
                        "provider": self.provider,
                        "selected": normalized,
                        "price_display": delivery_price_display(normalized),
                    }
                    if change:
                        response.update({
                            "staged_for_order": change["order_id"],
                            "next": "Prepare a fresh review of the exact window and full original/new totals. An unchanged or lower total is authorized by the requested window; an increase needs a covering price limit or one approval.",
                        })
                    return response
                if self.provider == "meny":
                    self.browser.verify_order_change(
                        change.get("order_id") if change else None,
                        change.get("code") if change else None,
                        deadline=deadline,
                    )
                    result = self._scheduled_select(request, arguments, deadline)
                    self.browser.verify_order_change(
                        change.get("order_id") if change else None,
                        change.get("code") if change else None,
                        deadline=deadline,
                    )
                    selected = result.get("selected") if isinstance(result, Mapping) else None
                    normalized = validate_delivery_slot(selected)
                    if normalized["slot_ref"] != slot_ref or normalized["selected"] is not True:
                        raise HouseholdError("MENY selected delivery does not match the requested slot")
                    if change:
                        with self.store.locked() as locked:
                            if canonical(locked.get("order_change")) != canonical(change):
                                raise HouseholdError("order change state changed while selecting delivery")
                            if change.get("delivery_only"):
                                locked["order_change"].update(kind="delivery", requested_delivery={
                                    "slot": normalized, "slot_id": normalized["provider_slot_id"],
                                    "max_total_ore": maximum})
                            else:
                                locked["order_change"]["kind"] = "full_order"
                    self._record_scheduled_effect(request.get("_scheduler_context"), "resolved", normalized)
                    with self.store.locked() as current_state:
                        self._guard_scheduled_context(current_state, request.get("_scheduler_context"))
                    if request.get("_defer_record") is not True:
                        scope_cart = self.provider_client.call("get_cart", {}, deadline=deadline)
                        self._record_delivery_selection(
                            normalized,
                            origin=str(request.get("_origin") or "explicit"),
                            candidate_digest=request.get("_candidate_digest"),
                            baseline=self.store.read(),
                            cart=scope_cart,
                            occurrence=str(request.get("_occurrence") or "") or None,
                        )
                    return result
        raise HouseholdError("unknown delivery action")

    def _orders(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "list")
        if "delivery_only" in request and (type(request["delivery_only"]) is not bool or action != "change_begin"):
            raise HouseholdError("delivery_only is a boolean for beginning an exact delivery-only order change")
        if self.provider in {"oda", "mathem"} and action == "change_begin" and self.browser is None:
            raise HouseholdError(f"{self.provider.title()} order changes require the dedicated logged-in browser")
        if self.provider == "mathem" and action.startswith("cancel_") and self.browser is None:
            raise HouseholdError("Mathem cancellation requires the dedicated logged-in browser; use the store website when no browser is configured")
        cancellation_deadline = time.monotonic() + CANCELLATION_OPERATION_TIMEOUT if action in {"cancel_prepare", "cancel_confirm", "cancel_reconcile", "cancel_submit"} else None
        if cancellation_deadline is not None and request.get("_deadline") is not None:
            cancellation_deadline = min(cancellation_deadline, request["_deadline"])
        if action == "list":
            limit = bounded_limit(request.get("limit"), default=10)
            return self.provider_client.call("get_orders", {"page": 1, "size": limit}, deadline=request.get("_deadline"), allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.provider_client.call("get_orders", {"page": 1, "size": limit}, deadline=request.get("_deadline"))
        supplied_order_id = request.get("order_id")
        order_id = safe_order_id(supplied_order_id) if supplied_order_id is not None and supplied_order_id != "" else ""
        if action == "get":
            deadline = request.get("_deadline")
            if self.provider == "meny":
                order = self.provider_client.call("get_order", {"order_number": order_id}, deadline=deadline, allow_recovery=request.get("_allow_browser_recovery") is True)
                require_provider_identity(order, order_id)
                return {
                    "order": order,
                    "tracking": {"order_id": order_id, "status": str(order.get("status") or "unknown")},
                }
            order = self.provider_client.call("get_order", {"order_number": order_id}, deadline=deadline)
            tracking = self.provider_client.call("order_tracking", {"order_number": order_id}, deadline=deadline)
            require_provider_identity(order, order_id)
            require_provider_identity(tracking, order_id, tracking=True)
            return {"order": order, "tracking": tracking}
        if action == "change_begin":
            if not order_id:
                raise HouseholdError("order_id is required for an order change")
            deadline = time.monotonic() + MENY_ORDER_TIMEOUT if self.provider == "meny" else request.get("_deadline")
            if self.provider == "meny" and request.get("_deadline") is not None:
                deadline = min(deadline, request["_deadline"])
            reservation = {
                "provider": self.provider,
                "order_id": order_id,
                "status": "starting",
                "token": secrets.token_urlsafe(18),
                "started_at": self._now().isoformat(),
            }
            with self.store.locked() as state:
                if state.get("order_change"):
                    raise HouseholdError("another order change is active; abort it before changing a different order")
                if state.get("pending_checkout") or state.get("pending_cancellation"):
                    raise HouseholdError("finish the pending protected operation before changing an order")
            with self._browser_operation(deadline):
                with self.store.locked() as state:
                    active = deepcopy(state.get("order_change"))
                    if state.get("pending_cart_change"):
                        raise HouseholdError("reconcile_change before changing the order")
                    if active:
                        raise HouseholdError("another order change is active; abort it before changing a different order")
                    if state.get("pending_checkout") or state.get("pending_cancellation"):
                        raise HouseholdError("finish the pending protected operation before changing an order")
                    state["order_change"] = deepcopy(reservation)
            try:
                if self.provider == "meny":
                    with self._browser_operation(deadline):
                        started = self.browser.begin_order_change(order_id, deadline=deadline)
                    code = str(started.get("code") or "").strip()
                    if not code:
                        raise HouseholdError("MENY order change identity is unavailable")
                    order = started.get("order")
                    if not isinstance(order, Mapping):
                        raise HouseholdError("MENY order change did not return the verified order")
                    current = {
                        "order": dict(order),
                        "tracking": {"order_id": order_id, "status": str(order.get("status") or "unknown")},
                    }
                    quantities = None
                    if request.get("delivery_only"):
                        try:
                            quantities, _names = self._cart_lines(cart_summary(self.provider_client.call("get_cart", {}, deadline=deadline)))
                        except Exception as exc:
                            # The merchant has already reopened this exact order.
                            # Preserve its edit for recovery; never start it again.
                            raise MenyOrderChangeDispatchError(order_id, code, order,
                                "MENY order was reopened but its original cart could not be read; recover or abort the same change") from exc
                else:
                    current = self._orders({"action": "get", "order_id": order_id, "_deadline": deadline})
                    status = str((current.get("tracking") or {}).get("status") or "").casefold()
                    if status != "paid_and_modifiable":
                        raise HouseholdError("Oda order is not currently modifiable")
                    cart = cart_summary(self.provider_client.call("get_cart", {}, deadline=deadline))
                    quantities, _names = self._cart_lines(cart)
                    digest = self._cart_digest(quantities)
                    if cart["items"] and request.get("cart_digest") != digest:
                        with self.store.locked() as state:
                            if canonical(state.get("order_change")) == canonical(reservation):
                                state["order_change"] = None
                        return {"editing": False, "cart_confirmation_required": True,
                                "order_id": order_id, "cart": cart, "cart_digest": digest,
                                "next": "Preserve these goods. Pass this cart_digest only when the user has authorized all shown staged goods for this exact order; otherwise clarify their destination."}
                    started = {"provider": self.provider, "order_id": order_id, "editing": True}
                    if self.provider in {"oda", "mathem"}:
                        with self._browser_operation(deadline):
                            started["binding"] = self.browser.read_order_binding(order_id, current["order"], deadline=deadline)
                    code = ""
                change = {
                    "provider": self.provider,
                    "order_id": order_id,
                    "before": current,
                    "status": "editing",
                    "started_at": reservation["started_at"],
                    **({"code": code} if code else {}),
                    **({"binding": started["binding"]} if self.provider in {"oda", "mathem"} else {}),
                    **({"delivery_only": True} if request.get("delivery_only") else {}),
                    **({"starting_cart_quantities": quantities} if quantities is not None else {}),
                    **({"expected_cart_quantities": quantities} if self.provider in {"oda", "mathem"} else {}),
                }
                with self.store.locked() as state:
                    if canonical(state.get("order_change")) != canonical(reservation):
                        raise HouseholdError("order change state changed while starting")
                    state["order_change"] = change
            except MenyOrderChangeDispatchError as exc:
                with self.store.locked() as state:
                    if canonical(state.get("order_change")) == canonical(reservation):
                        state["order_change"] = {
                            "provider": "meny", "order_id": exc.order_id, "status": "uncertain",
                            "code": exc.code,
                            "before": {
                                "order": deepcopy(exc.order),
                                "tracking": {"order_id": exc.order_id, "status": str(exc.order.get("status") or "unknown")},
                            },
                            "started_at": reservation["started_at"],
                        }
                raise
            except Exception:
                with self.store.locked() as state:
                    if canonical(state.get("order_change")) == canonical(reservation):
                        state["order_change"] = None
                raise
            return {**started, **change, "next": "Select the exact requested delivery window, preserve the goods and prepare its full-total review." if request.get("delivery_only") else "Use cart ensure for minimum quantities (Oda and Mathem include goods already ordered), or change for explicit extra quantities. If no additions are needed, abort the empty edit. Otherwise prepare/submit checkout for this exact order; provider permission is rechecked, never infer a fixed cutoff."}
        if action == "change_abort":
            with self.store.locked() as state:
                change = deepcopy(state.get("order_change"))
                if state.get("pending_checkout"):
                    raise HouseholdError("finish or reconcile the prepared checkout before aborting the order change")
            if not change or (order_id and order_id != change.get("order_id")):
                raise HouseholdError("no matching order change is active")
            if change.get("status") == "starting":
                try:
                    started_at = datetime.fromisoformat(str(change.get("started_at") or ""))
                except (TypeError, ValueError) as exc:
                    raise HouseholdError("the starting order change cannot be recovered safely") from exc
                try:
                    still_starting = self._now() < started_at + timedelta(minutes=5)
                except TypeError as exc:
                    raise HouseholdError("the starting order change cannot be recovered safely") from exc
                if still_starting:
                    raise HouseholdError("the order change is still starting")
            deadline = time.monotonic() + MENY_ORDER_TIMEOUT if self.provider == "meny" else request.get("_deadline")
            if self.provider == "meny" and request.get("_deadline") is not None:
                deadline = min(deadline, request["_deadline"])
            with self._browser_operation(deadline):
                with self.store.locked() as state:
                    if state.get("pending_cancellation"):
                        raise HouseholdError("finish the pending cancellation before aborting the order change")
                    if canonical(state.get("order_change")) != canonical(change):
                        raise HouseholdError("order change state changed before aborting")
                if self.provider == "meny":
                    code = str(change.get("code") or "")
                    if not code and change.get("status") == "starting":
                        current = self._orders({"action": "get", "order_id": change["order_id"], "_deadline": deadline})
                        code = str((current.get("order") or {}).get("code") or "")
                    if change.get("status") in {"starting", "uncertain", "abort_uncertain"}:
                        try:
                            self.browser.verify_order_change(change["order_id"], code, deadline=deadline)
                        except HouseholdError as active_error:
                            try:
                                self.browser.verify_order_change(None, None, deadline=deadline)
                            except HouseholdError as neutral_error:
                                raise HouseholdError("MENY order-change mode cannot be reconciled safely") from neutral_error
                            result = {"provider": "meny", "order_id": change["order_id"], "aborted": True, "recovered": True}
                        else:
                            try:
                                result = self.browser.abort_order_change(change["order_id"], code, deadline=deadline)
                            except HouseholdError:
                                with self.store.locked() as state:
                                    if canonical(state.get("order_change")) == canonical(change):
                                        state["order_change"]["status"] = "abort_uncertain"
                                raise
                    else:
                        try:
                            result = self.browser.abort_order_change(change["order_id"], code, deadline=deadline)
                        except HouseholdError:
                            with self.store.locked() as state:
                                if canonical(state.get("order_change")) == canonical(change):
                                    state["order_change"]["status"] = "abort_uncertain"
                            raise
                else:
                    if change.get("status") == "starting":
                        result = {"provider": self.provider, "order_id": change["order_id"], "aborted": True, "recovered": True}
                    elif request.get("retain_cart") is True:
                        result = {"provider": self.provider, "order_id": change["order_id"], "aborted": True, "cart_retained": True}
                    elif self._cart_lines(cart_summary(self.provider_client.call("get_cart", {}, deadline=deadline)))[0] != change.get("starting_cart_quantities", {}):
                        raise HouseholdError("abort with retain_cart=true to preserve the staged additions, or remove them before aborting")
                    else:
                        result = {"provider": self.provider, "order_id": change["order_id"], "aborted": True}
                with self.store.locked() as state:
                    if canonical(state.get("order_change")) != canonical(change):
                        raise HouseholdError("order change state changed while aborting")
                    state["order_change"] = None
            return result
        if action == "cancel_prepare":
            if not order_id:
                raise HouseholdError("order_id is required for cancellation")
            with self.store.locked() as state:
                baseline = deepcopy(state.get("pending_cancellation"))
                if baseline and baseline.get("status") in {"clicking", "uncertain"}:
                    raise HouseholdError("reconcile the pending cancellation before preparing another")
                if state.get("order_change"):
                    raise HouseholdError("finish or abort the active order change before cancellation")
            current = self._orders({"action": "get", "order_id": order_id, "_deadline": cancellation_deadline})
            with self._browser_operation(cancellation_deadline):
                state = self.store.read()
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending checkout before preparing cancellation")
                if state.get("order_change"):
                    raise HouseholdError("finish or abort the active order change before cancellation")
                browser = self.browser.review_cancellation(order_id, current["order"], deadline=cancellation_deadline)
                if browser.get("available") is not True:
                    return browser
                confirmation_id = secrets.token_urlsafe(18)
                with self.store.locked() as state:
                    if canonical(state.get("pending_cancellation")) != canonical(baseline):
                        raise HouseholdError("cancellation state changed while preparing the summary")
                    state["pending_cancellation"] = {"order_id": order_id, "confirmation_id": confirmation_id, "before": current, "browser": browser, "expires_at": (self._now() + timedelta(minutes=30)).isoformat(), "status": "awaiting_confirmation"}
                return {
                    "available": True,
                    "confirmation_id": confirmation_id,
                    "confirmation_policy": self.confirmation_policy,
                    "confirmation_required": self.confirmation_policy == "fresh",
                    "order": current["order"],
                    "tracking": current["tracking"],
                    "consequence": browser.get("consequence"),
                    "next": (
                        "Ask once for explicit confirmation of this exact order, then call cancel_confirm with both the same order_id and this confirmation_id unchanged."
                        if self.confirmation_policy == "fresh"
                        else "Standing authorization is configured. If the current request explicitly asks to cancel this order, call cancel_confirm now with both the same order_id and this confirmation_id; do not ask again."
                    ),
                }
        if action == "cancel_confirm":
            return self._cancel(
                action,
                cancellation_deadline,
                order_id=order_id,
                confirmation_id=str(request.get("confirmation_id") or ""),
            )
        if action == "cancel_submit":
            if self.confirmation_policy != "standing":
                raise HouseholdError("standing authorization is not configured; prepare cancellation and ask for confirmation")
            idempotency_key = self._idempotency_key(request.get("idempotency_key"), "cancellation")
            with self.store.locked() as state:
                pending = deepcopy(state.get("pending_cancellation"))
                protected_request = deepcopy(self._protected_request(state, "cancellation", idempotency_key, target_id=order_id))
            if protected_request:
                if isinstance(protected_request.get("result"), Mapping):
                    return {**self._protected_result_view(protected_request["result"], "cancellation"), "idempotent": True}
                bound_confirmation = str(protected_request.get("confirmation_id") or "")
                if pending and pending.get("confirmation_id") == bound_confirmation and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending, self._now()):
                    prepared = {
                        "available": True, "confirmation_id": bound_confirmation,
                        "order": deepcopy((pending.get("before") or {}).get("order")),
                        "tracking": deepcopy((pending.get("before") or {}).get("tracking")),
                        "consequence": deepcopy((pending.get("browser") or {}).get("consequence")),
                    }
                else:
                    return self._cancel_reconcile(cancellation_deadline, bound_confirmation)
            else:
                prepared = self._orders({"action": "cancel_prepare", "order_id": order_id, "_deadline": cancellation_deadline})
            if prepared.get("available") is not True:
                return prepared
            with self.store.locked() as state:
                existing = self._protected_request(state, "cancellation", idempotency_key, target_id=order_id)
                if existing is None:
                    current_pending = state.get("pending_cancellation")
                    if not isinstance(current_pending, Mapping) or current_pending.get("confirmation_id") != prepared["confirmation_id"]:
                        raise HouseholdError("cancellation state changed before binding its idempotency key")
                    self._bind_protected_request(state, "cancellation", idempotency_key, prepared["confirmation_id"], target_id=order_id)
                elif existing.get("confirmation_id") != prepared["confirmation_id"]:
                    raise HouseholdError("cancellation idempotency_key is bound to another attempt")
            result = self._cancel(
                "cancel_confirm",
                cancellation_deadline,
                order_id=order_id,
                confirmation_id=str(prepared.get("confirmation_id") or ""),
            )
            return {**result, "confirmation_id": prepared.get("confirmation_id"), "authorized_summary": {"order": prepared.get("order"), "tracking": prepared.get("tracking"), "consequence": prepared.get("consequence")}}
        if action == "cancel_reconcile":
            return self._cancel(action, cancellation_deadline, confirmation_id=str(request.get("confirmation_id") or ""))
        raise HouseholdError("unknown order action")

    def _cancel(
        self,
        action: str,
        deadline: float | None = None,
        *,
        order_id: str = "",
        confirmation_id: str = "",
    ) -> dict[str, Any]:
        if action == "cancel_reconcile":
            return self._cancel_reconcile(deadline, confirmation_id)
        with self.store.locked() as state:
            pending = deepcopy(state.get("pending_cancellation"))
            recovered = self._read_protected_result(state, confirmation_id, "cancellation")
        if recovered:
            return recovered
        if not pending:
            raise HouseholdError("no order cancellation is pending")
        if order_id != pending.get("order_id") or confirmation_id != pending.get("confirmation_id"):
            raise HouseholdError("cancellation confirmation does not match the prepared order")
        if self._now() >= datetime.fromisoformat(pending["expires_at"]):
            raise HouseholdError("cancellation confirmation expired")
        current = self._orders({"action": "get", "order_id": order_id, "_deadline": deadline})
        if pending["status"] != "awaiting_confirmation" or canonical(current) != canonical(pending["before"]):
            raise HouseholdError("the order changed; ask for a new cancellation confirmation")
        if self.provider in {"oda", "mathem"}:
            require_order_binding(pending["browser"].get("binding"))
        with self._browser_operation(deadline):
            with self.store.locked() as state:
                current_pending = state.get("pending_cancellation")
                if not current_pending or current_pending.get("status") != "awaiting_confirmation" or canonical(current_pending) != canonical(pending):
                    raise HouseholdError("no fresh cancellation confirmation is pending")
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending checkout before cancelling an order")
                state["pending_cancellation"]["status"] = "clicking"
            pending["status"] = "clicking"

            def before_click() -> None:
                if self._now() >= datetime.fromisoformat(pending["expires_at"]):
                    raise CancellationPreconditionError("cancellation confirmation expired before the final click")
                with self.store.locked() as state:
                    current_pending = state.get("pending_cancellation")
                    if not current_pending or current_pending.get("status") != "clicking":
                        raise CancellationPreconditionError("cancellation confirmation changed before the final click")
                    expected = {**pending, "status": "clicking"}
                    if canonical(current_pending) != canonical(expected):
                        raise CancellationPreconditionError("cancellation confirmation changed before the final click")
            try:
                self.browser.submit_cancellation(
                    order_id,
                    current["order"],
                    pending["browser"],
                    before_click,
                    deadline=deadline,
                )
                tracking = self.provider_client.call("order_tracking", {"order_number": order_id}, deadline=deadline)
                require_provider_identity(tracking, order_id, tracking=True)
                if self.provider in {"oda", "mathem"}:
                    self.browser.read_order_binding(order_id, current["order"], deadline=deadline,
                                                    expected_binding=require_order_binding(pending["browser"].get("binding")))
                current = {"order": current["order"], "tracking": tracking}
            except CancellationPreconditionError:
                with self.store.locked() as state:
                    current_pending = state.get("pending_cancellation")
                    if current_pending and current_pending.get("status") == "clicking" and current_pending.get("browser") == pending.get("browser"):
                        state["pending_cancellation"] = None
                raise
            except HouseholdError:
                with self.store.locked() as state:
                    current_pending = state.get("pending_cancellation")
                    if current_pending and current_pending.get("status") == "clicking" and current_pending.get("browser") == pending.get("browser"):
                        state["pending_cancellation"]["status"] = "uncertain"
                raise
            cancelled = str(tracking.get("status") or "").casefold() in {"cancelled", "canceled"}
            with self.store.locked() as state:
                if canonical(state.get("pending_cancellation")) != canonical(pending):
                    raise HouseholdError("cancellation state changed while reconciling the order")
                if cancelled:
                    state["pending_cancellation"] = None
                    self._mark_order_cancelled(
                        state, order_id, provider=self.provider, active_provider=self.provider,
                    )
                    terminal = {
                        "cancelled": True, "order_id": order_id,
                        "tracking_status": str(tracking.get("status") or "").casefold(),
                        "retry_allowed": False, "confirmation_id": pending["confirmation_id"],
                        "payment_resolution": {"authorization_release": "unknown", "refund": "unknown"},
                    }
                    self._store_protected_result(state, pending["confirmation_id"], "cancellation", terminal, target_id=order_id)
                else:
                    state["pending_cancellation"]["status"] = "uncertain"
        return {**(terminal if cancelled else {}), "cancelled": cancelled, "tracking": current["tracking"], "retry_allowed": False}

    @staticmethod
    def _prune_order_snapshots(state: dict[str, Any], *, keep_order_id: str | None = None) -> None:
        keep = {str(keep_order_id)} if keep_order_id else set()
        terminal = set()
        snapshots = state.setdefault("order_snapshots", {})
        snapshot_times = state.setdefault("order_snapshot_times", {})
        snapshot_providers = state.setdefault("order_snapshot_providers", {})
        active_provider = state.get("provider")
        current = state.get("menu")
        if isinstance(current, Mapping) and current.get("order_id"):
            current_order_id = str(current["order_id"])
            if snapshot_providers.get(current_order_id) == active_provider:
                keep.add(current_order_id)
        for job in state.get("email_jobs", []):
            if not isinstance(job, Mapping) or not job.get("order_id"):
                continue
            order_id = str(job["order_id"])
            if email_job_provider(job) != snapshot_providers.get(order_id):
                continue
            if job.get("status") in {"pending", "claimed", "sending"}:
                keep.add(order_id)
            elif job.get("status") in {"sent", "cancelled", "invalid"}:
                terminal.add(order_id)
        unscheduled = [
            (str(snapshot_times.get(order_id) or ""), order_id)
            for order_id in snapshots
            if order_id not in keep and order_id not in terminal
        ]
        keep.update(order_id for _recorded_at, order_id in sorted(unscheduled)[-20:])
        for order_id in list(snapshots):
            if order_id not in keep:
                snapshots.pop(order_id, None)
                snapshot_times.pop(order_id, None)
                snapshot_providers.pop(order_id, None)

    def _mark_order_cancelled(self,
        state: dict[str, Any], order_id: str, *, provider: str | None = None,
        active_provider: str | None = None,
    ) -> None:
        for job in state["email_jobs"]:
            provider_matches = provider is None or email_job_provider(job) == provider
            if provider_matches and job.get("order_id") == order_id and job.get("status") in {"pending", "claimed"}:
                job["status"] = "cancelled"
                job.pop("claim_token", None)
                job.pop("claim_expires_at", None)
                job.pop("html", None)
                job.pop("menu_snapshot", None)
                job.pop("subject", None)
        if provider is not None and provider != active_provider:
            return
        for usage in state.get("recipe_usage", {}).values():
            if isinstance(usage, dict) and usage.get("order_id") == order_id and usage.get("status") == "ordered":
                usage["previous_status"] = "ordered"
                usage["status"] = "planned"
                usage["cancelled_order_id"] = order_id
                usage["order_id"] = None
                usage["updated_at"] = self._now().isoformat()
        current = state.get("menu")
        if isinstance(current, dict) and current.get("order_id") == order_id:
            current["phase"] = "draft"
            current.pop("order_id", None)
        OrderOperations._prune_order_snapshots(state)

    def _cancel_reconcile(self, deadline: float | None = None, confirmation_id: str = "") -> dict[str, Any]:
        with self._browser_operation(deadline):
            with self.store.locked() as state:
                pending = deepcopy(state.get("pending_cancellation"))
                recovered = self._read_protected_result(state, confirmation_id, "cancellation") if confirmation_id else None
            if recovered:
                return recovered
            if confirmation_id and isinstance(pending, Mapping) and pending.get("confirmation_id") != confirmation_id:
                raise HouseholdError("cancellation reconciliation does not match the pending attempt")
            if not pending:
                raise HouseholdError("no order cancellation is pending")
            if pending.get("status") not in {"clicking", "uncertain"}:
                raise HouseholdError("cancellation has not reached reconciliation")
            order_id = pending["order_id"]
            current = self._orders({"action": "get", "order_id": order_id, "_deadline": deadline})
            cancelled = str((current.get("tracking") or {}).get("status") or "").casefold() in {"cancelled", "canceled"}
            if self.provider in {"oda", "mathem"}:
                self.browser.read_order_binding(order_id, current["order"], deadline=deadline,
                                                expected_binding=require_order_binding(pending["browser"].get("binding")))
            with self.store.locked() as state:
                if canonical(state.get("pending_cancellation")) != canonical(pending):
                    raise HouseholdError("cancellation state changed while reconciling the order")
                if cancelled:
                    state["pending_cancellation"] = None
                    self._mark_order_cancelled(
                        state, order_id, provider=self.provider, active_provider=self.provider,
                    )
                    terminal = {
                        "cancelled": True, "order_id": order_id,
                        "tracking_status": str((current.get("tracking") or {}).get("status") or "").casefold(),
                        "retry_allowed": False, "confirmation_id": pending["confirmation_id"],
                        "payment_resolution": {"authorization_release": "unknown", "refund": "unknown"},
                    }
                    self._store_protected_result(state, pending["confirmation_id"], "cancellation", terminal, target_id=order_id)
                else:
                    state["pending_cancellation"]["status"] = "uncertain"
            return {**(terminal if cancelled else {}), "cancelled": cancelled, "tracking": current["tracking"], "retry_allowed": False}

    def _checkout_dietary(self, summary, deadline=None):
        from dietary_assessment import assess, digest, rules
        profile = self.store.read()['profile']
        findings = []
        if rules(profile):
            for item in summary.get('items', []):
                reference = item.get('product_id')
                product = {'product_ref': reference, 'name': item.get('name'), 'dietary_evidence': {}}
                try:
                    observed = self.provider_client.call('product_search', {'queries': [item.get('name') or str(reference)], 'page': 1, 'size': 20}, deadline=deadline)
                    matches = [p for p in observed.get('products', []) if str(p.get('product_ref')) == str(reference)]
                    if observed.get('provider') == self.provider and len(matches) == 1:
                        product = matches[0]
                except HouseholdError:
                    pass
                reader = getattr(self.provider_client, 'product_dietary_evidence', None)
                if reader is not None:
                    try:
                        details = reader(reference, deadline=deadline)
                        if isinstance(details, Mapping):
                            product = {**product, 'dietary_evidence': {**product.get('dietary_evidence', {}), **details}}
                    except HouseholdError:
                        pass
                findings.extend(assess(profile, product))
        value = {'findings': findings, 'profile_digest': digest(profile['diet'])}
        value['assessment_digest'] = digest(value)
        return value

    def _checkout_notice(self, confirmation_id, phase, payload):
        from dietary_assessment import digest
        descriptions = {'unknown': 'information unresolved', 'preference_deviation': 'preference deviation',
                        'sensitivity_conflict': 'contains a sensitivity-related ingredient', 'conflict': 'excluded ingredient'}
        lines = [f"{f['item']} (product {f['product_ref']}): {f['kind']} / {f['term']} — {descriptions.get(f['condition'], f['condition'])}."
                 + (f" Source: {f['evidence']['source_url']}" if f.get('evidence', {}).get('source_url') else ' Product information is incomplete.')
                 for f in payload.get('findings', [])]
        shortfall = payload.get('menu_shortfall', (payload.get('summary') or {}).get('menu_shortfall', []))
        lines.extend(f"Incomplete menu shopping: {row['name']} (product {row['product_id']}): "
                     f"{row['live_quantity']} of {row['required_quantity']} required packages; "
                     f"{row['missing_quantity']} missing." for row in shortfall)
        summary = payload.get('summary') or {}
        if 'merchant_summary_total' in summary:
            lines.append(f"Payment due now: {summary['total']:.2f} SEK. "
                         f"The merchant overview separately shows {summary['merchant_summary_total']:.2f} SEK; "
                         "the payment button uses the amount due now.")
        if phase == 'before_dispatch':
            message = f"Before checkout at {self.provider.upper()}:\n" + '\n'.join(lines) + "\nProceeding under your accepted rule after this notice; no reply is required."
        else:
            options = payload['correction_options']
            message = f"Order {payload['order_id']} accepted at {self.provider.upper()} (provider status: {payload.get('tracking_status') or 'confirmed'}).\n" + '\n'.join(lines)
            payment = payload['payment']
            message += f"\nBank payment authorization: {payment['authorization']}; charge: {payment['charge']}."
            availability = {'additions_only': 'additions currently supported', 'unavailable': 'currently unavailable',
                            'unknown': 'unknown', 'requires_current_provider_review': 'requires current provider review',
                            'manual_provider_review': 'manual provider review only'}
            message += '\nCurrent editing: ' + availability[options['edit_availability']] + '. ' + options['message']
            message += '\nModification deadline: ' + (options['deadline'] or options.get('deadline_text') or 'unknown') + '.'
            if options.get('cancellation_deadline_text'):
                message += '\nCancellation deadline: ' + options['cancellation_deadline_text']
        payload = {**deepcopy(payload), 'message': message}
        key = digest({'confirmation_id': confirmation_id, 'phase': phase, 'payload': payload})
        with self.store.locked() as state:
            notices = state.setdefault('checkout_notices', {})
            existing = notices.get(key)
            if existing:
                return {**deepcopy(existing), 'dispatch': False}
            if len(notices) >= 2000:
                raise HouseholdError('checkout notice journal is full')
            notice = {'notice_token': key, 'confirmation_id': confirmation_id, 'phase': phase,
                      'payload': deepcopy(payload), 'status': 'sending', 'delivered': False}
            notices[key] = notice
            return {**deepcopy(notice), 'dispatch': True,
                    'next': 'Send payload.message exactly once through the existing authorized native household messaging route. Record the actual sender outcome with notice_result, then resume checkout confirm with this confirmation_id (or the same submit idempotency key / auto occurrence). No user reply is required. An uncertain send must be reconciled, never resent blindly.'}

    def _checkout_notice_result(self, request):
        outcome = request.get('send_outcome')
        receipt = request.get('sender_receipt')
        if outcome not in {'sent', 'not_sent', 'unknown'} or not isinstance(receipt, str) or not 1 <= len(receipt.strip()) <= 1000:
            raise HouseholdError('notice_result requires actual native sender outcome and bounded sender receipt')
        with self.store.locked() as state:
            notice = state.get('checkout_notices', {}).get(request.get('notice_token'))
            if not notice:
                raise HouseholdError('notice token does not identify an existing dispatch')
            if notice['status'] == 'sent' and (outcome != 'sent' or notice.get('sender_receipt') != receipt):
                raise HouseholdError('a verified sent notice cannot be overwritten')
            notice.update(status=outcome, delivered=outcome == 'sent', sender_receipt=receipt)
            return deepcopy(notice)

    def _dietary_checkout_gate(self, pending, request=None):
        from dietary_assessment import covered
        request = request or {}
        findings = pending.get('dietary_assessment', {}).get('findings', [])
        material = [f for f in findings if f['condition'] != 'compatible_label']
        blocked = [f for f in material if f['blocked']]
        if blocked:
            return {'confirmed': False, 'dietary_review_required': True, 'findings': blocked,
                    'reason': 'Choose another item for each documented allergy or never-buy conflict; the order remains incomplete.'}
        reviewed = request.get('dietary_review') or []
        if not isinstance(reviewed, list) or any(not isinstance(v, str) for v in reviewed) or len(reviewed) != len(set(reviewed)) or not set(reviewed) <= {f['finding_id'] for f in findings}:
            raise HouseholdError('dietary_review must contain distinct current-summary finding IDs actually reviewed in the final confirmation')
        manually_reviewed = bool(reviewed) and {f['finding_id'] for f in material} <= set(reviewed)
        automatic = pending.get('automatic_checkout') or (self.confirmation_policy == 'standing' and not manually_reviewed)
        profile = self.store.read()['profile']
        if automatic:
            uncovered = [f for f in material if not covered(profile, f)]
        else:
            uncovered = [f for f in material if f['kind'] in {'allergy', 'never_buy', 'allergy_or_sensitivity'} and f['finding_id'] not in reviewed]
        if uncovered:
            return {'confirmed': False, 'dietary_review_required': True, 'findings': uncovered,
                    'reason': 'Affected-item review or an explicitly covering standing uncertainty rule is required; other work may continue.'}
        if automatic and material:
            notice = self._checkout_notice(pending['confirmation_id'], 'before_dispatch', {'provider': self.provider, 'summary': pending['summary'], 'findings': material})
            if notice['status'] != 'sent':
                return {'confirmed': False, 'notification_required': True, 'notice': notice,
                        'reason': 'The required pre-dispatch notice has no verified successful sender result.'}
        return None

    def _checkout_result_notice(self, result):
        confirmation_id = result.get('confirmation_id')
        if not result.get('confirmed') or not confirmation_id:
            return result
        state = self.store.read()
        before = next((n for n in state.get('checkout_notices', {}).values() if n['confirmation_id'] == confirmation_id and n['phase'] == 'before_dispatch'), None)
        if not before and result.get('original_confirmation_id'):
            confirmation_id = result['original_confirmation_id']
            before = next((n for n in state.get('checkout_notices', {}).values() if n['confirmation_id'] == confirmation_id and n['phase'] == 'before_dispatch'), None)
        if not before:
            return result
        existing = next((n for n in state.get('checkout_notices', {}).values() if n['confirmation_id'] == confirmation_id and n['phase'] == 'after_reconciliation'), None)
        if existing:
            return {**result, 'notice': {**deepcopy(existing), 'dispatch': False}}
        order_id = result.get('order_id')
        options = {'edit_availability': 'unknown', 'deadline': None, 'deadline_status': 'unknown',
                   'message': 'Edit availability and deadline are unknown; removal, replacement and refund are not guaranteed.'}
        try:
            current = self._orders({'action': 'get', 'order_id': order_id})
            status = str((current.get('tracking') or {}).get('status') or '').casefold()
            for source_name in ('tracking', 'order'):
                raw_deadline = (current.get(source_name) or {}).get('modificationDeadline')
                try:
                    parsed_deadline = datetime.fromisoformat(raw_deadline.replace('Z', '+00:00')) if isinstance(raw_deadline, str) else None
                except ValueError:
                    parsed_deadline = None
                if parsed_deadline is not None and parsed_deadline.tzinfo is not None:
                    options.update(deadline=parsed_deadline.isoformat(), deadline_status='provider_reported', deadline_source=source_name + '.modificationDeadline')
                    break
            if self.provider == 'oda':
                options.update(edit_availability='additions_only' if status == 'paid_and_modifiable' else 'unavailable' if status in {'paid_and_not_modifiable', 'picking', 'shipped', 'delivered', 'cancelled', 'canceled'} else 'unknown',
                               message='User-directed additions are supported only while this order remains modifiable. Removal/replacement/refund is not promised.')
            elif self.provider == 'meny':
                options.update(edit_availability='requires_current_provider_review', message='MENY full-order editing requires a current editable order, another checkout and Vipps approval; no removal/refund guarantee.')
            else:
                if status == 'paid_and_modifiable' and self.browser is not None:
                    options.update(edit_availability='additions_only',
                        message='User-directed additions use the original order and another protected checkout. Cancellation requires a fresh available cancellation review. Moving delivery preserves goods and requires a fresh original/new full-total review; a higher total requires approval unless covered by the authorized price limit. Removal, replacement, refund and payment release are not promised.')
                    followup_deadline = time.monotonic() + 90
                    with self._browser_operation(followup_deadline):
                        # A terminal-result replay may run while a newer
                        # checkout owns the browser, including a bank challenge.
                        followup = ({} if self.store.read().get('pending_checkout') else
                                    self.browser.order_followup(order_id, current['order'], deadline=followup_deadline))
                    options.update(followup)
                    if followup.get('deadline_text'):
                        options.update(deadline_status='provider_reported_text', deadline_source='mathem_order_page')
                elif status in {'paid_and_not_modifiable', 'picking', 'shipped', 'delivered', 'cancelled', 'canceled'}:
                    options.update(edit_availability='unavailable', message='This order is no longer modifiable. Refund and payment release are not established.')
                else:
                    options.update(edit_availability='manual_provider_review', message='Review current changes with Mathem; no automatic edit or refund promise.')
        except HouseholdError:
            pass
        notice = self._checkout_notice(confirmation_id, 'after_reconciliation', {'provider': self.provider, 'confirmed': True,
            'order_id': order_id, 'tracking_status': result.get('tracking_status'),
            'payment': result.get('payment') or self._payment_evidence(result.get('tracking_status')),
            'menu_shortfall': deepcopy(result.get('menu_shortfall', [])),
            'findings': before['payload']['findings'], 'correction_options': options})
        return {**result, 'notice': notice}

    def _checkout(self, request):
        if request.get('action') == 'notice_result':
            return self._checkout_notice_result(request)
        return self._checkout_result_notice(self._checkout_operation(request))

    def _checkout_operation(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "prepare")
        if "delivery_price_approved" in request and (type(request["delivery_price_approved"]) is not bool or action != "confirm"):
            raise HouseholdError("delivery_price_approved is a boolean for one freshly reviewed checkout confirmation only")
        if self.provider == "mathem" and self.browser is None and action not in {"prepare", "auto"}:
            raise HouseholdError("Mathem checkout is manual; use prepare for the cart summary and finish at https://www.mathem.se/se/cart/")
        deadline = time.monotonic() + (MENY_CHECKOUT_OPERATION_TIMEOUT if self.provider == "meny" else 240)
        if "recovery" in request and (type(request["recovery"]) is not bool or action != "prepare"):
            raise HouseholdError("recovery is a boolean option for checkout prepare only")
        if "checkout_payment" in request and not (action == "prepare" and request.get("recovery")):
            raise HouseholdError("checkout_payment override is available only for recovery preparation")
        if action == "prepare" and request.get("recovery"):
            return self._checkout_recovery_prepare(deadline, request.get("checkout_payment"))
        if action == "prepare":
            occurrence = str(request.get("occurrence") or "") or None
            state = self.store.read()
            observation = state.get("delivery_selection")
            scope = observation.get("scope") if isinstance(observation, Mapping) else None
            scoped_occurrence = scope.get("occurrence") if isinstance(scope, Mapping) else None
            scoped_record = state.get("occurrences", {}).get(scoped_occurrence)
            if (
                occurrence is None
                and scoped_occurrence
                and isinstance(scoped_record, Mapping)
                and scoped_record.get("status") == "cart_ready"
            ):
                raise HouseholdError("carry the cart_ready occurrence into checkout prepare")
            if occurrence is not None:
                record = state.get("occurrences", {}).get(occurrence)
                if not isinstance(record, Mapping) or record.get("status") != "cart_ready":
                    raise HouseholdError("checkout occurrence is not a cart_ready scheduled run")
            return self._checkout_prepare(
                deadline,
                occurrence=occurrence,
                cart_ready_continuation=occurrence is not None,
            )
        if action == "confirm":
            return self._checkout_confirm(deadline, str(request.get("confirmation_id") or ""), request=request)
        if action == "submit":
            idempotency_key = self._idempotency_key(request.get("idempotency_key"), "checkout")
            with self.store.locked() as state:
                pending = deepcopy(state.get("pending_checkout"))
                protected_request = deepcopy(self._protected_request(state, "checkout", idempotency_key))
            if self.confirmation_policy != "standing" and not ((pending or {}).get("order_change") or {}).get("requested_delivery"):
                if protected_request and isinstance(protected_request.get("result"), Mapping):
                    return {**self._protected_result_view(protected_request["result"], "checkout"), "idempotent": True}
                if protected_request and (not pending or pending.get("status") != "awaiting_confirmation"):
                    return self._checkout_reconcile(deadline, str(protected_request.get("confirmation_id") or ""))
                raise HouseholdError("standing authorization is not configured; prepare checkout and ask for confirmation")
            if protected_request:
                if isinstance(protected_request.get("result"), Mapping):
                    return {**self._protected_result_view(protected_request["result"], "checkout"), "idempotent": True}
                bound_confirmation = str(protected_request.get("confirmation_id") or "")
                if not bound_confirmation:
                    raise HouseholdError("checkout idempotency record is incomplete; reconcile before retrying")
                if pending and pending.get("confirmation_id") == bound_confirmation and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending, self._now()):
                    prepared = {
                        "confirmation_id": bound_confirmation,
                        "summary": deepcopy(pending["summary"]),
                        "order_change": deepcopy(pending.get("order_change")),
                    }
                else:
                    return self._checkout_reconcile(deadline, bound_confirmation)
            elif pending and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending, self._now()):
                prepared = {
                    "confirmation_id": pending["confirmation_id"],
                    "summary": deepcopy(pending["summary"]),
                    "order_change": deepcopy(pending.get("order_change")),
                }
            else:
                prepared = self._checkout({"action": "prepare", "occurrence": request.get("occurrence")})
            if prepared.get("cart_reconciliation_required") is True:
                return {"confirmed": False, **prepared}
            with self.store.locked() as state:
                existing = self._protected_request(state, "checkout", idempotency_key)
                if existing is None:
                    current_pending = state.get("pending_checkout")
                    if not isinstance(current_pending, Mapping) or current_pending.get("confirmation_id") != prepared["confirmation_id"]:
                        raise HouseholdError("checkout state changed before binding its idempotency key")
                    self._bind_protected_request(state, "checkout", idempotency_key, prepared["confirmation_id"])
                elif existing.get("confirmation_id") != prepared["confirmation_id"]:
                    raise HouseholdError("checkout idempotency_key is bound to another attempt")
            result = self._checkout_confirm(deadline, prepared["confirmation_id"])
            if result.get("reprepared") is True:
                replacement_id = str(result.get("confirmation_id") or "")
                if not replacement_id:
                    raise HouseholdError("reprepared checkout has no confirmation identity")
                with self.store.locked() as state:
                    existing = self._protected_request(state, "checkout", idempotency_key)
                    if (
                        not isinstance(existing, dict)
                        or existing.get("confirmation_id") != prepared["confirmation_id"]
                    ):
                        raise HouseholdError("checkout idempotency binding changed during reprepare")
                    current_pending = state.get("pending_checkout")
                    if (
                        not isinstance(current_pending, Mapping)
                        or current_pending.get("confirmation_id") != replacement_id
                    ):
                        raise HouseholdError("reprepared checkout state changed")
                    existing["confirmation_id"] = replacement_id
                    existing["rebound_at"] = self._now().isoformat()
                return {
                    **result,
                    "confirmation_id": replacement_id,
                    "authorized_summary": result["summary"],
                    "order_change": result.get("order_change"),
                }
            return {**result, "confirmation_id": prepared["confirmation_id"], "authorized_summary": prepared["summary"], "order_change": prepared.get("order_change")}
        if action == "reconcile":
            return self._checkout_reconcile(deadline, str(request.get("confirmation_id") or ""))
        if action == "authenticate":
            return self._checkout_authenticate(deadline, str(request.get("confirmation_id") or ""))
        if action == "auto":
            occurrence = str(request.get("occurrence") or "")
            state = self.store.read()
            pending = state.get('pending_checkout')
            if self.confirmation_policy == 'standing' and pending and pending.get('occurrence') == occurrence and pending.get('status') == 'awaiting_confirmation' and pending.get('automatic_checkout'):
                self._require_weekly_scheduler(state, request.get('scheduler'))
                self._pending_scheduler_guard(state, pending)
                result = self._checkout_confirm(deadline, pending['confirmation_id'])
                return {**result, 'completed': result.get('confirmed') is True, 'confirmation_id': result.get('confirmation_id', pending['confirmation_id'])}
            with self.store.locked() as state:
                scheduler_identity = self._require_weekly_scheduler(state, request.get("scheduler"))
                if self._unresolved_scheduled_effects(state):
                    raise HouseholdError("reconcile the original scheduled delivery effect before retry")
                schedule = deepcopy(state["schedule"])
                validate_schedule(schedule, self.provider)
                if not schedule.get("enabled"):
                    raise HouseholdError("scheduled run is off")
                if not schedule.get("auto_checkout") and schedule.get("mode") != "cart_ready":
                    raise HouseholdError("scheduled delivery choice requires cart_ready or auto_checkout mode")
                if scheduler_identity is None and (not isinstance(schedule.get("cron_job_id"), str) or not schedule["cron_job_id"].strip()):
                    raise HouseholdError("auto-checkout is not linked to its configured cron job")
                expected_occurrence = scheduled_occurrence(schedule, self._now())
                if state.get("order_change"):
                    raise HouseholdError("scheduled checkout cannot submit an interactive order change")
                if occurrence != expected_occurrence:
                    raise HouseholdError("scheduled occurrence does not match the currently due local week")
                existing = state["occurrences"].get(occurrence)
                if isinstance(existing, Mapping) and existing.get("status") == "completed":
                    order_id = str(existing.get("order_id") or "")
                    if not order_id:
                        raise HouseholdError("the completed scheduled occurrence has no bound order")
                    recovered = self._read_protected_result(state, existing.get('confirmation_id'), 'checkout') if existing.get('confirmation_id') else None
                    if recovered and recovered.get('order_id') != order_id:
                        raise HouseholdError('completed scheduled occurrence differs from its checkout result')
                    return {
                        'payment': self._payment_evidence(), **(recovered or {}),
                        "completed": True, "confirmed": True, "order_id": order_id,
                        "confirmation_id": existing.get("confirmation_id"),
                        "idempotent": True, "retry_allowed": False,
                    }
                if isinstance(existing, Mapping) and existing.get("status") == "started":
                    try:
                        started_at = datetime.fromisoformat(str(existing.get("at") or ""))
                    except ValueError:
                        started_at = None
                    if started_at is not None and started_at.tzinfo is not None and self._now() < started_at + SCHEDULE_OCCURRENCE_LEASE:
                        raise HouseholdError("this scheduled occurrence is already running")
                self._require_menu_provider(state.get("menu"))
                pending = state.get("pending_checkout")
                if pending and pending.get("status") == "awaiting_confirmation" and (
                    pending.get("occurrence") or expired_awaiting_confirmation(pending, self._now())
                ):
                    self._abandon_predispatch(state, reason="expired review or scheduled run retried")
                elif pending:
                    raise HouseholdError("finish the pending interactive or dispatched checkout before the scheduled run")
                attempts = int(existing.get("attempts", 0)) + 1 if isinstance(existing, Mapping) else 1
                context = {"occurrence": occurrence, "attempt_id": secrets.token_urlsafe(18),
                           "scheduler": scheduler_identity, "settings_digest": scheduler_settings_digest(schedule)}
                state["occurrences"][occurrence] = {"status": "started", "at": self._now().isoformat(),
                                                    "attempts": attempts, "attempt_id": context["attempt_id"],
                                                    "scheduler_context": deepcopy(context)}
            try:
                delivery_choice = self._scheduled_delivery_choice(
                    schedule, occurrence=occurrence, deadline=deadline, scheduler_context=context,
                )
                if delivery_choice.get("ready") is not True:
                    with self.store.locked() as state:
                        self._weekly_attempt_status(state, context, "needs_input")
                    return {
                        "completed": False,
                        "confirmed": False,
                        "mode": "cart_ready",
                        **delivery_choice,
                    }
                if not schedule.get("auto_checkout"):
                    if not delivery_matches(
                        schedule["delivery"],
                        delivery_choice.get("selected"),
                        timezone_name=str(schedule["timezone"]),
                    ):
                        with self.store.locked() as state:
                            self._weekly_attempt_status(state, context, "needs_input")
                        return {
                            "completed": False,
                            "confirmed": False,
                            "mode": "cart_ready",
                            "reason": "delivery does not match preference",
                            **delivery_choice,
                        }
                    cart = (
                        self.provider_client.call("get_cart", {}, deadline=deadline)
                        if self.provider == "meny"
                        else self.provider_client.call("get_cart", {}, deadline=deadline)
                    )
                    summary = self._bind_delivery_summary(cart_summary(cart), delivery_choice, provider=self.provider)
                    with self.store.locked() as state:
                        self._guard_scheduled_context(state, context)
                        self._weekly_attempt_status(state, context, "cart_ready")
                    return {
                        "completed": False,
                        "confirmed": False,
                        "mode": "cart_ready",
                        "occurrence": occurrence,
                        "summary": summary,
                        **delivery_choice,
                    }
                prepared = self._checkout_prepare(
                    deadline, occurrence=occurrence, delivery_binding=delivery_choice, automatic_checkout=True, scheduler_context=context,
                )
            except HouseholdError:
                with self.store.locked() as state:
                    self._weekly_attempt_status(state, context, "needs_input")
                raise
            if prepared.get("cart_reconciliation_required") is True:
                with self.store.locked() as state:
                    self._weekly_attempt_status(state, context, "needs_input")
                return {"completed": False, "confirmed": False, "mode": "cart_ready", **prepared}
            problem = self._scheduled_checkout_problem(prepared["summary"], occurrence)
            if problem is not None:
                with self.store.locked() as state:
                    pending = state.get("pending_checkout")
                    if pending and pending.get("confirmation_id") == prepared["confirmation_id"] and pending.get("status") == "awaiting_confirmation":
                        state["pending_checkout"] = None
                    self._weekly_attempt_status(state, context, "needs_input")
                return {"completed": False, "reason": problem, "summary": prepared["summary"]}
            if self.confirmation_policy == "standing":
                try:
                    result = self._checkout_confirm(deadline, prepared["confirmation_id"])
                except HouseholdError:
                    with self.store.locked() as state:
                        self._weekly_attempt_status(state, context, "needs_input")
                    raise
                with self.store.locked() as state:
                    self._weekly_attempt_status(state, context, "completed" if result.get("confirmed") is True else "needs_input")
                return {**result, "completed": result.get("confirmed") is True, "authorized_summary": prepared["summary"]}
            with self.store.locked() as state:
                self._weekly_attempt_status(state, context, "awaiting_confirmation")
            return {
                "completed": False,
                "confirmed": False,
                "awaiting_confirmation": True,
                "confirmation_id": prepared["confirmation_id"],
                "summary": prepared["summary"],
                "next": "Show this exact guarded scheduled summary and require a fresh explicit user confirmation before checkout confirm.",
            }
        raise HouseholdError("unknown checkout action")

    def _checkout_prepare(
        self,
        deadline: float | None = None,
        *,
        occurrence: str | None = None,
        delivery_binding: Mapping[str, Any] | None = None,
        delivery_reselections: int = 0,
        cart_ready_continuation: bool = False,
        automatic_checkout: bool = False,
        scheduler_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.provider == "mathem" and self.browser is None:
            if automatic_checkout:
                raise HouseholdError("Mathem supports manual checkout only")
            with self._browser_operation(deadline):
                state = self.store.read()
                if state.get("pending_checkout") or state.get("pending_cancellation") or state.get("order_change"):
                    raise HouseholdError("finish the pending provider operation before manual checkout")
                self._require_menu_provider(state.get("menu"))
                summary = cart_summary(self.provider_client.call("get_cart", {}, deadline=deadline))
                plan = state.get("cart_plan")
                if isinstance(plan, Mapping) and plan.get("provider") == self.provider and plan.get("menu_ref") == self._cart_menu_ref(state.get("menu")):
                    summary["menu_shortfall"] = self._menu_shortfall(plan, summary)
                summary["dietary_assessment"] = self._checkout_dietary(summary, deadline)
                return {
                    "provider": "mathem", "currency": "SEK", "confirmed": False,
                    "manual_checkout_required": True,
                    "checkout_url": "https://www.mathem.se/se/cart/",
                    "summary": summary,
                    "occurrence": occurrence,
                    "message": "Review delivery, final total and payment in Mathem and complete the order there.",
                }
        with self.store.locked() as state:
            if state.get("pending_cart_change"):
                raise HouseholdError("reconcile_change before checkout")
            if not state.get("order_change"):
                self._require_menu_provider(state.get("menu"))
            if cart_ready_continuation:
                record = state.get("occurrences", {}).get(occurrence)
                if not isinstance(record, Mapping) or record.get("status") != "cart_ready":
                    raise HouseholdError("checkout occurrence is no longer cart_ready")
                if record.get("scheduler_context"):
                    scheduler_context = {**deepcopy(record["scheduler_context"]), "manual": True}
            checkout_payment = deepcopy(state["checkout_payment"])
            current_pending = state.get("pending_checkout")
            inherited_occurrence = (
                current_pending.get("occurrence")
                if occurrence is None
                and isinstance(current_pending, Mapping)
                and current_pending.get("status") == "awaiting_confirmation"
                else None
            )
            if inherited_occurrence:
                automatic_checkout = current_pending.get("automatic_checkout", True)
                scheduler_context = current_pending.get("scheduler_context")
                self._pending_scheduler_guard(state, current_pending)
                occurrence = inherited_occurrence
                occurrence_record = state.get("occurrences", {}).get(occurrence)
                if isinstance(occurrence_record, dict):
                    occurrence_record["status"] = "started"
                    occurrence_record["at"] = self._now().isoformat()
            if expired_awaiting_confirmation(current_pending, self._now()):
                state["pending_checkout"] = None
            if self._unresolved_scheduled_effects(state):
                raise HouseholdError("reconcile the original scheduled delivery effect before preparing checkout")
            self._guard_scheduled_context(state, scheduler_context)
            baseline = deepcopy(state.get("pending_checkout"))
            if baseline and baseline.get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                raise HouseholdError("reconcile the pending checkout before preparing another")
            order_change = deepcopy(state.get("order_change"))
            menu_baseline = deepcopy(state.get("menu"))
            current_usage = (
                state.get("recipe_usage", {}).get(menu_baseline.get("menu_id"))
                if isinstance(menu_baseline, Mapping)
                else None
            )
            if not order_change and isinstance(menu_baseline, Mapping) and (
                menu_baseline.get("phase") == "ordered"
                or menu_baseline.get("order_id")
                or (isinstance(current_usage, Mapping) and current_usage.get("status") == "ordered")
            ):
                raise HouseholdError("the current menu already belongs to an order; save or select a new menu before a new checkout")
            if expired_awaiting_confirmation(state.get("pending_cancellation"), self._now()):
                state["pending_cancellation"] = None
            pending_cancellation = deepcopy(state.get("pending_cancellation"))
        allow_recovery = self.provider == "meny" and not order_change and not pending_cancellation
        if order_change:
            if self.provider == "oda" and checkout_payment["method"] == "vipps":
                raise HouseholdError("Configured Oda Vipps payment supports new orders only; finish this existing-order change in Oda")
            if order_change.get("provider") != self.provider or order_change.get("status") != "editing":
                raise HouseholdError("the order change is not ready for checkout; abort or recover it first")
            if order_change.get("delivery_only") and not order_change.get("requested_delivery"):
                raise HouseholdError("Select the exact requested window before preparing a delivery-only change")
        if order_change and (self.provider in {"oda", "mathem"} or order_change.get("requested_delivery")):
            fresh_target = self._orders({"action": "get", "order_id": order_change["order_id"], "_deadline": deadline})
            if canonical(fresh_target) != canonical(order_change["before"]):
                raise HouseholdError("the target order changed; begin the order change again")
        cart = self.provider_client.call("get_cart", {}, deadline=deadline, allow_recovery=allow_recovery) if self.provider == "meny" else self.provider_client.call("get_cart", {}, deadline=deadline)
        summary = cart_summary(cart)
        if order_change and self.provider in {"oda", "mathem"} and self._cart_lines(summary)[0] != order_change.get("expected_cart_quantities", {}):
            raise HouseholdError("Oda addition cart changed outside this edit; abort with retain_cart=true and review the goods before checkout")
        cart_plan_baseline = None
        if not order_change and isinstance(menu_baseline, Mapping):
            cart_gate = self._cart_checkout_gate(summary, menu_baseline)
            if cart_gate is not None:
                return cart_gate
            cart_plan_baseline = deepcopy(self.store.read().get("cart_plan"))
        delivery_change = bool(order_change and order_change.get("requested_delivery"))
        if delivery_change and self.provider == "meny" and self._cart_lines(summary)[0] != order_change.get("starting_cart_quantities"):
            raise HouseholdError("A delivery-only change must preserve the original MENY cart products and quantities")
        retail_addition = self.provider in {"oda", "mathem"} and bool(order_change) and not delivery_change
        if retail_addition:
            summary["delivery"] = {
                "display": order_change["before"]["order"]["deliverySlotDisplay"],
                "address": require_order_binding(order_change.get("binding"))["receipt_address"],
                "order_id": order_change["order_id"], "selection_origin": "existing_order",
            }
        if self.provider in {"oda", "mathem"} and not delivery_change and not retail_addition:
            delivery = summary.get("delivery")
            if not isinstance(delivery, Mapping) or not delivery.get("display"):
                raise HouseholdError("select a delivery slot before checkout")
            address = delivery.get("address")
            if not isinstance(address, str) or not address.strip():
                raise HouseholdError("select a delivery address before checkout")
            summary["delivery"]["address"] = unicodedata.normalize("NFC", " ".join(address.split()))
        before = self.provider_client.call("get_orders", {"page": 1, "size": 20}, deadline=deadline, allow_recovery=allow_recovery) if self.provider == "meny" else self.provider_client.call("get_orders", {"page": 1, "size": 20}, deadline=deadline)
        if self.browser is None:
            raise HouseholdError("Oda checkout browser is not configured; configure the dedicated browser and sign into the same intended Oda account as OAuth before requesting checkout again")
        with self._browser_operation(deadline):
            state = self.store.read()
            if (state.get("pending_cancellation") or {}).get("status") in {"clicking", "uncertain"}:
                raise HouseholdError("reconcile the pending cancellation before checkout")
            if delivery_change and self.provider in {"oda", "mathem"}:
                review = self.browser.review_delivery_change(
                    order_change["order_id"],
                    order_change["before"]["order"],
                    order_change["requested_delivery"],
                    deadline=deadline,
                    **({"expected_binding": require_order_binding(order_change.get("binding"))} if self.provider in {"oda", "mathem"} else {}),
                )
                reviewed_summary = review.get("summary")
                if not isinstance(reviewed_summary, Mapping):
                    raise HouseholdError("Oda delivery change returned no verified summary")
                summary = deepcopy(dict(reviewed_summary))
            elif order_change and self.provider in {"oda", "mathem"}:
                review = self.browser.review_order_change(
                    cart,
                    order_change["order_id"],
                    order_change["before"]["order"],
                    deadline=deadline,
                    **({"expected_binding": require_order_binding(order_change.get("binding"))} if self.provider in {"oda", "mathem"} else {}),
                )
            else:
                if self.provider == "meny":
                    review = self.browser.review_checkout(
                        cart,
                        order_change=order_change,
                        deadline=deadline,
                        allow_recovery=allow_recovery,
                    )
                else:
                    try:
                        review = self.browser.review_checkout(cart, deadline=deadline, **({"payment": checkout_payment} if self.provider == "oda" else {}))
                    except OdaCheckoutMismatchError:
                        refreshed_cart = self.provider_client.call("get_cart", {}, deadline=deadline)
                        refreshed_summary = cart_summary(refreshed_cart)
                        if canonical(refreshed_summary) == canonical(summary):
                            raise
                        if isinstance(menu_baseline, Mapping):
                            cart_gate = self._cart_checkout_gate(refreshed_summary, menu_baseline)
                            if cart_gate is not None:
                                return cart_gate
                        raise HouseholdError("Oda cart or delivery changed while preparing checkout; prepare a new summary")
            if self.provider == "meny":
                reviewed_summary = review.get("summary")
                if not isinstance(reviewed_summary, Mapping):
                    raise HouseholdError("MENY checkout returned no verified summary")
                summary = deepcopy(dict(reviewed_summary))
                refreshed_cart = self.provider_client.call(
                    "get_cart",
                    {},
                    deadline=deadline,
                    allow_recovery=allow_recovery,
                )
                refreshed_summary = cart_summary(refreshed_cart)
                reviewed_lines = sorted(
                    (str(item["product_id"]), int(item["quantity"]))
                    for item in summary["items"]
                )
                refreshed_lines = sorted(
                    (str(item["product_id"]), int(item["quantity"]))
                    for item in refreshed_summary["items"]
                )
                if refreshed_summary["count"] != summary["count"] or refreshed_lines != reviewed_lines:
                    raise HouseholdError("MENY cart items changed after the delivery reservation")
                cart = refreshed_cart
                reviewed_delivery = summary.get("delivery")
                if not isinstance(reviewed_delivery, Mapping):
                    raise HouseholdError("MENY checkout returned no verified delivery")
                review_cart = dict(cart)
                review_cart["delivery"] = deepcopy(dict(reviewed_delivery))
                for _ in range(3):
                    stable_review = self.browser.review_checkout(
                        review_cart,
                        order_change=order_change,
                        deadline=deadline,
                        allow_recovery=allow_recovery,
                    )
                    stable_summary = stable_review.get("summary")
                    if not isinstance(stable_summary, Mapping):
                        raise HouseholdError("MENY checkout returned no verified summary")
                    if meny_checkout_reviews_match(review, stable_review):
                        review = stable_review
                        summary = deepcopy(dict(stable_summary))
                        break
                    review = stable_review
                    summary = deepcopy(dict(stable_summary))
                else:
                    raise HouseholdError("MENY checkout summary did not settle")
            elif retail_addition:
                # Native checkout can populate the original order's delivery
                # in a previously slotless addition cart. Freeze that result,
                # while keeping the reviewed goods and inherited delivery fixed.
                refreshed_cart = self.provider_client.call("get_cart", {}, deadline=deadline)
                original_cart_summary = cart_summary(cart)
                refreshed_summary = cart_summary(refreshed_cart)
                before_delivery = original_cart_summary.pop("delivery")
                after_delivery = refreshed_summary.pop("delivery")
                if canonical(original_cart_summary) != canonical(refreshed_summary):
                    raise HouseholdError("Addition cart changed while preparing checkout; prepare a new summary")
                binding = require_order_binding(order_change.get("binding"))
                normalize_address = lambda value: unicodedata.normalize("NFC", " ".join(value.split())) if isinstance(value, str) else None
                expected_address = normalize_address(binding["receipt_address"])
                for source in (cart, refreshed_cart):
                    address = source.get("deliveryAddress")
                    if address is not None and normalize_address(address) != expected_address:
                        raise HouseholdError("Addition cart address differs from the original order")
                if before_delivery is not None and canonical(before_delivery) != canonical(after_delivery):
                    raise HouseholdError("Addition delivery changed while preparing checkout")
                if after_delivery is not None:
                    expected_window = oda_delivery_signature(order_change["before"]["order"]["deliverySlotDisplay"], provider=self.provider)
                    if (expected_window is None
                            or oda_delivery_signature(after_delivery.get("display"), provider=self.provider) != expected_window
                            or normalize_address(after_delivery.get("address")) != expected_address):
                        raise HouseholdError("Addition delivery differs from the original order")
                cart = refreshed_cart
            elif not order_change:
                refreshed_cart = self.provider_client.call("get_cart", {}, deadline=deadline)
                refreshed_summary = cart_summary(refreshed_cart)
                if canonical(refreshed_summary) != canonical(summary):
                    if isinstance(menu_baseline, Mapping):
                        cart_gate = self._cart_checkout_gate(refreshed_summary, menu_baseline)
                        if cart_gate is not None:
                            return cart_gate
                    raise HouseholdError("Oda cart or delivery changed while preparing checkout; prepare a new summary")
                cart = refreshed_cart
                summary = refreshed_summary
            if self.provider in {"oda", "mathem"} and isinstance(review.get("amounts"), Mapping):
                if delivery_change:
                    summary["amounts"] = deepcopy(dict(review["amounts"]))
                else:
                    amount_cart = dict(cart)
                    amount_cart["amounts"] = deepcopy(dict(review["amounts"]))
                    reviewed_amounts = cart_summary(amount_cart).get("amounts")
                    if not isinstance(reviewed_amounts, Mapping):
                        raise HouseholdError("Oda checkout returned no verified amounts")
                    summary["amounts"] = deepcopy(dict(reviewed_amounts))
            payment_display = None
            if self.provider in {"oda", "mathem"}:
                payment_display = str((review.get("summary") or {}).get("payment") or review.get("payment_display") or "")
                vipps = self.provider == "oda" and not order_change and checkout_payment["method"] == "vipps"
                if (vipps and payment_display != "Vipps") or (not vipps and re.fullmatch(r"•••• \d{4}", payment_display) is None):
                    raise HouseholdError("checkout returned no verified configured payment identity")
                summary["payment"] = payment_display
                if self.provider == "oda" and not order_change:
                    summary["payment_method"] = checkout_payment["method"]
            if retail_addition:
                if self.provider == "mathem" or isinstance(review.get("order_amounts"), Mapping):
                    summary["order_amounts"] = deepcopy(review["order_amounts"])
            elif delivery_binding is None:
                delivery_binding = self._current_delivery_choice(
                    occurrence=occurrence, deadline=deadline, allow_recovery=allow_recovery,
                    scope_cart=cart,
                )
            else:
                delivery_binding = self._unchanged_delivery_binding(
                    delivery_binding,
                    occurrence=occurrence,
                    deadline=deadline,
                    allow_recovery=allow_recovery,
                    scope_cart=cart,
                )
            price_review = self._delivery_price_review(summary, order_change)
            if price_review is not None:
                summary["delivery_change"] = price_review
            if not retail_addition:
                summary = self._bind_delivery_summary(summary, delivery_binding, provider=self.provider,
                                                      discount_breakdown=review.get("discount_breakdown"))
            summary["menu_shortfall"] = self._menu_shortfall(cart_plan_baseline, summary)
            if not order_change:
                summary["menu_attribution"] = self._checkout_menu_attribution(menu_baseline, cart_plan_baseline)
                if menu_baseline and summary["menu_attribution"] == "cart_only":
                    summary["menu_coverage"] = "not_assessed"
            if self.provider == "meny":
                review = deepcopy(dict(review))
                review["delivery_guard"] = deepcopy(dict(delivery_binding))
            dietary_assessment = self._checkout_dietary(summary, deadline)
            summary["dietary_assessment"] = dietary_assessment
            confirmation_id = secrets.token_urlsafe(18)
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) != canonical(baseline):
                    raise HouseholdError("checkout state changed while preparing the summary")
                if canonical(state.get("order_change")) != canonical(order_change):
                    raise HouseholdError("order change state changed while preparing the summary")
                if canonical(state.get("menu")) != canonical(menu_baseline):
                    raise HouseholdError("menu changed while preparing checkout; prepare a new summary")
                if cart_plan_baseline is not None and canonical(state.get("cart_plan")) != canonical(cart_plan_baseline):
                    raise HouseholdError("cart plan changed while preparing checkout; prepare a new summary")
                self._guard_scheduled_context(state, scheduler_context)
                if state["checkout_payment"] != checkout_payment:
                    raise HouseholdError("payment preference changed while preparing checkout; prepare a new summary")
                state["pending_checkout"] = {
                    "checkout_payment": checkout_payment,
                    "scheduler_context": deepcopy(scheduler_context),
                    "status": "awaiting_confirmation",
                    "confirmation_id": confirmation_id,
                    "cart": cart,
                    "summary": summary,
                    "orders_before": before,
                    "browser_review": review,
                    "expires_at": (self._now() + timedelta(minutes=20)).isoformat(),
                    "menu": menu_baseline,
                    "cart_plan": cart_plan_baseline,
                    "menu_ref": {
                        "menu_id": menu_baseline.get("menu_id"),
                        "revision": menu_baseline.get("revision"),
                        "digest": menu_baseline.get("digest"),
                    } if isinstance(menu_baseline, Mapping) else None,
                    "occurrence": occurrence,
                    "automatic_checkout": automatic_checkout,
                    "dietary_assessment": dietary_assessment,
                    "order_change": order_change,
                    "delivery_reselections": delivery_reselections,
                }
                if occurrence:
                    occurrence_record = state.get("occurrences", {}).get(occurrence)
                    if isinstance(occurrence_record, dict):
                        occurrence_record["status"] = "awaiting_confirmation"
        return {
            "confirmation_id": confirmation_id,
            "confirmation_policy": self.confirmation_policy,
            "confirmation_required": price_review["confirmation_required"] if price_review else self.confirmation_policy == "fresh",
            "summary": {
                **deepcopy(summary),
                **({"payment": payment_display} if self.provider in {"oda", "mathem"} else {}),
            },
            "order_change": {"order_id": order_change["order_id"], "kind": order_change.get("kind")} if order_change else None,
            "next": (
                "Show the exact window, original/new full totals, difference and payable amount. Ask once to approve this higher total, then confirm this exact confirmation_id with delivery_price_approved=true."
                if price_review and price_review["confirmation_required"] else
                "The requested window and applicable price limit authorize this unchanged-goods delivery change. Show the fresh original/new totals and payable amount, then confirm this confirmation_id without another price question. Provider/device approval still applies."
                if price_review else
                "Ask once for an explicit final confirmation of this unchanged cart, total, delivery and target order, then pass this confirmation_id unchanged."
                if self.confirmation_policy == "fresh"
                else "Standing authorization is configured. If the current request explicitly asks to order, pay or check out, call checkout confirm now with this confirmation_id; do not ask again."
            ),
        }

    def _checkout_recovery_target(self, pending, deadline):
        if self.provider not in {"oda", "mathem"} or self.browser is None:
            raise HouseholdError("Merchant payment recovery is unavailable for this installation")
        if pending.get("order_change"):
            change = pending["order_change"]
            failure = pending.get("payment_failure") or {}
            if (self.provider != "mathem" or change.get("requested_delivery")
                    or failure.get("payment_failed") is not True
                    or failure.get("order_id") != change["order_id"]
                    or not failure.get("order_change_id")):
                raise HouseholdError("The original merchant change and its goods must be bound before addition recovery")
            order_id = change["order_id"]
            order = self.provider_client.call("get_order", {"order_number": order_id}, deadline=deadline)
            tracking = self.provider_client.call("order_tracking", {"order_number": order_id}, deadline=deadline)
            if (str(order.get("orderNumber") or order.get("order_number") or "") != order_id
                    or str(tracking.get("orderNumber") or tracking.get("order_id") or "") != order_id
                    or tracking.get("status") != "unpaid_order_change"):
                raise HouseholdError("The original addition is no longer unpaid; reconcile it before recovery")
            # The paid base must still be unchanged. Added goods are bound by
            # the retry target captured during the original validated submit.
            if not oda_order_matches_addition(change["before"]["order"], order,
                    {"items": [], "total": 0}, provider=self.provider):
                raise HouseholdError("The original paid order changed before addition recovery")
            return order_id
        retained_order_id = pending.get("unpaid_order_id")
        failed_order_id = ((pending.get("payment_failure") or {}).get("order_id")
                           if self.provider == "mathem" else None)
        if (self.provider == "oda"
                and (pending.get("checkout_payment") or {}).get("method") == "vipps"
                and (retained_order_id is None
                     or pending.get("unpaid_order_binding_source") != "oda_checkout_pay_response")):
            raise HouseholdError(
                "Recovery has no order identity bound before the original Vipps request; "
                "preserve this attempt for manual reconciliation"
            )
        if failed_order_id is not None:
            order_id = safe_order_id(failed_order_id)
        elif retained_order_id is not None:
            order_id = safe_order_id(retained_order_id)
        else:
            if pending.get("candidate_binding_ambiguous"):
                raise HouseholdError("Recovery cannot resolve an earlier ambiguous merchant-order set")
            before = {str(row.get("orderNumber") or row.get("order_number") or row.get("id") or "")
                      for row in pending["orders_before"].get("orders", [])}
            orders = self.provider_client.call("get_orders", {"page": 1, "size": 20}, deadline=deadline)
            candidates = [row for row in orders.get("orders", [])
                          if str(row.get("orderNumber") or row.get("order_number") or row.get("id") or "") not in before]
            if len(candidates) != 1:
                raise HouseholdError("Recovery cannot identify one original merchant order; reconcile this attempt")
            order_id = safe_order_id(str(candidates[0].get("orderNumber") or candidates[0].get("order_number") or candidates[0].get("id") or ""))
        if pending.get("payment_failure") and pending["payment_failure"]["order_id"] != order_id:
            raise HouseholdError("The unpaid order differs from the original payment failure")
        order = self.provider_client.call("get_order", {"order_number": order_id}, deadline=deadline)
        tracking = self.provider_client.call("order_tracking", {"order_number": order_id}, deadline=deadline)
        if (str(order.get("orderNumber") or order.get("order_number") or "") != order_id
                or str(tracking.get("orderNumber") or tracking.get("order_id") or "") != order_id
                or tracking.get("status") != "unpaid_order"):
            raise HouseholdError("The original order is no longer unpaid; reconcile it before recovery")
        # MCP omits an unpaid order's address. The native retry review below
        # independently verifies it and the original account reference.
        addressed = order if "deliveryAddress" in order or "delivery_address" in order else {
            **order, "deliveryAddress": pending["summary"]["delivery"]["address"]}
        if not order_matches_checkout(addressed, pending["summary"], provider=self.provider):
            raise HouseholdError("The unpaid order goods, amount or delivery differ from the original checkout")
        return order_id

    def _bind_oda_vipps_order_before_request(self, pending, response_order_id, deadline) -> str:
        """Verify the exact Oda checkout-response order before hosted Vipps Next."""

        candidate_id = safe_order_id(response_order_id)
        before = {
            str(row.get("orderNumber") or row.get("order_number") or row.get("id") or "")
            for row in pending["orders_before"].get("orders", []) if isinstance(row, Mapping)
        }
        if candidate_id in before:
            raise HouseholdError("The Oda checkout response reused an earlier order; do not send payment")
        for attempt in range(30):
            try:
                order = self.provider_client.call(
                    "get_order", {"order_number": candidate_id}, deadline=deadline
                )
                tracking = self.provider_client.call(
                    "order_tracking", {"order_number": candidate_id}, deadline=deadline
                )
                details_id = str(order.get("orderNumber") or order.get("order_number") or order.get("id") or "")
                tracking_id = str(tracking.get("orderNumber") or tracking.get("order_number") or tracking.get("order_id") or tracking.get("id") or "")
                addressed = order if "deliveryAddress" in order or "delivery_address" in order else {
                    **order, "deliveryAddress": pending["summary"]["delivery"]["address"],
                }
                if (candidate_id == details_id == tracking_id
                        and tracking.get("status") == "unpaid_order"
                        and order_matches_checkout(addressed, pending["summary"], provider="oda")):
                    return candidate_id
            except HouseholdError:
                pass
            if attempt < 29:
                if deadline is not None and time.monotonic() + 0.5 >= deadline:
                    break
                time.sleep(0.5)
        raise HouseholdError("The Oda order does not match the reviewed Vipps checkout; do not send payment")

    def _checkout_recovery_prepare(self, deadline, checkout_payment=None):
        with self._browser_operation(deadline):
            return self._checkout_recovery_prepare_unlocked(deadline, checkout_payment)

    @staticmethod
    def _recovery_browser_options(pending):
        if not pending.get("order_change"):
            return {}
        return {"addition": {**pending["order_change"],
                             "order_change_id": pending["payment_failure"]["order_change_id"]}}

    def _recovery_summary(self, pending, review, assessment):
        summary = {**deepcopy(pending["summary"]), "dietary_assessment": assessment,
                   "payment": review["payment_display"],
                   "payment_method": review["payment_choice"]["method"]}
        if self.provider == "mathem" and not pending.get("order_change"):
            # Show the retry's actual rows, including absent delivery/credit
            # rows, while preserving the original review in the journal.
            summary["amounts"] = {
                key: ({label: amount / 100 for label, amount in value.items()} if isinstance(value, Mapping)
                      else value / 100 if value is not None else None)
                for key, value in review["amounts_minor"].items() if key != "discount_breakdown"
            }
            summary["discount_breakdown"] = {
                key: value / 100 if value is not None else None
                for key, value in review["amounts_minor"]["discount_breakdown"].items()
            }
        overview = review["amounts_minor"]["provider_total"]
        if pending.get("order_change") and overview != money_cents(summary["total"]):
            summary["merchant_summary_total"] = overview / 100
        return summary

    def _checkout_recovery_prepare_unlocked(self, deadline, checkout_payment=None):
        pending = deepcopy(self.store.read().get("pending_checkout"))
        if not pending or pending.get("status") not in {"clicking", "uncertain", "awaiting_user_payment"}:
            raise HouseholdError("No original dispatched checkout is pending recovery")
        active_attempt = pending.get("recovery") or pending
        if (self.provider == "oda" and (pending.get("checkout_payment") or {}).get("method") == "vipps"
                and (active_attempt is pending or active_attempt.get("status") != "awaiting_confirmation")
                and active_attempt.get("vipps_request_status") not in {"expired", "verifying"}
                and not active_attempt.get("payment_failure")):
            return self._checkout_reconcile_unlocked(deadline, pending["confirmation_id"])
        child = pending.get("recovery")
        if child and child.get("status") != "awaiting_confirmation":
            result = self._checkout_reconcile_unlocked(deadline, pending["confirmation_id"])
            pending = deepcopy(self.store.read().get("pending_checkout"))
            child = (pending or {}).get("recovery")
            if (not result.get("recovery_preparation_available") or not child
                    or not child.get("payment_failure")):
                return result
        authentication = self._checkout_authentication_wait(pending, deadline)
        if authentication:
            return authentication
        order_id = self._checkout_recovery_target(pending, deadline)
        binding = require_order_binding(pending["order_change"]["binding"] if pending.get("order_change") else {
            "account_reference_digest": pending["browser_review"].get("account_reference_digest"),
            "receipt_address": pending["summary"]["delivery"]["address"],
        })
        payment = checkout_payment_settings(checkout_payment, self.provider) if checkout_payment is not None else pending["checkout_payment"]
        review = self.browser.review_payment_recovery(pending["cart"], order_id,
            payment=payment, expected_binding=binding, deadline=deadline,
            **self._recovery_browser_options(pending))
        if checkout_payment is None and review["payment_display"] != pending["browser_review"]["payment_display"]:
            raise HouseholdError("Recovery payment differs from the original reviewed method")
        expected_amounts = _oda_checkout_amounts_minor(pending["browser_review"]["amounts"], provider=self.provider)
        expected_breakdown = None
        if self.provider == "mathem" and not pending.get("order_change"):
            breakdown = pending["browser_review"].get("discount_breakdown")
            if not isinstance(breakdown, Mapping):
                raise HouseholdError("Recovery discount rows differ from the original review")
            expected_breakdown = {key: round(value * 100) if value is not None else None for key, value in breakdown.items()}
            delivery = expected_amounts["delivery_price"]
            current = review["amounts_minor"]
            slot = (pending["summary"].get("delivery") or {}).get("slot") or {}
            # Mathem's retry omits the gross delivery row and its full credit
            # for an explicitly free slot. No payable component changes.
            if (delivery is not None and delivery > 0
                    and expected_amounts["discounts"] == -delivery
                    and expected_breakdown == {"product_discount": None, "delivery_discount": -delivery}
                    and slot.get("price_kind") == "exact" and slot.get("price_ore") == 0
                    and current["delivery_price"] is None and current["discounts"] is None
                    and current.get("discount_breakdown") == {"product_discount": None, "delivery_discount": None}):
                expected_amounts.update(delivery_price=None, discounts=None)
                expected_breakdown = {"product_discount": None, "delivery_discount": None}
        if pending.get("order_change"):
            # The retry overview total is not the calculated payable shown by
            # the final button. Freeze it separately without inventing a fee.
            expected_amounts.pop("provider_total")
        if {key: review["amounts_minor"].get(key) for key in expected_amounts} != expected_amounts:
            raise HouseholdError("Recovery fees differ from the original reviewed amounts")
        if self.provider == "mathem" and not pending.get("order_change"):
            if expected_breakdown != review["amounts_minor"].get("discount_breakdown"):
                raise HouseholdError("Recovery discount rows differ from the original review")
        assessment = self._checkout_dietary(pending["summary"], deadline)
        child = {"confirmation_id": secrets.token_urlsafe(24),
                 "expires_at": (self._now() + timedelta(minutes=20)).isoformat(),
                 "status": "awaiting_confirmation", "order_id": order_id,
                 "browser_review": review, "dietary_assessment": assessment}
        with self.store.locked() as state:
            if canonical(state.get("pending_checkout")) != canonical(pending):
                raise HouseholdError("The pending checkout changed during recovery preparation")
            previous = pending.get("recovery")
            if previous and previous.get("payment_failure"):
                # Only a positively failed payment can be replaced. Keep its
                # complete private evidence outside the replayed result.
                self._store_protected_result(state, previous["confirmation_id"], "checkout", {
                    "confirmed": False, "payment_failed": True, "retry_allowed": False,
                    "confirmation_id": previous["confirmation_id"],
                    "original_confirmation_id": pending["confirmation_id"], "order_id": previous["order_id"],
                    "next": "This payment failed and was superseded by a fresh recovery review. Use the current confirmation; never resend this payment.",
                }, target_id=previous["order_id"])
                state["protected_results"][previous["confirmation_id"]]["failed_attempt"] = deepcopy(previous)
            state["pending_checkout"]["recovery"] = child
        return {"confirmed": False, "recovery": True, "order_id": order_id,
                "confirmation_id": child["confirmation_id"], "original_confirmation_id": pending["confirmation_id"],
                "confirmation_policy": self.confirmation_policy, "confirmation_required": True,
                "summary": self._recovery_summary(pending, review, assessment),
                "next": "Review this same merchant order and authorize its recovery payment. Confirm only with this fresh confirmation_id and actually reviewed dietary finding IDs. No goods have been restaged or payment sent."}

    def _checkout_recovery_confirm(self, pending, deadline, request):
        confirmation_id = pending["recovery"]["confirmation_id"]
        with self._browser_operation(deadline):
            state = self.store.read()
            recovered = self._read_protected_result(state, confirmation_id, "checkout")
            if recovered:
                return recovered
            pending = deepcopy(state.get("pending_checkout"))
            if not pending or (pending.get("recovery") or {}).get("confirmation_id") != confirmation_id:
                raise HouseholdError("Recovery confirmation is no longer current")
            child = pending["recovery"]
            if child["status"] != "awaiting_confirmation":
                return self._checkout_reconcile_unlocked(deadline, child["confirmation_id"])
            authentication = self._checkout_authentication_wait(pending, deadline)
            if authentication:
                return authentication
            if self._now() >= datetime.fromisoformat(child["expires_at"]):
                raise HouseholdError("Recovery confirmation expired; prepare recovery again for the original order")
            if self.store.read()["checkout_payment"] != pending["checkout_payment"]:
                raise HouseholdError("Payment preference changed during recovery")
            assessment = self._checkout_dietary(pending["summary"], deadline)
            if assessment != child["dietary_assessment"]:
                return {"confirmed": False, "reprepared": True, **self._checkout_recovery_prepare_unlocked(deadline, child["browser_review"]["payment_choice"])}
            recovery_summary = self._recovery_summary(pending, child["browser_review"], assessment)
            gate_pending = {**pending, "confirmation_id": child["confirmation_id"],
                            "dietary_assessment": assessment, "summary": recovery_summary}
            gate = self._dietary_checkout_gate(gate_pending, request)
            if gate:
                return {**gate, "confirmation_id": child["confirmation_id"],
                        "summary": recovery_summary}

            dispatch_claimed = False
            dispatched = deepcopy(pending)
            vipps_dispatched = None
            dispatched["recovery"]["status"] = "clicking"
            if child["browser_review"]["payment_choice"]["method"] == "saved_card":
                dispatched["recovery"]["authentication_unresolved"] = True

            def before_click():
                nonlocal dispatch_claimed
                if self._checkout_recovery_target(pending, deadline) != child["order_id"]:
                    raise HouseholdError("Recovery merchant target changed")
                if self._checkout_dietary(pending["summary"], deadline) != child["dietary_assessment"]:
                    raise HouseholdError("Recovery dietary findings changed; prepare a new review")
                with self.store.locked() as state:
                    if canonical(state.get("pending_checkout")) != canonical(pending):
                        raise HouseholdError("Recovery state changed before payment")
                    if self._now() >= datetime.fromisoformat(child["expires_at"]):
                        raise HouseholdError("Recovery confirmation expired before payment")
                    self._pending_scheduler_guard(state, pending)
                    state["pending_checkout"]["recovery"] = deepcopy(dispatched["recovery"])
                dispatch_claimed = True

            def before_vipps_request(context):
                nonlocal vipps_dispatched
                if (not isinstance(context, Mapping)
                        or context.get("order_id") != child["order_id"]):
                    raise HouseholdError("Recovery Vipps response belongs to a different Oda order")
                with self.store.locked() as state:
                    if not dispatch_claimed or canonical(state.get("pending_checkout")) != canonical(dispatched):
                        raise HouseholdError("Recovery changed before the Vipps request")
                    current = state["pending_checkout"]["recovery"]
                    current["vipps_request_status"] = "dispatching"
                    current["vipps_request_attempted_at"] = self._now().isoformat()
                    current["vipps_request_context"] = deepcopy(context)
                    vipps_dispatched = deepcopy(state["pending_checkout"])

            try:
                submit_result = self.browser.submit_payment_recovery(
                    pending["cart"], child["browser_review"], before_click,
                    deadline=deadline,
                    before_vipps_request=(before_vipps_request if self.provider == "oda"
                                          and child["browser_review"]["payment_choice"]["method"] == "vipps" else None),
                    **self._recovery_browser_options(pending),
                )
                context = submit_result.get("authentication_context") if isinstance(submit_result, Mapping) else None
                unresolved = isinstance(submit_result, Mapping) and submit_result.get("authentication_unresolved") is True
                vipps_request_sent = (self.provider == "oda"
                                      and child["browser_review"]["payment_choice"]["method"] == "vipps"
                                      and isinstance(submit_result, Mapping)
                                      and submit_result.get("vipps_request_sent") is True)
                with self.store.locked() as state:
                    expected_dispatched = vipps_dispatched or dispatched
                    if not dispatch_claimed or canonical(state.get("pending_checkout")) != canonical(expected_dispatched):
                        raise HouseholdError("Recovery changed after its payment dispatch")
                    current = state["pending_checkout"]["recovery"]
                    if context:
                        current.pop("authentication_unresolved", None)
                        current["authentication_context"] = deepcopy(context)
                    if unresolved:
                        current["authentication_unresolved"] = True
                    if vipps_request_sent:
                        current["status"] = "awaiting_user_payment"
                        current["vipps_request_status"] = "sent"
                        current["payment_requested_at"] = self._now().isoformat()
            except CheckoutPreconditionError:
                # This exception guarantees no final click. Discard only the
                # recovery review; the original uncertain purchase stays intact.
                with self.store.locked() as state:
                    current = state.get("pending_checkout")
                    expected = (vipps_dispatched or dispatched) if dispatch_claimed else pending
                    if canonical(current) == canonical(expected):
                        current.pop("recovery")
                raise
            if vipps_request_sent:
                return {
                    "confirmed": False,
                    "recovery": True,
                    "order_id": child["order_id"],
                    "confirmation_id": child["confirmation_id"],
                    "original_confirmation_id": pending["confirmation_id"],
                    "summary": recovery_summary,
                    "awaiting_user_payment": True,
                    "payment_method": "vipps",
                    "payment_request_sent": True,
                    "payment": self._payment_evidence("unpaid_order"),
                    "retry_allowed": False,
                    "next": "Approve this exact Oda recovery payment in Vipps, then reconcile this same confirmation. Do not submit again.",
                }
            return self._checkout_reconcile_unlocked(deadline, child["confirmation_id"])

    def _checkout_confirm(self, deadline: float | None = None, confirmation_id: str = "", *, request=None) -> dict[str, Any]:
        with self.store.locked() as state:
            pending = deepcopy(state.get("pending_checkout"))
            recovered = self._read_protected_result(state, confirmation_id, "checkout")
            current_payment = deepcopy(state["checkout_payment"])
        if recovered:
            return recovered
        if pending and pending.get("recovery"):
            if confirmation_id == pending["recovery"]["confirmation_id"]:
                return self._checkout_recovery_confirm(pending, deadline, request)
            if confirmation_id == pending["confirmation_id"]:
                return self._checkout_reconcile(deadline, confirmation_id)
        if not pending or pending["status"] != "awaiting_confirmation":
            raise HouseholdError("no fresh checkout confirmation is pending")
        if not pending.get("order_change"):
            self._require_menu_provider(pending.get("menu"))
        if confirmation_id != pending.get("confirmation_id"):
            raise HouseholdError("checkout confirmation does not match the prepared summary")
        if current_payment != pending.get("checkout_payment"):
            raise HouseholdError("payment preference changed or was not bound; prepare a new summary")
        if self._now() >= datetime.fromisoformat(pending["expires_at"]):
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) == canonical(pending):
                    state["pending_checkout"] = None
            raise HouseholdError("checkout confirmation expired")
        price_review = self._delivery_price_review(pending["summary"], pending.get("order_change"))
        if price_review is not None:
            if price_review != pending["summary"].get("delivery_change"):
                raise HouseholdError("Delivery price review changed or is missing; prepare a fresh review")
            if price_review["confirmation_required"] and not (request or {}).get("delivery_price_approved"):
                return {"confirmed": False, "confirmation_required": True, "confirmation_id": confirmation_id,
                        "summary": deepcopy(pending["summary"]),
                        "next": "The new full total exceeds the original and any authorized price limit. Obtain one approval of this exact window, difference and total, then confirm this ID with delivery_price_approved=true."}
        elif (request or {}).get("delivery_price_approved"):
            raise HouseholdError("Delivery price approval cannot authorize a different checkout operation")
        try:
            reprepared = self._revalidate_checkout_delivery(pending, deadline=deadline)
        except HouseholdError:
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) == canonical(pending):
                    state["pending_checkout"] = None
            raise
        if reprepared is not None:
            return reprepared
        cart = self.provider_client.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.provider_client.call("get_cart", {}, deadline=deadline)
        pending_change = pending.get("order_change") or {}
        expected_cart = cart_summary(pending["cart"])
        if canonical(cart_summary(cart)) != canonical(expected_cart):
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) == canonical(pending):
                    state["pending_checkout"] = None
            raise HouseholdError("cart or delivery changed; show a new summary")
        current_dietary = self._checkout_dietary(cart_summary(cart), deadline)
        if canonical(current_dietary) != canonical(pending.get('dietary_assessment')):
            return {'confirmed': False, 'reprepared': True, **self._checkout_prepare(deadline,
                occurrence=pending.get('occurrence'), automatic_checkout=pending.get('automatic_checkout', False),
                scheduler_context=pending.get('scheduler_context'), delivery_reselections=pending.get('delivery_reselections', 0))}
        gate = self._dietary_checkout_gate(pending, request)
        if gate:
            return {**gate, 'confirmation_id': pending['confirmation_id'], 'summary': pending['summary']}
        with self.store.locked() as state:
            if canonical(state.get("order_change")) != canonical(pending.get("order_change")):
                raise HouseholdError("order change changed; show a new summary")
            if pending.get("cart_plan") is not None and canonical(state.get("cart_plan")) != canonical(pending.get("cart_plan")):
                raise HouseholdError("cart plan changed; show a new summary")
        try:
            with self._browser_operation(deadline):
                with self.store.locked() as state:
                    current_pending = state.get("pending_checkout")
                    if state.get("pending_cart_change"):
                        raise HouseholdError("reconcile_change before checkout")
                    if not current_pending or current_pending.get("status") != "awaiting_confirmation" or canonical(current_pending) != canonical(pending):
                        raise HouseholdError("no fresh checkout confirmation is pending")
                    if (state.get("pending_cancellation") or {}).get("status") in {"clicking", "uncertain"}:
                        raise HouseholdError("reconcile the pending cancellation before checkout")
                    if canonical(state.get("order_change")) != canonical(pending.get("order_change")):
                        raise HouseholdError("order change changed; show a new summary")
                    if pending.get("cart_plan") is not None and canonical(state.get("cart_plan")) != canonical(pending.get("cart_plan")):
                        raise HouseholdError("cart plan changed; show a new summary")
                    if state["checkout_payment"] != pending.get("checkout_payment"):
                        raise HouseholdError("payment preference changed or was not bound; prepare a new summary")
                    self._pending_scheduler_guard(state, pending)
                    state["pending_checkout"]["status"] = "clicking"
                    if (self.provider in {"oda", "mathem"}
                            and (not pending_change.get("requested_delivery") or price_review["payable_ore"] > 0)
                            and (pending_change or pending["checkout_payment"]["method"] == "saved_card")):
                        # Persist before browser dispatch so a crash or lost
                        # first response cannot enable an overlapping payment.
                        state["pending_checkout"]["authentication_unresolved"] = True
                        pending["authentication_unresolved"] = True

                def before_click() -> None:
                    # MENY's provider client is this same locked browser tab;
                    # submit_checkout performs its own exact fresh review.
                    if self.provider in {"oda", "mathem"}:
                        fresh = self.provider_client.call("get_cart", {}, deadline=deadline)
                        expected = cart_summary(pending["cart"])
                        if canonical(cart_summary(fresh)) != canonical(expected):
                            raise CheckoutPreconditionError("cart or delivery changed before the final click")
                        try:
                            if not (self.provider in {"oda", "mathem"} and pending_change and not pending_change.get("requested_delivery")):
                                self._unchanged_delivery_binding(
                                    pending["summary"]["delivery"],
                                    occurrence=str(pending.get("occurrence") or "") or None,
                                    deadline=deadline,
                                )
                        except HouseholdError as exc:
                            raise CheckoutPreconditionError(
                                f"delivery changed again before the final click: {exc}"
                            ) from exc
                    problem = self._scheduled_checkout_problem(
                        pending["summary"], str(pending.get("occurrence") or "") or None,
                        automatic=pending.get("automatic_checkout", bool(pending.get("occurrence"))),
                    )
                    if problem is not None:
                        raise CheckoutPreconditionError(
                            f"scheduled checkout stopped before the final click: {problem}"
                        )
                    if pending_change:
                        with self.store.locked() as state:
                            if canonical(state.get("order_change")) != canonical(pending_change):
                                raise CheckoutPreconditionError("order change changed before the final click")
                        if self.provider in {"oda", "mathem"}:
                            fresh_target = self._orders({"action": "get", "order_id": pending_change["order_id"], "_deadline": deadline})
                            if canonical(fresh_target) != canonical(pending_change["before"]):
                                raise CheckoutPreconditionError("the target order changed before the final click")
                    from dietary_assessment import digest
                    with self.store.locked() as state:
                        if pending.get('dietary_assessment') and digest(state['profile']['diet']) != pending['dietary_assessment']['profile_digest']:
                            raise CheckoutPreconditionError('dietary rules changed before dispatch; prepare a new summary')
                        try:
                            self._pending_scheduler_guard(state, pending)
                        except HouseholdError as exc:
                            raise CheckoutPreconditionError(str(exc)) from exc
                        if pending.get("cart_plan") is not None and canonical(state.get("cart_plan")) != canonical(pending.get("cart_plan")):
                            raise CheckoutPreconditionError("cart plan changed before the final click")
                        if state["checkout_payment"] != pending.get("checkout_payment"):
                            raise CheckoutPreconditionError("payment preference changed before dispatch; prepare a new summary")
                        current_pending = state.get("pending_checkout")
                        expected = {**pending, "status": "clicking"}
                        if not current_pending or canonical(current_pending) != canonical(expected):
                            raise CheckoutPreconditionError("checkout confirmation changed before the final click")
                    if not pending_change:
                        self._require_menu_provider(pending.get("menu"))
                    if self._now() >= datetime.fromisoformat(pending["expires_at"]):
                        raise CheckoutPreconditionError("checkout confirmation expired before the final click")

                vipps_dispatched = None

                def before_vipps_request(context) -> None:
                    nonlocal vipps_dispatched
                    order_id = safe_order_id(
                        context.get("order_id") if isinstance(context, Mapping) else None
                    )
                    if order_id in {
                        str(row.get("orderNumber") or row.get("order_number") or row.get("id") or "")
                        for row in pending["orders_before"].get("orders", []) if isinstance(row, Mapping)
                    }:
                        raise HouseholdError("The Oda checkout response reused an earlier order; do not send payment")
                    with self.store.locked() as state:
                        current = state.get("pending_checkout")
                        expected = {**pending, "status": "clicking"}
                        if not current or canonical(current) != canonical(expected):
                            raise HouseholdError("checkout changed before the Oda/Vipps request")
                        current["vipps_request_status"] = "verifying"
                        current["vipps_request_context"] = deepcopy(context)
                        current["unpaid_order_id"] = order_id
                        current["unpaid_order_binding_source"] = "oda_checkout_pay_response"
                        bound = deepcopy(current)
                    self._bind_oda_vipps_order_before_request(pending, order_id, deadline)
                    with self.store.locked() as state:
                        current = state.get("pending_checkout")
                        if not current or canonical(current) != canonical(bound):
                            raise HouseholdError("checkout changed while verifying the Oda/Vipps order")
                        current["vipps_request_status"] = "dispatching"
                        current["vipps_request_attempted_at"] = self._now().isoformat()
                        vipps_dispatched = deepcopy(current)

                if self.provider == "meny":
                    try:
                        self._unchanged_delivery_binding(
                            pending["summary"]["delivery"],
                            occurrence=str(pending.get("occurrence") or "") or None,
                            deadline=deadline,
                        )
                    except HouseholdError as exc:
                        raise CheckoutPreconditionError(
                            f"delivery changed again before the final provider action: {exc}"
                        ) from exc

                if pending_change.get("requested_delivery") and self.provider in {"oda", "mathem"}:
                    change = pending["order_change"]
                    submit_result = self.browser.submit_delivery_change(
                        change["order_id"],
                        change["before"]["order"],
                        change["requested_delivery"],
                        pending["browser_review"],
                        before_click,
                        deadline=deadline,
                    )
                elif pending.get("order_change") and self.provider in {"oda", "mathem"}:
                    change = pending["order_change"]
                    submit_result = self.browser.submit_order_change(
                        cart,
                        change["order_id"],
                        change["before"]["order"],
                        pending["browser_review"],
                        before_click,
                        deadline=deadline,
                    )
                else:
                    submit_result = self.browser.submit_checkout(
                        cart,
                        pending["browser_review"],
                        before_click,
                        order_change=pending_change or None,
                        deadline=deadline,
                    ) if self.provider == "meny" else self.browser.submit_checkout(
                        cart,
                        pending["browser_review"],
                        before_click,
                        deadline=deadline,
                        **({"before_vipps_request": before_vipps_request}
                           if pending["checkout_payment"]["method"] == "vipps" else {}),
                    )
                if self.provider == "meny" and isinstance(submit_result, Mapping) and submit_result.get("awaiting_user_payment") is True:
                    requested_at = self._now()
                    with self.store.locked() as state:
                        current_pending = state.get("pending_checkout")
                        if not current_pending or current_pending.get("status") != "clicking" or current_pending.get("browser_review") != pending.get("browser_review"):
                            raise HouseholdError("checkout state changed after the Vipps request")
                        state["pending_checkout"]["status"] = "awaiting_user_payment"
                        state["pending_checkout"]["payment_requested_at"] = requested_at.isoformat()
                        state["pending_checkout"]["payment_expires_at"] = (requested_at + MENY_VIPPS_EXPIRY_BUFFER).isoformat()
                    return {
                        "confirmed": False,
                        "awaiting_user_payment": True,
                        "payment": "vipps",
                        "retry_allowed": False,
                        "next": "Approve this exact MENY payment in Vipps, then call checkout reconcile. Do not submit again.",
                    }
                if (self.provider == "oda" and pending["checkout_payment"]["method"] == "vipps"
                        and isinstance(submit_result, Mapping) and submit_result.get("vipps_request_sent") is True):
                    requested_at = self._now()
                    with self.store.locked() as state:
                        current_pending = state.get("pending_checkout")
                        if (not current_pending or vipps_dispatched is None
                                or canonical(current_pending) != canonical(vipps_dispatched)):
                            raise HouseholdError("checkout state changed after the Oda/Vipps request")
                        state["pending_checkout"]["status"] = "awaiting_user_payment"
                        state["pending_checkout"]["vipps_request_status"] = "sent"
                        state["pending_checkout"]["payment_requested_at"] = requested_at.isoformat()
                    return {
                        "confirmed": False,
                        "confirmation_id": pending["confirmation_id"],
                        "summary": deepcopy(pending["summary"]),
                        "awaiting_user_payment": True,
                        "payment_method": "vipps",
                        "payment_request_sent": True,
                        "payment": self._payment_evidence(),
                        "retry_allowed": False,
                        "next": "Approve this exact Oda payment in Vipps, then call checkout reconcile. Do not submit again.",
                    }
                evidence = {}
                if self.provider in {"oda", "mathem"} and isinstance(submit_result, Mapping):
                    if submit_result.get("authentication_context"):
                        evidence["authentication_context"] = deepcopy(submit_result["authentication_context"])
                    if submit_result.get("authentication_unresolved") is True:
                        evidence["authentication_unresolved"] = True
                    if (submit_result.get("payment_failed") is True and self.provider == "mathem"
                            and (not pending_change or (not pending_change.get("requested_delivery")
                                 and submit_result.get("order_id") == pending_change["order_id"]))):
                        evidence["payment_failure"] = {key: submit_result[key] for key in
                            (("payment_failed", "order_id", "order_change_id") if pending_change else ("payment_failed", "order_id"))}
                if evidence or pending.get("authentication_unresolved"):
                    with self.store.locked() as state:
                        expected_dispatched = vipps_dispatched or {**pending, "status": "clicking"}
                        if canonical(state.get("pending_checkout")) != canonical(expected_dispatched):
                            raise HouseholdError("Checkout changed after the original payment dispatch")
                        if evidence.get("authentication_context") or evidence.get("payment_failure"):
                            state["pending_checkout"].pop("authentication_unresolved", None)
                        state["pending_checkout"].update(evidence)
                return self._checkout_reconcile_unlocked(deadline)
        except CheckoutPreconditionError:
            with self.store.locked() as state:
                current_pending = state.get("pending_checkout")
                if current_pending and current_pending.get("status") == "clicking" and current_pending.get("browser_review") == pending.get("browser_review"):
                    state["pending_checkout"] = None
            raise
        except HouseholdError:
            with self.store.locked() as state:
                current_pending = state.get("pending_checkout")
                if current_pending and current_pending.get("status") == "clicking" and current_pending.get("browser_review") == pending.get("browser_review"):
                    state["pending_checkout"]["status"] = "uncertain"
            raise

    def _record_order_snapshot(self, state: dict[str, Any], pending: Mapping[str, Any], order_id: str) -> None:
        order_id = safe_order_id(order_id)
        occurrence = pending.get("occurrence")
        if occurrence:
            record = state.setdefault("occurrences", {}).get(occurrence)
            context = pending.get("scheduler_context")
            if isinstance(record, dict) and (not context or record.get("attempt_id") == context["attempt_id"]):
                record["status"] = "completed"
                record["order_id"] = order_id
                record["confirmation_id"] = pending["confirmation_id"]
                record["completed_at"] = self._now().isoformat()
        if pending.get("order_change"):
            return
        attribution = pending.get("summary", {}).get("menu_attribution") or self._checkout_menu_attribution(
            pending.get("menu"), pending.get("cart_plan"))
        if attribution != "menu_bound":
            return
        if isinstance(pending.get("cart_plan"), Mapping) and canonical(state.get("cart_plan")) == canonical(pending.get("cart_plan")):
            state["cart_plan"] = None
        snapshot = deepcopy(pending.get("menu"))
        if not isinstance(snapshot, dict):
            return
        previous_order_id = snapshot.get("order_id")
        if previous_order_id and previous_order_id != order_id:
            return
        snapshot["phase"] = "ordered"
        snapshot["order_id"] = order_id
        state.setdefault("order_snapshots", {})[order_id] = snapshot
        state.setdefault("order_snapshot_times", {})[order_id] = self._now().isoformat()
        state.setdefault("order_snapshot_providers", {})[order_id] = self.provider
        menu_id = snapshot.get("menu_id")
        usage = state.setdefault("recipe_usage", {}).get(menu_id)
        if isinstance(usage, dict):
            usage["status"] = "ordered"
            usage["ordered_slot_ids"] = [s["slot_id"] for s in snapshot.get("slots", []) if s["slot_id"] not in snapshot.get("historical_slot_ids", [])]
            usage["order_id"] = order_id
            usage["updated_at"] = self._now().isoformat()
        current = state.get("menu")
        if isinstance(current, Mapping) and current.get("menu_id") == snapshot.get("menu_id") and current.get("digest") == snapshot.get("digest"):
            state["menu"] = deepcopy(snapshot)
        OrderOperations._prune_order_snapshots(state, keep_order_id=order_id)

    def _checkout_authentication_wait(self, pending, deadline):
        if self.provider not in {"oda", "mathem"}:
            return {}
        child = pending.get("recovery")
        attempt = child if child and child.get("status") != "awaiting_confirmation" else pending
        context = attempt.get("authentication_context")
        if attempt.get("payment_failure"):
            return {}
        if not context and attempt.get("authentication_unresolved") is not True:
            return {}
        observed = self.browser.checkout_payment_authentication(context, deadline=deadline) if context else None
        change = pending.get("order_change") or {}
        if (not observed and context and self.provider == "mathem"
                and not change.get("requested_delivery")):
            failure = self.browser.checkout_payment_failure(context, deadline=deadline,
                **({"expected_order_id": change["order_id"]} if change else {}))
            if failure:
                if attempt is child and (failure != pending.get("payment_failure")
                        or failure.get("order_id") != child["order_id"]):
                    raise HouseholdError("Recovery bank verification resolved to another merchant change")
                candidate = {**pending, "payment_failure": failure} if change and attempt is pending else pending
                if failure["order_id"] != self._checkout_recovery_target(candidate, deadline):
                    raise HouseholdError("The original bank verification resolved to another merchant order")
                with self.store.locked() as state:
                    if canonical(state.get("pending_checkout")) != canonical(pending):
                        raise HouseholdError("The pending checkout changed while resolving bank verification")
                    current = state["pending_checkout"]["recovery"] if attempt is child else state["pending_checkout"]
                    current["payment_failure"] = deepcopy(failure)
                    attempt["payment_failure"] = deepcopy(failure)
                return {}
        observed = observed or {}
        active = observed.get("active") is True
        challenge = active and observed.get("challenge") is True
        recovery = attempt is child
        summary = (self._recovery_summary(pending, child["browser_review"], child["dietary_assessment"])
                   if recovery else deepcopy(pending["summary"]))
        method = (child["browser_review"]["payment_choice"]["method"] if recovery else
                  "saved_card" if pending.get("order_change") else pending["checkout_payment"]["method"])
        return {
            "confirmed": False, "confirmation_id": attempt["confirmation_id"],
            **({"original_confirmation_id": pending["confirmation_id"]} if recovery else {}),
            "summary": summary, "payment_method": method, "retry_allowed": False,
            "recovery_preparation_available": False, "payment_followup_required": True,
            "awaiting_user_payment": challenge, "authentication_required": challenge,
            "authentication_status": "challenge" if challenge else "awaiting_outcome" if active else "unavailable",
            "bank_app_choice_attempted": attempt.get("bank_app_choice_attempted") is True,
            **({"authentication_mode": "bank_ui"} if challenge else {}),
            "next": (
                ("Use authenticate with this confirmation to select a supported bank-app method if the payment page asks for one. "
                 if not attempt.get("bank_app_choice_attempted") else "")
                + "Complete the matching payment approval in your bank's own app or payment page, then reconcile this same confirmation. "
                "Do not send a BankID password to the assistant. Keep the existing payment page open and do not submit again."
                if challenge else
                "The original bank verification has no confirmed outcome. Preserve its payment page and reconcile this same confirmation; do not submit again."
                if active else
                "The original bank verification can no longer be observed. Its outcome remains unknown. Reconcile this same confirmation; do not start another payment."
            ),
        }

    def _checkout_authenticate(self, deadline, confirmation_id):
        if self.provider not in {"oda", "mathem"} or not confirmation_id:
            raise HouseholdError("Bank-app authentication requires the original Oda or Mathem confirmation")
        with self._browser_operation(deadline):
            result = self._checkout_reconcile_unlocked(deadline, confirmation_id)
            if result.get("authentication_required") is not True:
                return result
            pending = deepcopy(self.store.read().get("pending_checkout"))
            child = pending.get("recovery")
            attempt = child if child and child.get("status") != "awaiting_confirmation" else pending
            if attempt["confirmation_id"] != confirmation_id:
                raise HouseholdError("Use the current payment authentication confirmation")
            if attempt.get("payment_failure") or attempt.get("bank_app_choice_attempted"):
                return result
            context = attempt.get("authentication_context")
            if not context:
                return result

            def before_choice():
                with self.store.locked() as state:
                    if canonical(state.get("pending_checkout")) != canonical(pending):
                        raise HouseholdError("The pending payment changed before selecting its bank app")
                    current = state["pending_checkout"]["recovery"] if attempt is child else state["pending_checkout"]
                    current["bank_app_choice_attempted"] = True

            choice = self.browser.choose_checkout_bank_app(context, before_choice, deadline=deadline)
            return {**self._checkout_reconcile_unlocked(deadline, confirmation_id),
                    "bank_app_method_chosen": choice.get("chosen") is True}

    def _checkout_reconcile(self, deadline: float | None = None, confirmation_id: str = "") -> dict[str, Any]:
        with self._browser_operation(deadline):
            return self._checkout_reconcile_unlocked(deadline, confirmation_id)

    def _meny_confirmation_before_navigation(self, pending, deadline):
        observed = self.browser.checkout_confirmation_order_id(deadline=deadline)
        captured = pending.get("meny_confirmation_order_id")
        if captured:
            if observed is not None and observed != captured:
                raise HouseholdError("MENY checkout confirmation changed from the retained order")
            return captured, pending
        if observed is None:
            return None, pending
        change = pending.get("order_change")
        if change:
            if observed != change["order_id"]:
                raise HouseholdError("MENY checkout confirmation does not match the changed order")
        elif observed in {
            str(item.get("orderNumber") or item.get("order_number") or item.get("id") or "")
            for item in pending["orders_before"].get("orders", []) if isinstance(item, Mapping)
        }:
            raise HouseholdError("MENY checkout confirmation belongs to an earlier order")
        # Order reads navigate away from this authenticated receipt. Retain its
        # identity first so a failed read or restart cannot lose the binding.
        updated = {**pending, "meny_confirmation_order_id": observed}
        with self.store.locked() as state:
            if canonical(state.get("pending_checkout")) != canonical(pending):
                raise HouseholdError("checkout state changed while retaining its confirmation")
            state["pending_checkout"] = updated
        return observed, updated

    def _checkout_reconcile_unlocked(self, deadline: float | None = None, confirmation_id: str = "") -> dict[str, Any]:
        with self.store.locked() as state:
            pending = deepcopy(state.get("pending_checkout"))
            recovered = self._read_protected_result(state, confirmation_id, "checkout") if confirmation_id else None
        if recovered:
            return recovered
        if confirmation_id and isinstance(pending, Mapping) and confirmation_id not in {
                pending.get("confirmation_id"), (pending.get("recovery") or {}).get("confirmation_id")}:
            raise HouseholdError("checkout reconciliation does not match the pending attempt")
        if not pending:
            raise HouseholdError("no checkout attempt is pending")
        if pending.get("status") not in {"clicking", "uncertain", "awaiting_user_payment"}:
            raise HouseholdError("checkout has not reached reconciliation")
        if pending.get("order_change"):
            return self._order_change_reconcile(pending, deadline)
        recovery_attempt = pending.get("recovery")
        active_attempt = (recovery_attempt if recovery_attempt
                          and recovery_attempt.get("status") != "awaiting_confirmation" else pending)
        active_payment = ((active_attempt.get("browser_review") or {}).get("payment_choice")
                          if active_attempt is not pending else pending.get("checkout_payment")) or {}
        oda_vipps_observation = None
        oda_vipps_active = False
        oda_vipps_expired = False
        oda_vipps_not_sent = False
        oda_vipps_bound = not (
            self.provider == "oda" and active_payment.get("method") == "vipps"
        )
        if self.provider == "oda" and active_payment.get("method") == "vipps":
            if active_attempt is pending:
                oda_vipps_bound = pending.get("unpaid_order_binding_source") == "oda_checkout_pay_response"
            else:
                recovery_context = active_attempt.get("vipps_request_context")
                oda_vipps_bound = (
                    pending.get("unpaid_order_binding_source") == "oda_checkout_pay_response"
                    or (
                        isinstance(recovery_context, Mapping)
                        and recovery_context.get("order_id") == active_attempt.get("order_id")
                        and active_attempt.get("order_id") == pending.get("unpaid_order_id")
                    )
                )
        if self.provider == "oda" and active_payment.get("method") == "vipps":
            request_status = active_attempt.get("vipps_request_status")
            if request_status in {"dispatching", "sent"}:
                context = active_attempt.get("vipps_request_context")
                observed = self.browser.checkout_vipps_request_state(context, deadline=deadline)
                oda_vipps_observation = str((observed or {}).get("status") or "unknown")
            elif request_status == "expired":
                oda_vipps_observation = "expired"
            elif request_status == "verifying" and oda_vipps_bound:
                oda_vipps_observation = "not_sent"
                oda_vipps_not_sent = True
            else:
                oda_vipps_observation = "unknown"
            oda_vipps_expired = oda_vipps_observation == "expired"
            oda_vipps_active = (not oda_vipps_expired and not oda_vipps_not_sent
                                and not active_attempt.get("payment_failure"))
        payment_expiry = pending.get("payment_expires_at")
        try:
            payment_expires_at = datetime.fromisoformat(str(payment_expiry or ""))
        except ValueError:
            payment_expires_at = None
        if (
            self.provider == "meny"
            and payment_expires_at is not None
            and payment_expires_at.tzinfo is not None
            and self._now() < payment_expires_at
            and self.browser.checkout_payment_awaiting_user(deadline=deadline)
        ):
            return {
                "confirmed": False,
                "expired": False,
                "order": None,
                "tracking": None,
                "retry_allowed": False,
                "awaiting_user_payment": True,
            }
        payment_not_dispatched = (
            self.provider == "meny"
            and pending.get("status") == "uncertain"
            and self.browser.checkout_payment_not_dispatched(pending["browser_review"], deadline=deadline)
        )
        confirmation_order_id = None
        if self.provider == "meny":
            confirmation_order_id, pending = self._meny_confirmation_before_navigation(pending, deadline)
        after = self.provider_client.call("get_orders", {"page": 1, "size": 20}, deadline=deadline)
        before_ids = {str(item.get("orderNumber") or item.get("order_number") or item.get("id") or "") for item in pending["orders_before"].get("orders", []) if isinstance(item, Mapping)}
        candidates = [item for item in after.get("orders", []) if isinstance(item, Mapping) and str(item.get("orderNumber") or item.get("order_number") or item.get("id") or "") not in before_ids]
        exact_failed_order_id = ((pending.get("payment_failure") or {}).get("order_id")
                                 if self.provider == "mathem" else None)
        if self.provider == "meny" and confirmation_order_id:
            candidates = [item for item in candidates if str(item.get("orderNumber") or item.get("order_number") or item.get("id") or "") == confirmation_order_id]
        candidate_ambiguous = (
            bool(pending.get("candidate_binding_ambiguous")) or len(candidates) > 1
        ) and not exact_failed_order_id and not confirmation_order_id
        order = None
        tracking = None
        candidate_id = ""
        details_id = ""
        tracking_id = ""
        retained_unpaid_id = pending.get("unpaid_order_id") if self.provider in {"oda", "mathem"} else None
        if exact_failed_order_id is not None:
            candidate_id = safe_order_id(exact_failed_order_id)
            matching_rows = [item for item in after.get("orders", []) if isinstance(item, Mapping)
                             and str(item.get("orderNumber") or item.get("order_number") or item.get("id") or "") == candidate_id]
            if len(matching_rows) > 1:
                raise HouseholdError("The exact failed order is ambiguous in the merchant order list")
            details = self.provider_client.call("get_order", {"order_number": candidate_id}, deadline=deadline)
            tracking = self.provider_client.call("order_tracking", {"order_number": candidate_id}, deadline=deadline)
            details_id = str(details.get("orderNumber") or details.get("order_number") or details.get("id") or "")
            tracking_id = str(tracking.get("orderNumber") or tracking.get("order_number") or tracking.get("order_id") or tracking.get("id") or "")
            order = {**(matching_rows[0] if matching_rows else {}), **details}
        elif retained_unpaid_id is not None:
            candidate_id = safe_order_id(retained_unpaid_id)
            matching_rows = [item for item in after.get("orders", []) if isinstance(item, Mapping)
                             and str(item.get("orderNumber") or item.get("order_number") or item.get("id") or "") == candidate_id]
            if len(matching_rows) > 1:
                raise HouseholdError("The retained unpaid order is ambiguous in the merchant order list")
            details = self.provider_client.call("get_order", {"order_number": candidate_id}, deadline=deadline)
            tracking = self.provider_client.call("order_tracking", {"order_number": candidate_id}, deadline=deadline)
            details_id = str(details.get("orderNumber") or details.get("order_number") or details.get("id") or "")
            tracking_id = str(tracking.get("orderNumber") or tracking.get("order_number") or tracking.get("order_id") or tracking.get("id") or "")
            order = {**(matching_rows[0] if matching_rows else {}), **details}
        if exact_failed_order_id is None and retained_unpaid_id is None and len(candidates) == 1 and not candidate_ambiguous:
            candidate_id = str(candidates[0].get("orderNumber") or candidates[0].get("order_number") or candidates[0].get("id") or "")
            details = self.provider_client.call("get_order", {"order_number": candidate_id}, deadline=deadline)
            tracking = self.provider_client.call("order_tracking", {"order_number": candidate_id}, deadline=deadline)
            details_id = str(details.get("orderNumber") or details.get("order_number") or details.get("id") or "")
            tracking_id = str(tracking.get("orderNumber") or tracking.get("order_number") or tracking.get("order_id") or tracking.get("id") or "")
            order = {**candidates[0], **details}
        tracking_status = str((tracking or {}).get("status") or "").casefold()
        receipt_order = order
        if (self.provider in {"oda", "mathem"} and order is not None and candidate_id == details_id == tracking_id
                and tracking_status in {"paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered"}):
            # Keep an unpaid checkout or bank challenge on its current page.
            # Receipt navigation is needed only for a potentially accepted order.
            address = (pending["summary"].get("delivery") or {}).get("address")
            verified = False
            # Receipt fallback may fill an omitted address, never replace explicit MCP evidence.
            addressed_order = order if "deliveryAddress" in order or "delivery_address" in order else {**order, "deliveryAddress": address}
            if (isinstance(address, str) and address.strip() and self.browser is not None
                    and order_matches_checkout(addressed_order, pending["summary"], provider=self.provider)):
                try:
                    reference = (pending.get("browser_review") or {}).get("account_reference_digest")
                    if reference:
                        binding = {"account_reference_digest": reference, "receipt_address": address}
                        verified = self.browser.read_order_binding(candidate_id, order, deadline=deadline, expected_binding=binding) == binding
                    else:
                        verified = self.browser.receipt_address_matches(candidate_id, address, deadline=deadline)
                except HouseholdError:
                    pass  # Keep this attempt uncertain; no second payment.
            if verified is True:
                receipt_order = {**order, "deliveryAddress": address}
            else:
                receipt_order = {**order, "deliveryAddress": None, "delivery_address": None}
        if self.provider == "meny":
            confirmed = (
                order is not None
                and confirmation_order_id == candidate_id
                and candidate_id == details_id == tracking_id
                and tracking_status in {"confirmed", "delivered"}
                and meny_order_matches_checkout(order, pending["summary"])
            )
        else:
            fulfillable = {"paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered"}
            confirmed = (order is not None and candidate_id and candidate_id == details_id == tracking_id
                         and tracking_status in fulfillable and oda_vipps_bound
                         and order_matches_checkout(receipt_order, pending["summary"], provider=self.provider))
        expired_unpaid = False
        candidate_matches = order is not None and meny_order_matches_checkout(order, pending["summary"])
        undispatched_retryable = payment_not_dispatched and confirmation_order_id is None and not candidates
        expirable_payment = pending.get("status") == "awaiting_user_payment" or (
            pending.get("status") == "uncertain" and payment_expiry is not None
        )
        if self.provider == "meny" and expirable_payment and not confirmed and confirmation_order_id is None and len(candidates) <= 1 and not candidate_matches:
            expiry = payment_expiry or pending.get("expires_at")
            if expiry == payment_expiry:
                expires_at = payment_expires_at
            else:
                try:
                    expires_at = datetime.fromisoformat(str(expiry or ""))
                except ValueError:
                    expires_at = None
            expired_unpaid = expires_at is not None and expires_at.tzinfo is not None and self._now() >= expires_at
        unpaid_matches = (
            self.provider in {"oda", "mathem"} and order is not None
            and candidate_id == details_id == tracking_id and tracking_status == "unpaid_order"
            and order_matches_checkout(
                order if "deliveryAddress" in order or "delivery_address" in order else {
                    **order, "deliveryAddress": (pending["summary"].get("delivery") or {}).get("address")},
                pending["summary"], provider=self.provider)
        )
        recovery_dispatched = pending.get("recovery") and pending["recovery"].get("status") != "awaiting_confirmation"
        authentication = self._checkout_authentication_wait(pending, deadline) if not confirmed else {}
        recovery_failed = bool(
            recovery_dispatched and (
                pending["recovery"].get("payment_failure")
                or (oda_vipps_expired and unpaid_matches)
            )
        )
        with self.store.locked() as state:
            if canonical(state.get("pending_checkout")) != canonical(pending):
                raise HouseholdError("checkout state changed while reconciling the order")
            if confirmed:
                order_id = str(order.get("orderNumber") or order.get("order_number") or order.get("id"))
                state["pending_checkout"] = None
                self._record_order_snapshot(state, pending, order_id)
                terminal = {
                    "confirmed": True, "order_id": order_id, "tracking_status": tracking_status,
                    "retry_allowed": False, "confirmation_id": (pending["recovery"] if recovery_dispatched else pending)["confirmation_id"],
                    **({"original_confirmation_id": pending["confirmation_id"]} if recovery_dispatched else {}),
                    "payment": self._payment_evidence(tracking_status),
                    "menu_shortfall": deepcopy(pending["summary"].get("menu_shortfall", [])),
                    "menu_attribution": pending["summary"].get("menu_attribution") or self._checkout_menu_attribution(
                        pending.get("menu"), pending.get("cart_plan")),
                    **({"menu_coverage": pending["summary"]["menu_coverage"]} if "menu_coverage" in pending["summary"] else {}),
                }
                self._store_protected_result(
                    state, pending["confirmation_id"], "checkout", terminal,
                    target_id=order_id, intent_signature=checkout_intent_signature(pending["summary"]),
                )
                if pending.get("recovery"):
                    self._store_protected_result(state, pending["recovery"]["confirmation_id"], "checkout", terminal,
                        target_id=order_id, intent_signature=checkout_intent_signature(pending["summary"]))
            elif expired_unpaid or undispatched_retryable:
                state["pending_checkout"] = None
            else:
                if candidate_ambiguous:
                    state["pending_checkout"]["candidate_binding_ambiguous"] = True
                if (unpaid_matches and self.provider in {"oda", "mathem"}
                        and not (self.provider == "mathem" and pending.get("payment_failure"))
                        and oda_vipps_bound):
                    state["pending_checkout"]["unpaid_order_id"] = safe_order_id(candidate_id)
                if oda_vipps_active:
                    state["pending_checkout"]["status"] = "awaiting_user_payment"
                else:
                    state["pending_checkout"]["status"] = "uncertain"
                if oda_vipps_expired:
                    target = (state["pending_checkout"].get("recovery")
                              if active_attempt is not pending else state["pending_checkout"])
                    target["vipps_request_status"] = "expired"
                    if active_attempt is not pending and unpaid_matches:
                        target["payment_failure"] = {
                            "payment_failed": True,
                            "order_id": safe_order_id(candidate_id),
                            "reason": "vipps_request_expired",
                        }
        return {
            **(terminal if confirmed else {}),
            "confirmed": confirmed,
            "expired": expired_unpaid,
            "order": order if confirmed else None,
            "tracking": tracking if confirmed else None,
            "retry_allowed": expired_unpaid or undispatched_retryable,
            **({"payment": self._payment_evidence(tracking_status or None)}
               if self.provider in {"oda", "mathem"} else {}),
            **({"payment_dispatched": False} if undispatched_retryable else {}),
            **({"payment_failed": True} if not confirmed and (oda_vipps_expired or recovery_failed or (pending.get("payment_failure") and not recovery_dispatched)) else {}),
            **({"payment_followup_required": True,
                "payment_method": (pending["recovery"]["browser_review"]["payment_choice"]
                                   if recovery_dispatched else pending["checkout_payment"])["method"]}
               if self.provider == "oda" and (pending.get("checkout_payment") or {}).get("method") == "vipps" and not confirmed else {}),
            **({"unpaid_order_id": candidate_id, "tracking_status": tracking_status,
                "recovery_preparation_available": True,
                "next": "The original merchant order is unpaid. Inspect its supported retry review with checkout prepare recovery=true; do not recreate the cart or resubmit the original confirmation. A new review does not send payment."}
               if unpaid_matches and oda_vipps_bound and not oda_vipps_active
               and (not recovery_dispatched or recovery_failed) and not authentication else {}),
            **({"tracking_status": tracking_status,
                "recovery_payment_unconfirmed": True,
                "next": "Recovery payment was dispatched but is not confirmed. Preserve that payment and reconcile this same attempt; the earlier failure does not authorize another payment."}
               if not confirmed and recovery_dispatched and not recovery_failed and not oda_vipps_active else {}),
            **({"awaiting_user_payment": True,
                "payment_request_state": oda_vipps_observation,
                "tracking_status": tracking_status or None,
                **({"unpaid_order_id": candidate_id} if unpaid_matches else {}),
                "next": "The original Oda/Vipps request may still be active. Approve it if present, then reconcile this same attempt. Recovery and cancellation remain blocked until the same request is paid or positively expired."}
               if not confirmed and oda_vipps_active else {}),
            **({"payment_request_state": "expired", "payment_request_expired": True}
               if not confirmed and oda_vipps_expired else {}),
            **({"payment_request_state": "not_sent", "payment_dispatched": False}
               if not confirmed and oda_vipps_not_sent else {}),
            **({"payment_followup_required": True,
                "payment_method": "vipps",
                "next": "The original Oda/Vipps payment is not confirmed. Check its original payment page and complete any requested approval, then reconcile this same attempt. Do not submit again."}
               if self.provider == "oda" and (pending.get("checkout_payment") or {}).get("method") == "vipps"
               and not confirmed and tracking_status != "unpaid_order" and not recovery_dispatched
               and not oda_vipps_active else {}),
            **({"tracking_status": tracking_status or None,
                "payment": self._payment_evidence(tracking_status or None),
                "next": "The original Mathem payment is not yet confirmed. Preserve its payment page and any bank approval; reconcile this same attempt without submitting again."}
               if self.provider == "mathem" and not confirmed and tracking_status != "unpaid_order" and not recovery_dispatched else {}),
            **authentication,
        }

    def _order_change_reconcile(self, pending: Mapping[str, Any], deadline: float | None = None) -> dict[str, Any]:
        change = pending["order_change"]
        recovery_dispatched = pending.get("recovery") and pending["recovery"].get("status") != "awaiting_confirmation"
        binding = require_order_binding(change.get("binding")) if self.provider in {"oda", "mathem"} else None
        order_id = change["order_id"]
        confirmation_order_id = order_id
        if self.provider == "meny":
            confirmation_order_id, pending = self._meny_confirmation_before_navigation(pending, deadline)
        current = self._orders({"action": "get", "order_id": order_id, "_deadline": deadline})
        current_id = str((current.get("order") or {}).get("orderNumber") or (current.get("order") or {}).get("order_number") or "")
        tracking_id = str((current.get("tracking") or {}).get("order_id") or (current.get("tracking") or {}).get("orderNumber") or "")
        status = str((current.get("tracking") or {}).get("status") or "").casefold()
        if self.provider == "meny":
            matched = confirmation_order_id == order_id and meny_order_matches_checkout(current["order"], pending["summary"])
            if change.get("requested_delivery"):
                total = money_cents(current["order"].get("order_total"))
                expected = (pending["summary"].get("delivery_change") or {}).get("new_total_ore")
                matched = (confirmation_order_id == order_id and current["order"].get("code") == change.get("code")
                           and total is not None and total == expected
                           and meny_order_matches_checkout({**current["order"], "grossAmount": total / 100}, pending["summary"]))
            fulfillable = status in {"confirmed", "delivered"}
        elif change.get("requested_delivery"):
            expected_delivery = str(change["requested_delivery"].get("display") or "")
            actual_delivery = str(current["order"].get("deliverySlotDisplay") or current["order"].get("deliveryDate") or "")
            expected_signature = oda_delivery_signature(expected_delivery, provider=self.provider)
            actual_signature = oda_delivery_signature(actual_delivery, provider=self.provider)
            actual_date_value = current["order"].get("deliveryDate")
            try:
                actual_date = date.fromisoformat(actual_date_value) if isinstance(actual_date_value, str) else None
            except ValueError:
                actual_date = None
            expected_month = {
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mai": 5, "maj": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "des": 12, "dec": 12,
            }.get(expected_signature[5]) if expected_signature is not None else None
            # The frozen receipt/account binding is rechecked below only once
            # MCP reports the expected accepted change. No new address is inferred.
            before_address = oda_order_address_identity(change["before"]["order"])
            current_address = oda_order_address_identity(current["order"])
            address_matches = binding is not None and before_address == current_address
            before_quantities = oda_order_quantities(change["before"]["order"])
            current_quantities = oda_order_quantities(current["order"])
            before_total = money_cents(change["before"]["order"].get("grossAmount"))
            current_total = money_cents(current["order"].get("grossAmount"))
            price_review = pending["summary"].get("delivery_change") or {}
            expected_total = price_review.get("new_total_ore")
            # Legacy already-dispatched free reviews remain reconciliation-only.
            # They cannot authorize a new dispatch or a changed-price outcome.
            if not price_review and money_cents(pending["summary"].get("total")) == 0:
                expected_total = before_total
            matched = (
                expected_signature is not None
                and expected_signature == actual_signature
                and actual_date is not None
                and actual_date.isoformat() == actual_date_value
                and (actual_date.month, actual_date.day) == (expected_month, expected_signature[4])
                and address_matches
                and before_quantities is not None
                and before_quantities == current_quantities
                and before_total is not None
                and current_total is not None
                and type(expected_total) is int
                and current_total == expected_total
            )
            currency = "SEK" if self.provider == "mathem" else "NOK"
            matched = (matched and change["before"]["order"].get("currency") == current["order"].get("currency") == currency
                       and actual_date_value == self._delivery_slot_date(change["requested_delivery"]["slot"]))
            fulfillable = status in {"paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered"}
        else:
            matched = oda_order_matches_addition(change["before"]["order"], current["order"], pending["summary"], provider=self.provider)
            fulfillable = status in {"paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered"}
        confirmed = current_id == tracking_id == order_id and matched and fulfillable
        authentication = self._checkout_authentication_wait(pending, deadline) if not confirmed else {}
        if self.provider in {"oda", "mathem"} and confirmed:
            # An unpaid change may still be completing in the checkout page or
            # showing a bank challenge. Do not navigate away for receipt binding
            # until the provider actually reports the expected accepted change.
            self.browser.read_order_binding(order_id, current["order"], deadline=deadline,
                                            expected_binding=binding)
        with self.store.locked() as state:
            if canonical(state.get("pending_checkout")) != canonical(pending):
                raise HouseholdError("checkout state changed while reconciling the order change")
            if confirmed:
                state["pending_checkout"] = None
                state["order_change"] = None
                self._record_order_snapshot(state, pending, order_id)
                terminal = {
                    "confirmed": True, "changed_existing_order": True, "order_id": order_id,
                    "tracking_status": status, "retry_allowed": False,
                    "confirmation_id": (pending["recovery"] if recovery_dispatched else pending)["confirmation_id"],
                    **({"original_confirmation_id": pending["confirmation_id"]} if recovery_dispatched else {}),
                    "payment": self._payment_evidence(status),
                }
                self._store_protected_result(
                    state, pending["confirmation_id"], "checkout", terminal,
                    target_id=order_id, intent_signature=checkout_intent_signature(pending["summary"]),
                )
                if pending.get("recovery"):
                    self._store_protected_result(state, pending["recovery"]["confirmation_id"], "checkout", terminal,
                        target_id=order_id, intent_signature=checkout_intent_signature(pending["summary"]))
            else:
                state["pending_checkout"]["status"] = "uncertain"
        recovery_failed = bool(recovery_dispatched and pending["recovery"].get("payment_failure"))
        recovery_available = (self.provider == "mathem" and (not recovery_dispatched or recovery_failed) and status == "unpaid_order_change"
                              and (pending.get("payment_failure") or {}).get("payment_failed") is True and not authentication)
        return {**(terminal if confirmed else {}), "confirmed": confirmed, "changed_existing_order": confirmed, "order": current["order"] if confirmed else None, "tracking": current["tracking"] if confirmed else None, "retry_allowed": False,
                **({"tracking_status": status, "payment": self._payment_evidence(status),
                    "next": "The original Mathem change is not yet confirmed. Preserve its payment page and any bank approval; reconcile this same attempt without submitting again."}
                   if self.provider == "mathem" and not confirmed else {}),
                **({"payment_failed": True, "recovery_preparation_available": True,
                    "confirmation_id": (pending["recovery"] if recovery_dispatched else pending)["confirmation_id"],
                    "next": "The merchant rejected this addition payment. Use checkout prepare recovery=true for a fresh review of its retained order/change and original goods; do not restage the cart or resubmit the failed confirmation."}
                   if not confirmed and recovery_available else {}),
                **({"recovery_payment_unconfirmed": True,
                    "next": "Recovery payment was dispatched but is not confirmed. Reconcile this same attempt; the earlier failure never authorizes another payment."}
                   if not confirmed and recovery_dispatched and not recovery_failed else {}), **authentication}
