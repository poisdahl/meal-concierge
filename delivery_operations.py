"""Explicit recipe occurrences and per-outbound-send receipts, separate from orders."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
import re
import secrets

from core import HouseholdError, valid_email_address, mask_email
from menu_planning import exact_menu, menu_ref, digest
from recipe_assets import RecipeAssets, read_local_file
from recipe_delivery import render_menu, render_pdf, render_email, split_text, limit_images

MAX_FILE = 32 * 1024 * 1024
CHUNK = 128 * 1024


def identifier(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
        raise HouseholdError(f"{label} must be a bounded identifier")
    return value


def evidence(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise HouseholdError("native evidence must be a bounded receipt or capability reference")
    return value


def legacy_held(state, job=None):
    delivery = state.get("recipe_delivery", {})
    return bool(delivery.get("paused") or delivery.get("legacy_email_disabled") or (job or {}).get("delivery_hold"))


def legacy_id(job):
    return "order-email:" + digest({"provider": job.get("provider"), "order_id": job.get("order_id")})[:24]


def order_email_summary(state):
    return [{"id": legacy_id(j), "purpose": "order_delivery_day",
             "recipient": mask_email(j.get("recipient_snapshot")), "status": j.get("status"),
             "held": j.get("status") in {"pending", "claimed", "sending"} and legacy_held(state, j), "delivery_date": j.get("delivery_date")}
            for j in state["email_jobs"]]


def summary(job, offset=0, part_id=None, *, include_parts=True):
    if type(offset) is not int or offset < 0:
        raise HouseholdError("part offset must be a non-negative integer")
    selected = [p for p in job["parts"] if p["id"] == part_id] if part_id is not None else job["parts"][offset:offset + 25]
    parts = [{k: deepcopy(v) for k, v in p.items() if k not in {"text", "file", "attempts"}}
             for p in selected] if include_parts else []
    return {k: deepcopy(job[k]) for k in ("id", "purpose", "menu_ref", "destinations", "warnings")} | {
        "parts": parts, "part_count": len(job["parts"]),
        "next_offset": offset + 25 if part_id is None and offset + 25 < len(job["parts"]) else None,
        "all_accepted": bool(job["parts"]) and all(p["status"] == "accepted" for p in job["parts"]),
        "omissions_reported": bool(job["warnings"]),
        "recipient_read": "unknown"}


def capabilities(value, channel, destination):
    if not isinstance(value, dict) or value.get("verified") is not True:
        raise HouseholdError(f"{channel} requires inspected native transport capability, without a probe send")
    evidence(value.get("evidence"))
    identifier(value.get("transport"), "transport")
    if not isinstance(destination, dict):
        raise HouseholdError(f"{channel} needs its explicitly selected destination")
    if channel == "chat":
        if set(destination) != {"platform", "conversation"} or destination["platform"] != value["transport"]:
            raise HouseholdError("chat must bind the actual requesting platform and conversation")
        evidence(destination["conversation"])
        required = "text_limit"
    else:
        if set(destination) != {"recipient", "sender"} or not all(valid_email_address(v) for v in destination.values()):
            raise HouseholdError("email needs the selected recipient and verified sender address")
        if value.get("sender") != destination["sender"]:
            raise HouseholdError("email capability does not verify this selected sender")
        required = "message_limit"
    limit = value.get(required)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 256 <= limit <= MAX_FILE:
        raise HouseholdError(f"native {required} must be between 256 and {MAX_FILE} bytes")
    if channel == "chat" and limit > 100000:
        raise HouseholdError("chat text_limit exceeds the tool response limit")
    attachment = value.get("attachment_limit", 0)
    if isinstance(attachment, bool) or not isinstance(attachment, int) or not 0 <= attachment <= MAX_FILE:
        raise HouseholdError("native attachment_limit is invalid")
    for field in ("pdf", "images"):
        if type(value.get(field, False)) is not bool:
            raise HouseholdError("format capabilities must be booleans")
    return deepcopy(value)


class DeliveryOperations:
    def _recipe_delivery(self, request):
        action = request.get("action", "status")
        with self.store.locked() as state:
            delivery = state["recipe_delivery"]
            jobs = delivery["jobs"]
            held = sorted([j["id"] + "/" + p["id"] for j in jobs.values() for p in j["parts"] if p.get("held")]
                          + [legacy_id(j) for j in state["email_jobs"] if j.get("delivery_hold") and j.get("status") in {"pending", "claimed", "sending"}])
            if action == "status":
                offset = request.get("offset", 0)
                if type(offset) is not int or offset < 0:
                    raise HouseholdError("status offset must be a non-negative integer")
                return {"preferences": deepcopy(delivery["preferences"]), "paused": delivery["paused"],
                        "held_work": held[:100], "held_work_count": len(held), "held_work_digest": digest(held),
                        "jobs": [summary(j, include_parts=False) for j in list(jobs.values())[offset:offset + 25]],
                        "job_count": len(jobs), "next_offset": offset + 25 if offset + 25 < len(jobs) else None,
                        "order_delivery_day_email": order_email_summary(state),
                        "automatic_chat": "unavailable: no verified native timer binding; no timer is created"}
            if action == "configure":
                changes = request.get("changes")
                if not isinstance(changes, dict) or not changes or not set(changes) <= {"chat", "email"}:
                    raise HouseholdError("changes must select chat or email format preferences")
                new = deepcopy(delivery["preferences"])
                for channel, values in changes.items():
                    if not isinstance(values, dict) or not values or not set(values) <= {"enabled", "pdf", "images"} or any(type(v) is not bool for v in values.values()):
                        raise HouseholdError("channel preferences are enabled/pdf/images booleans")
                    if values.get("enabled") is False:
                        raise HouseholdError("use disable for explicit channel disable and queued-work accounting")
                    if channel == "email" and values.get("enabled") is True:
                        caps, destinations = request.get("capabilities"), request.get("destinations")
                        if not isinstance(caps, dict) or not isinstance(destinations, dict):
                            raise HouseholdError("email opt-in requires selected destination and verified sender capabilities")
                        capabilities(caps.get("email"), "email", destinations.get("email"))
                    new[channel].update(values)
                delivery["preferences"] = new
                if new["email"]["enabled"]:
                    delivery["legacy_email_disabled"] = False
                return {"preferences": deepcopy(new), "future_occurrences_only": True,
                        "order_delivery_day_email": order_email_summary(state),
                        "held_work_requires_explicit_resume": True}
            if action in {"pause", "disable"}:
                if action == "disable" and request.get("channel") not in ("chat", "email"):
                    raise HouseholdError("disable needs chat or email")
                channels = {"chat", "email"} if action == "pause" else {request.get("channel")}
                if not channels <= {"chat", "email"}:
                    raise HouseholdError("disable needs chat or email")
                if action == "pause":
                    delivery["paused"] = True
                else:
                    channel = request["channel"]
                    delivery["preferences"][channel]["enabled"] = False
                    if channel == "email":
                        delivery["legacy_email_disabled"] = True
                affected, uncertain = [], []
                for job in jobs.values():
                    for part in job["parts"]:
                        if part["channel"] in channels:
                            key = job["id"] + "/" + part["id"]
                            if part["status"] in {"ready", "not_sent", "held"}:
                                part["held"] = True
                                affected.append(key)
                            elif part["status"] in {"attempting", "unknown"}:
                                part["held"] = True
                                uncertain.append(key)
                if "email" in channels:
                    for job in state["email_jobs"]:
                        if job.get("status") in {"pending", "claimed"}:
                            job["delivery_hold"] = True
                            affected.append(legacy_id(job))
                        elif job.get("status") == "sending":
                            job["delivery_hold"] = True
                            uncertain.append(legacy_id(job))
                return {"affected": affected[:100], "affected_count": len(affected), "affected_digest": digest(affected),
                        "uncertain_original_attempts": uncertain[:100], "uncertain_count": len(uncertain),
                        "dispatched_messages_cannot_be_recalled": True}
            if action == "resume":
                # Explicit accounting does not release a backlog to native timers.
                if request.get("held_work") != held and request.get("held_work_digest") != digest(held):
                    raise HouseholdError("resume requires the exact held_work list or digest; inspect status first")
                delivery["paused"] = False
                return {"paused": False, "retained_held_work": held[:100], "held_work_count": len(held),
                        "next": "Release or discard individual held parts explicitly; no backlog was dispatched."}
            if action == "request":
                return self._delivery_request(state, request) | {"order_delivery_day_email": order_email_summary(state)}
            if action == "automatic":
                if not any(v["enabled"] for v in delivery["preferences"].values()):
                    raise HouseholdError("automatic recipe delivery requires a configured channel")
                raise HouseholdError("automatic chat needs a separately verified native timer; use existing order email for its configured occurrence")
            if action == "release_order_hold":
                matches = [j for j in state["email_jobs"] if legacy_id(j) == request.get("job_id")]
                if len(matches) != 1 or not matches[0].get("delivery_hold"):
                    raise HouseholdError("exact held order email is required")
                if delivery["paused"] or delivery["legacy_email_disabled"]:
                    raise HouseholdError("resume delivery and enable email before releasing this occurrence")
                if matches[0].get("status") not in {"pending", "claimed", "sent", "cancelled"}:
                    raise HouseholdError("reconcile the original uncertain order-email send first")
                matches[0].pop("delivery_hold")
                return {"released": request["job_id"], "sent": False,
                        "next": "Original scoped native order-email timer may now run; use its existing cancel controls to discard."}
            job = jobs.get(identifier(request.get("job_id"), "job_id"))
            if job is None:
                raise HouseholdError("original delivery job_id is required")
            if action == "get":
                return summary(job, request.get("offset", 0), request.get("part_id"))
            part = next((p for p in job["parts"] if p["id"] == request.get("part_id")), None)
            if part is None:
                raise HouseholdError("exact original part_id is required")
            if action == "read":
                return self._delivery_read(job, part, request)
            if action in {"release_hold", "discard"}:
                if part["status"] in {"attempting", "unknown", "accepted"}:
                    raise HouseholdError("accepted/uncertain sends cannot be released or discarded; reconcile first")
                if action == "discard":
                    part["status"], part["held"] = "discarded", False
                else:
                    if delivery["paused"] or not delivery["preferences"][part["channel"]]["enabled"]:
                        raise HouseholdError("resume and enable the channel before releasing held work")
                    part["held"] = False
                return summary(job)
            if action == "retry":
                if part["status"] != "not_sent":
                    raise HouseholdError("retry requires affirmative evidence that the original send did not occur")
                part["status"] = "ready"
                return summary(job)
            if action == "begin":
                if delivery["paused"] or part.get("held") or not delivery["preferences"][part["channel"]]["enabled"]:
                    return {"dispatch": False, "reason": "delivery or this original part is held"}
                if part["status"] != "ready":
                    return {"dispatch": False, "status": part["status"], "next": "Reconcile the original attempt; never blindly retry."}
                # Check exact frozen bytes before granting permission to dispatch.
                self._delivery_read(job, part, {"offset": 0})
                part["status"], part["token"] = "attempting", secrets.token_hex(24)
                part.setdefault("attempts", []).append({"token": part["token"], "outcome": "unknown"})
                return {"dispatch": True, "token": part["token"], "job_id": job["id"], "part_id": part["id"],
                        "destination": deepcopy(job["destinations"][part["channel"]]),
                        "kind": part["kind"], "sha256": part["sha256"], "bytes": part["bytes"],
                        **({"text": part["text"]} if "text" in part else {}),
                        "next": "Send this exact part once with the verified native transport. Ack accepted only with its receipt; a lost acknowledgement remains unknown."}
            if action in {"ack", "reconcile"}:
                token = request.get("token")
                if not isinstance(token, str) or not secrets.compare_digest(part.get("token", ""), token) or part["status"] not in {"attempting", "unknown", "accepted", "not_sent"}:
                    raise HouseholdError("original attempt token and part are required")
                outcome = request.get("outcome")
                if outcome not in {"accepted", "not_sent", "unknown"}:
                    raise HouseholdError("outcome must be accepted, not_sent or unknown")
                receipt = evidence(request.get("evidence")) if outcome != "unknown" else None
                if part["status"] in {"accepted", "not_sent"}:
                    if outcome != part["status"] or receipt != part.get("receipt"):
                        raise HouseholdError("a confirmed outcome cannot be overwritten")
                else:
                    part["status"], part["receipt"] = outcome, receipt
                    part["attempts"][-1].update(outcome=outcome, evidence=receipt)
                    if outcome == "accepted":
                        part["held"] = False
                return summary(job)
            raise HouseholdError("unknown recipe delivery action")

    def _delivery_read(self, job, part, request):
        if "text" in part:
            return {"text": part["text"], "sha256": part["sha256"]}
        data = read_local_file(self.store.directory / "recipe-deliveries", part["file"], maximum=MAX_FILE)
        if len(data) != part["bytes"] or hashlib.sha256(data).hexdigest() != part["sha256"]:
            raise HouseholdError("original delivery file is unavailable or changed; no replacement was generated")
        offset = request.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset < len(data):
            raise HouseholdError("file offset is outside the frozen attachment")
        chunk = data[offset:offset + CHUNK]
        return {"data_base64": base64.b64encode(chunk).decode(), "offset": offset,
                "next_offset": offset + len(chunk) if offset + len(chunk) < len(data) else None,
                "bytes": len(data), "sha256": part["sha256"], "content_type": part["content_type"],
                "filename": part["filename"]}

    def _delivery_request(self, state, request):
        delivery = state["recipe_delivery"]
        request_id = identifier(request.get("request_id"), "request_id")
        if request.get("delivery_requested") is not True:
            raise HouseholdError("request requires an explicit user intent to deliver this saved menu")
        intent = {k: request.get(k) for k in ("menu_ref", "destinations", "capabilities")}
        if request_id in delivery["jobs"]:
            job = delivery["jobs"][request_id]
            if job["intent_digest"] != digest(intent):
                raise HouseholdError("request_id is already bound to another intent; inspect the original job")
            return summary(job)
        if delivery["paused"]:
            raise HouseholdError("recipe delivery is explicitly paused")
        if len(delivery["jobs"]) >= 2000:
            raise HouseholdError("recipe delivery history is full; retain receipts and archive it explicitly")
        menu = deepcopy(exact_menu(state, request.get("menu_ref")))
        selected = {c for c, p in delivery["preferences"].items() if p["enabled"]}
        destinations = request.get("destinations")
        if not selected or not isinstance(destinations, dict) or set(destinations) != selected:
            raise HouseholdError("supply exactly the enabled channel destinations; at least one channel is required")
        caps = request.get("capabilities")
        if not isinstance(caps, dict) or set(caps) != selected:
            raise HouseholdError("each selected channel needs its actual verified native capabilities")
        caps = {c: capabilities(caps[c], c, destinations[c]) for c in selected}
        job = {"id": request_id, "purpose": "explicit_finalized_menu", "menu_ref": menu_ref(menu),
               "menu_snapshot": menu, "intent_digest": digest(intent), "destinations": deepcopy(destinations),
               "capabilities": caps, "preferences": deepcopy(delivery["preferences"]), "parts": [], "warnings": []}
        root = self.store.directory / "recipe-deliveries"
        root.mkdir(mode=0o700, exist_ok=True)
        directory = secrets.token_hex(16)
        (root / directory).mkdir(mode=0o700)
        def part(channel, kind, value, content_type=None, filename=None):
            data = value.encode() if isinstance(value, str) else value
            if len(data) > MAX_FILE:
                raise HouseholdError("frozen delivery file exceeds the supported 32 MiB bound")
            record = {"id": str(len(job["parts"]) + 1), "channel": channel, "kind": kind,
                      "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "status": "ready", "held": False}
            if isinstance(value, str):
                record["text"] = value
            else:
                relative = directory + "/" + record["id"]
                descriptor = os.open(root / relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                record.update(file=relative, content_type=content_type, filename=filename)
            job["parts"].append(record)
        assets = RecipeAssets(self.store.directory / "recipe-assets")
        # Read/render once for each actual selected format. Cached bytes remain
        # immutable through subsequent preference, recipe or source changes.
        cache, pdf_cache = {}, {}
        for channel in ("chat", "email"):
            if channel not in selected:
                continue
            pref, cap = job["preferences"][channel], caps[channel]
            image_output = pref["images"] and cap.get("images", False)
            if pref["images"] and not image_output:
                job["warnings"].append(f"{channel}: image previews unavailable")
            # PDF covers follow the channel's image preference, even when its
            # native preview transport cannot show inline images.
            for images in {False, image_output, pref["images"]}:
                if images not in cache:
                    cache[images] = render_menu(menu, assets, images=images)
            rendered = cache[image_output]
            if channel == "email":
                rendered = limit_images(rendered, cap.get("attachment_limit", 0))
            job["warnings"].extend(f"{channel}: {w}" for w in rendered["warnings"])
            job["warnings"].extend(f"{channel}: {w}" for w in cache[pref["images"]]["warnings"])
            pdf = None
            if pref["pdf"]:
                if not cap.get("pdf") or not cap.get("attachment_limit"):
                    job["warnings"].append(f"{channel}: PDF attachment unsupported; recipe text remains available")
                else:
                    try:
                        if pref["images"] not in pdf_cache:
                            pdf_cache[pref["images"]] = render_pdf(cache[pref["images"]])
                        pdf = pdf_cache[pref["images"]]
                    except Exception:
                        job["warnings"].append(f"{channel}: PDF generation failed; recipe text remains available")
                    if pdf is not None and len(pdf) > cap["attachment_limit"]:
                        pdf = None
                        job["warnings"].append(f"{channel}: PDF exceeds native attachment limit")
            if channel == "chat":
                for text in split_text(rendered["html"], cap["text_limit"]):
                    part(channel, "text", text)
                if pdf is not None:
                    part(channel, "pdf", pdf, "application/pdf", "ukesmeny.pdf")
                for cid, data in rendered["covers"].items():
                    if len(data) <= cap.get("attachment_limit", 0):
                        part(channel, "image_preview", data, "image/jpeg", "oppskriftsbilde.jpg")
                    else:
                        job["warnings"].append("chat: image exceeds native preview limit")
            else:
                destination = destinations[channel]
                raw = render_email(rendered, **destination, subject="Ukesmeny " + str(menu.get("week")), pdf=pdf)
                if len(raw) > cap["message_limit"]:
                    # One email is one dispatch. Do not truncate recipes or
                    # silently fan out emails when the actual sender rejects it.
                    raw = render_email(cache[False], **destination, subject="Ukesmeny " + str(menu.get("week")))
                    job["warnings"].append("email: attachments omitted to fit native message limit")
                if len(raw) > cap["message_limit"]:
                    job["warnings"].append("email: complete recipe text exceeds native message limit; email not dispatched")
                    for text in split_text(cache[False]["html"], 100000):
                        part(channel, "text_fallback", text)
                        job["parts"][-1]["status"] = "unavailable"
                else:
                    part(channel, "email", raw, "message/rfc822", "ukesmeny.eml")
        job["warnings"] = list(dict.fromkeys(job["warnings"]))
        delivery["jobs"][request_id] = job
        return summary(job)
