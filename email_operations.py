"""Order-bound recipe email scheduling and dispatch lifecycle.

Application owns shared state and locks; these methods run on that same instance.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import secrets
from typing import Any, Mapping
from core import HouseholdError, mask_email, valid_email_address
from recipe_assets import RecipeAssets
from recipe_email import prepare_recipe_media
from service_common import (
    EMAIL_AUTOMATION_PROTOCOL,
    EMAIL_CLAIM_LEASE,
    MAX_REQUEST,
    canonical,
    email_automation_ack,
    email_automation_key,
    email_automation_prompt,
    email_job_provider,
    expired_awaiting_confirmation,
    menu_email_html,
    menu_email_period,
    require_provider_identity,
    safe_order_id
)


class EmailOperations:
    def _email_media_payload(self, menu: Mapping[str, Any], fallback_html: str,
                             request: Mapping[str, Any], *, test: bool = False) -> dict[str, Any]:
        supported = request.get("images_supported", False)
        if not isinstance(supported, bool):
            raise HouseholdError("images_supported must be a boolean")
        if any(isinstance(recipe, Mapping) and isinstance(recipe.get("image"), Mapping)
               for group in ("dishes", "salads") if isinstance(menu.get(group), list) for recipe in menu[group]):
            # Queued jobs can predate image-credit rendering. Upgrade both
            # alternatives from their frozen snapshot, never from the bank.
            fallback_html = menu_email_html(menu, test=test)
        media = prepare_recipe_media(menu, RecipeAssets(self.store.directory / "recipe-assets"),
                                     images_supported=supported)
        payload = {"html": fallback_html, "inline_images": media["inline_images"],
                   "image_warnings": media["image_warnings"]}
        if media["image_cids"]:
            payload["html_without_images"] = fallback_html
            payload["html"] = menu_email_html(menu, test=test, image_cids=media["image_cids"])
        return payload

    @staticmethod
    def _email_payload_within_transport(payload: dict[str, Any]) -> dict[str, Any]:
        def fits() -> bool:
            return len(json.dumps({"ok": True, "result": payload}, ensure_ascii=True).encode()) <= MAX_REQUEST - 1_024
        if not fits():
            payload["html"] = payload.pop("html_without_images", payload["html"])
            payload["inline_images"] = []
            payload["image_warnings"] = [{"reason": "optional_image_payload_exceeds_transport"}]
            if not fits():
                payload.pop("inline_images")
                payload.pop("image_warnings")
            if not fits():
                raise HouseholdError("email cannot fit the meal concierge response transport")
        return payload

    @staticmethod
    def _scheduler_binding(value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping) or set(value) != {"platform", "scope", "job_id"}:
            raise HouseholdError("scheduler binding requires exact platform, private scope and job_id")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 512
               or any(ord(char) < 32 for char in item) for item in value.values()):
            raise HouseholdError("scheduler binding fields must be bounded nonempty text")
        return dict(value)

    @staticmethod
    def _email_occurrence(job: Mapping[str, Any]) -> str:
        # One recipe email per provider order, even when delivery is rescheduled.
        return email_automation_key(email_job_provider(job), safe_order_id(job.get("order_id")))

    def _scheduler_invocation(self, job: Mapping[str, Any]) -> dict[str, Any]:
        scheduler = job["scheduler"]
        return {"binding": deepcopy(scheduler["binding"]), "generation": scheduler["generation"],
                "occurrence_id": self._email_occurrence(job)}

    def _require_email_scheduler(self, job: Mapping[str, Any], request: Mapping[str, Any]) -> None:
        # Presence, including an unfinished first adoption, permanently fences
        # this job from the unowned legacy invocation path.
        if "scheduler" not in job:
            return
        scheduler = job["scheduler"]
        if scheduler.get("state") != "active":
            raise HouseholdError("email scheduler is paused or awaiting verified handover")
        if scheduler.get("delivery_date") != job.get("delivery_date") or job.get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL:
            raise HouseholdError("email scheduler delivery changed; call scheduler_plan and verify the native update before ack_scheduler")
        if canonical(request.get("scheduler")) != canonical(self._scheduler_invocation(job)):
            raise HouseholdError("email scheduler identity, generation or occurrence does not match")

    def _scheduler_plan_result(self, job: Mapping[str, Any]) -> dict[str, Any]:
        scheduler = job["scheduler"]
        invocation = self._scheduler_invocation(job)
        prompt = email_automation_prompt(email_job_provider(job), job["order_id"],
                                         job["delivery_date"], self._email_occurrence(job))
        prompt += (" For both due and begin_send, include this exact scheduler object: "
                   + canonical(invocation) + ". Preserve the same occurrence on every retry. "
                   "An uncertain sender result stays locked; reconcile_send requires the original claim_token "
                   "and affirmative sender evidence before recording sent or not_sent.")
        return {"scheduler": deepcopy(scheduler), "invocation": invocation,
                "cron_prompt": prompt, "automation_digest": hashlib.sha256(prompt.encode()).hexdigest(),
                "delivery_date": job["delivery_date"], "scheduler_protocol": 1}

    def _email_scheduler(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request["action"]
        provider = request.get("provider")
        order_id = safe_order_id(request.get("order_id"))
        supplied = request.get("scheduler")
        if provider not in {"oda", "meny", "mathem"} or not isinstance(supplied, Mapping):
            raise HouseholdError("scheduler management requires exact provider, order_id and scheduler object")
        with self.store.locked() as state:
            jobs = [job for job in state["email_jobs"]
                    if job.get("order_id") == order_id and email_job_provider(job) == provider]
            if len(jobs) != 1 or jobs[0].get("status") not in {"pending", "claimed", "sending"}:
                raise HouseholdError("scheduler management requires one nonterminal email job")
            job = jobs[0]
            scheduler = job.get("scheduler")
            if action == "pause_scheduler":
                if not scheduler or canonical(supplied) != canonical(self._scheduler_invocation(job)):
                    raise HouseholdError("pause requires the exact current scheduler invocation")
                if scheduler["state"] != "paused":
                    scheduler["state"] = "paused"
                    scheduler["generation"] = secrets.token_urlsafe(18)
                    scheduler.pop("ack", None)
            elif action == "scheduler_plan":
                if job["status"] == "sending":
                    raise HouseholdError("email send outcome needs reconciliation before handover")
                binding = self._scheduler_binding(supplied.get("binding"))
                if scheduler and (scheduler["state"] == "handover" or
                                  (scheduler.get("previous_binding") and not scheduler.get("previous_job_removed"))):
                    if binding != scheduler["binding"]:
                        raise HouseholdError("reconcile the existing scheduler plan before replacing it")
                    if scheduler["delivery_date"] != job["delivery_date"]:
                        scheduler["delivery_date"] = job["delivery_date"]
                        scheduler["generation"] = secrets.token_urlsafe(18)
                    return {"acknowledged": False, **self._scheduler_plan_result(job)}
                if scheduler:
                    if supplied.get("generation") != scheduler["generation"]:
                        raise HouseholdError("scheduler plan generation is stale")
                    previous = scheduler["binding"]
                else:
                    # The trusted adapter must inspect the old private scope;
                    # absence of a saved binding does not establish no old jobs.
                    inventory = supplied.get("inventory")
                    if not isinstance(inventory, Mapping) or inventory.get("verified") is not True:
                        raise HouseholdError("inspect the authoritative old scheduler inventory before adoption")
                    scope = inventory.get("scope")
                    platform = inventory.get("platform")
                    self._scheduler_binding({"platform": platform, "scope": scope, "job_id": "inventory"})
                    previous = supplied.get("previous_binding")
                    if previous is not None:
                        previous = self._scheduler_binding(previous)
                        if (previous["platform"], previous["scope"]) != (platform, scope):
                            raise HouseholdError("previous scheduler does not match the inspected inventory scope")
                        if inventory.get("matching_jobs") != 1 or isinstance(inventory.get("matching_jobs"), bool):
                            raise HouseholdError("reconcile multiple old scheduler jobs before adoption")
                    elif inventory.get("matching_jobs") != 0 or isinstance(inventory.get("matching_jobs"), bool):
                        raise HouseholdError("verified old inventory must establish zero matching jobs or exact previous binding")
                for other in state["email_jobs"]:
                    owner = other.get("scheduler")
                    if other is job or not owner or other.get("status") == "sent":
                        continue
                    reserved = [owner["binding"]]
                    if owner.get("previous_binding") and not owner.get("previous_job_removed"):
                        reserved.append(owner["previous_binding"])
                    if any(candidate is not None and candidate in reserved for candidate in (binding, previous)):
                        raise HouseholdError("native scheduler job is already bound to another email follow-up")
                scheduler = {"binding": binding, "previous_binding": deepcopy(previous) if previous != binding else None,
                             "state": "handover", "generation": secrets.token_urlsafe(18),
                             "delivery_date": job["delivery_date"]}
                if not job.get("scheduler"):
                    scheduler["adoption_inventory"] = {key: inventory[key] for key in ("platform", "scope", "verified", "matching_jobs")}
                job["scheduler"] = scheduler
            elif action == "ack_scheduler":
                if not scheduler or supplied.get("generation") != scheduler["generation"]:
                    raise HouseholdError("scheduler acknowledgement generation is stale")
                binding = self._scheduler_binding(supplied.get("binding"))
                wanted = supplied.get("state")
                expected = self._scheduler_plan_result(job)
                if (binding != scheduler["binding"] or wanted not in {"active", "paused"}
                        or supplied.get("verified") is not True
                        or supplied.get("previous_binding") != scheduler.get("previous_binding")
                        or (scheduler.get("previous_binding") is not None and supplied.get("previous_job_removed") is not True)
                        or scheduler["delivery_date"] != job["delivery_date"]
                        or request.get("automation_digest") != expected["automation_digest"]):
                    raise HouseholdError("scheduler acknowledgement needs the exact plan, verified old removal and native job state")
                if scheduler.get("ack"):
                    if canonical(scheduler["ack"]) != canonical(supplied):
                        raise HouseholdError("scheduler acknowledgement differs from the completed plan")
                    return {"acknowledged": True, "idempotent": True, **expected}
                if job["status"] == "sending":
                    raise HouseholdError("email send outcome needs reconciliation before enabling a scheduler")
                scheduler["state"] = wanted
                scheduler["ack"] = deepcopy(dict(supplied))
                scheduler["previous_job_removed"] = supplied.get("previous_job_removed") is True
                job["automation_protocol"] = EMAIL_AUTOMATION_PROTOCOL
            if job["status"] == "claimed":
                job["status"] = "pending"
                job.pop("claim_token", None)
                job.pop("claim_expires_at", None)
            return {"acknowledged": action == "ack_scheduler", **self._scheduler_plan_result(job)}

    def _email_order_read(self, provider: str, order_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        client = self.email_provider_clients.get(provider)
        if client is None:
            raise HouseholdError(f"provider {provider} is unavailable for the bound email job")
        if provider in {"oda", "mathem"}:
            # Cancellation tracking can remain available after order details are
            # removed. A generic provider/auth/not-found error is never proof.
            tracking = client.call("order_tracking", {"order_number": order_id})
            require_provider_identity(tracking, order_id, tracking=True)
            if str(tracking.get("status") or "").casefold() in {"cancelled", "canceled"}:
                return {"order": {}, "tracking": tracking}
            order = client.call("get_order", {"order_number": order_id})
            require_provider_identity(order, order_id)
            return {"order": order, "tracking": tracking}
        if provider != self.provider:
            order = client.call("get_order", {"order_number": order_id})
            tracking = client.call("order_tracking", {"order_number": order_id})
            require_provider_identity(order, order_id)
            require_provider_identity(tracking, order_id, tracking=True)
            return {"order": order, "tracking": tracking}
        return self._orders({"action": "get", "order_id": order_id, "_deadline": request.get("_deadline")})

    def _email(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "status")
        requested_provider = request.get("provider")
        if requested_provider is not None and (
            not isinstance(requested_provider, str) or requested_provider not in {"oda", "meny", "mathem"}
        ):
            raise HouseholdError("email provider must be oda, meny or mathem")
        if action in {"scheduler_plan", "ack_scheduler", "pause_scheduler"}:
            return self._email_scheduler(request)

        def matching_jobs(state: Mapping[str, Any], order_id: str, statuses: set[str] | None = None) -> list[dict[str, Any]]:
            candidates = [
                job for job in state.get("email_jobs", [])
                if isinstance(job, dict) and job.get("order_id") == order_id
                and (statuses is None or job.get("status") in statuses)
            ]
            if requested_provider is not None:
                candidates = [job for job in candidates if email_job_provider(job) == requested_provider]
            if any(email_job_provider(job) is None for job in candidates):
                raise HouseholdError("email job has no valid bound provider")
            providers = {email_job_provider(job) for job in candidates}
            if len(providers) > 1:
                raise HouseholdError("email order identity is ambiguous; specify provider")
            return candidates

        def cleanup_action(job: Mapping[str, Any]) -> dict[str, Any]:
            provider = email_job_provider(job)
            order_id = safe_order_id(job.get("order_id"))
            scheduler = deepcopy(job.get("scheduler"))
            if scheduler and scheduler.get("previous_job_removed") is True:
                # The old ID may since have been reused by a different job.
                scheduler["previous_binding"] = None
            return {"provider": provider, "order_id": order_id,
                    "automation_key": job.get("automation_key") or email_automation_key(provider, order_id),
                    **({"scheduler": scheduler} if scheduler else {}),
                    "action": "remove", "reason": "order cancelled"}

        if action in {"cancel_followup", "reconcile"}:
            order_id = safe_order_id(request.get("order_id"))
            if requested_provider is None:
                raise HouseholdError("follow-up reconciliation requires an exact provider and order_id")
            state = self.store.read()
            jobs = matching_jobs(state, order_id)
            if len(jobs) != 1:
                raise HouseholdError("follow-up reconciliation requires exactly one existing email job")
            initial_job = deepcopy(jobs[0])
            if initial_job.get("status") == "sending":
                raise HouseholdError("email send outcome needs reconciliation before closing its follow-up")
            if initial_job.get("status") == "cancelled":
                return {"send": False, "cancelled": True, "automation_cleanup": cleanup_action(initial_job)}
            if initial_job.get("status") not in {"pending", "claimed"}:
                return {"send": False, "cancelled": False, "reason": "email follow-up is already terminal"}
            if action == "cancel_followup":
                if request.get("owner_confirmed_cancelled") is not True:
                    raise HouseholdError("the owner must explicitly confirm external order cancellation")
            else:
                current = self._email_order_read(requested_provider, order_id, request)
                if str(current["tracking"].get("status") or "").casefold() not in {"cancelled", "canceled"}:
                    return {"send": False, "cancelled": False, "reason": "provider has not confirmed cancellation"}
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id)
                if len(jobs) != 1 or canonical(jobs[0]) != canonical(initial_job):
                    raise HouseholdError("email job changed while checking its cancellation")
                self._mark_order_cancelled(state, order_id, provider=requested_provider, active_provider=self.provider)
            return {"send": False, "cancelled": True, "automation_cleanup": cleanup_action(initial_job)}

        if action == "status":
            state = self.store.read()
            jobs = [{
                "order_id": job.get("order_id"), "delivery_date": job.get("delivery_date"),
                "status": job.get("status"), "sent_at": job.get("sent_at"),
                "provider": email_job_provider(job),
                "recipient": mask_email(job.get("recipient_snapshot")),
                "scheduler": deepcopy(job.get("scheduler")),
                "scheduler_ownership": "managed" if "scheduler" in job else "unowned_legacy",
                "occurrence_id": self._email_occurrence(job) if email_job_provider(job) and job.get("status") != "invalid" else None,
                "automation_update_required": job.get("status") == "pending" and job.get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL,
            } for job in state["email_jobs"]]
            return {"jobs": jobs, "automation_updates_required": sum(bool(job["automation_update_required"]) for job in jobs)}
        if action == "automation_plan":
            state = self.store.read()
            updates = []
            scheduler_updates = []
            for job in state["email_jobs"]:
                if job.get("status") != "pending":
                    continue
                if "scheduler" in job:
                    if job.get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL or job["scheduler"]["state"] != "active":
                        scheduler_updates.append({"provider": email_job_provider(job), "order_id": job["order_id"],
                                                  "scheduler": self._scheduler_invocation(job),
                                                  "next": "Call scheduler_plan, apply and verify its native update, then ack_scheduler."})
                    continue
                if job.get("automation_protocol") == EMAIL_AUTOMATION_PROTOCOL:
                    continue
                provider = email_job_provider(job)
                order_id = safe_order_id(job.get("order_id"))
                delivery_date = str(job.get("delivery_date") or "")
                automation_key = email_automation_key(provider, order_id) if provider else ""
                if not provider or not order_id or not valid_email_address(job.get("recipient_snapshot")) or not isinstance(job.get("menu_snapshot"), Mapping):
                    raise HouseholdError("pending email automation is not bound to one exact order, menu and recipient")
                updates.append({
                    "provider": provider, "order_id": order_id, "delivery_date": delivery_date, "automation_key": automation_key,
                    "cron_prompt": email_automation_prompt(provider, order_id, delivery_date, automation_key),
                    "ack": email_automation_ack(provider, order_id, delivery_date, automation_key),
                })
            return {"protocol": EMAIL_AUTOMATION_PROTOCOL, "updates": updates, "scheduler_updates": scheduler_updates,
                    "removals": [cleanup_action(job) for job in state["email_jobs"] if job.get("status") == "cancelled"]}
        if action == "ack_automation":
            order_id = safe_order_id(request.get("order_id"))
            automation_key = str(request.get("automation_key") or "")
            delivery_date = request.get("delivery_date")
            automation_digest = request.get("automation_digest")
            if request.get("protocol") != EMAIL_AUTOMATION_PROTOCOL:
                raise HouseholdError(f"email automation protocol must be {EMAIL_AUTOMATION_PROTOCOL}")
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id, {"pending"})
                if any("scheduler" in job for job in jobs):
                    raise HouseholdError("managed email requires scheduler_plan and ack_scheduler after a verified native update")
                provider = email_job_provider(jobs[0]) if len(jobs) == 1 else None
                expected_key = (
                    str(jobs[0].get("automation_key") or email_automation_key(provider, order_id))
                    if len(jobs) == 1 and provider and jobs[0].get("automation_protocol") == EMAIL_AUTOMATION_PROTOCOL
                    else email_automation_key(provider, order_id) if len(jobs) == 1 and provider else ""
                )
                current_delivery = str(jobs[0].get("delivery_date") or "") if len(jobs) == 1 else ""
                expected_digest = hashlib.sha256(email_automation_prompt(provider, order_id, current_delivery, expected_key).encode()).hexdigest() if len(jobs) == 1 and provider else ""
                if (
                    len(jobs) != 1 or not automation_key or not secrets.compare_digest(expected_key, automation_key)
                    or delivery_date != current_delivery or not isinstance(automation_digest, str)
                    or not secrets.compare_digest(expected_digest, automation_digest)
                ):
                    raise HouseholdError("email automation acknowledgement does not match one pending job")
                jobs[0]["automation_key"] = automation_key
                jobs[0]["automation_protocol"] = EMAIL_AUTOMATION_PROTOCOL
            return {"acknowledged": True, "provider": provider, "order_id": order_id, "automation_key": automation_key, "protocol": EMAIL_AUTOMATION_PROTOCOL}
        if action == "schedule":
            if requested_provider is not None and requested_provider != self.provider:
                raise HouseholdError("schedule provider must match the active household provider")
            order_id = safe_order_id(request.get("order_id"))
            supplied_delivery_date = request.get("delivery_date")
            if not isinstance(supplied_delivery_date, str):
                raise HouseholdError("delivery_date must be a canonical ISO date")
            try:
                delivery_date = date.fromisoformat(supplied_delivery_date).isoformat()
            except ValueError as exc:
                raise HouseholdError("delivery_date must be a canonical ISO date") from exc
            if delivery_date != supplied_delivery_date:
                raise HouseholdError("delivery_date must be a canonical ISO date")
            with self.store.locked() as locked:
                snapshot = None
                if isinstance(locked.get("menu"), Mapping) and locked["menu"].get("order_id") == order_id:
                    snapshot = deepcopy(locked["menu"])
                elif (locked.get("order_snapshot_providers") or {}).get(order_id) == self.provider:
                    snapshot = deepcopy((locked.get("order_snapshots") or {}).get(order_id))
                recipient = locked.get("email_recipient")
                if not isinstance(snapshot, Mapping) or not isinstance(recipient, str) or not recipient.strip():
                    raise HouseholdError("confirmed order, exact menu and email recipient are required")
                period = menu_email_period(snapshot)
                existing = [
                    job for job in locked["email_jobs"]
                    if job.get("order_id") == order_id and email_job_provider(job) == self.provider
                ]
                automation_key = email_automation_key(self.provider, order_id)
                created = not existing
                rescheduled = False
                if not existing:
                    locked["email_jobs"].append({
                        "order_id": order_id, "delivery_date": delivery_date, "status": "pending", "sent_at": None,
                        "provider": self.provider,
                        "recipient_snapshot": recipient, "menu_snapshot": snapshot,
                        "subject": f"Ukesmeny og oppskrifter – {period}", "html": menu_email_html(snapshot),
                        "automation_key": automation_key, "automation_protocol": 0,
                    })
                elif len(existing) == 1:
                    if existing[0].get("status") != "pending":
                        raise HouseholdError("the order email is already claimed, sent or cancelled")
                    if not valid_email_address(existing[0].get("recipient_snapshot")) or not isinstance(existing[0].get("menu_snapshot"), Mapping):
                        raise HouseholdError("the pending order email is not bound to its original menu and recipient")
                    recipient = existing[0]["recipient_snapshot"]
                    existing[0]["provider"] = self.provider
                    rescheduled = existing[0].get("delivery_date") != delivery_date
                    existing[0]["delivery_date"] = delivery_date
                    if existing[0].get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL:
                        existing[0]["automation_key"] = automation_key
                    if rescheduled:
                        existing[0]["automation_protocol"] = 0
                else:
                    raise HouseholdError("the order has multiple email jobs")
                job = (existing or [locked["email_jobs"][-1]])[0]
                automation_update_required = job.get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL
            result = {
                "scheduled": created,
                "provider": self.provider,
                "idempotent": not created and not rescheduled,
                "rescheduled": rescheduled,
                "automation_key": automation_key,
                "delivery_date": delivery_date,
                "recipient": mask_email(recipient),
                "automation_update_required": automation_update_required,
                "cron_prompt": email_automation_prompt(self.provider, order_id, delivery_date, automation_key),
                "automation_ack": email_automation_ack(self.provider, order_id, delivery_date, automation_key),
            }
            if "scheduler" in job:
                result.pop("cron_prompt")
                result.pop("automation_ack")
                result.update(scheduler=self._scheduler_invocation(job), scheduler_update_required=automation_update_required,
                              next="Call scheduler_plan for the managed job, apply and verify its native update, then ack_scheduler.")
            return result
        if action == "test":
            order_id = safe_order_id(request.get("order_id"))
            state = self.store.read()
            jobs = matching_jobs(state, order_id, {"pending"})
            job = jobs[0] if len(jobs) == 1 else {}
            menu = job.get("menu_snapshot")
            recipient = job.get("recipient_snapshot")
            if len(jobs) != 1 or not isinstance(menu, Mapping) or menu.get("order_id") != order_id or not isinstance(recipient, str) or not recipient.strip():
                raise HouseholdError("pending email, exact menu and recipient are required for a test")
            period = menu_email_period(menu)
            result = {
                "send": True,
                "test": True,
                "recipient": recipient,
                "subject": f"TEST – Ukesmeny og oppskrifter – {period}",
                "html": menu_email_html(menu, test=True),
                "order_id": order_id,
                "mark_sent_after_success": False,
                "next": "Send this test once; do not call mark_sent.",
            }
            result.update(self._email_media_payload(menu, result["html"], request, test=True))
            if self.email_automation_profile:
                result["automation_environment"] = {"HERMES_WORKSPACE_AUTOMATION_PROFILE": self.email_automation_profile}
            return self._email_payload_within_transport(result)
        if action == "due":
            order_id = safe_order_id(request.get("order_id"))
            state = self.store.read()
            matching = matching_jobs(state, order_id, {"pending", "claimed", "sending"})
            if not matching:
                return {"send": False, "reason": "no pending email"}
            if len(matching) != 1:
                return {"send": False, "reason": "multiple email jobs for the provider order"}
            initial_job = deepcopy(matching[0])
            self._require_email_scheduler(initial_job, request)
            job_provider = email_job_provider(initial_job)
            if job_provider is None:
                raise HouseholdError("email job has no valid bound provider")
            if initial_job.get("status") == "pending" and initial_job.get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL:
                delivery_date = str(initial_job.get("delivery_date") or "")
                automation_key = email_automation_key(job_provider, order_id)
                return {
                    "send": False, "reason": "email automation update is required",
                    "provider": job_provider, "order_id": order_id, "delivery_date": delivery_date,
                    "automation_key": automation_key, "automation_update_required": True,
                    "cron_prompt": email_automation_prompt(job_provider, order_id, delivery_date, automation_key),
                    "automation_ack": email_automation_ack(job_provider, order_id, delivery_date, automation_key),
                }
            current = self._email_order_read(job_provider, order_id, request)
            tracking = str((current.get("tracking") or {}).get("status") or "").casefold()
            with self.store.locked() as state:
                matching = matching_jobs(state, order_id, {"pending", "claimed", "sending"})
                if len(matching) != 1 or canonical(matching[0]) != canonical(initial_job):
                    raise HouseholdError("email job changed while checking its provider order")
                pending_cancellation = state.get("pending_cancellation")
                if expired_awaiting_confirmation(pending_cancellation, self._now()):
                    state["pending_cancellation"] = None
                    pending_cancellation = None
                if job_provider == self.provider and isinstance(pending_cancellation, Mapping) and pending_cancellation.get("order_id") == order_id:
                    return {"send": False, "reason": "order cancellation is pending"}
                if len(matching) == 1 and matching[0].get("status") == "claimed":
                    try:
                        claim_expires_at = datetime.fromisoformat(str(matching[0].get("claim_expires_at") or ""))
                    except ValueError:
                        claim_expires_at = None
                    if claim_expires_at is not None and claim_expires_at.tzinfo is not None and self._now() >= claim_expires_at:
                        matching[0]["status"] = "pending"
                        matching[0].pop("claim_token", None)
                        matching[0].pop("claim_expires_at", None)
                    else:
                        return {"send": False, "reason": "email is already claimed before dispatch"}
                elif len(matching) == 1 and matching[0].get("status") == "sending":
                    return {"send": False, "reason": "email send outcome needs reconciliation"}
                jobs = [job for job in matching if job.get("status") == "pending"]
                if not jobs:
                    return {"send": False, "reason": "no pending email"}
                job = jobs[0] if len(jobs) == 1 else {}
                menu = job.get("menu_snapshot")
                recipient = job.get("recipient_snapshot")
                if len(jobs) != 1 or not isinstance(menu, Mapping) or menu.get("order_id") != order_id or not isinstance(recipient, str) or not recipient.strip():
                    return {"send": False, "reason": "pending email is not bound to one exact menu and recipient"}
                if tracking in {"cancelled", "canceled"}:
                    self._mark_order_cancelled(
                        state, order_id, provider=job_provider, active_provider=self.provider,
                    )
                    return {"send": False, "reason": "order cancelled", "automation_cleanup": cleanup_action(initial_job)}
                fulfillable = {"confirmed", "delivered"} if job_provider == "meny" else {
                    "paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered",
                }
                if tracking not in fulfillable:
                    return {"send": False, "reason": "order status is not confirmed for recipe email"}
                order = current.get("order") if isinstance(current.get("order"), Mapping) else {}
                delivery_values = [order.get(key) for key in ("deliveryDate", "delivery_date") if key in order]
                if not delivery_values or not all(isinstance(value, str) for value in delivery_values) or len(set(delivery_values)) != 1:
                    raise HouseholdError("provider order does not establish one delivery date")
                delivery = delivery_values[0]
                try:
                    canonical_delivery = date.fromisoformat(delivery).isoformat()
                except ValueError as exc:
                    raise HouseholdError("provider returned an invalid delivery date") from exc
                if canonical_delivery != delivery:
                    raise HouseholdError("provider returned an invalid delivery date")
                automation_key = job.get("automation_key") or email_automation_key(job_provider, order_id)
                job["automation_key"] = automation_key
                local_today = self._household_today(state).isoformat()
                if delivery != local_today or ("scheduler" in job and delivery != job["delivery_date"]):
                    job["delivery_date"] = delivery
                    job["automation_protocol"] = 0
                    if "scheduler" in job:
                        return {"send": False, "reason": "delivery moved", "delivery_date": delivery,
                                "scheduler": self._scheduler_invocation(job), "scheduler_update_required": True,
                                "next": "Call scheduler_plan, apply and verify its native update, then ack_scheduler."}
                    return {
                        "send": False, "reason": "delivery moved", "delivery_date": delivery,
                        "automation_key": automation_key,
                        "automation_update_required": True,
                        "cron_prompt": email_automation_prompt(job_provider, order_id, delivery, automation_key),
                        "automation_ack": email_automation_ack(job_provider, order_id, delivery, automation_key),
                    }
                period = menu_email_period(menu)
                claim_token = secrets.token_urlsafe(18)
                job["status"] = "claimed"
                job["claim_token"] = claim_token
                job["claim_expires_at"] = (self._now() + EMAIL_CLAIM_LEASE).isoformat()
                result = {
                    "send": False,
                    "claim": True,
                    "provider": job_provider,
                    "order_id": order_id,
                    "claim_token": claim_token,
                    "mark_sent_after_success": True,
                    "next": f"Call begin_send with provider={job_provider}, this exact order_id and claim_token. Invoke the sender only with the payload returned when dispatch=true. After confirmed success call mark_sent with provider={job_provider}. On a definite no-send failure only call release with provider={job_provider}; leave uncertain post-dispatch outcomes locked.",
                }
                if "scheduler" in job:
                    result["scheduler"] = self._scheduler_invocation(job)
                    result["occurrence_id"] = self._email_occurrence(job)
                return result
        if action == "begin_send":
            order_id = safe_order_id(request.get("order_id"))
            claim_token = request.get("claim_token")
            if not isinstance(claim_token, str) or not claim_token:
                raise HouseholdError("the email claim_token is required")
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id, {"claimed"})
                if len(jobs) != 1 or not secrets.compare_digest(str(jobs[0].get("claim_token") or ""), claim_token):
                    raise HouseholdError("email claim_token does not match a claimed job")
                self._require_email_scheduler(jobs[0], request)
                pending_cancellation = state.get("pending_cancellation")
                if email_job_provider(jobs[0]) == self.provider and isinstance(pending_cancellation, Mapping) and pending_cancellation.get("order_id") == order_id:
                    raise HouseholdError("order cancellation is pending; do not send its recipe email")
                try:
                    expires_at = datetime.fromisoformat(str(jobs[0].get("claim_expires_at") or ""))
                except ValueError as exc:
                    raise HouseholdError("email claim is invalid; request due again") from exc
                if expires_at.tzinfo is None or self._now() >= expires_at:
                    jobs[0]["status"] = "pending"
                    jobs[0].pop("claim_token", None)
                    jobs[0].pop("claim_expires_at", None)
                    raise HouseholdError("email claim expired before dispatch; request due again")
                menu = jobs[0].get("menu_snapshot")
                recipient = jobs[0].get("recipient_snapshot")
                if not isinstance(menu, Mapping) or menu.get("order_id") != order_id or not isinstance(recipient, str) or not recipient.strip():
                    raise HouseholdError("claimed email is not bound to one exact menu and recipient")
                period = menu_email_period(menu)
                payload = {
                    "dispatch": True, "send": True, "recipient": recipient,
                    "subject": jobs[0].get("subject") or f"Ukesmeny og oppskrifter – {period}",
                    "html": jobs[0].get("html") or menu_email_html(menu),
                    "provider": email_job_provider(jobs[0]), "order_id": order_id, "claim_token": claim_token,
                }
                payload.update(self._email_media_payload(menu, payload["html"], request))
                if "scheduler" in jobs[0]:
                    payload["scheduler"] = self._scheduler_invocation(jobs[0])
                    payload["occurrence_id"] = self._email_occurrence(jobs[0])
                if self.email_automation_profile:
                    payload["automation_environment"] = {"HERMES_WORKSPACE_AUTOMATION_PROFILE": self.email_automation_profile}
                self._email_payload_within_transport(payload)
                jobs[0]["status"] = "sending"
                jobs[0].pop("sender_receipt", None)
                jobs[0].pop("claim_expires_at", None)
                jobs[0]["dispatch_started_at"] = self._now().isoformat()
            return payload
        if action == "reconcile_send":
            order_id = safe_order_id(request.get("order_id"))
            outcome = request.get("send_outcome")
            token = request.get("claim_token")
            if requested_provider is None or outcome not in {"sent", "not_sent", "unknown"} or not isinstance(token, str) or not token:
                raise HouseholdError("send reconciliation requires exact provider, order, claim_token and send_outcome")
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id, {"sending", "sent"})
                if len(jobs) != 1 or not secrets.compare_digest(str(jobs[0].get("claim_token") or jobs[0].get("sent_claim_token") or ""), token):
                    raise HouseholdError("send reconciliation token does not match the original dispatch")
                if outcome == "unknown":
                    return {"resolved": jobs[0]["status"] == "sent", "status": jobs[0]["status"], "retry_allowed": False}
                receipt = request.get("sender_receipt")
                if not isinstance(receipt, str) or not receipt.strip() or len(receipt) > 1024 or any(ord(char) < 32 for char in receipt):
                    raise HouseholdError("affirmative sender evidence requires a bounded sender_receipt reference")
                if jobs[0]["status"] == "sent" and outcome != "sent":
                    raise HouseholdError("a confirmed sent email cannot be released")
            # The token is rechecked under the write lock by the existing final
            # transition. A pause/generation change never changes this identity.
            return self._email({**request, "action": "mark_sent" if outcome == "sent" else "release"})
        if action == "mark_sent":
            order_id = safe_order_id(request.get("order_id"))
            claim_token = request.get("claim_token")
            if not isinstance(claim_token, str) or not claim_token:
                raise HouseholdError("the email claim_token is required")
            receipt = request.get("sender_receipt")
            if receipt is not None and (not isinstance(receipt, str) or not receipt.strip() or len(receipt) > 1024 or any(ord(char) < 32 for char in receipt)):
                raise HouseholdError("sender_receipt must be a bounded evidence reference")
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id, {"sending", "sent"})
                if len(jobs) != 1:
                    raise HouseholdError("email is not claimed for sending")
                if not secrets.compare_digest(str(jobs[0].get("claim_token") or jobs[0].get("sent_claim_token") or ""), claim_token):
                    raise HouseholdError("email claim_token does not match")
                if jobs[0]["status"] == "sent":
                    return {"sent": True, "idempotent": True}
                jobs[0]["status"] = "sent"
                jobs[0]["sent_at"] = self._now().isoformat()
                jobs[0]["sent_claim_token"] = claim_token
                if request.get("sender_receipt"):
                    jobs[0]["sender_receipt"] = request["sender_receipt"]
                jobs[0].pop("claim_token", None)
                jobs[0].pop("dispatch_started_at", None)
                jobs[0].pop("html", None)
                jobs[0].pop("menu_snapshot", None)
                if email_job_provider(jobs[0]) == self.provider:
                    self._prune_order_snapshots(state)
            return {"sent": True}
        if action == "release":
            order_id = safe_order_id(request.get("order_id"))
            claim_token = request.get("claim_token")
            if not isinstance(claim_token, str) or not claim_token:
                raise HouseholdError("the email claim_token is required")
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id, {"claimed", "sending"})
                if len(jobs) != 1 or not secrets.compare_digest(str(jobs[0].get("claim_token") or ""), claim_token):
                    raise HouseholdError("email claim_token does not match a claimed or sending job")
                if "scheduler" in jobs[0] and jobs[0]["status"] == "sending":
                    receipt = request.get("sender_receipt")
                    if request.get("send_outcome") != "not_sent" or not isinstance(receipt, str) or not receipt.strip() or len(receipt) > 1024 or any(ord(char) < 32 for char in receipt):
                        raise HouseholdError("dispatched email requires affirmative not_sent sender evidence; uncertainty stays locked")
                    jobs[0]["sender_receipt"] = receipt
                jobs[0]["status"] = "pending"
                jobs[0].pop("claim_token", None)
                jobs[0].pop("claim_expires_at", None)
                jobs[0].pop("dispatch_started_at", None)
            return {"released": True}
        raise HouseholdError("unknown email action")
