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
PROTECTED_GUIDANCE = ("Muse's protected Oda connection supports catalog, cart, delivery and order reads, "
                      "guarded cart changes and delivery selection. Browser checkout, order edits, "
                      "email and scheduling are unavailable. Recipes use the builtin bank and supplied text.")
PROTECTED_ALLOWED = {**ALLOWED,
    "catalog": {"products", "recipes", "usuals"},
    "products": {"get", "prepare", "record_ingredients", "apply", "lowest_cost"},
    "cart": {None, "get", "change", "apply", "set", "update", "ensure", "clear", "reconcile_change", "sync", "reconcile", "weekly"},
    "delivery": {None, "list", "select"}, "orders": {None, "list", "get"},
    "product_favorites": {"list", "add", "remove"}, "recurring": {"list", "add", "remove", "substitute"},
    "menu": ALLOWED["menu"] | {"lock", "clear", "replan_prepare", "replan_apply", "batch_prepare", "batch_apply"},
}
NATIVE_BROWSER_GUIDANCE = ("Muse's protected Oda connection and opted-in native browser support manual new "
    "saved-card checkout and cancellation of this household's confirmed checkout orders. "
    "Native task receipts, fresh reviews and provider/device approval remain required. "
    "Order edits, payment retry/switching, weekly checkout, email and scheduling are unavailable.")


def _owned_checkout(state, order_id):
    return (state.get("provider") == "oda"
        and state.get("order_snapshot_providers", {}).get(order_id, "oda") == "oda"
        and any(isinstance(row, Mapping) and row.get("kind") == "checkout"
        and isinstance(row.get("result"), Mapping) and row["result"].get("confirmed") is True
        and row["result"].get("changed_existing_order") is not True
        and row.get("target_id") == order_id == row["result"].get("order_id")
        for row in state.get("protected_results", {}).values()))


def guard_native_browser(request, state):
    """Keep the optional adapter on the implemented manual core paths."""
    operation, action = request["operation"], request.get("action")
    if operation == "checkout":
        allowed = {"operation", "action", "checkout_payment", "identity_review"} if action == "prepare" else {
            "operation", "action", "confirmation_id"}
        if set(request) - (allowed | {"contract"}):
            raise HouseholdError("Muse native checkout requires a manual new-order request")
        if state.get("order_change"):
            raise HouseholdError("Finish the original order change outside Muse native checkout")
        pending = state.get("pending_checkout")
        if action != "prepare":
            confirmation = request.get("confirmation_id")
            if not isinstance(confirmation, str) or not confirmation:
                raise HouseholdError("Muse checkout requires the exact confirmation_id")
            record = state.get("protected_results", {}).get(confirmation)
            if (isinstance(record, Mapping) and record.get("kind") == "checkout"
                    and isinstance(record.get("result"), Mapping)
                    and record["result"].get("confirmed") is True):
                return
            if not isinstance(pending, Mapping) or pending.get("confirmation_id") != confirmation:
                raise HouseholdError("Muse checkout confirmation does not match its original journal")
        if pending and (not isinstance(pending, Mapping) or any(pending.get(key) for key in (
                "order_change", "occurrence", "automatic_checkout", "scheduler_context", "recovery", "payment_switch"))
                or (pending.get("checkout_payment") or {}).get("method") != "saved_card"):
            raise HouseholdError("Preserve the unsupported original checkout journal")
        from core import checkout_payment_settings
        payment = request.get("checkout_payment", state.get("checkout_payment")) if action == "prepare" else pending["checkout_payment"]
        if checkout_payment_settings(payment, "oda")["method"] != "saved_card":
            raise HouseholdError("Muse native checkout supports saved-card payment only")
    else:
        if set(request) - {"operation", "action", "order_id", "confirmation_id", "contract"}:
            raise HouseholdError("Muse cancellation requires its original order and confirmation")
        if action == "cancel_prepare":
            order_id = request.get("order_id")
        else:
            confirmation = request.get("confirmation_id")
            if not isinstance(confirmation, str) or not confirmation:
                raise HouseholdError("Muse cancellation requires the exact confirmation_id")
            record = state.get("protected_results", {}).get(confirmation)
            if (isinstance(record, Mapping) and record.get("kind") == "cancellation"
                    and isinstance(record.get("result"), Mapping) and record["result"].get("cancelled") is True):
                order_id = record.get("target_id")
            else:
                pending = state.get("pending_cancellation")
                if not isinstance(pending, Mapping) or pending.get("confirmation_id") != confirmation:
                    raise HouseholdError("Muse cancellation confirmation does not match its original journal")
                order_id = pending.get("order_id")
            if request.get("order_id") is not None and request["order_id"] != order_id:
                raise HouseholdError("Muse cancellation target differs from its original journal")
        if not isinstance(order_id, str) or not order_id or not _owned_checkout(state, order_id):
            raise HouseholdError("Muse native cancellation requires this household's confirmed new checkout order")


