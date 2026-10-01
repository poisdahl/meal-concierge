#!/usr/bin/env python3
"""Limited native-host catalog observations and local planning over the real RPC."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Mapping
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore
from product_observations import MAX_PRODUCTS, normalize_retail_product_search
from runtime_ownership import ownership
from service import Application, Server, config
from service_common import strict_json_loads


MAX_RESPONSE = 65_536
WAIT_SECONDS = 90.0
ORIGINS = {"oda": "https://oda.com", "mathem": "https://www.mathem.se"}
READINESS = ("Store account readiness is unverified; this client supports catalog "
             "observation and local planning only.")
GUIDANCE = ("This client supports host-attested catalog observations and local planning. "
            "Account access, cart, delivery, orders, checkout, email and scheduling are unavailable.")
ALLOWED = {
    "health": {None}, "status": {None}, "setup": {"show"},
    "profile": {"show", "overview", "update", "review_pantry"},
    "recipes": {"search", "get", "resolve", "libraries", "import", "save", "update",
                "adapt", "convert", "accept_estimates"},
    "menu": {"get", "assess", "resolve_handoff", "plan", "save", "add_slot", "edit_slots"},
    "catalog": {"products"}, "products": {"get", "prepare", "record_ingredients"},
}


def private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HouseholdError("Muse directories must be private owned directories, without symlinks")


def read_json(path: Path, *, maximum: int = MAX_RESPONSE):
    """Opening nonblocking also prevents a substituted FIFO from hanging the host."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise HouseholdError("Muse input must be a private owned regular file")
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise HouseholdError("Muse observation exceeds the byte limit")
        return strict_json_loads(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise HouseholdError("Muse input is not valid bounded JSON") from exc


def publish_json(path: Path, value) -> None:
    """Publish a complete file once, without replacing an earlier response."""
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_RESPONSE:
        raise HouseholdError("Muse observation exceeds the byte limit")
    descriptor, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def timestamp(value) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise HouseholdError("Muse observation timestamp is invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HouseholdError("Muse observation timestamp is invalid") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise HouseholdError("Muse observation timestamp must include its timezone")
    return result


def bounded_text(value, maximum: int) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def validate_response(request, response):
    if not isinstance(response, dict) or set(response) != {
        "request_id", "provider", "query", "page", "size", "source_url", "observed_at", "hasMore", "products"
    }:
        raise HouseholdError("Muse response fields changed")
    for key in ("request_id", "provider", "query"):
        if response[key] != request[key] or not isinstance(response[key], str):
            raise HouseholdError("Muse response " + key + " changed")
    for key in ("page", "size"):
        if type(response[key]) is not int or response[key] != request[key]:
            raise HouseholdError("Muse response " + key + " changed")
    source = response["source_url"]
    if not bounded_text(source, 2_000):
        raise HouseholdError("Muse response source_url is invalid")
    try:
        url = urlsplit(source)
        origin = urlsplit(ORIGINS[request["provider"]])
        correct = (url.scheme == "https" and url.hostname == origin.hostname
                   and url.port in {None, 443} and url.username is None and url.password is None
                   and not url.fragment)
    except ValueError:
        correct = False
    if not correct:
        raise HouseholdError("Muse response source_url changed")
    emitted, expires, observed = (timestamp(request["emitted_at"]), timestamp(request["expires_at"]),
                                  timestamp(response["observed_at"]))
    now = datetime.now(timezone.utc)
    if not emitted <= observed <= now < expires:
        raise HouseholdError("Muse observation is stale, future or expired")
    if type(response["hasMore"]) is not bool:
        raise HouseholdError("Muse observation requires actual boolean pagination evidence")
    products = response["products"]
    if not isinstance(products, list) or len(products) > request["size"]:
        raise HouseholdError("Muse observation exceeds its requested product scope")
    fields = {"id", "name", "description", "price", "unitPrice", "unitName", "availability"}
    for product in products:
        if not isinstance(product, dict) or set(product) - fields:
            raise HouseholdError("Muse product fields changed")
        for field in ("name", "description", "price", "unitPrice", "unitName"):
            text = product.get(field)
            if text is not None and not bounded_text(text, 300):
                raise HouseholdError("Muse product text is invalid")
        available = product.get("availability")
        if available is not None and type(available) is not bool:
            raise HouseholdError("Muse product availability must be boolean or unknown")
    normalized = normalize_retail_product_search({"result": [{"query": request["query"],
        "products": products, "hasMore": response["hasMore"]}]},
        observed_at=response["observed_at"], provider=request["provider"])
    normalized["query"] = request["query"]
    normalized["scope"]["requested_size"] = request["size"]
    normalized["source"] = {"kind": "host_attested", "url": source, "request_id": request["request_id"]}
    return normalized


class HostObservationShop:
    def __init__(self, directory: Path, provider: str):
        if provider not in ORIGINS:
            raise HouseholdError("Muse catalog provider must be oda or mathem")
        self.provider = provider
        self.directory = directory
        private_directory(directory)
        for name in ("requests", "responses"):
            private_directory(directory / name)

    def probe(self):
        # A successful core probe always means ready. No account is proved here.
        raise HouseholdError(READINESS)

    def call(self, tool, arguments, *, deadline=None):
        if tool != "product_search":
            raise HouseholdError("Muse catalog client does not support provider tool " + str(tool))
        if not isinstance(arguments, Mapping) or set(arguments) != {"queries", "page", "size"}:
            raise HouseholdError("Muse product search arguments changed")
        queries, page, size = arguments["queries"], arguments["page"], arguments["size"]
        if (not isinstance(queries, list) or len(queries) != 1 or not bounded_text(queries[0], 200)
                or not queries[0].strip()
                or type(page) is not int or page != 1 or type(size) is not int or not 1 <= size <= MAX_PRODUCTS):
            raise HouseholdError("Muse product search must be one bounded page-1 query")
        started = time.monotonic()
        if deadline is not None and (type(deadline) not in {int, float} or not math.isfinite(deadline)):
            raise HouseholdError("Muse observation deadline must be finite")
        cutoff = min(started + WAIT_SECONDS, deadline) if deadline is not None else started + WAIT_SECONDS
        if cutoff <= started:
            raise HouseholdError("Muse observation deadline reached")
        emitted = datetime.now(timezone.utc)
        request = {"request_id": uuid.uuid4().hex, "provider": self.provider, "query": queries[0],
                   "page": page, "size": size, "emitted_at": emitted.isoformat(),
                   "expires_at": (emitted + timedelta(seconds=cutoff - started)).isoformat(),
                   "source_origin": ORIGINS[self.provider]}
        request_path = self.directory / "requests" / (request["request_id"] + ".json")
        response_path = self.directory / "responses" / (request["request_id"] + ".json")
        publish_json(request_path, request)
        try:
            while time.monotonic() < cutoff:
                try:
                    response = read_json(response_path)
                except FileNotFoundError:
                    time.sleep(min(0.05, max(0, cutoff - time.monotonic())))
                    continue
                if time.monotonic() >= cutoff:
                    break
                result = validate_response(request, response)
                if time.monotonic() >= cutoff:
                    break
                return result
            raise HouseholdError("Muse observation deadline reached")
        except OSError as exc:
            raise HouseholdError("Muse observation file is unavailable or unsafe") from exc
        finally:
            request_path.unlink()


class MuseApplication(Application):
    def handle(self, request):
        if not isinstance(request, Mapping):
            raise HouseholdError("request must be an object")
        operation, action = request.get("operation"), request.get("action")
        if (not isinstance(operation, str) or action is not None and not isinstance(action, str)
                or operation not in ALLOWED or action not in ALLOWED[operation]):
            raise HouseholdError("Unsupported Muse operation/action. " + GUIDANCE)
        if operation == "products" and action == "prepare" and request.get("include_recurring") is not False:
            raise HouseholdError("Muse product preparation requires include_recurring=false")
        if operation == "recipes":
            library = request.get("library_id")
            libraries = request.get("library_ids")
            reference = request.get("library_recipe_ref")
            if (library not in (None, "builtin") or libraries is not None and libraries != ["builtin"]
                    or reference is not None and (not isinstance(reference, Mapping)
                        or reference.get("library_id") != "builtin")):
                raise HouseholdError("Muse recipes use the builtin bank only")
            if action == "import":
                kind, decision = request.get("source_kind"), request.get("storage_decision")
                if kind != "transcript" and not (kind == "url" and isinstance(decision, Mapping)
                                                  and decision.get("storage") == "link_only"):
                    raise HouseholdError("Muse imports accept transcripts or nonfetching link-only URLs")
        result = super().handle(request)
        result["client_guidance"] = GUIDANCE
        if operation == "profile" and action == "overview":
            result["details"] = GUIDANCE
        if operation == "products":
            result.pop("apply_arguments", None)
            result.pop("partial_apply_arguments", None)
            continuation = result.get("continue_arguments")
            if isinstance(continuation, dict):
                if continuation.get("action") == "prepare":
                    continuation["include_recurring"] = False
                else:
                    result.pop("continue_arguments")
            result["next"] = "Review this local plan or continue prepare with include_recurring=false. Cart apply is unavailable in this client."
        return result

    def _plan_menu(self, value, *args, **kwargs):
        # Ref/handoff reconstruction also passes here; ingress alone is insufficient.
        if (not isinstance(value, Mapping) or not isinstance(value.get("candidates"), list)
                or not value["candidates"] or value.get("web_candidates") is not None
                or value.get("web_search_result") is not None):
            raise HouseholdError("Muse planning requires explicit local recipe/discovery candidates")
        return super()._plan_menu(value, *args, **kwargs)

    def _store_readiness(self, checkout_payment=None):
        return {"provider": self.provider,
                "connection_check": {"status": "unknown", "scope": READINESS, "next_action": None},
                "browser_check": {"status": "not_configured", "next_action": None},
                "delivery_check": {"status": "unknown"}, "payment_check": {"status": "unknown"},
                "local_recipes_available": True, "note": GUIDANCE}

    @staticmethod
    def _user_guide():
        return {"during_preferences": "Choose meals, portions, dietary needs and ingredients to use up.",
                "during_recipe_sources": "Use your built-in recipe bank. Import supplied transcripts or keep a recipe link without fetching it.",
                "after_setup": "Plan from exact local recipe candidates, edit the menu and review observed product candidates. Shopping and checkout are unavailable in this client.",
                "presentation": "Explain the relevant local capabilities and current limits in the user's language."}


def initialize(home: Path, provider: str, household: str) -> None:
    if provider not in ORIGINS or not household.strip() or len(household) > 100:
        raise HouseholdError("Muse initialization requires a provider and bounded household name")
    if len(str(home / "service.sock").encode()) > 100:
        raise HouseholdError("Choose a shorter Muse home; its Unix socket path must fit within 100 bytes")
    home.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("state", "profile-lock", "observations", "observations/requests", "observations/responses"):
        (home / name).mkdir(mode=0o700)
    publish_json(home / "config.json", {"household": household, "instance": "muse", "provider": provider,
        "confirmation_policy": "fresh", "recipe_libraries": [], "primary_recipe_library_id": "builtin"})
    publish_json(home / "muse-client.json", {"format": 1, "kind": "catalog_local_planning",
                                             "household": household, "provider": provider})


def load_home(home: Path):
    private_directory(home)
    marker = read_json(home / "muse-client.json")
    if not isinstance(marker, dict) or marker.get("format") != 1 or marker.get("kind") != "catalog_local_planning":
        raise HouseholdError("Use a fresh household initialized by the Muse client")
    # Read with no-follow before the established core config normalizer.
    raw = read_json(home / "config.json")
    if not isinstance(raw, dict) or set(raw) - {"household", "instance", "provider", "confirmation_policy",
                                               "recipe_libraries", "primary_recipe_library_id"}:
        raise HouseholdError("Muse configuration must contain only the local client settings")
    settings = config(home / "config.json")
    if (settings["provider"] not in ORIGINS or settings["provider"] != marker.get("provider")
            or settings["household"] != marker.get("household")
            or settings["primary_recipe_library_id"] != "builtin"
            or any(row["library_id"] != "builtin" for row in settings["recipe_libraries"])):
        raise HouseholdError("Muse configuration requires its own builtin-only household")
    for name in ("state", "profile-lock", "observations", "observations/requests", "observations/responses"):
        private_directory(home / name)
    return settings


def serve(home: Path) -> None:
    settings = load_home(home)
    shop = HostObservationShop(home / "observations", settings["provider"])
    with ownership(home / "state", home / "profile-lock"):
        app = MuseApplication(StateStore(home / "state", settings), shop, None)
        Server(home / "service.sock", os.getgid(), os.getuid(), app).run()


def respond(home: Path, request_id: str, response) -> None:
    settings = load_home(home)
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise HouseholdError("Muse request_id must be one emitted UUID")
    path = home / "observations/requests" / (request_id + ".json")
    request = read_json(path)
    if (not isinstance(request, dict) or request.get("request_id") != request_id
            or request.get("provider") != settings["provider"]
            or request.get("source_origin") != ORIGINS[settings["provider"]]
            or type(request.get("page")) is not int or request["page"] != 1
            or type(request.get("size")) is not int or not 1 <= request["size"] <= MAX_PRODUCTS
            or not isinstance(request.get("query"), str)):
        raise HouseholdError("Muse request identity changed")
    validate_response(request, response)
    publish_json(home / "observations/responses" / (request_id + ".json"), response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "run", "respond"))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--provider", choices=tuple(ORIGINS))
    parser.add_argument("--household")
    parser.add_argument("--request-id")
    args = parser.parse_args()
    try:
        if not args.home.is_absolute():
            raise HouseholdError("Muse home must be absolute")
        if args.action == "init":
            if not args.provider or not args.household:
                raise HouseholdError("init requires --provider and --household")
            initialize(args.home, args.provider, args.household)
        elif args.action == "run":
            serve(args.home)
        else:
            if not args.request_id:
                raise HouseholdError("respond requires --request-id")
            data = sys.stdin.buffer.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise HouseholdError("Muse observation exceeds the byte limit")
            respond(args.home, args.request_id, strict_json_loads(data.decode("utf-8")))
        return 0
    except (HouseholdError, OSError, ValueError, UnicodeError, RecursionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
