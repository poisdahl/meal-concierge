#!/usr/bin/env python3
"""Foreground native-host observations into read-only product planning."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import on_demand
from core import HouseholdError
from product_observations import normalize_meny_product_search
from product_planner import build_product_plan, ingredient_search, menu_requirements
from runtime_ownership import file_lock

MAX_RESPONSE = 65536
MAX_RESULT = on_demand.MAX_MENU
PRODUCT_FIELDS = {"product_id", "name", "package", "price", "unit_price", "available",
                  "original_price", "campaign_tag", "campaign", "deposit", "detail_deposit",
                  "detail_price", "detail_original_price", "deposit_status"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def object_fields(value, allowed, required):
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise ValueError("unsupported or missing observation fields")


def existing_root(path):
    path = Path(path).absolute()
    on_demand._private_directory(path)
    if path.resolve(strict=True) != path:
        raise ValueError("observation and batch roots must have canonical paths")
    return path


def batch_menu(path):
    path = existing_root(path)
    result = on_demand.inspect(path)
    if result.get("status") != "completed" or "menu_snapshot" not in result:
        raise ValueError("original completed batch with a frozen menu is required; no state initialized")
    data = on_demand._read_file(path / "menu.json", on_demand.MAX_MENU)
    menu = on_demand._json(data)
    if not isinstance(menu, dict) or any(menu.get(k) != v for k, v in result["menu_ref"].items()):
        raise ValueError("frozen menu does not match the completed batch")
    return path, result, menu


def requirements(path):
    _, result, menu = batch_menu(path)
    required, unresolved = menu_requirements(menu)
    return {"kind": "observation_requirements", "dispatchable": False,
            "batch_ref": result["menu_ref"], "requirements": required,
            "unresolved_requirements": unresolved,
            "queries": {r["requirement_id"]: ingredient_search(r["item"], "meny") for r in required}}


def exclusive_json(path, value, maximum):
    data = encoded(value)
    if len(data) > maximum:
        raise ValueError("observation record exceeds its byte limit")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    on_demand._sync_directory(path.parent)


def issue_request(batch, root, value):
    object_fields(value, {"requirement_id", "limit"}, {"requirement_id"})
    limit = value.get("limit", 5)
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("limit must be an integer from 1 to 5")
    batch, result, menu = batch_menu(batch)
    required, _ = menu_requirements(menu)
    requirement = next((r for r in required if r["requirement_id"] == value["requirement_id"]), None)
    if requirement is None:
        raise ValueError("requirement_id must identify the exact frozen menu")
    root = Path(root).absolute()
    if root.parent.resolve(strict=True) != root.parent:
        raise ValueError("observation root parent must be canonical")
    now = datetime.now(timezone.utc)
    request = {"format": 1, "request_id": str(uuid.uuid4()), "provider": "meny",
               "batch": str(batch), "menu_ref": result["menu_ref"],
               "menu_sha256": result["menu_snapshot"]["sha256"],
               "requirement_id": requirement["requirement_id"],
               "query": ingredient_search(requirement["item"], "meny"), "limit": limit,
               "origin": "https://meny.no", "emitted_at": now.isoformat(),
               "expires_at": (now + timedelta(minutes=5)).isoformat()}
    root.mkdir(mode=0o700)  # Existing/interrupted requests are never adopted.
    on_demand._sync_directory(root.parent)
    with file_lock(root / "command.lock"):
        exclusive_json(root / "request.json", request, MAX_RESPONSE)
    return request


def timestamp(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("observation timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("observation timestamp must be timezone-aware")
    return parsed


def load_request(root):
    request = on_demand._json(on_demand._read_file(root / "request.json", MAX_RESPONSE))
    object_fields(request, {"format", "request_id", "provider", "batch", "menu_ref", "menu_sha256",
                            "requirement_id", "query", "limit", "origin", "emitted_at", "expires_at"},
                  {"format", "request_id", "provider", "batch", "menu_ref", "menu_sha256",
                   "requirement_id", "query", "limit", "origin", "emitted_at", "expires_at"})
    if request["format"] != 1 or request["provider"] != "meny" or request["origin"] != "https://meny.no":
        raise ValueError("unsupported observation request")
    return request


def validate_observation(request, value):
    object_fields(value, {"request_id", "query", "source_url", "observed_at", "products", "candidate_refs"},
                  {"request_id", "query", "source_url", "observed_at", "products", "candidate_refs"})
    if value["request_id"] != request["request_id"] or value["query"] != request["query"]:
        raise ValueError("observation does not match the original request")
    url = value["source_url"]
    if not isinstance(url, str) or len(url) > 2000 or any(ord(c) <= 32 for c in url):
        raise ValueError("observation source URL is invalid")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "meny.no" or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or not (parsed.path == "/varer" or parsed.path.startswith("/varer/"))):
        raise ValueError("observation source must be a public MENY product/search URL")
    observed = timestamp(value["observed_at"])
    now = datetime.now(timezone.utc)
    if not timestamp(request["emitted_at"]) <= observed <= now < timestamp(request["expires_at"]):
        raise ValueError("observation is stale, future-dated or outside its request lifetime")
    products = value["products"]
    if not isinstance(products, list) or len(products) > request["limit"]:
        raise ValueError("observation exceeds the requested product count")
    for product in products:
        object_fields(product, PRODUCT_FIELDS, {"product_id", "name"})
        if not isinstance(product["product_id"], str) or re.fullmatch(
                r"/varer/[A-Za-z0-9._~%/-]+-\d{4,14}", product["product_id"]) is None:
            raise ValueError("product_id must be the actually observed MENY product path")
        for key, field in product.items():
            if key == "available":
                if field is not None and type(field) is not bool:
                    raise ValueError("available must be an observed boolean or null")
            elif field is not None and (not isinstance(field, str) or len(field) > 500):
                raise ValueError("product display fields must be bounded text or null")
    normalized = normalize_meny_product_search({"query": request["query"], "products": products},
                                               observed_at=value["observed_at"])
    refs = value["candidate_refs"]
    actual = {p["product_ref"] for p in normalized["products"]}
    if (not isinstance(refs, list) or len(refs) > len(actual)
            or any(not isinstance(r, str) or r not in actual for r in refs) or len(set(refs)) != len(refs)):
        raise ValueError("candidate_refs must select unique actually observed MENY paths")
    return normalized


def retained_result(root, request):
    result = on_demand._json(on_demand._read_file(root / "result.json", MAX_RESULT))
    data = on_demand._read_file(root / "observation.json", MAX_RESPONSE)
    if (not isinstance(result, dict) or result.get("request_id") != request["request_id"]
            or result.get("dispatchable") is not False or result.get("observation_sha256") != digest(data)):
        raise ValueError("retained observation result is missing or changed")
    return result


def plan(root, value):
    root = existing_root(root)
    # Require the existing lock before file_lock can create any file.
    on_demand._read_file(root / "command.lock", 0)
    with file_lock(root / "command.lock"):
        request = load_request(root)
        _, batch, menu = batch_menu(request["batch"])
        if batch["menu_ref"] != request["menu_ref"] or batch["menu_snapshot"]["sha256"] != request["menu_sha256"]:
            raise ValueError("original batch/menu binding changed")
        supplied = encoded(value)
        if len(supplied) > MAX_RESPONSE:
            raise ValueError("observation exceeds its byte limit")
        if os.path.lexists(root / "observation.json"):
            original = on_demand._read_file(root / "observation.json", MAX_RESPONSE)
            if supplied != original:
                raise ValueError("the original observation is already accepted; changed input conflicts")
            return retained_result(root, request)  # Never replay an interrupted publication.
        normalized = validate_observation(request, value)
        approvals = []
        if value["candidate_refs"]:
            approvals = [{"requirement_id": request["requirement_id"],
                          "candidate_refs": value["candidate_refs"], "search_query": request["query"]}]
        result = build_product_plan(provider="meny", binding=request["menu_ref"], menu=menu,
                                    observations={request["requirement_id"]: normalized},
                                    candidate_approvals=approvals, price_mode="exact",
                                    deadline=time.monotonic() + 20)
        output = {"kind": "host_observed_plan", "dispatchable": False,
                  "request_id": request["request_id"], "batch_ref": request["menu_ref"],
                  "observation_sha256": digest(supplied),
                  "provenance": {"kind": "host_attested", "source_url": value["source_url"],
                                 "observed_at": value["observed_at"]},
                  "status": "candidate_plan" if result["status"] == "prepared" else "needs_input",
                  "requirements": result["requirements"],
                  "unresolved_requirements": result["unresolved_requirements"],
                  "candidate_totals": result.get("totals"), "excluded_costs": result["excluded_costs"],
                  "warnings": batch["warnings"]}
        if len(encoded(output)) > MAX_RESULT:
            raise ValueError("planning result exceeds its byte limit")
        exclusive_json(root / "observation.json", value, MAX_RESPONSE)
        exclusive_json(root / "result.json", output, MAX_RESULT)
        return output


def inspect(root):
    root = existing_root(root)
    on_demand._read_file(root / "command.lock", 0)
    with file_lock(root / "command.lock"):
        request = load_request(root)
        if not os.path.lexists(root / "result.json"):
            return {"kind": "host_observed_plan", "dispatchable": False, "status": "incomplete",
                    "request_id": request["request_id"], "message": "No completed result; no replay performed."}
        return retained_result(root, request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("requirements", "request", "plan", "inspect"))
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    try:
        if args.action in {"requirements", "request"} and args.batch is None:
            raise ValueError("this command requires --batch")
        if args.action != "requirements" and args.root is None:
            raise ValueError("this command requires --root")
        if args.action in {"request", "plan"}:
            data = sys.stdin.buffer.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise ValueError("input exceeds its byte limit")
            value = on_demand._json(data)
        result = (requirements(args.batch) if args.action == "requirements"
                  else issue_request(args.batch, args.root, value) if args.action == "request"
                  else plan(args.root, value) if args.action == "plan" else inspect(args.root))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
        return 0
    except (HouseholdError, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "dispatchable": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
