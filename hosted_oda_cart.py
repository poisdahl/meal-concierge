"""One reversible, owner-authorized cart probe through the attached Oda MCP."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import stat
import uuid

import hosted_oda
from install import bounded_json, write_json
from on_demand import _sync_directory
from runtime_ownership import file_lock


REGISTRY_NAME = ".meal-concierge-oda-carts"
ANCHOR_NAME = REGISTRY_NAME + "-identity.json"
DOT_WORKSPACE = Path("/workspace/shared")
BINDING_NAME = "hosted-oda-cart-binding.json"
FINISHED = {"restored", "aborted"}
BEFORE_WRITE = {"reading_baseline", "baseline_verified", "checking_add"}
READBACK = {"verify_add", "verify_remove"}
WRITE_PHASES = {"awaiting_add_response": 1, "awaiting_remove_response": -1}
READ_TOOLS = ("get_delivery_addresses", "get_cart")
STATUSES = BEFORE_WRITE | READBACK | WRITE_PHASES.keys() | FINISHED | {
    "added", "checking_remove", "add_response_unverified", "remove_response_unverified"}


def require_fields(value, fields):
    if not isinstance(value, dict) or value.keys() != set(fields):
        raise ValueError("unexpected fields for the hosted Oda cart action")


def provider_id(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def private_path(path, kind):
    info = path.lstat()
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("hosted Oda cart records must be private and owned by the installer user")


def read_private(path):
    private_path(path, stat.S_ISREG)
    return bounded_json(path)


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
            or not provider_id(record.get("product_id"))
            or not isinstance(record.get("owner"), str) or not isinstance(record.get("plugin_id"), str)
            or not isinstance(record.get("build"), dict)):
        raise ValueError("the original hosted Oda cart journal is invalid")


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
        saved = read_private(ledger)
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
        if binding is None and initialize:
            write_json(binding_file, expected)
        elif binding is not None and binding != expected:
            raise ValueError("the original hosted Oda cart registry binding changed")
        yield anchor, ledger, saved


def require_idle(state):
    """Keep the issuing program selectable until its original cart is settled."""
    bound = os.path.lexists(state / BINDING_NAME)
    if not bound and not state.is_relative_to(DOT_WORKSPACE):
        return
    directory = workspace_registry(state)
    if not (bound or os.path.lexists(directory) or os.path.lexists(directory.parent / ANCHOR_NAME)):
        return
    with registry(state, directory, initialize=False, allow_unbound=True) as (_, _, saved):
        current = saved["current"]
        if current and current["owner"] == str(state) and current["status"] not in FINISHED:
            raise ValueError("finish the original hosted Oda cart operation before changing its program")


def now():
    return datetime.now(timezone.utc)


def frame(record, *, observed=None):
    started = now()
    expires = started + timedelta(minutes=5) if observed is None else observed + timedelta(minutes=2)
    if expires <= started:
        raise ValueError("the Oda pre-dispatch observation expired")
    value = {"frame_id": uuid.uuid4().hex, "plugin_id": record["plugin_id"],
             "issued_at": started.isoformat(), "expires_at": expires.isoformat()}
    if observed is not None:
        return {**value, "tool": "manipulate_cart",
                "arguments": {"operations": [{"productId": record["product_id"],
                                             "quantity": WRITE_PHASES[record["status"]]}]}}
    return {**value, "tools": [{"tool": name, "arguments": {}} for name in READ_TOOLS]}


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


def cart_matches(cart, product_id, quantity):
    groups = cart["groups"]
    if (cart["deliverySlot"] is not None or cart["productQuantityCount"] != quantity or len(groups) > 1
            or any(group.keys() != {"items"} for group in groups)):
        return False
    if quantity:
        item, = groups[0]["items"]
        return item["product"]["id"] == product_id and item["quantity"] == 1
    return True


def observe(record, value, key):
    require_fields(value, ("action", "frame_id", "plugin_id", "address_observed_at",
                           "cart_observed_at", "addresses", "cart"))
    status, pending = record["status"], record.get("frame")
    if status not in {"reading_baseline", "checking_add", "checking_remove", *READBACK} or pending is None:
        raise ValueError("no original read frame can accept this observation")
    match_frame(pending, value)
    address_time = hosted_oda.response_time(value["address_observed_at"])
    cart_time = hosted_oda.response_time(value["cart_observed_at"])
    if not (hosted_oda.response_time(pending["issued_at"]) <= address_time <= cart_time
            <= now() < hosted_oda.response_time(pending["expires_at"])):
        raise ValueError("the original hosted Oda observation times or read frame changed")
    account = account_fingerprint(value["addresses"], key)
    if not hmac.compare_digest(record["account"], account):
        raise ValueError("the selected Oda account/address context changed")
    validate_cart(value["cart"])
    if not cart_matches(value["cart"], record["product_id"], int(status in {"verify_add", "checking_remove"})):
        raise ValueError("the isolated Oda cart has unexpected products, grouped lines or a selected slot")
    record.pop("frame")
    if status == "reading_baseline":
        record["status"] = "baseline_verified"
    elif status in {"checking_add", "checking_remove"}:
        record["status"] = "awaiting_add_response" if status == "checking_add" else "awaiting_remove_response"
        record["frame"] = frame(record, observed=address_time)
    else:
        record["status"] = "added" if status == "verify_add" else "restored"


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
        matches = cart_matches(result, record["product_id"], int(added))
    step = "add" if added else "remove"
    record[step + "_response"] = {
        "frame_id": pending["frame_id"], "dispatched_at": dispatched.isoformat(), "observed_at": observed.isoformat(),
        "response_sha256": hosted_oda.arguments_digest(result),
        "invocation_sha256": hosted_oda.arguments_digest(invocation) if invocation is not None else None,
        "is_error": value["is_error"]}
    if matches:
        record["status"] = "verify_" + step
        record["frame"] = frame(record)
    else:
        record["status"] = step + "_response_unverified"
        record.pop("frame", None)


def status_view(record):
    result = {"ok": True, "status": record["status"], "operation_id": record["operation_id"],
              "source_commit": record["build"]["source_commit"], "product_id": record["product_id"],
              "add_acknowledged": "add_response" in record, "remove_acknowledged": "remove_response" in record,
              "evidence": hosted_oda.EVIDENCE, "backend_freshness": "unverified", "dispatch": False}
    if record["status"] in WRITE_PHASES:
        result["pending_response"] = {key: record["frame"][key] for key in ("frame_id", "issued_at", "expires_at")}
    return result


def advance(record, action):
    status = record["status"]
    if action == "add" and status == "baseline_verified":
        record["status"] = "checking_add"
    elif action == "remove" and status == "added":
        record["status"] = "checking_remove"
    elif action == "retry_read" and status in {"reading_baseline", "checking_add", "checking_remove", *READBACK}:
        pass  # Only an observation can be reissued.
    elif action == "abort" and status in BEFORE_WRITE:
        record["status"] = "aborted"
        record.pop("frame", None)
        return
    else:
        raise ValueError("the hosted Oda cart stage does not allow this action; inspect the original operation")
    record["frame"] = frame(record)


def run(state, directory, operation_id, value, plugin, build):
    action = value.get("action") if isinstance(value, dict) else None
    if action == "begin":
        require_fields(value, ("action", "product_id", "isolated_new_cart", "authorized_addresses"))
        if not provider_id(value["product_id"]) or value["isolated_new_cart"] is not True:
            raise ValueError("the owner must designate an isolated new cart and one exact Oda product")
    elif action not in ("observe", "acknowledge"):
        require_fields(value, ("action",))

    build = {field: build[field] for field in ("source_commit", "source_sha256", "installed_runtime_sha256")}
    owner = str(state)
    with registry(state, directory, initialize=action == "begin") as (anchor, ledger, saved):
        account = account_fingerprint(value["authorized_addresses"], saved["key"]) if action == "begin" else None
        current = saved["current"]
        history_path = directory / (operation_id + ".json")
        if current is not None and current["operation_id"] == operation_id:
            record = current
        else:
            record = read_private(history_path) if os.path.lexists(history_path) else None
            verify_record(record)
            if record is not None and (record["operation_id"] != operation_id or record["status"] not in FINISHED):
                raise ValueError("the original hosted Oda cart history changed")
        if record is not None and (record["owner"] != owner or record["plugin_id"] != plugin):
            raise ValueError("this operation belongs to the original hosted Oda household and plugin")

        if action == "status" or (action == "begin" and record is not None):
            if record is None:
                raise ValueError("hosted Oda cart operation not found in its original registry")
            if action == "begin" and (record["product_id"] != value["product_id"]
                                      or not hmac.compare_digest(record["account"], account)):
                raise ValueError("the operation ID belongs to a different product or account")
            return status_view(record)
        if action == "begin":
            if current is not None:
                if current["status"] not in FINISHED:
                    raise ValueError("an original hosted Oda cart operation still owns this registry")
                archive = directory / (current["operation_id"] + ".json")
                if os.path.lexists(archive):
                    if read_private(archive) != current:
                        raise ValueError("the original hosted Oda cart history changed")
                else:
                    write_json(archive, current)
            record = {"operation_id": operation_id, "owner": owner, "plugin_id": plugin, "build": build,
                      "product_id": value["product_id"], "account": account, "status": "reading_baseline"}
            record["frame"] = frame(record)
        elif record is None or record is not current or record["build"] != build:
            raise ValueError("use the original active hosted Oda operation and installed build")
        elif action == "observe":
            observe(record, value, saved["key"])
        elif action == "acknowledge":
            acknowledge(record, value)
        else:
            advance(record, action)

        dispatch = record["status"] in WRITE_PHASES
        generation = saved["write_generation"] + int(dispatch)
        authority = {"key": saved["key"], "write_generation": generation}
        write_json(ledger, {**authority, "current": record})
        if dispatch:
            # Both files must agree before an external mutation can be issued.
            write_json(anchor, authority)
        result = status_view(record)
        if "frame" in record:
            result.update(hosted_request=record["frame"], dispatch=dispatch)
        return result