def guard_local_recipes(request):
    if request.get("operation") != "recipes":
        return
    library, libraries, reference = (request.get("library_id"), request.get("library_ids"),
                                     request.get("library_recipe_ref"))
    if (library not in (None, "builtin") or libraries is not None and libraries != ["builtin"]
            or reference is not None and (not isinstance(reference, Mapping)
                or reference.get("library_id") != "builtin")):
        raise HouseholdError("Muse recipes use the builtin bank only")
    if request.get("action") == "import":
        kind, decision = request.get("source_kind"), request.get("storage_decision")
        if kind != "transcript" and not (kind == "url" and isinstance(decision, Mapping)
                                          and decision.get("storage") == "link_only"):
            raise HouseholdError("Muse imports accept transcripts or nonfetching link-only URLs")


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
        guard_local_recipes(request)
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


class ProtectedMuseApplication(MuseApplication):
    def _observe_terminal_failure(self):
        failure = self.provider_client.terminal_failure
        if failure:
            self.integration = {"status": "unavailable", "provider": "oda", "message": failure}

    def handle(self, request):
        if not isinstance(request, Mapping):
            raise HouseholdError("request must be an object")
        if "_restore_missing_cart_digest" in request:
            raise HouseholdError("Muse cart restoration requires a freshly reviewed products.apply request")
        operation, action = request.get("operation"), request.get("action")
        native = (self.browser is not None and (
            operation == "checkout" and isinstance(action, str) and action in {"prepare", "confirm", "reconcile"}
            or operation == "orders" and isinstance(action, str) and action in {"cancel_prepare", "cancel_confirm", "cancel_reconcile"}))
        if (not isinstance(operation, str) or action is not None and not isinstance(action, str)
                or not native and (operation not in PROTECTED_ALLOWED or action not in PROTECTED_ALLOWED[operation])):
            raise HouseholdError("Unsupported Muse operation/action. " + PROTECTED_GUIDANCE)
        if native:
            guard_native_browser(request, self.store.read())
        guard_local_recipes(request)
        self._observe_terminal_failure()
        try:
            result = Application.handle(self, request)
        finally:
            self._observe_terminal_failure()
        guidance = NATIVE_BROWSER_GUIDANCE if self.browser is not None else PROTECTED_GUIDANCE
        result["client_guidance"] = guidance
        if operation == "profile" and action == "overview":
            result["details"] = guidance
        return result

    def _refresh_integration(self, *args, **kwargs):
        # A terminal startup failure stays terminal for this service. Restart
        # after supported human recovery; status must not replay rejected auth.
        if not getattr(self, "_muse_probe_attempted", False):
            self._muse_probe_attempted = True
            return super()._refresh_integration(*args, **kwargs)

    def _store_readiness(self, checkout_payment=None):
        ready = self.integration.get("status") == "ready"
        return {"provider": self.provider,
                "connection_check": {"status": "verified" if ready else "unknown",
                    "scope": "Last protected MCP initialize/tools-list only; account, address and payment readiness remain unverified.",
                    "next_action": None if ready else "Inspect the original error and use Muse's provider recovery guidance before restarting."},
                "browser_check": {"status": "configured" if self.browser is not None else "not_configured",
                    "action_mode": getattr(self.browser, "action_mode", None),
                    "last_checkout_review_at": getattr(self.browser, "last_review_at", None), "next_action": None},
                "delivery_check": {"status": "unknown"}, "payment_check": {"status": "unknown"},
                "local_recipes_available": True,
                "note": NATIVE_BROWSER_GUIDANCE if self.browser is not None else PROTECTED_GUIDANCE}

    def _user_guide(self):
        return {**MuseApplication._user_guide(),
                "after_setup": ("Plan from exact local recipe candidates, prepare products, review and apply guarded cart changes, and select delivery. "
                    + ("Use a fresh native review for manual new saved-card checkout; preserve task and core journals for reconciliation."
                       if self.browser is not None else "Browser checkout is unavailable.")),
                "presentation": "Explain demonstrated Muse capabilities and unverified checkout readiness in the user's language."}


