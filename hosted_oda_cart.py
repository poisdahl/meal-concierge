"""Guarded cart changes through the attached Oda MCP."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import uuid

import hosted_oda
import hosted_oda_menu
from install import bounded_json, write_json
from on_demand import _sync_directory
from runtime_ownership import file_lock


REGISTRY_NAME = ".meal-concierge-oda-carts"
ANCHOR_NAME = REGISTRY_NAME + "-identity.json"
DOT_WORKSPACE = Path("/workspace/shared")
BINDING_NAME = "hosted-oda-cart-binding.json"
GENERATION_NAME = "hosted-oda-cart-generation.json"
FINISHED = {"restored", "aborted", "reconciled_empty", "applied"}
BEFORE_WRITE = {"reading_baseline", "baseline_verified", "checking_add", "menu_search", "menu_choices", "menu_review"}
READBACK = {"verify_add", "verify_remove"}
WRITE_PHASES = {"awaiting_add_response": 1, "awaiting_remove_response": -1}
UNVERIFIED = {"add_response_unverified", "remove_response_unverified"}
NEEDS_RECOVERY = UNVERIFIED | WRITE_PHASES.keys()
READ_TOOLS = ("get_delivery_addresses", "get_cart")
STATUSES = BEFORE_WRITE | READBACK | NEEDS_RECOVERY | FINISHED | {"added", "checking_remove"}
JOURNAL_LIMIT = 512 * 1024
JOURNAL_ENDING_RESERVE = 64 * 1024


def require_fields(value, fields):
    if not isinstance(value, dict) or value.keys() != set(fields):
        raise ValueError("unexpected fields for the hosted Oda cart action")


def provider_id(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def private_path(path, kind):
    info = path.lstat()
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("hosted Oda cart records must be private and owned by the installer user")


def read_private(path, maximum=16384):
    private_path(path, stat.S_ISREG)
    return bounded_json(path, maximum)


def workspace_registry(state):
    if DOT_WORKSPACE.resolve(strict=True) != DOT_WORKSPACE or not state.is_relative_to(DOT_WORKSPACE):
        raise ValueError("hosted Oda cart writes require a household in the retained Dot workspace")
    return DOT_WORKSPACE / REGISTRY_NAME


def verify_record(record):
    if record is None:
        return
    identity = record.get("operation_id") if isinstance(record, dict) else None
    try:
        valid = isinstance(identity, str) and str(uuid.UUID(identity)) == identity
    except ValueError:
        valid = False
    if (not valid or not isinstance(record.get("status"), str) or record["status"] not in STATUSES
            or not isinstance(record.get("owner"), str) or not isinstance(record.get("plugin_id"), str)
            or not isinstance(record.get("build"), dict)
            or "source_commit" not in record["build"] or not isinstance(record.get("account"), str)):
        raise ValueError("the original hosted Oda cart journal is invalid")
    if "menu" not in record and not provider_id(record.get("product_id")):
        raise ValueError("the original hosted Oda cart product is invalid")
    if "menu" in record:
        menu = record["menu"]
        if not isinstance(menu, dict) or menu.get("mode") not in ("keep", "restore") or not isinstance(menu.get("source"), dict):
            raise ValueError("the original hosted Oda menu journal is invalid")


@contextmanager
def registry(state, directory, *, initialize, allow_unbound=False):
    binding_file = state / BINDING_NAME
    binding = read_private(binding_file) if os.path.lexists(binding_file) else None
    if (not directory.is_absolute() or directory.name != REGISTRY_NAME
            or directory.resolve(strict=False) != directory or not directory.parent.is_dir()):
        raise ValueError("use the canonical private hosted Oda cart registry in the retained workspace")
    if binding is not None:
        require_fields(binding, ("registry", "key"))
        if binding["registry"] != str(directory):
            raise ValueError("use the original hosted Oda cart registry")
    elif not initialize and not allow_unbound:
        raise ValueError("this household has no hosted Oda cart registry binding")

    anchor = directory.parent / ANCHOR_NAME
    with file_lock(directory.parent / (REGISTRY_NAME + ".lock")):
        ledger = directory / "ledger.json"
        if not os.path.lexists(anchor) and not os.path.lexists(directory) and binding is None and initialize:
            key = uuid.uuid4().hex
            write_json(anchor, {"key": key, "write_generation": 0})
            directory.mkdir(mode=0o700)
            _sync_directory(directory.parent)
            write_json(ledger, {"key": key, "write_generation": 0, "current": None})
        anchor_value = read_private(anchor)
        require_fields(anchor_value, ("key", "write_generation"))
        private_path(directory, stat.S_ISDIR)
        saved = read_private(ledger, JOURNAL_LIMIT)
        require_fields(saved, ("key", "write_generation", "current"))
        verify_record(saved["current"])
        key = saved["key"]
        if (key != anchor_value["key"] or not isinstance(key, str) or len(key) != 32
                or any(char not in "0123456789abcdef" for char in key)):
            raise ValueError("the original hosted Oda cart registry is invalid")
        generation = saved["write_generation"]
        if (type(generation) is not int or generation < 0 or type(anchor_value["write_generation"]) is not int
                or generation != anchor_value["write_generation"]):
            raise ValueError("the hosted Oda cart ledger and workspace write generation disagree")
        expected = {"registry": str(directory), "key": key}
        witness_file = state / GENERATION_NAME
        if os.path.lexists(witness_file):
            witness = read_private(witness_file)
            require_fields(witness, ("key", "write_generation"))
            if (witness["key"] != key or type(witness["write_generation"]) is not int
                    or not 0 <= witness["write_generation"] <= generation):
                raise ValueError("the hosted Oda cart registry predates this household's last write")
        if binding is None and initialize:
            write_json(binding_file, expected)
        elif binding is not None and binding != expected:
            raise ValueError("the original hosted Oda cart registry binding changed")
        yield anchor, ledger, saved


def require_idle(state):
    """Keep the issuing program selectable until its original cart is settled."""
    bound = os.path.lexists(state / BINDING_NAME) or os.path.lexists(state / GENERATION_NAME)
    if not bound and not state.is_relative_to(DOT_WORKSPACE):
        return
    directory = workspace_registry(state)
    if not (bound or os.path.lexists(directory) or os.path.lexists(directory.parent / ANCHOR_NAME)):
        return
    with registry(state, directory, initialize=False, allow_unbound=True) as (_, ledger, saved):
        current = saved["current"]
        if (current and current["owner"] == str(state) and current["status"] not in FINISHED
                and current.get("maintenance_deferred") is not True):
            raise ValueError("finish the original hosted Oda cart operation before changing its program")
        if current and current["owner"] == str(state) and "menu" in current and current["status"] in FINISHED:
            archive_record(directory, current)
            write_json(ledger, {**saved, "current": None})


def now():
    return datetime.now(timezone.utc)


def target_quantities(record):
    if "menu" not in record:
        return {str(record["product_id"]): 1}
    menu = record["menu"]
    if "partial_add_quantities" in menu:
        return menu["partial_add_quantities"]
    return {str(row["product_id"]): row["package_count"] for row in menu["quote"]["items"]}


def frame(record, *, observed=None):
    started = now()
    expires = started + timedelta(minutes=5) if observed is None else observed + timedelta(minutes=2)
    if expires <= started:
        raise ValueError("the Oda pre-dispatch observation expired")
    value = {"frame_id": uuid.uuid4().hex, "plugin_id": record["plugin_id"],
             "issued_at": started.isoformat(), "expires_at": expires.isoformat()}
    if observed is not None:
        sign = WRITE_PHASES[record["status"]]
        return {**value, "tool": "manipulate_cart",
                "arguments": {"operations": [{"productId": int(product_id), "quantity": sign * quantity}
                                             for product_id, quantity in target_quantities(record).items()]}}
    tools = [{"tool": name, "arguments": {}} for name in READ_TOOLS]
    if "menu" in record and record["status"] in {"menu_search", "checking_add"}:
        tools.insert(1, {"tool": "product_search", "arguments": record["menu"]["search_arguments"]})
        if record["status"] == "menu_search":
            tools.pop()
    return {**value, "tools": tools}


def match_frame(saved, value):
    if value["frame_id"] != saved["frame_id"] or value["plugin_id"] != saved["plugin_id"]:
        raise ValueError("the observation belongs to a different hosted Oda frame or plugin")


def account_fingerprint(addresses, key):
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 100:
        raise ValueError("Oda cart writes require a stable selected-address observation")
    identifiers, selections = set(), []
    for item in addresses:
        require_fields(item, ("id", "isSelected"))
        identity, selected = item["id"], item["isSelected"]
        if not provider_id(identity) or type(selected) is not bool or identity in identifiers:
            raise ValueError("the Oda address identity observation is invalid")
        identifiers.add(identity)
        if selected:
            selections.append(identity)
    if len(selections) != 1:
        raise ValueError("Oda cart writes require exactly one selected delivery address")
    payload = hosted_oda.arguments_digest({"ids": sorted(identifiers), "selected": selections[0]})
    return hmac.new(bytes.fromhex(key), payload.encode(), hashlib.sha256).hexdigest()


def validate_cart(cart):
    if not isinstance(cart, dict) or cart.keys() != hosted_oda.CART_FIELDS | {"deliverySlot"}:
        raise ValueError("the original unreserved Oda test cart could not be verified")
    hosted_oda.cart_view({key: cart[key] for key in hosted_oda.CART_FIELDS})


def cart_quantities(cart, *, one_group=False):
    groups = cart["groups"]
    if (cart["deliverySlot"] is not None or (one_group and len(groups) > 1)
            or any(group.keys() != {"items"} for group in groups)):
        return None
    quantities = {}
    for group in groups:
        for item in group["items"]:
            product = str(item["product"]["id"])
            quantity = item["quantity"]
            if (product in quantities or type(quantity) not in (int, float)
                    or quantity <= 0 or quantity % 1):
                return None
            quantities[product] = int(quantity)
    return quantities if cart["productQuantityCount"] == sum(quantities.values()) else None


def cart_matches(cart, record, filled):
    expected = target_quantities(record) if filled else {}
    return cart_quantities(cart, one_group="menu" not in record) == expected


def bound_times(record, pending, value, key, time_field):
    match_frame(pending, value)
    address_time = hosted_oda.response_time(value["address_observed_at"])
    observation_time = hosted_oda.response_time(value[time_field])
    if not (hosted_oda.response_time(pending["issued_at"]) <= address_time <= observation_time
            <= now() < hosted_oda.response_time(pending["expires_at"])):
        raise ValueError("the original hosted Oda observation times or read frame changed")
    account = account_fingerprint(value["addresses"], key)
    if not hmac.compare_digest(record["account"], account):
        raise ValueError("the selected Oda account/address context changed")
    return address_time, observation_time


def bound_observation(record, pending, value, key, *, catalog=False):
    fields = ("action", "frame_id", "plugin_id", "address_observed_at", "cart_observed_at", "addresses", "cart")
    require_fields(value, fields + ("products_observed_at", "catalog_arguments", "catalog") if catalog else fields)
    address_time, cart_time = bound_times(record, pending, value, key, "cart_observed_at")
    validate_cart(value["cart"])
    return address_time, cart_time


def approved_menu_source(record, locks):
    source = record["menu"]["source"]
    context = locks.enter_context(hosted_oda_menu.source(source["home"], DOT_WORKSPACE))
    if context["binding"] != source:
        raise ValueError("the saved menu, shopping list, stock or household profile changed; review a new plan")
    return context


def menu_search_views(menu, value, observed):
    arguments = menu["search_arguments"]
    if hosted_oda.arguments_digest(arguments) != hosted_oda.arguments_digest(value["catalog_arguments"]):
        raise ValueError("arguments do not match the original hosted Oda product search")
    return hosted_oda.product_search_views(value["catalog"], arguments, observed.isoformat())


def observe_menu_search(record, value, key, locks):
    require_fields(value, ("action", "frame_id", "plugin_id", "address_observed_at",
                           "products_observed_at", "addresses", "catalog_arguments", "catalog"))
    if record["status"] != "menu_search":
        raise ValueError("no original Oda menu search frame is awaiting a reply")
    _, observed = bound_times(record, record["frame"], value, key, "products_observed_at")
    context = approved_menu_source(record, locks)
    menu = record["menu"]
    menu["observations"] = menu_search_views(menu, value, observed)
    menu["candidates"] = hosted_oda_menu.candidates(context, menu["observations"])
    menu["observed_at"] = observed.isoformat()
    record["status"] = "menu_choices"
    record.pop("frame")


def select_menu(record, choices, locks):
    if record["status"] not in {"menu_choices", "menu_review"}:
        raise ValueError("complete the official Oda menu search before choosing products")
    context = approved_menu_source(record, locks)
    menu = record["menu"]
    review = hosted_oda_menu.quote(context, menu["observations"], choices)
    menu.update(choices=choices, quote=review, review_digest=hosted_oda.arguments_digest({
        "source": menu["source"], "quote": review, "mode": menu["mode"], "plugin_id": record["plugin_id"],
        "operation_id": record["operation_id"], "account": record["account"],
    }))
    record["status"] = "menu_review"


def verify_menu_prices(record, value, address_time, cart_time, locks):
    observed = hosted_oda.response_time(value["products_observed_at"])
    if not address_time <= observed <= cart_time:
        raise ValueError("the fresh Oda product search must occur between the account and cart reads")
    menu = record["menu"]
    context = approved_menu_source(record, locks)
    views = menu_search_views(menu, value, observed)
    if hosted_oda_menu.quote(context, views, menu["choices"]) != menu["quote"]:
        raise ValueError("Oda package or merchandise price changed; prepare and approve a new plan")


def observe(record, value, key, locks):
    status, pending = record["status"], record.get("frame")
    if status not in {"reading_baseline", "checking_add", "checking_remove", *READBACK} or pending is None:
        raise ValueError("no original read frame can accept this observation")
    reprice = status == "checking_add" and "menu" in record
    address_time, cart_time = bound_observation(record, pending, value, key, catalog=reprice)
    if not cart_matches(value["cart"], record, status in {"verify_add", "checking_remove"}):
        raise ValueError("the Oda cart has unexpected products, grouped lines or a selected slot")
    if reprice:
        verify_menu_prices(record, value, address_time, cart_time, locks)
        if hosted_oda_menu.cart_amounts(value["cart"]) != record["menu"]["baseline_amounts_ore"]:
            raise ValueError("the Oda cart amounts changed before application; review a new plan")
    if status == "verify_add" and "menu" in record:
        menu = record["menu"]
        menu["cart_review"] = hosted_oda_menu.cart_prices(value["cart"], menu["quote"])
        menu["cart_url"] = value["cart"]["url"]
    record.pop("frame")
    record.pop("recovery_read", None)
    record.pop("recovery_observation", None)
    if status == "reading_baseline":
        if "menu" in record:
            record["menu"]["baseline_amounts_ore"] = hosted_oda_menu.cart_amounts(value["cart"])
        record["status"] = "baseline_verified"
    elif status in {"checking_add", "checking_remove"}:
        record["status"] = "awaiting_add_response" if status == "checking_add" else "awaiting_remove_response"
        record["frame"] = frame(record, observed=address_time)
    elif status == "verify_add":
        menu = record.get("menu")
        keep = (menu and menu["mode"] == "keep" and "partial_add_quantities" not in menu and menu["cart_review"]["matches_review"]
                and menu["add_reply_prices"]["matches_review"])
        record["status"] = "applied" if keep else "added"
    else:
        if "menu" in record:
            record["menu"]["restored_amounts_ore"] = hosted_oda_menu.cart_amounts(value["cart"])
        record["status"] = "restored"


def observe_recovery(record, value, key):
    pending = record.get("recovery_read")
    if record["status"] not in NEEDS_RECOVERY | READBACK | {"added"} or pending is None:
        raise ValueError("request a read-only hosted Oda recovery observation first")
    _, cart_time = bound_observation(record, pending, value, key)
    cart = value["cart"]
    observation = "other_cart"
    if cart_matches(cart, record, False):
        observation = "empty_unreserved"
    elif cart_matches(cart, record, True):
        observation = "reviewed_products_unreserved" if "menu" in record else "single_test_unit_unreserved"
    record["recovery_observation"] = {"observed_at": cart_time.isoformat(), "cart": observation}
    record.pop("recovery_read")
    status = record["status"]
    # The normal verification stages are reached only after a matching original
    # mutation reply. An unverifiable reply can never release this fence.
    if observation == "empty_unreserved" and status in READBACK | {"added"}:
        record["status"] = "restored" if status == "verify_remove" else "reconciled_empty"
        if "menu" in record:
            record["menu"]["restored_amounts_ore"] = hosted_oda_menu.cart_amounts(cart)
        record.pop("frame", None)


def acknowledge(record, value):
    require_fields(value, ("action", "frame_id", "plugin_id", "tool", "arguments",
                           "dispatched_at", "observed_at", "result_source", "is_error", "invocation_id", "cart"))
    status, pending = record["status"], record.get("frame")
    if status not in WRITE_PHASES or pending is None:
        raise ValueError("no original hosted Oda mutation is awaiting its response")
    match_frame(pending, value)
    invocation = value["invocation_id"]
    if (value["tool"] != pending["tool"] or value["result_source"] != "original_attached_mutation_response"
            or type(value["is_error"]) is not bool
            or (invocation is not None and (not isinstance(invocation, str)
                or not 1 <= len(invocation) <= 200 or not invocation.isprintable()))
            or hosted_oda.arguments_digest(value["arguments"]) != hosted_oda.arguments_digest(pending["arguments"])):
        raise ValueError("the response does not belong to the original hosted Oda mutation")
    dispatched = hosted_oda.response_time(value["dispatched_at"])
    observed = hosted_oda.response_time(value["observed_at"])
    if (not hosted_oda.response_time(pending["issued_at"]) <= dispatched < hosted_oda.response_time(pending["expires_at"])
            or not dispatched <= observed <= now()):
        raise ValueError("the original hosted Oda dispatch time could not be verified")
    added = status == "awaiting_add_response"
    result = value["cart"]
    if value["is_error"]:
        if result is not None:
            raise ValueError("a confirmed Oda provider error has no successful cart response")
        matches = False
    else:
        validate_cart(result)
        matches = cart_matches(result, record, added)
        if "menu" in record:
            menu = record["menu"]
            if added:
                if not matches:
                    # An original successful response can prove a bounded partial
                    # effect. It never permits another add, only verified cleanup.
                    actual = cart_quantities(result)
                    approved = target_quantities(record)
                    if actual and all(quantity <= approved.get(identity, 0) for identity, quantity in actual.items()):
                        menu["partial_add_quantities"] = actual
                        matches = True
                if matches:
                    menu["add_reply_prices"] = hosted_oda_menu.cart_prices(result, menu["quote"])
            elif matches:
                menu["remove_reply_amounts_ore"] = hosted_oda_menu.cart_amounts(result)
    step = "add" if added else "remove"
    record[step + "_response"] = {
        "frame_id": pending["frame_id"], "dispatched_at": dispatched.isoformat(), "observed_at": observed.isoformat(),
        "response_sha256": hosted_oda.arguments_digest(result),
        "invocation_sha256": hosted_oda.arguments_digest(invocation) if invocation is not None else None,
        "is_error": value["is_error"]}
    record.pop("recovery_read", None)
    record.pop("recovery_observation", None)
    if matches:
        record["status"] = "verify_" + step
        record["frame"] = frame(record)
    else:
        record["status"] = step + "_response_unverified"
        record.pop("frame", None)


def status_view(record):
    result = {"ok": True, "status": record["status"], "operation_id": record["operation_id"],
              "source_commit": record["build"]["source_commit"],
              "add_acknowledged": "add_response" in record, "remove_acknowledged": "remove_response" in record,
              "evidence": hosted_oda.EVIDENCE, "backend_freshness": "unverified", "dispatch": False}
    if "menu" in record:
        menu = record["menu"]
        result["menu_ref"] = menu["source"]["menu_ref"]
        result["mode"] = menu["mode"]
        fields = ("observed_at", "quote", "review_digest", "add_reply_prices", "cart_review", "cart_url",
                  "partial_add_quantities", "baseline_amounts_ore", "remove_reply_amounts_ore", "restored_amounts_ore")
        for field in fields + ("candidates",) if record["status"] == "menu_choices" else fields:
            if field in menu:
                result[field] = menu[field]
        if "restored_amounts_ore" in menu:
            result["restoration_amounts_match"] = (menu["baseline_amounts_ore"] == menu["restored_amounts_ore"]
                and menu.get("remove_reply_amounts_ore", menu["restored_amounts_ore"]) == menu["baseline_amounts_ore"])
    else:
        result["product_id"] = record["product_id"]
    if record["status"] in WRITE_PHASES:
        result["pending_response"] = {key: record["frame"][key] for key in ("frame_id", "issued_at", "expires_at")}
    if "recovery_observation" in record:
        result["recovery_observation"] = record["recovery_observation"]
    if record.get("maintenance_deferred") is True:
        result["maintenance_deferred"] = True
    return result


def advance(record, action):
    status = record["status"]
    if action == "add" and status == "baseline_verified":
        record["status"] = "checking_add"
    elif action == "remove" and status == "added":
        record["status"] = "checking_remove"
        record.pop("maintenance_deferred", None)
    elif action == "retry_read" and status in {"reading_baseline", "checking_add", "checking_remove", *READBACK, "menu_search"}:
        pass  # Only an observation can be reissued.
    elif action == "abort" and status in BEFORE_WRITE:
        record["status"] = "aborted"
        record.pop("frame", None)
        return
    else:
        raise ValueError("the hosted Oda cart stage does not allow this action; inspect the original operation")
    record.pop("recovery_read", None)
    record["frame"] = frame(record)


def continue_from_other_build(record, action):
    if record.get("maintenance_deferred") is not True:
        return False
    if action in {"acknowledge", "recheck", "observe_recovery", "defer"}:
        return True
    return action in {"observe", "retry_read"} and record["status"] in READBACK


def defer_maintenance(record):
    status = record["status"]
    if status in FINISHED | BEFORE_WRITE:
        raise ValueError("the hosted Oda cart stage does not allow deferring maintenance")
    if status in WRITE_PHASES and now() < hosted_oda.response_time(record["frame"]["expires_at"]):
        raise ValueError("wait for the original hosted Oda frame to expire before deferring")
    if status == "checking_remove":
        # No inverse has been granted. Retire its read so maintenance cannot
        # race a caller still holding the original pre-delete frame.
        record.pop("frame")
        record["status"] = "added"
    record["maintenance_deferred"] = True


def menu_request_matches(value, source):
    return (value["planning_home"] == source["home"]
            and all(value[key] == source[key] for key in ("menu_ref", "list_ref", "stock_ref")))


def initial_record(action, value, account, locks):
    if action == "begin":
        return {"product_id": value["product_id"], "account": account, "status": "reading_baseline"}
    context = locks.enter_context(hosted_oda_menu.source(value["planning_home"], DOT_WORKSPACE))
    if not menu_request_matches(value, context["binding"]):
        raise ValueError("use current menu, shopping list and stock references from the original planning home")
    return {"account": account, "status": "menu_search",
            "menu": {"source": context["binding"], "mode": value["mode"],
                     "search_arguments": hosted_oda_menu.search_arguments(context)}}


def approve_menu(record, value, locks):
    if record["status"] != "menu_review" or record["menu"]["review_digest"] != value["review_digest"]:
        raise ValueError("approve the exact Oda menu package and merchandise price review")
    approved_menu_source(record, locks)
    record["menu"].pop("observations")
    record["menu"].pop("candidates")
    record["status"] = "reading_baseline"
    record["frame"] = frame(record)


def archive_record(directory, record):
    path = directory / (record["operation_id"] + ".json")
    if not os.path.lexists(path):
        write_json(path, record)
    elif read_private(path, JOURNAL_LIMIT) != record:
        raise ValueError("the original hosted Oda cart history changed")


def write_record(ledger, authority, record):
    value = {**authority, "current": record}
    limit = JOURNAL_LIMIT - JOURNAL_ENDING_RESERVE if record["status"] in BEFORE_WRITE else JOURNAL_LIMIT
    size = len((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
    if size > limit:
        raise ValueError("the Oda shopping review exceeds its journal limit; the original operation is unchanged")
    write_json(ledger, value)


def run(state, directory, operation_id, value, plugin, build):
    action = value.get("action") if isinstance(value, dict) else None
    beginning = action in ("begin", "begin_menu")
    if action == "begin":
        require_fields(value, ("action", "product_id", "isolated_new_cart", "authorized_addresses"))
        if not provider_id(value["product_id"]) or value["isolated_new_cart"] is not True:
            raise ValueError("the owner must designate an isolated new cart and one exact Oda product")
    elif action == "begin_menu":
        require_fields(value, ("action", "planning_home", "menu_ref", "list_ref", "stock_ref", "mode",
                               "isolated_new_cart", "authorized_addresses"))
        if value["isolated_new_cart"] is not True or value["mode"] not in ("keep", "restore"):
            raise ValueError("designate an empty Oda cart and choose keep or restore for this menu operation")
    elif action == "select_menu":
        require_fields(value, ("action", "choices"))
    elif action == "approve_menu":
        require_fields(value, ("action", "review_digest"))
    elif action not in ("observe", "acknowledge", "observe_recovery", "observe_menu_search"):
        require_fields(value, ("action",))

    build = {field: build[field] for field in ("source_commit", "source_sha256", "installed_runtime_sha256")}
    owner = str(state)
    with registry(state, directory, initialize=beginning) as (anchor, ledger, saved), ExitStack() as locks:
        account = account_fingerprint(value["authorized_addresses"], saved["key"]) if beginning else None
        current = saved["current"]
        history_path = directory / (operation_id + ".json")
        if current is not None and current["operation_id"] == operation_id:
            record = current
        else:
            record = read_private(history_path, JOURNAL_LIMIT) if os.path.lexists(history_path) else None
            verify_record(record)
            if record is not None and (record["operation_id"] != operation_id or record["status"] not in FINISHED):
                raise ValueError("the original hosted Oda cart history changed")
        if record is not None and (record["owner"] != owner or record["plugin_id"] != plugin):
            raise ValueError("this operation belongs to the original hosted Oda household and plugin")

        if action == "status" or (beginning and record is not None):
            if record is None:
                raise ValueError("hosted Oda cart operation not found in its original registry")
            if action == "begin" and (record.get("product_id") != value["product_id"]
                                      or not hmac.compare_digest(record["account"], account)):
                raise ValueError("the operation ID belongs to a different product or account")
            if action == "begin_menu" and ("menu" not in record or value["mode"] != record["menu"]["mode"]
                                          or not menu_request_matches(value, record["menu"]["source"])
                                          or not hmac.compare_digest(record["account"], account)):
                raise ValueError("the operation ID belongs to a different saved menu or account")
            return status_view(record)
        if beginning:
            if current is not None:
                if current["status"] not in FINISHED:
                    raise ValueError("an original hosted Oda cart operation still owns this registry")
                archive_record(directory, current)
            record = {"operation_id": operation_id, "owner": owner, "plugin_id": plugin, "build": build,
                      **initial_record(action, value, account, locks)}
            record["frame"] = frame(record)
        elif (record is None or record is not current
                or (record["build"] != build and not continue_from_other_build(record, action))):
            raise ValueError("use the original active hosted Oda operation and installed build")
        elif action == "observe":
            observe(record, value, saved["key"], locks)
        elif action == "observe_menu_search":
            observe_menu_search(record, value, saved["key"], locks)
        elif action == "select_menu":
            select_menu(record, value["choices"], locks)
        elif action == "approve_menu":
            approve_menu(record, value, locks)
        elif action == "acknowledge":
            acknowledge(record, value)
        elif action == "recheck" and record["status"] in NEEDS_RECOVERY | READBACK | {"added"}:
            if record["status"] in READBACK:
                record.pop("frame", None)
            record["recovery_read"] = frame(record)
        elif action == "observe_recovery":
            observe_recovery(record, value, saved["key"])
        elif action == "defer":
            defer_maintenance(record)
        else:
            advance(record, action)

        dispatch = action == "observe" and record["status"] in WRITE_PHASES
        generation = saved["write_generation"] + int(dispatch)
        authority = {"key": saved["key"], "write_generation": generation}
        write_record(ledger, authority, record)
        if dispatch:
            # Persist the latest issued write in the workspace and owning home.
            write_json(anchor, authority)
            write_json(state / GENERATION_NAME, authority)
        elif "menu" in record and record["status"] in FINISHED:
            archive_record(directory, record)
            write_json(ledger, {**authority, "current": None})
        result = status_view(record)
        if action == "recheck":
            result["hosted_request"] = record["recovery_read"]
        elif action not in {"defer", "observe_recovery"} and "frame" in record:
            result.update(hosted_request=record["frame"], dispatch=dispatch)
        return result
