"""Native Muse browser observations for the ordinary protected Oda core."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import unicodedata
import uuid

from core import (CancellationPreconditionError, CheckoutPreconditionError,
                  HouseholdError, checkout_payment_settings)
from checkout_identity import review_checkout_lines
from clients.muse import private_directory, read_json as _read_json, timestamp
from oda_browser import (ODA_CHECKOUT_AMOUNT_KEYS, ODA_CHECKOUT_AMOUNT_LABELS,
                         OdaBrowser, _oda_checkout_amounts_minor,
                         cancellation_delivery_matches, cancellation_total_matches,
                         checkout_delivery_matches, oda_checkout_amount_minor,
                         require_order_binding)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path):
    try:
        return _read_json(path)
    except FileNotFoundError:
        raise
    except OSError:
        raise HouseholdError("Muse browser record is not a readable private file") from None


def required_json(path):
    try:
        return read_json(path)
    except FileNotFoundError:
        raise HouseholdError("Muse browser required custody record is missing") from None


def durable_publish(path, value):
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if len(data) > 65_536:
        raise HouseholdError("Muse browser record exceeds the byte limit")
    fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise HouseholdError("Muse browser record could not be published once") from None
    finally:
        os.unlink(temporary)


def process_start(pid):
    if type(pid) is not int or pid <= 0:
        raise HouseholdError("Muse browser owner is invalid")
    try:
        # The process identity, rather than PID alone, prevents PID reuse.
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        return None


def broker_paths(directory):
    directory = Path(directory)
    if not directory.is_absolute():
        raise HouseholdError("Muse browser directory must be absolute")
    try:
        private_directory(directory)
        for name in ("requests", "claims", "consumed", "responses", "endings", "closed"):
            private_directory(directory / name)
    except OSError:
        raise HouseholdError("Muse browser requires its existing private broker directories") from None
    return directory


def request_record(directory, request_id, task_id, *, active=True):
    if (not isinstance(request_id, str)
            or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", request_id) is None
            or not isinstance(task_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task_id) is None):
        raise HouseholdError("Muse browser request identity is invalid")
    directory = broker_paths(directory)
    record = required_json(directory / "requests" / (request_id + ".json"))
    if (not isinstance(record, Mapping)
            or set(record) != {"version", "request_id", "provider", "operation", "task_id",
                               "owner_pid", "owner_start", "issued_at", "expires_at", "payload"}
            or record.get("request_id") != request_id
            or record.get("task_id") != task_id or record.get("provider") != "oda"
            or type(record.get("version")) is not int or record["version"] != 1
            or type(record.get("owner_pid")) is not int or record["owner_pid"] <= 0
            or not isinstance(record.get("operation"), str)
            or record["operation"] not in {"checkout_review", "checkout_click",
                "order_binding", "cancellation_review", "cancellation_click", "payment_state"}
            or not isinstance(record.get("payload"), Mapping)
            or not isinstance(record.get("owner_start"), str)
            or not record["owner_start"].isdigit()
            or not 0 < (timestamp(record["expires_at"]) - timestamp(record["issued_at"])).total_seconds() <= 180
            or active and (((directory / "closed" / (request_id + ".json")).exists()
                           or (directory / "closed" / (request_id + ".json")).is_symlink())
                or process_start(record.get("owner_pid")) != record.get("owner_start")
                or not timestamp(record["issued_at"]) <= datetime.now(timezone.utc) < timestamp(record["expires_at"]))):
        raise HouseholdError("Muse browser request expired, changed or lost its owner")
    return directory, record


def claim_request(directory, request_id, task_id):
    directory = broker_paths(directory)
    with transition_lock(directory):
        directory, record = request_record(directory, request_id, task_id)
        durable_publish(directory / "claims" / (request_id + ".json"),
                        {"request_digest": digest(record), "task_id": task_id})


def consume_request(directory, request_id, task_id):
    directory = broker_paths(directory)
    with transition_lock(directory):
        directory, record = request_record(directory, request_id, task_id)
        claim = required_json(directory / "claims" / (request_id + ".json"))
        if (record["operation"] not in {"checkout_click", "cancellation_click"}
                or claim != {"request_digest": digest(record), "task_id": task_id}):
            raise HouseholdError("Muse browser action was not claimed")
        name = request_id + ".json"
        if any((directory / kind / name).exists() or (directory / kind / name).is_symlink()
               for kind in ("responses", "endings")):
            raise HouseholdError("Muse browser task already returned; no action is authorized")
        durable_publish(directory / "consumed" / (request_id + ".json"),
                        {**claim, "consumed_at": datetime.now(timezone.utc).isoformat()})


@contextmanager
def transition_lock(directory):
    """Serialize only claim/consume/revocation, never browser or MCP work."""
    fd = os.open(directory / ".transition.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise HouseholdError("Muse browser transition lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def respond_request(directory, request_id, task_id, response):
    # A late actual ending closes native custody. It never makes its old facts
    # fresh for a new core operation, and cannot grant another action permit.
    directory = broker_paths(directory)
    with transition_lock(directory):
        directory, record = request_record(directory, request_id, task_id, active=False)
        validate_response(record, response, fresh=False)
        if response["task_state"] != "completed":
            request_record(directory, request_id, task_id)
        validate_claim(directory, record, response=response)
        durable_publish(directory / "responses" / (request_id + ".json"), response)


def end_request(directory, request_id, task_id, response):
    """Append an actual task ending after a missing, invalid or paused reply.

    This closes custody only. It neither replaces the earlier observation nor
    makes expired facts usable by the core.
    """
    directory = broker_paths(directory)
    with transition_lock(directory):
        directory, record = request_record(directory, request_id, task_id, active=False)
        try:
            prior = read_json(directory / "responses" / (request_id + ".json"))
            validate_response(record, prior, fresh=False)
        except (FileNotFoundError, HouseholdError):
            prior = None
        validate_response(record, response, fresh=False)
        if response["task_state"] != "completed" or prior is not None and prior["task_state"] == "completed":
            raise HouseholdError("Muse browser ending requires an unresolved original task")
        validate_claim(directory, record, response=response)
        durable_publish(directory / "endings" / (request_id + ".json"), response)


def validate_claim(directory, record, *, response):
    claim = {"request_digest": digest(record), "task_id": record["task_id"]}
    name = record["request_id"] + ".json"
    if required_json(directory / "claims" / name) != claim:
        raise HouseholdError("Muse browser custody record changed")
    if record["operation"].endswith("_click"):
        refused = response["task_state"] == "completed" and response["facts"] == {"dispatch": "not_dispatched"}
        try:
            consumed = read_json(directory / "consumed" / name)
        except FileNotFoundError:
            if refused:
                return
            raise HouseholdError("Muse browser action has no consumed permit") from None
        if refused:
            raise HouseholdError("Muse browser consumed action cannot attest pre-dispatch refusal")
        if (not isinstance(consumed, Mapping) or set(consumed) != set(claim) | {"consumed_at"}
                or any(consumed.get(key) != value for key, value in claim.items())
                or not timestamp(record["issued_at"]) <= timestamp(consumed["consumed_at"]) < timestamp(record["expires_at"])):
            raise HouseholdError("Muse browser consumed permit changed")


def validate_response(record, response, *, fresh=True):
    if (not isinstance(response, Mapping)
            or set(response) != {"request_id", "request_digest", "task_id", "observed_at", "task_state", "facts"}
            or response.get("request_id") != record["request_id"]
            or response.get("request_digest") != digest(record)
            or response.get("task_id") != record["task_id"]
            or not isinstance(response.get("task_state"), str)
            or response["task_state"] not in {"completed", "waiting_for_information"}
            or not isinstance(response.get("facts"), Mapping)):
        raise HouseholdError("Muse browser observation identity or state changed")
    observed = timestamp(response["observed_at"])
    current = datetime.now(timezone.utc)
    if (not timestamp(record["issued_at"]) <= observed <= current
            or fresh and ((current - observed).total_seconds() > 30
                          or current >= timestamp(record["expires_at"]))):
        raise HouseholdError("Muse browser observation is stale or expired")


class NativeBridge:
    """One original native task chain, claimed through the supported producer.

    Host receipts and complete AX facts are attested by the main native agent.
    Echoed IDs or these files alone do not prove what a browser actually did.
    task_id is the chain anchor; the producer privately correlates each actual
    predecessor/successor receipt before steering or returning an observation.
    """
    def __init__(self, directory, task_id):
        self.directory = broker_paths(directory)
        if (not isinstance(task_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task_id) is None):
            raise HouseholdError("Muse browser needs its actual original task identity")
        self.task_id = task_id

    @contextmanager
    def custody(self):
        fd = os.open(self.directory / ".owner.lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise HouseholdError("Muse browser lock is unsafe")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HouseholdError("Muse browser has an active owner") from None
            # A timed-out claimed native task may still be working. Its actual
            # ending must be returned to the original request before replacement.
            for claim_path in (self.directory / "claims").glob("*.json"):
                reply = self.directory / "responses" / claim_path.name
                original = required_json(self.directory / "requests" / claim_path.name)
                if not isinstance(original, Mapping):
                    raise HouseholdError("Muse browser custody record is invalid")
                _, record = request_record(self.directory, claim_path.stem,
                                           original.get("task_id"), active=False)
                try:
                    ending = read_json(self.directory / "endings" / claim_path.name)
                except FileNotFoundError:
                    pass
                else:
                    validate_response(record, ending, fresh=False)
                    if ending["task_state"] != "completed":
                        raise HouseholdError("Muse browser has no actual task ending")
                    validate_claim(self.directory, record, response=ending)
                    continue
                response = required_json(reply)
                validate_response(record, response, fresh=False)
                validate_claim(self.directory, record, response=response)
                if response["task_state"] != "completed":
                    if (record["operation"].endswith("_click")
                            or record["task_id"] != self.task_id
                            or process_start(record["owner_pid"]) != record["owner_start"]):
                        raise HouseholdError("Finish the original waiting Muse task before replacement")
            yield
        finally:
            os.close(fd)

    def request(self, operation, payload, *, deadline=None, expires_at=None):
        seconds = 30.0 if operation.endswith("_click") else 180.0
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (float, int)) or not math.isfinite(deadline):
                raise HouseholdError("Muse browser deadline must be finite")
            seconds = min(seconds, deadline - time.monotonic())
        if seconds <= 0:
            raise HouseholdError("Muse browser deadline reached")
        start = datetime.now(timezone.utc)
        if expires_at is not None:
            seconds = min(seconds, (timestamp(expires_at) - start).total_seconds())
        if seconds <= 0:
            raise HouseholdError("Muse browser original confirmation expired")
        record = {"version": 1, "request_id": str(uuid.uuid4()), "provider": "oda",
                  "operation": operation, "task_id": self.task_id,
                  "owner_pid": os.getpid(), "owner_start": process_start(os.getpid()),
                  "issued_at": start.isoformat(), "expires_at": (start + timedelta(seconds=seconds)).isoformat(),
                  "payload": payload}
        cutoff = time.monotonic() + seconds
        durable_publish(self.directory / "requests" / (record["request_id"] + ".json"), record)
        reply_path = self.directory / "responses" / (record["request_id"] + ".json")
        try:
            while time.monotonic() < cutoff:
                try:
                    response = read_json(reply_path)
                except FileNotFoundError:
                    time.sleep(min(0.05, max(0, cutoff - time.monotonic())))
                    continue
                validate_response(record, response)
                validate_claim(self.directory, record, response=response)
                if operation.endswith("_click") and response["task_state"] != "completed":
                    raise HouseholdError("Muse browser action is unresolved; reconcile its original task")
                return dict(response["facts"])
            # Never remove a request/claim/action record on timeout or parent loss.
            raise HouseholdError("Muse browser result is unknown; reconcile its original task without replay")
        finally:
            # This closes only the specific waiter. Consumed permits and native
            # custody remain until an actual ending is reconciled.
            with transition_lock(self.directory):
                durable_publish(self.directory / "closed" / (record["request_id"] + ".json"),
                                {"request_digest": digest(record), "task_id": self.task_id,
                                 "closed_at": datetime.now(timezone.utc).isoformat()})


def text(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise HouseholdError("Muse browser text is unavailable")
    return " ".join(unicodedata.normalize("NFC", value).split())


def native_amount_minor(label, value):
    # Native AX joins adjacent number/currency nodes on the observed page.
    value = re.sub(r"(?<=\d)(kr|NOK)$", r" \1", text(value), flags=re.IGNORECASE)
    return oda_checkout_amount_minor(label, value)


def checkout_amounts(rows, product_count, expected_total):
    """Validate the complete native summary from its observed label/value rows.

    The host must capture one complete amount section. No totals, missing rows
    or IDs may be filled from the expected MCP cart.
    """
    if (type(product_count) is not int or not 0 < product_count <= 1_000_000
            or type(expected_total) is not int or expected_total < 0
            or not isinstance(rows, list) or not 3 <= len(rows) <= 8):
        raise HouseholdError("Muse checkout amount section is unavailable")
    product_label = "1 vare" if product_count == 1 else f"{product_count} varer"
    labels = {product_label: "product_subtotal",
              **{label: key for key, label in ODA_CHECKOUT_AMOUNT_LABELS.items()}}
    observed = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"label", "value"}:
            raise HouseholdError("Muse checkout amount row changed")
        label = "" if row["label"] is None else text(row["label"])
        value = text(row["value"])
        if not label:
            # One observed zero with no label has no financial effect. Preserve
            # it in the raw surface without inventing a delivery or fee meaning.
            if "unlabeled_zero" in observed or native_amount_minor("Total inkl. MVA", value) != 0:
                raise HouseholdError("Muse unlabeled checkout amount is nonzero or ambiguous")
            observed["unlabeled_zero"] = 0
            continue
        key = labels.get(label)
        if key is None or key in observed:
            raise HouseholdError("Muse checkout amount rows are unknown or ambiguous")
        observed[key] = native_amount_minor(label, value)
    if not {"product_subtotal", "discounted_subtotal", "provider_total"} <= observed.keys():
        raise HouseholdError("Muse checkout required amount row is missing")
    subtotal = observed["product_subtotal"] + observed.get("discounts", 0)
    payable = (subtotal + observed.get("delivery_price", 0)
               + observed.get("bags", 0) + observed.get("other_fee", 0))
    if (observed["discounted_subtotal"] != subtotal
            or observed["provider_total"] != payable
            or payable != expected_total):
        raise HouseholdError("Muse checkout amount arithmetic or cart total changed")
    amounts = {key: observed[key] / 100 if key in observed else None
               for key in ODA_CHECKOUT_AMOUNT_KEYS}
    if "other_fee" in observed:
        amounts["other_fees"] = {ODA_CHECKOUT_AMOUNT_LABELS["other_fee"]:
                                 observed["other_fee"] / 100}
    _oda_checkout_amounts_minor(amounts)
    return amounts


def account_links(value):
    if (not isinstance(value, Mapping) or set(value) != {"url", "edit_urls"}
            or value["url"] != "https://oda.com/no/account/delivery/"
            or not isinstance(value["edit_urls"], list) or not 1 <= len(value["edit_urls"]) <= 200):
        raise HouseholdError("Muse browser account evidence is unavailable")
    references = set()
    for url in value["edit_urls"]:
        if not isinstance(url, str):
            raise HouseholdError("Muse browser account edit link is invalid")
        match = re.fullmatch(r"https://oda\.com/no/account/delivery/edit/([1-9][0-9]*)/", url)
        if match is None or not 0 < int(match[1]) < 2**53:
            raise HouseholdError("Muse browser account edit link is invalid")
        references.add(int(match[1]))
    return references


def address_text(value):
    result = text(value)
    if not result:
        raise HouseholdError("Muse browser address is unavailable")
    return result


def controls(value, pattern):
    if (not isinstance(value, list) or len(value) != 1
            or not isinstance(value[0], Mapping) or set(value[0]) != {"label", "enabled"}
            or value[0]["enabled"] is not True
            or re.fullmatch(pattern, text(value[0]["label"]), re.IGNORECASE) is None):
        raise HouseholdError("Muse browser action control is unavailable or ambiguous")
    return {"label": text(value[0]["label"]), "enabled": True}


class MuseBrowser:
    """Use Muse's original native browser task, with no browser SDK or CDP.

    Complete raw observations and actual task receipts come from the supported
    native host. The host is a trusted producer, as with Muse catalog files;
    these records are not independently verified DOM snapshots.
    """
    checkout_provider = "oda"

    def __init__(self, directory, task_id, provider_client, *, state_store=None):
        self.bridge = NativeBridge(directory, task_id)
        self.provider_client = provider_client
        self.state_store = state_store
        self.last_review_at = None

    def _addresses(self, deadline):
        value = self.provider_client.call("get_delivery_addresses", {}, deadline=deadline)
        rows = value.get("result") if isinstance(value, Mapping) else None
        if not isinstance(rows, list):
            raise HouseholdError("Muse provider account addresses are unavailable")
        return rows

    def _selected_reference(self, address, deadline):
        selected = [r for r in self._addresses(deadline)
                    if isinstance(r, Mapping) and r.get("isSelected") is True]
        if (len(selected) != 1 or type(selected[0].get("id")) is not int
                or not 0 < selected[0]["id"] < 2**53
                or address_text(selected[0].get("address")).casefold() != address.casefold()):
            raise HouseholdError("Muse provider selected address changed")
        return selected[0]["id"]

    def review_checkout(self, cart, *, payment=None, identity_review=None, deadline=None):
        with self.bridge.custody():
            return self._review_checkout(cart, payment=payment, identity_review=identity_review, deadline=deadline)

    def _review_checkout(self, cart, *, payment, identity_review=None, deadline=None):
        expected = OdaBrowser._cart_expectation(cart)
        if not expected["delivery_text"]:
            raise HouseholdError("Select a delivery window before Muse checkout")
        choice = checkout_payment_settings(payment or {"method": "saved_card"}, "oda")
        if choice["method"] != "saved_card":
            raise HouseholdError("Muse native checkout supports the current saved card only")
        reference = self._selected_reference(expected["delivery_address"], deadline)
        facts = self.bridge.request("checkout_review", {"kind": "new_separate_order",
                    "expected": expected, "payment": choice}, deadline=deadline)
        required = {"url", "account", "address", "delivery_sections", "items", "warnings",
                    "amount_rows", "payment", "submit_controls", "complete_sections"}
        if (set(facts) != required or facts["url"] != "https://oda.com/no/checkout/confirm/"
                or facts["complete_sections"] != ["account", "items", "warnings", "amounts", "delivery", "payment", "submit"]
                or facts["warnings"] != [] or reference not in account_links(facts["account"])
                or address_text(facts["address"]).casefold() != expected["delivery_address"].casefold()
                or not checkout_delivery_matches(expected["delivery_text"], facts["delivery_sections"])):
            raise HouseholdError("Muse checkout account, address, delivery or stock does not match")
        actual = facts["items"]
        if (not isinstance(actual, list) or len(actual) != len(expected["lines"])
                or any(not isinstance(r, Mapping) or set(r) != {"product_id", "title", "subtitle", "quantity"}
                       or type(r["product_id"]) is not int or r["product_id"] <= 0
                       or type(r["quantity"]) is not int or not 0 < r["quantity"] <= 1_000_000
                       or not isinstance(r["title"], str) or not isinstance(r["subtitle"], str)
                       for r in actual)):
            raise HouseholdError("Muse checkout needs independently observed IDs and integer quantities")
        account_digest = hashlib.sha256(str(reference).encode()).hexdigest()
        # The shared identity parser represents IDs as strings; retain the raw
        # integer observations in the bound surface and convert only its input.
        identity_rows = [{**row, "product_id": str(row["product_id"])} for row in actual]
        try:
            identity = review_checkout_lines(expected["lines"], identity_rows,
                binding={"checkout": facts, "account": account_digest, "total": expected["total_minor"],
                         "delivery": expected["delivery_text"], "address": expected["delivery_address"]},
                review=identity_review)
        except ValueError:
            raise HouseholdError("Muse checkout identity review is stale or invalid") from None
        if identity.get("matched") is not True:
            raise HouseholdError("Muse checkout product identity or quantity differs from the current cart")
        selected = facts["payment"]
        if (not isinstance(selected, Mapping) or set(selected) != {"display", "selected"}
                or selected["selected"] is not True
                or re.fullmatch(r"•••• [0-9]{4}", text(selected["display"])) is None
                or choice.get("card_last4") is not None and selected["display"][-4:] != choice["card_last4"]):
            raise HouseholdError("Muse selected saved-card identity is unavailable or changed")
        final = controls(facts["submit_controls"], r"(?:Bekreft og betal|Confirm and pay)(?: .*)?")
        money = re.fullmatch(r"(?:Bekreft og betal|Confirm and pay) (\d+(?:[ .]\d{3})*,\d{2} ?(?:kr|NOK))",
                             final["label"], re.IGNORECASE)
        if money is None or native_amount_minor("Total inkl. MVA", money[1]) != expected["total_minor"]:
            raise HouseholdError("Muse final payment control amount changed")
        amounts = checkout_amounts(facts["amount_rows"], expected["product_count"], expected["total_minor"])
        self.last_review_at = datetime.now(timezone.utc).isoformat()
        return {"url": facts["url"], "authenticated": True, "available": True,
                "line_matches": True, "total_matches": True, "delivery_matches": True,
                "address_matches": True, "masked_payment": True, "submit_controls": 1,
                "payment_display": selected["display"], "payment_choice": choice,
                "amounts": amounts, "account_reference_digest": account_digest,
                "surface": {"evidence_kind": "native_host_observation", **facts},
                **({"identity_review": identity["review"]} if identity.get("review") else {})}

    def submit_checkout(self, cart, review, before_click=None, *, deadline=None):
        with self.bridge.custody():
            try:
                current = self._review_checkout(cart, payment=review["payment_choice"],
                    identity_review=review.get("identity_review"), deadline=deadline)
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            if current != dict(review):
                raise CheckoutPreconditionError("Muse checkout changed before dispatch")
            if before_click is not None:
                before_click()
            result = self._effect_request("checkout_click", {"review": current,
                        "review_digest": digest(current), "effect": "one_final_new_order_click"},
                        current, deadline=deadline)
            if result != {"dispatch": "clicked_once"}:
                raise HouseholdError("Muse checkout outcome is unknown; reconcile the original attempt")
            # The core independently reconciles exact new order/tracking/binding.
            # Never invent bank-tab context from a URL or click acknowledgement.
            return {"authentication_unresolved": True}

    def _effect_request(self, operation, payload, review, *, deadline=None):
        if self.state_store is None:
            raise HouseholdError("Muse browser effect requires its original core state store")
        cancellation = operation == "cancellation_click"
        pending = self.state_store.read().get("pending_cancellation" if cancellation else "pending_checkout")
        if (not isinstance(pending, Mapping) or pending.get("status") != "clicking"
                or pending.get("browser" if cancellation else "browser_review") != review
                or not isinstance(pending.get("confirmation_id"), str) or not pending["confirmation_id"]):
            raise HouseholdError("Muse browser original clicking journal changed")
        binding = {"confirmation_id": pending["confirmation_id"], "expires_at": pending["expires_at"],
                   "journal_digest": digest(pending)}
        return self.bridge.request(operation, {**payload, "journal_binding": binding},
                                   deadline=deadline, expires_at=pending["expires_at"])

    def _order_binding(self, order_id, order, facts, expected_binding, deadline):
        expected = OdaBrowser._order_expectation(order_id, order)
        if (set(facts) != {"url", "order_id", "currency", "receipt_address", "account", "delivery_sections", "total_rows", "complete_sections"}
                or facts["complete_sections"] != ["receipt", "account"]
                or facts["url"] != OdaBrowser._order_url(order_id)
                or facts["order_id"] != order_id or facts["currency"] != "NOK"
                or not cancellation_delivery_matches(expected["delivery_text"], facts["delivery_sections"])
                or not cancellation_total_matches(expected["total_minor"], facts["total_rows"])):
            raise HouseholdError("Muse order receipt identity, delivery or total changed")
        address = address_text(facts["receipt_address"])
        references = account_links(facts["account"])
        candidates = []
        for row in self._addresses(deadline):
            if (isinstance(row, Mapping) and type(row.get("id")) is int
                    and row["id"] in references and isinstance(row.get("address"), str)
                    and address_text(row["address"]).casefold() == address.casefold()):
                binding = {"receipt_address": address,
                           "account_reference_digest": hashlib.sha256(str(row["id"]).encode()).hexdigest()}
                if expected_binding is None or (binding["account_reference_digest"] == expected_binding["account_reference_digest"]
                        and address.casefold() == address_text(expected_binding["receipt_address"]).casefold()):
                    candidates.append(binding)
        if len(candidates) != 1:
            raise HouseholdError("Muse order account/address binding is unavailable or ambiguous")
        return dict(expected_binding) if expected_binding is not None else candidates[0]

    def read_order_binding(self, order_id, order, *, expected_binding=None, deadline=None):
        if expected_binding is not None:
            require_order_binding(expected_binding)
        OdaBrowser._order_url(order_id)
        with self.bridge.custody():
            facts = self.bridge.request("order_binding", {"order_id": order_id,
                       "preserve_payment_page": True}, deadline=deadline)
            return self._order_binding(order_id, order, facts, expected_binding, deadline)

    def order_payment_state(self, order_id, *, deadline=None):
        OdaBrowser._order_url(order_id)
        with self.bridge.custody():
            facts = self.bridge.request("payment_state", {"order_id": order_id,
                        "preserve_payment_page": True}, deadline=deadline)
            # Retry/payment resumption are outside this manual saved-card path.
            # Missing observations remain unknown; they never mean unpaid/failed.
            if facts != {"status": "unknown"}:
                raise HouseholdError("Muse payment state requires supported native evidence")
            return facts

    def review_cancellation(self, order_id, order, *, deadline=None):
        with self.bridge.custody():
            return self._review_cancellation(order_id, order, deadline=deadline)

    def _review_cancellation(self, order_id, order, *, deadline=None, expected_binding=None):
        OdaBrowser._order_url(order_id)
        facts = self.bridge.request("cancellation_review", {"order_id": order_id,
                    "dialog_open_only_if_proven_nonfinal": True, "dismiss_without_cancelling": True}, deadline=deadline)
        if set(facts) != {"receipt", "dialog"} or not isinstance(facts["receipt"], Mapping):
            raise HouseholdError("Muse cancellation review is unavailable")
        binding = self._order_binding(order_id, order, facts["receipt"], expected_binding, deadline)
        dialog = facts["dialog"]
        if (not isinstance(dialog, Mapping) or set(dialog) != {"text", "final_controls", "dismiss_controls", "closed"}
                or dialog["closed"] is not True or not text(dialog["text"])):
            raise HouseholdError("Muse cancellation dialog was not reviewed and dismissed")
        final = controls(dialog["final_controls"], r"Kanseller bestillingen min|Avbestill bestillingen|Confirm cancellation")
        dismiss = controls(dialog["dismiss_controls"], r"Nei, ikke kanseller|Ikke avbestill|Do not cancel")
        if final == dismiss:
            raise HouseholdError("Muse cancellation and dismissal controls are ambiguous")
        return {"available": True, "binding": binding, "consequence": text(dialog["text"]),
                "surface": {"evidence_kind": "native_host_observation", **facts}}

    def submit_cancellation(self, order_id, order, review, before_click=None, *, deadline=None):
        with self.bridge.custody():
            try:
                current = self._review_cancellation(order_id, order, deadline=deadline,
                                                   expected_binding=review["binding"])
            except HouseholdError as exc:
                raise CancellationPreconditionError(str(exc)) from exc
            if current != dict(review):
                raise CancellationPreconditionError("Muse cancellation changed before dispatch")
            if before_click is not None:
                before_click()
            result = self._effect_request("cancellation_click", {"order_id": order_id,
                        "review": current, "review_digest": digest(current),
                        "effect": "one_final_cancellation_click"}, current, deadline=deadline)
            if result != {"dispatch": "clicked_once"}:
                raise HouseholdError("Muse cancellation outcome is unknown; reconcile the original attempt")