def initialize(home: Path, provider: str, household: str, *, credential_name=None, operation_directory=None) -> None:
    if provider not in ORIGINS or not household.strip() or len(household) > 100:
        raise HouseholdError("Muse initialization requires a provider and bounded household name")
    if len(str(home / "service.sock").encode()) > 100:
        raise HouseholdError("Choose a shorter Muse home; its Unix socket path must fit within 100 bytes")
    marker = {"format": 1, "kind": "catalog_local_planning", "household": household, "provider": provider}
    if credential_name is not None or operation_directory is not None:
        from muse_mcp import MuseProtectedMcpClient
        if provider != "oda" or credential_name is None or operation_directory is None:
            raise HouseholdError("Protected Muse mode requires Oda, a credential name and shared operation directory")
        shop = MuseProtectedMcpClient(operation_directory, credential_name)
        marker.update(kind="protected_oda_mcp", credential_name=shop.credential_name,
                      operation_directory=str(shop.operation_directory))
    home.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("state", "profile-lock", "observations", "observations/requests", "observations/responses"):
        (home / name).mkdir(mode=0o700)
    publish_json(home / "config.json", {"household": household, "instance": "muse", "provider": provider,
        "confirmation_policy": "fresh", "recipe_libraries": [], "primary_recipe_library_id": "builtin"})
    publish_json(home / "muse-client.json", marker)


def load_home(home: Path):
    private_directory(home)
    marker = read_json(home / "muse-client.json")
    if (not isinstance(marker, dict) or marker.get("format") != 1
            or marker.get("kind") not in {"catalog_local_planning", "protected_oda_mcp"}):
        raise HouseholdError("Use a fresh household initialized by the Muse client")
    if marker["kind"] == "protected_oda_mcp":
        from muse_mcp import MuseProtectedMcpClient
        if marker.get("provider") != "oda":
            raise HouseholdError("Protected Muse mode requires Oda")
        MuseProtectedMcpClient(marker.get("operation_directory"), marker.get("credential_name"))
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


def serve(home: Path, *, browser_directory=None, browser_task_id=None,
          browser_action_mode="timed") -> None:
    settings = load_home(home)
    marker = read_json(home / "muse-client.json")
    protected = marker["kind"] == "protected_oda_mcp"
    if (browser_directory is None) != (browser_task_id is None):
        raise HouseholdError("Muse native browser requires both directory and original task identity")
    if browser_action_mode != "timed" and browser_directory is None:
        raise HouseholdError("Muse action mode requires its opted-in native browser")
    if browser_directory is not None and not protected:
        raise HouseholdError("Muse native browser requires protected Oda mode")
    browser = None
    if protected:
        from muse_mcp import MuseProtectedMcpClient
        shop = MuseProtectedMcpClient(marker["operation_directory"], marker["credential_name"])
        if browser_directory is not None:
            if Path(browser_directory) != shop.operation_directory / "browser":
                raise HouseholdError("Muse browser must use the canonical shared provider browser directory")
            from muse_browser import MuseBrowser
            browser = MuseBrowser(browser_directory, browser_task_id, shop,
                                  action_mode=browser_action_mode)
    else:
        shop = HostObservationShop(home / "observations", settings["provider"])
    with ownership(home / "state", home / "profile-lock"):
        cls = ProtectedMuseApplication if protected else MuseApplication
        store = StateStore(home / "state", settings)
        if browser is not None:
            browser.state_store = store
        app = cls(store, shop, browser, external_recipe_sources={})
        Server(home / "service.sock", os.getgid(), os.getuid(), app).run()


def respond(home: Path, request_id: str, response) -> None:
    settings = load_home(home)
    if read_json(home / "muse-client.json")["kind"] != "catalog_local_planning":
        raise HouseholdError("Protected Muse mode does not accept catalog observation files")
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
    parser.add_argument("--credential-name", help="Muse connected custom Oda credential reference, never a token")
    parser.add_argument("--operation-directory", type=Path, help="existing private shared directory for every native Oda client")
    parser.add_argument("--browser-directory", type=Path, help="existing canonical private native browser broker; run only")
    parser.add_argument("--browser-task-id", help="actual original Muse browser task identity; run only")
    parser.add_argument("--browser-action-mode", choices=("timed", "native_approval"),
                        help="final-action authority: timed permit or one-shot native approval delegation; run only")
    args = parser.parse_args()
    try:
        if not args.home.is_absolute():
            raise HouseholdError("Muse home must be absolute")
        if args.action != "run" and (args.browser_directory is not None or args.browser_task_id is not None
                                     or args.browser_action_mode is not None):
            raise HouseholdError("Muse native browser options apply only to run")
        if args.action == "init":
            if not args.provider or not args.household:
                raise HouseholdError("init requires --provider and --household")
            initialize(args.home, args.provider, args.household, credential_name=args.credential_name,
                       operation_directory=args.operation_directory)
        elif args.action == "run":
            serve(args.home, browser_directory=args.browser_directory, browser_task_id=args.browser_task_id,
                  browser_action_mode=args.browser_action_mode or "timed")
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
