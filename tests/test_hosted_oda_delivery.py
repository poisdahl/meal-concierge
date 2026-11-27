"""Hosted Oda delivery approval and independent reservation readback."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hosted_oda
import hosted_oda_cart as journal
import install


TIME = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
DATE = "2026-11-28"
SLOT = "oda:" + DATE + ":515"
CUTOFF = "2026-11-27T19:00:00+00:00"
ADDRESS = [{"id": 7, "isSelected": True}, {"id": 9, "isSelected": False}]
BUILD = {"source_commit": "a" * 40, "source_sha256": "b" * 64, "installed_runtime_sha256": "c" * 64}


def snapshot(selected=None, *, fee="0.00", total="20.00"):
    amounts = dict.fromkeys(hosted_oda.BREAKDOWN_FIELDS, "0.00")
    amounts.update(subtotal="20.00", deliveryFee=fee, total=total)
    product = {"product": {"id": 515, "name": "Synthetic oats"}, "quantity": 1, "totalGrossAmount": "20.00"}
    return {"groups": [{"items": [product]}], "productQuantityCount": 1, "totalGrossAmount": total,
            "totalBreakdown": amounts, "url": "https://oda.com/no/cart/", "deliverySlot": selected,
            "isUnattendedDelivery": False, "deliveryAddress": "Fixture address"}


def provider_slot(**overrides):
    return {"id": 515, "cutoffTime": CUTOFF, "openDatetime": DATE + "T03:00:00+00:00",
            "closeDatetime": DATE + "T08:00:00+00:00", "price": "kr\u00a029", "isFull": False,
            "isUnavailable": False, "isSelected": False, **overrides}


def selected_slot(**overrides):
    return {"id": 515, "cutoffTime": CUTOFF, "name": "Hjemlevering mellom kl 04 og 09, i morgen",
            "expireAt": "2026-11-27T13:00:00+00:00", **overrides}


class HostedOdaDeliveryTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = root / "state"
        self.state.mkdir(mode=0o700)
        self.directory = root / journal.REGISTRY_NAME
        self.identifier = str(uuid.uuid4())
        self.clock = self.enterContext(patch.object(journal, "now", return_value=TIME))

    def step(self, action, **fields):
        return journal.run(self.state, self.directory, self.identifier, {"action": action, **fields}, "fixture", BUILD)

    def begin(self):
        return self.step("begin_delivery", slot_ref=SLOT, authorized_addresses=ADDRESS)

    def observation(self, response, *, cart=None, slot=None, **overrides):
        frame = response["hosted_request"]
        slot_arguments = next(entry["arguments"] for entry in frame["tools"] if entry["tool"] == "get_delivery_slots")
        stamp = self.clock.return_value.isoformat()
        return {"frame_id": frame["frame_id"], "plugin_id": "fixture", "addresses": ADDRESS,
                "address_observed_at": stamp, "cart_observed_at": stamp, "slots_observed_at": stamp,
                "cart": snapshot() if cart is None else cart,
                "address_label": "Fixture address", "browser": {"browser_id": "cloud", "tab_id": "dedicated",
                    "address_id": 7, "new_cart": True, "cart_url": "https://oda.com/no/cart/"},
                "slots": {"deliveryDate": DATE, "slots": [provider_slot() if slot is None else slot]},
                "slot_arguments": slot_arguments, **overrides}

    def preflight(self):
        read = self.begin()
        self.assertEqual(read["hosted_request"]["tools"][-1]["arguments"],
                         {"delivery_date": DATE, "delivery_address_id": 7})
        review = self.step("observe_delivery", **self.observation(read))
        self.assertEqual(review["review"]["window"]["price_ore"], 2900)
        self.assertFalse(review["dispatch"])
        with self.assertRaisesRegex(ValueError, "exact Oda delivery review"):
            self.step("approve_delivery", review_digest="wrong")
        return self.step("approve_delivery", review_digest=review["review_digest"])

    def acknowledge(self, write, *, slot=None, error=False):
        frame = write["hosted_request"]
        return self.step("acknowledge_delivery", frame_id=frame["frame_id"], plugin_id="fixture",
                         tool=frame["tool"], arguments=frame["arguments"], dispatched_at=frame["issued_at"],
                         observed_at=self.clock.return_value.isoformat(), result_source="original_attached_mutation_response",
                         is_error=error, slot=None if error else (slot if slot is not None else selected_slot()))

    def verify(self, read, selected=None, *, fee="29.00", total="49.00", full=False):
        if selected is None:
            selected = selected_slot()
        return self.step("observe_delivery", **self.observation(
            read, cart=snapshot(selected, fee=fee, total=total), slot=provider_slot(isSelected=True, isFull=full)))

    def test_one_exact_dispatch_then_cart_and_slot_reads(self):
        preflight = self.preflight()
        write = self.step("observe_delivery", **self.observation(preflight))
        self.assertTrue(write["dispatch"])
        self.assertEqual(write["hosted_request"]["tool"], "select_delivery_slot")
        self.assertEqual(write["hosted_request"]["arguments"],
                         {"delivery_slot_id": 515, "delivery_address_id": 7, "is_unattended_delivery": False})
        self.assertEqual(journal.read_private(self.state / journal.GENERATION_NAME)["write_generation"], 1)
        self.assertFalse(self.begin()["dispatch"])
        self.assertNotIn("hosted_request", self.step("status"))
        with self.assertRaisesRegex(ValueError, "reconcile"):
            self.step("observe_delivery", **self.observation(preflight))
        with self.assertRaisesRegex(ValueError, "approved window"):
            self.acknowledge(write, slot=selected_slot(id=516))
        readback = self.acknowledge(write, slot=selected_slot())
        self.assertFalse(readback["dispatch"])
        with self.assertRaisesRegex(ValueError, "exact active reservation"):
            self.verify(readback, selected_slot(id=9))
        finished = self.verify(readback, full=True)
        self.assertEqual(finished["status"], "delivery_reserved")
        self.assertEqual(finished["reservation"]["expires_at"], "2026-11-27T13:00:00+00:00")
        self.assertEqual(self.step("status"), finished)
        self.assertFalse(finished["dispatch"])

    def test_stale_cart_price_cutoff_address_unattended_and_cart_ownership_stop(self):
        preflight = self.preflight()
        failures = [
            (self.observation(preflight, slot=provider_slot(price="kr\u00a030")), "changed; prepare"),
            (self.observation(preflight, slot=provider_slot(isFull=True)), "unavailable"),
            (self.observation(preflight, slot=provider_slot(cutoffTime=TIME.isoformat())), "cutoff expired"),
            (self.observation(preflight, addresses=[{"id": 9, "isSelected": True}]), "context changed"),
            (self.observation(preflight, cart={**snapshot(), "deliveryAddress": "Another place"}), "delivery address does not match"),
            (self.observation(preflight, cart={**snapshot(), "isUnattendedDelivery": True}), "changed; prepare"),
            (self.observation(preflight, cart=snapshot(selected_slot())), "change an existing selection"),
        ]
        for observation, error in failures:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.step("observe_delivery", **observation)
        conflicting = self.observation(preflight)
        conflicting["slots"]["slots"].append(provider_slot(id=519, isSelected=True, isFull=True))
        with self.assertRaisesRegex(ValueError, "change an existing selection"):
            self.step("observe_delivery", **conflicting)
        expired = self.observation(preflight)
        self.clock.return_value += timedelta(minutes=3)
        expired["slots_observed_at"] = expired["cart_observed_at"] = self.clock.return_value.isoformat()
        with self.assertRaisesRegex(ValueError, "quote or cutoff expired"):
            self.step("observe_delivery", **expired)
        other = self.state.parent / "other"
        other.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "original.*still owns"):
            journal.run(other, self.directory, str(uuid.uuid4()), {"action": "begin", "product_id": 515,
                        "isolated_new_cart": True, "authorized_addresses": ADDRESS}, "other-plugin", BUILD)
        self.assertEqual(self.step("abort")["status"], "aborted")
        self.assertFalse((self.state / journal.GENERATION_NAME).exists())

    def test_read_after_lost_reply_never_replays_or_claims_attribution(self):
        write = self.step("observe_delivery", **self.observation(self.preflight()))
        with self.assertRaisesRegex(ValueError, "frame to expire"):
            self.step("recheck")
        self.clock.return_value += timedelta(minutes=3)
        read = self.step("recheck")
        self.assertFalse(read["dispatch"])
        with self.assertRaisesRegex(ValueError, "reconcile"):
            self.acknowledge(write)
        with self.assertRaisesRegex(ValueError, "exact active reservation"):
            self.verify(read, fee="35.00", total="55.00")
        self.assertEqual(self.verify(read)["status"], "delivery_observed")

    def test_already_selected_is_a_read_without_dispatch_authority(self):
        observed = self.verify(self.begin(), selected_slot(expireAt=None))
        self.assertEqual(observed["status"], "delivery_already_selected")
        self.assertFalse(observed["dispatch"])
        self.assertNotIn("review_digest", observed)

    def test_provider_error_reconciles_only_after_a_fresh_unchanged_read(self):
        write = self.step("observe_delivery", **self.observation(self.preflight()))
        read = self.acknowledge(write, error=True)
        with self.assertRaisesRegex(ValueError, "exact active reservation"):
            self.step("observe_delivery", **self.observation(read))
        self.clock.return_value += timedelta(minutes=3)
        finished = self.step("observe_delivery", **self.observation(read))
        self.assertEqual(finished["status"], "delivery_inactive")
        self.assertEqual(finished["original_dispatch_outcome"], "unknown")
        self.assertEqual(finished["delivery_evidence_scope"], "historical_observation")

    def test_checkout_handoff_preserves_owner_payment_step(self):
        started = self.step("begin_checkout", slot_ref=SLOT, authorized_addresses=ADDRESS, payment_method="vipps")
        result = self.verify(started)
        self.assertEqual(result["status"], "checkout_handoff")
        self.assertEqual(result["handoff"]["requested_payment_method"], "vipps")
        self.assertEqual(result["handoff"]["payment_status"], "unverified")
        self.assertEqual(result["handoff"]["amount_scope"], "attached_app_cart_only")
        self.assertFalse(result["handoff"]["owner_completion_current"])
        self.assertFalse(result["dispatch"])
        with self.assertRaisesRegex(ValueError, "different delivery window or account"):
            self.step("begin_checkout", slot_ref=SLOT, authorized_addresses=ADDRESS, payment_method="saved_card")

    def checkout_observation(self, frame, *, preflight=False):
        value = self.observation(frame, cart=snapshot(selected_slot(), fee="29.00", total="49.00"),
                                 slot=provider_slot(isSelected=True))
        value["checkout"] = {
            "url": "https://oda.com/no/checkout/confirm/", "browser_id": "cloud", "tab_id": "dedicated",
            "new_order": True, "exclusive_checkout": True, "address_label": "Fixture address",
            "delivery": "Hjemlevering 04-09, 28. november", "warnings": [],
            "rows": [{"product_id": "515", "title": "Synthetic oats", "subtitle": "", "quantity": 1}],
            "amount_rows": [{"label": name, "value": amount} for name, amount in (
                ("1 vare", "20,00 kr"), ("Delsum", "20,00 kr"), ("Levering", "29,00 kr"), ("Total inkl. MVA", "49,00 kr"))],
            "payment": {"method": "saved_card", "selected": True, "masked_card": "•••• 4321"},
            "submit_control": {"label": "Bekreft og betal 49,00 kr", "enabled": True, "count": 1},
            "observed_at": self.clock.return_value.isoformat(),
        }
        if preflight:
            value["orders_observed_at"] = self.clock.return_value.isoformat()
            value["existing_orders"] = {"orders": [self.order_fixture("original")], "hasMore": False}
        return value

    @staticmethod
    def order_fixture(order_id):
        return {"orderNumber": order_id, "deliveryDate": DATE, "currency": "NOK", "grossAmount": 49.0,
                "products": [{"product": {"id": 515, "name": "Synthetic oats"}, "quantity": 1, "totalGrossAmount": "20.00"}]}

    def saved_card_preflight(self):
        started = self.step("begin_saved_card_checkout", slot_ref=SLOT, authorized_addresses=ADDRESS)
        self.assertFalse(started["dispatch"])
        self.assertFalse(started["hosted_request"]["native_browser"]["start_checkout_report"])
        review = self.step("observe_delivery", **self.checkout_observation(started))
        self.assertEqual(review["status"], "checkout_review")
        self.assertEqual(review["checkout_review"]["payable_ore"], 4900)
        for digest, total in (("wrong", 4900), (review["review_digest"], 4901)):
            with self.assertRaisesRegex(ValueError, "owner approval"):
                self.step("approve_saved_card_checkout", review_digest=digest, approved_total_ore=total)
        ready = self.step("approve_saved_card_checkout", review_digest=review["review_digest"], approved_total_ore=4900)
        self.assertTrue(ready["hosted_request"]["native_browser"]["start_checkout_report"])
        self.assertFalse(ready["dispatch"])
        return ready

    def receipt_observation(self, read, order, tracking="paid_and_modifiable"):
        frame = read["hosted_request"]
        stamp = self.clock.return_value.isoformat()
        return {"frame_id": frame["frame_id"], "plugin_id": "fixture", "addresses": ADDRESS,
                "address_observed_at": stamp, "observed_at": stamp, "order": self.order_fixture(order),
                "tracking": {"orderNumber": order, "status": tracking},
                "browser": {"url": f"https://oda.com/no/account/orders/{order}/", "browser_id": "cloud", "tab_id": "dedicated",
                            "attempt_id": self.identifier, "payment_label": "Betaling autorisert" if tracking.startswith("paid_and_") else None,
                            "address_id": 7, "address_label": "Fixture address", "total_ore": 4900, "original_tab_retired": False,
                            "order_number": order, "delivery": "Hjemlevering 04-09, 28. november"}}

    def checkout_confirmation(self, order, **overrides):
        return {"browser_id": "cloud", "tab_id": "dedicated", "attempt_id": self.identifier,
                "url": f"https://oda.com/no/checkout/success/?orderNumber={order}", "order_number": order,
                "order_link": None, "observed_at": self.clock.return_value.isoformat(),
                "native_report_id": "fixture-report-available-after-completion", **overrides}

    @staticmethod
    def checkout_retirement(**overrides):
        return {"browser_id": "cloud", "tab_id": "dedicated", "original_tab_retired": True,
                "owner_handoff_ended": True, "no_pending_bank_action": True, **overrides}

    def test_saved_card_dispatches_once_and_confirms_exact_original_purchase(self):
        ready = self.saved_card_preflight()
        facts = self.checkout_observation(ready, preflight=True)
        facts["existing_orders"]["hasMore"] = True
        submit = self.step("observe_delivery", **facts)
        self.assertTrue(submit["dispatch"])
        self.assertEqual(submit["hosted_request"]["native_browser"]["action"], "submit_saved_card_checkout")
        self.assertEqual(submit["hosted_request"]["native_browser"]["attempt_id"], self.identifier)
        protected = journal.read_private(self.directory / "ledger.json", journal.JOURNAL_LIMIT)
        self.assertEqual(protected["checkout_recovery_version"], 2)
        with self.assertRaises(ValueError):
            journal.require_fields(protected, ("key", "write_generation", "current"))
        self.assertFalse(self.step("status")["dispatch"])
        self.assertFalse(self.step("begin_saved_card_checkout", slot_ref=SLOT, authorized_addresses=ADDRESS)["dispatch"])
        with self.assertRaisesRegex(ValueError, "exact Oda delivery review"):
            self.step("observe_delivery", **facts)
        with self.assertRaisesRegex(ValueError, "still owns"):
            journal.run(self.state, self.directory, str(uuid.uuid4()),
                        {"action": "begin_saved_card_checkout", "slot_ref": SLOT, "authorized_addresses": ADDRESS}, "fixture", BUILD)
        with self.assertRaisesRegex(ValueError, "original Oda confirmation"):
            self.step("recheck_checkout", confirmation=self.checkout_confirmation("original"))
        with self.assertRaisesRegex(ValueError, "original Oda confirmation"):
            self.step("recheck_checkout", confirmation=self.checkout_confirmation("different", url="https://oda.com/no/checkout/success/?orderNumber=new"))
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation("new"))
        self.assertFalse(read["dispatch"])
        pending = self.step("observe_checkout_order", **self.receipt_observation(read, "new"))
        self.assertEqual(pending["status"], "checkout_readback")
        self.clock.return_value += timedelta(seconds=46)
        read = self.step("recheck_checkout")
        receipt = self.receipt_observation(read, "new")
        changed = deepcopy(receipt)
        changed["browser"]["tab_id"] = "retired"
        with self.assertRaisesRegex(ValueError, "bound native tab"):
            self.step("observe_checkout_order", **changed)
        changed = deepcopy(receipt)
        changed["order"]["grossAmount"] = 50
        with self.assertRaisesRegex(ValueError, "original order total and delivery"):
            self.step("observe_checkout_order", **changed)
        changed = deepcopy(receipt)
        changed["tracking"]["isError"] = True
        with self.assertRaisesRegex(ValueError, "returned an error"):
            self.step("observe_checkout_order", **changed)
        changed = deepcopy(receipt)
        changed["order"]["deliverySlotDisplay"] = "06:00–11:00"
        with self.assertRaisesRegex(ValueError, "original order total and delivery"):
            self.step("observe_checkout_order", **changed)
        receipt["browser"].update(tab_id="replacement", original_tab_retired=True)
        receipt["addresses"] = [{"id": 7, "isSelected": False}, {"id": 9, "isSelected": True}]
        done = self.step("observe_checkout_order", **receipt)
        self.assertEqual(done["status"], "checkout_confirmed")
        self.assertTrue(done["checkout_order"]["merchant_reports_payment_authorized"])
        self.assertFalse(self.step("status")["dispatch"])

    def test_saved_card_stale_purchase_review_or_unpaid_order_cannot_grant_another_click(self):
        ready = self.saved_card_preflight()
        original = self.checkout_observation(ready, preflight=True)
        for field, value in (("tab_id", "other"), ("new_order", False), ("delivery", "Hjemlevering 04-09, 29. november"),
                             ("exclusive_checkout", False), ("warnings", ["Changed goods"]),
                             ("payment", {"method": "saved_card", "selected": True, "masked_card": "•••• 9999"}),
                             ("rows", [{"product_id": "666", "title": "Synthetic oats", "subtitle": "", "quantity": 1}])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.step("observe_delivery", **{**original, "checkout": {**original["checkout"], field: value}})
        different_prices = deepcopy(original)
        for row, amount in zip(different_prices["checkout"]["amount_rows"], ("25,00 kr", "25,00 kr", "24,00 kr", "49,00 kr")):
            row["value"] = amount
        with self.assertRaisesRegex(ValueError, "goods, discounts or fees differ"):
            self.step("observe_delivery", **different_prices)
        self.assertTrue(self.step("observe_delivery", **original)["dispatch"])
        self.clock.return_value += timedelta(minutes=1)
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation("new", native_report_id=None))
        self.assertFalse(read["native_report_recorded"])
        conflict = self.receipt_observation(read, "new")
        conflict["browser"]["payment_label"] = "Betalingen mislyktes"
        unresolved = self.step("observe_checkout_order", **conflict)
        self.assertTrue(unresolved["checkout_order"]["payment_evidence_conflicting"])
        self.assertEqual(unresolved["status"], "checkout_readback")
        read = self.step("recheck_checkout", native_report_id="eventually-returned-native-report")
        self.assertTrue(read["native_report_recorded"])
        with self.assertRaisesRegex(ValueError, "report ID is invalid or changed"):
            self.step("recheck_checkout", native_report_id="different-native-report")
        outcome = self.step("observe_checkout_order", **self.receipt_observation(read, "new", "unpaid_order"))
        self.assertEqual(outcome["status"], "checkout_readback")
        self.assertFalse(outcome["checkout_order"]["merchant_reports_payment_authorized"])
        self.assertFalse(outcome["dispatch"])
        with self.assertRaises(ValueError):
            self.step("recheck_checkout", confirmation=self.checkout_confirmation("unrelated"))
        read = self.step("recheck_checkout")
        retired = self.step("observe_checkout_order", **self.receipt_observation(read, "new", "cancelled"))
        self.assertEqual(retired["status"], "checkout_order_inactive")
        self.assertFalse(retired["dispatch"])

    def test_managed_owner_handoff_blocks_new_checkout_until_original_handoff_is_retired(self):
        started = self.step("begin_checkout", slot_ref=SLOT, authorized_addresses=ADDRESS,
                            payment_method="vipps", owner_handoff=True)
        ledger_path = self.directory / "ledger.json"
        prior_ledger = journal.read_private(ledger_path, journal.JOURNAL_LIMIT)
        facts = self.checkout_observation(started, preflight=True)
        facts.pop("checkout")
        owner = self.step("observe_delivery", **facts)
        self.assertEqual(owner["status"], "checkout_owner_pending")
        self.assertTrue(owner["handoff"]["owner_completion_current"])
        self.assertTrue(owner["owner_handoff_ready"])
        self.assertNotIn("owner_handoff_ready", self.step("status"))
        self.assertFalse(owner["dispatch"])
        current_ledger = journal.read_private(ledger_path, journal.JOURNAL_LIMIT)
        self.assertEqual(current_ledger["write_generation"], prior_ledger["write_generation"] + 1)
        journal.write_json(ledger_path, prior_ledger)
        with self.assertRaisesRegex(ValueError, "generation"):
            self.step("observe_delivery", **facts)
        journal.write_json(ledger_path, current_ledger)
        new_order = {"action": "begin_saved_card_checkout", "slot_ref": SLOT, "authorized_addresses": ADDRESS}
        next_id = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "still owns"):
            journal.run(self.state, self.directory, next_id, new_order, "fixture", BUILD)
        retire = {"attempt_id": self.identifier, "reason": "owner_returned_without_submitting", "original_executor_retired": True,
                  "retirement": self.checkout_retirement(), "owner_confirmed_no_submit": True}
        with self.assertRaisesRegex(ValueError, "known pre-submit ending"):
            self.step("recheck_checkout_unsubmitted", **retire)
        self.clock.return_value += timedelta(minutes=3)
        with self.assertRaisesRegex(ValueError, "owner takeover"):
            self.step("recheck_checkout_unsubmitted", **{**retire, "retirement": self.checkout_retirement(owner_handoff_ended=False)})
        with self.assertRaisesRegex(ValueError, "known pre-submit ending"):
            self.step("recheck_checkout_unsubmitted", **{**retire, "owner_confirmed_no_submit": False})
        read = self.step("recheck_checkout_unsubmitted", **retire)
        current = {field: facts[field] for field in ("plugin_id", "addresses", "address_observed_at", "cart_observed_at",
                                                       "cart", "existing_orders", "orders_observed_at")}
        current["frame_id"] = read["hosted_request"]["frame_id"]
        stamp = self.clock.return_value.isoformat()
        current.update(address_observed_at=stamp, cart_observed_at=stamp, orders_observed_at=stamp)
        resolved = self.step("observe_checkout_unsubmitted", **current)
        self.assertEqual(resolved["status"], "checkout_not_submitted")
        self.assertFalse(resolved["handoff"]["owner_completion_current"])
        next_request = journal.run(self.state, self.directory, next_id, new_order, "fixture", BUILD)
        self.assertFalse(next_request["dispatch"])
        self.assertFalse(self.step("status")["handoff"]["owner_completion_current"])

    def test_lost_post_click_outcome_passes_only_compatible_maintenance_and_archives_after_original_window(self):
        ready = self.saved_card_preflight()
        self.assertTrue(self.step("observe_delivery", **self.checkout_observation(ready, preflight=True))["dispatch"])
        archive = {"attempt_id": self.identifier, "owner_accepted_unknown": True, "reviewed_total_ore": 4900,
                   "retirement": self.checkout_retirement()}
        with self.assertRaisesRegex(ValueError, "original delivery window ends"):
            self.step("recheck_checkout_archive", **archive)
        with self.assertRaisesRegex(ValueError, "permit to expire"):
            self.step("defer_checkout")
        self.clock.return_value += timedelta(minutes=1)
        self.assertTrue(self.step("defer_checkout")["maintenance_deferred"])
        with patch.object(journal, "DOT_WORKSPACE", self.state.parent):
            release = self.state.parent / "target"
            release.mkdir()
            with self.assertRaisesRegex(ValueError, "checkout-recovery compatible"):
                journal.require_idle(self.state, target_program=release)
            (release / "hosted_oda_checkout.py").write_text("CHECKOUT_RECOVERY_JOURNAL_VERSION = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checkout-recovery compatible"):
                journal.require_idle(self.state, target_program=release)
            (release / "hosted_oda_checkout.py").write_text("CHECKOUT_RECOVERY_JOURNAL_VERSION = 2\n", encoding="utf-8")
            journal.require_idle(self.state, target_program=release)
            with self.assertRaisesRegex(ValueError, "checkout-recovery compatible"):
                journal.require_idle(self.state)
        with self.assertRaisesRegex(ValueError, "still owns"):
            journal.run(self.state, self.directory, str(uuid.uuid4()),
                        {"action": "begin_saved_card_checkout", "slot_ref": SLOT, "authorized_addresses": ADDRESS}, "fixture", BUILD)
        self.clock.return_value += timedelta(days=2)
        with self.assertRaisesRegex(ValueError, "owner takeover"):
            self.step("recheck_checkout_archive", **{**archive, "retirement": self.checkout_retirement(no_pending_bank_action=False)})
        new_build = {**BUILD, "source_commit": "d" * 40}
        read = journal.run(self.state, self.directory, self.identifier, {"action": "recheck_checkout_archive", **archive}, "fixture", new_build)
        stamp = self.clock.return_value.isoformat()
        facts = {"frame_id": read["hosted_request"]["frame_id"], "plugin_id": "fixture", "addresses": ADDRESS,
                 "address_observed_at": stamp, "cart_observed_at": stamp, "cart": snapshot(), "orders_observed_at": stamp,
                 "existing_orders": {"orders": [self.order_fixture("new-candidate")], "hasMore": True}}
        archived = self.step("observe_checkout_archive", **facts)
        self.assertEqual(archived["status"], "checkout_archived_unknown")
        self.assertEqual(archived["original_dispatch_outcome"], "unknown")
        self.assertEqual(archived["checkout_archive"]["new_order_refs_on_original_date"], ["new-candidate"])
        self.assertFalse(self.step("status")["dispatch"])

    def test_original_retry_page_can_bind_failed_order_without_authorizing_payment(self):
        ready = self.saved_card_preflight()
        self.step("observe_delivery", **self.checkout_observation(ready, preflight=True))
        self.clock.return_value += timedelta(minutes=1)
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation(
            "declined", url="https://oda.com/no/checkout/retry/?orderNumber=declined"))
        unpaid = self.step("observe_checkout_order", **self.receipt_observation(read, "declined", "unpaid_order"))
        self.assertEqual(unpaid["status"], "checkout_readback")
        self.assertFalse(unpaid["dispatch"])
        read = self.step("recheck_checkout")
        observed = self.step("observe_checkout_order", **self.receipt_observation(read, "declined", "failed"))
        self.assertEqual(observed["checkout_order"]["original_reference_source"], "merchant_retry_page")
        self.assertEqual(observed["status"], "checkout_order_inactive")

    def test_owner_identified_original_confirmation_can_report_observed_changes(self):
        started = self.step("begin_checkout", slot_ref=SLOT, authorized_addresses=ADDRESS,
                            payment_method="saved_card", owner_handoff=True)
        facts = self.checkout_observation(started, preflight=True)
        facts.pop("checkout")
        self.step("observe_delivery", **facts)
        self.clock.return_value += timedelta(minutes=3)
        with self.assertRaisesRegex(ValueError, "tab that made the attempt"):
            self.step("recheck_checkout", confirmation=self.checkout_confirmation("changed", tab_id="replacement"))
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation(
            "changed", tab_id="replacement", owner_confirmed_original=True, retirement=self.checkout_retirement()))
        receipt = self.receipt_observation(read, "changed", "delivered")
        receipt["browser"].update(tab_id="replacement", original_tab_retired=True, total_ore=5200, payment_label="Betalt med kort")
        receipt["order"]["grossAmount"] = 52.0
        receipt["order"]["products"][0]["quantity"] = 2
        observed = self.step("observe_checkout_order", **receipt)
        self.assertEqual(observed["status"], "checkout_order_observed_changed")
        self.assertFalse(observed["checkout_order"]["matches_handoff_cart"])
        self.assertNotIn("approved_amount_ore", observed["checkout_order"])
        self.assertEqual(observed["checkout_order"]["quantity_changes"]["items"],
                         [{"product_id": "515", "reviewed_quantity": 1, "observed_quantity": 2}])
        self.assertEqual(observed["checkout_order"]["payment_observation"], "paid")
        self.assertEqual(observed["checkout_order"]["original_reference_source"], "owner_identified_original_success_link")

    def test_exact_order_archive_preserves_prior_conflict_and_reads_current_merchant_status(self):
        ready = self.saved_card_preflight()
        self.step("observe_delivery", **self.checkout_observation(ready, preflight=True))
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation("linked"))
        receipt = self.receipt_observation(read, "linked")
        receipt["browser"]["payment_label"] = "Betalingen avvist"
        earlier = self.step("observe_checkout_order", **receipt)
        self.assertTrue(earlier["checkout_order"]["payment_evidence_conflicting"])
        self.clock.return_value += timedelta(days=2)
        read = self.step("recheck_checkout_archive", attempt_id=self.identifier, owner_accepted_unknown=True,
                         reviewed_total_ore=4900, retirement=self.checkout_retirement())
        frame = read["hosted_request"]
        self.assertEqual([tool["tool"] for tool in frame["tools"][-2:]], ["get_order", "order_tracking"])
        stamp = self.clock.return_value.isoformat()
        facts = {"frame_id": frame["frame_id"], "plugin_id": "fixture", "addresses": ADDRESS, "address_observed_at": stamp,
                 "cart_observed_at": stamp, "cart": snapshot(), "orders_observed_at": stamp,
                 "existing_orders": {"orders": [self.order_fixture("linked")], "hasMore": False},
                 "order": self.order_fixture("linked"), "order_observed_at": stamp,
                 "tracking": {"orderNumber": "linked", "status": "delivered"}}
        done = self.step("observe_checkout_archive", **facts)
        self.assertEqual(done["status"], "checkout_order_resolved_by_merchant")
        self.assertTrue(done["checkout_order"]["payment_evidence_conflicting"])
        self.assertEqual(done["checkout_archive"]["payment_outcome"], "previously_conflicting_observations")
        self.assertEqual(done["checkout_archive"]["linked_merchant_tracking_status"], "delivered")
        self.assertEqual(done["checkout_archive"]["linked_order_detail"]["amount_ore"], 4900)
        self.assertEqual(done["checkout_archive"]["linked_order_detail"]["items"], [{"product_id": 515, "quantity": 1}])
        self.assertEqual(done["checkout_archive"]["new_order_refs_on_original_date"], [])

    def test_a_missing_later_label_preserves_the_last_explicit_payment_conflict(self):
        ready = self.saved_card_preflight()
        self.step("observe_delivery", **self.checkout_observation(ready, preflight=True))
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation("linked"))
        receipt = self.receipt_observation(read, "linked", "unpaid_order")
        receipt["browser"]["payment_label"] = "Betalingen avvist"
        self.step("observe_checkout_order", **receipt)
        self.clock.return_value += timedelta(minutes=1)
        read = self.step("recheck_checkout")
        later = self.receipt_observation(read, "linked")
        later["browser"]["payment_label"] = None
        done = self.step("observe_checkout_order", **later)
        self.assertEqual(done["status"], "checkout_readback")
        self.assertTrue(done["checkout_order"]["payment_conflict_unresolved"])
        self.assertEqual(done["checkout_order"]["prior_labeled_payment"]["payment_observation"], "negative")
        read = self.step("recheck_checkout")
        paid = self.receipt_observation(read, "linked")
        paid["browser"]["payment_label"] = "Betalt med kort"
        self.assertEqual(self.step("observe_checkout_order", **paid)["status"], "checkout_confirmed")

    def test_unfamiliar_visible_payment_wording_fences_checkout_even_if_the_label_disappears(self):
        ready = self.saved_card_preflight()
        self.step("observe_delivery", **self.checkout_observation(ready, preflight=True))
        self.clock.return_value += timedelta(minutes=1)
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation("linked"))
        for label in ("Betaling feilet", "Unfamiliar payment message", None):
            receipt = self.receipt_observation(read, "linked", "picking")
            receipt["browser"]["payment_label"] = label
            pending = self.step("observe_checkout_order", **receipt)
            self.assertEqual(pending["status"], "checkout_readback")
            self.assertFalse(pending["dispatch"])
            read = self.step("recheck_checkout")

    def test_unlabeled_tracking_changes_cannot_clear_an_earlier_payment_conflict(self):
        ready = self.saved_card_preflight()
        self.step("observe_delivery", **self.checkout_observation(ready, preflight=True))
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation("linked"))
        self.step("observe_checkout_order", **self.receipt_observation(read, "linked"))
        self.clock.return_value += timedelta(minutes=1)
        for tracking in ("unpaid_order", "picking"):
            read = self.step("recheck_checkout")
            receipt = self.receipt_observation(read, "linked", tracking)
            receipt["browser"]["payment_label"] = None
            pending = self.step("observe_checkout_order", **receipt)
            self.assertEqual(pending["status"], "checkout_readback")
            self.assertTrue(pending["checkout_order"]["payment_conflict_unresolved"])
            self.assertFalse(pending["dispatch"])
        read = self.step("recheck_checkout")
        self.assertEqual(self.step("observe_checkout_order", **self.receipt_observation(read, "linked"))["status"],
                         "checkout_confirmed")

    def archive_checkout_payment_observations(self, *, legacy,
                                               first=("paid_and_modifiable", "Betaling autorisert"),
                                               latest=("paid_and_modifiable", "Unfamiliar payment wording"),
                                               archive_tracking=None):
        ready = self.saved_card_preflight()
        self.step("observe_delivery", **self.checkout_observation(ready, preflight=True))
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation("linked"))
        for index, (tracking, label) in enumerate((first, latest)):
            if index:
                if not legacy:
                    self.clock.return_value += timedelta(minutes=1)
                read = self.step("recheck_checkout")
            receipt = self.receipt_observation(read, "linked", tracking)
            receipt["browser"]["payment_label"] = label
            self.assertEqual(self.step("observe_checkout_order", **receipt)["status"], "checkout_readback")
        self.clock.return_value += timedelta(days=2)
        build = BUILD
        if legacy:
            ledger_path = self.directory / "ledger.json"
            old_ledger = journal.read_private(ledger_path, journal.JOURNAL_LIMIT)
            old_ledger.pop("checkout_recovery_version")
            checkout = old_ledger["current"]["delivery"]["checkout"]
            checkout.pop("unclear_payment_label_at", None)
            checkout["result"].pop("payment_label_unresolved")
            checkout["result"]["payment_conflict_unresolved"] = checkout["last_labeled_payment"]["payment_evidence_conflicting"]
            journal.write_json(ledger_path, old_ledger)
            target = Path(journal.__file__).parent
            with patch.object(journal, "DOT_WORKSPACE", self.state.parent):
                journal.require_idle(self.state, target_program=target)
            build = {**BUILD, "source_commit": "d" * 40}
        read = journal.run(self.state, self.directory, self.identifier, {
            "action": "recheck_checkout_archive", "attempt_id": self.identifier, "owner_accepted_unknown": True,
            "reviewed_total_ore": 4900, "retirement": self.checkout_retirement(),
        }, "fixture", build)
        stamp = self.clock.return_value.isoformat()
        tracking = None if archive_tracking is None else {"orderNumber": "linked", "status": archive_tracking}
        return journal.run(self.state, self.directory, self.identifier, {
            "action": "observe_checkout_archive", "frame_id": read["hosted_request"]["frame_id"], "plugin_id": "fixture",
            "addresses": ADDRESS, "address_observed_at": stamp, "cart": snapshot(), "cart_observed_at": stamp,
            "existing_orders": {"orders": [], "hasMore": False}, "orders_observed_at": stamp,
            "order": None, "tracking": tracking, "order_observed_at": stamp,
        }, "fixture", build)

    def test_archived_payment_summary_retains_unfamiliar_wording_after_earlier_authorization(self):
        done = self.archive_checkout_payment_observations(legacy=False)
        self.assertEqual(done["checkout_archive"]["payment_outcome"], "previously_unresolved_payment_label")
        self.assertTrue(done["checkout_order"]["payment_label_unresolved"])
        self.assertEqual(done["checkout_order"]["prior_labeled_payment"]["payment_observation"], "authorized")

    def test_legacy_checkout_archive_sees_unrecognized_latest_payment_without_another_order_read(self):
        done = self.archive_checkout_payment_observations(legacy=True)
        self.assertEqual(done["status"], "checkout_archived_unknown")
        self.assertFalse(done["dispatch"])
        self.assertEqual(done["checkout_archive"]["payment_outcome"], "previously_unresolved_payment_label")
        self.assertEqual(done["checkout_order"]["payment_observation"], "unrecognized")
        self.assertEqual(done["checkout_order"]["prior_labeled_payment"]["payment_observation"], "authorized")

    def test_legacy_checkout_archive_rechecks_earlier_authorization_against_stored_unpaid_tracking(self):
        done = self.archive_checkout_payment_observations(legacy=True, latest=("unpaid_order", None))
        self.assertEqual(done["status"], "checkout_archived_unknown")
        self.assertFalse(done["dispatch"])
        self.assertEqual(done["checkout_archive"]["payment_outcome"], "previously_conflicting_observations")
        self.assertEqual(done["checkout_order"]["merchant_tracking_status"], "unpaid_order")
        self.assertEqual(done["checkout_order"]["prior_labeled_payment"]["payment_observation"], "authorized")

    def test_legacy_archive_conflict_takes_precedence_over_unrecognized_payment_wording(self):
        done = self.archive_checkout_payment_observations(legacy=True, first=("unpaid_order_change", "Ikke betalt"),
                                                           latest=("picking", "Unfamiliar payment wording"))
        self.assertEqual(done["status"], "checkout_archived_unknown")
        self.assertFalse(done["dispatch"])
        self.assertEqual(done["checkout_archive"]["payment_outcome"], "previously_conflicting_observations")
        self.assertEqual(done["checkout_order"]["merchant_tracking_status"], "picking")
        self.assertEqual(done["checkout_order"]["prior_labeled_payment"]["payment_observation"], "negative")

    def test_archive_reports_when_fresh_linked_tracking_disagrees_with_the_last_payment_label(self):
        done = self.archive_checkout_payment_observations(legacy=True, latest=("paid_and_modifiable", None),
                                                           archive_tracking="unpaid_order_change")
        self.assertEqual(done["status"], "checkout_archived_unknown")
        self.assertFalse(done["dispatch"])
        self.assertEqual(done["checkout_archive"]["payment_outcome"], "previously_conflicting_observations")
        self.assertEqual(done["checkout_archive"]["linked_merchant_tracking_status"], "unpaid_order_change")
        self.assertEqual(done["checkout_order"]["prior_labeled_payment"]["payment_observation"], "authorized")

    def test_migrated_payment_conflict_survives_two_unlabeled_reads(self):
        ready = self.saved_card_preflight()
        self.step("observe_delivery", **self.checkout_observation(ready, preflight=True))
        read = self.step("recheck_checkout", confirmation=self.checkout_confirmation("linked"))
        receipt = self.receipt_observation(read, "linked", "unpaid_order")
        receipt["browser"]["payment_label"] = "Betaling autorisert"
        self.step("observe_checkout_order", **receipt)
        ledger_path = self.directory / "ledger.json"
        legacy = journal.read_private(ledger_path, journal.JOURNAL_LIMIT)
        checkout = legacy["current"]["delivery"]["checkout"]
        checkout.pop("last_labeled_payment")
        checkout["result"]["payment_observation"] = "unrecognized"
        journal.write_json(ledger_path, legacy)
        for _ in range(2):
            read = self.step("recheck_checkout")
            observed = self.step("observe_checkout_order", **self.receipt_observation(read, "linked", "unpaid_order"))
            self.assertTrue(observed["checkout_order"]["payment_conflict_unresolved"])
            self.assertEqual(observed["checkout_order"]["prior_labeled_payment"]["payment_observation"], "unrecognized")
        self.clock.return_value += timedelta(days=2)
        read = self.step("recheck_checkout_archive", attempt_id=self.identifier, owner_accepted_unknown=True,
                         reviewed_total_ore=4900, retirement=self.checkout_retirement())
        stamp = self.clock.return_value.isoformat()
        archived = self.step("observe_checkout_archive", frame_id=read["hosted_request"]["frame_id"], plugin_id="fixture",
                             addresses=ADDRESS, address_observed_at=stamp, cart=snapshot(), cart_observed_at=stamp,
                             existing_orders={"orders": [], "hasMore": False}, orders_observed_at=stamp,
                             order=None, tracking=None, order_observed_at=stamp)
        self.assertEqual(archived["checkout_archive"]["payment_outcome"], "previously_conflicting_observations")

    def test_owner_handoff_admission_ends_with_the_observed_hold(self):
        started = self.step("begin_checkout", slot_ref=SLOT, authorized_addresses=ADDRESS,
                            payment_method="vipps", owner_handoff=True)
        facts = self.checkout_observation(started, preflight=True)
        facts.pop("checkout")
        end = (TIME + timedelta(seconds=5)).isoformat()
        facts["cart"]["deliverySlot"]["expireAt"] = end
        owner = self.step("observe_delivery", **facts)
        self.assertEqual(owner["owner_handoff_admit_by"], end)

    def test_installer_adopts_an_expired_legacy_checkout_but_refuses_its_unsubmitted_frame(self):
        started = self.step("begin_checkout", slot_ref=SLOT, authorized_addresses=ADDRESS,
                            payment_method="saved_card", owner_handoff=True)
        facts = self.checkout_observation(started, preflight=True)
        facts.pop("checkout")
        self.step("observe_delivery", **facts)
        self.clock.return_value += timedelta(minutes=3)
        read = self.step("recheck_checkout_unsubmitted", attempt_id=self.identifier, reason="owner_handoff_never_opened",
                         original_executor_retired=True, retirement=self.checkout_retirement(), owner_confirmed_no_submit=False)
        ledger_path = self.directory / "ledger.json"
        legacy = journal.read_private(ledger_path, journal.JOURNAL_LIMIT)
        legacy["current"]["frame"].pop("retirement_version")
        legacy.pop("checkout_recovery_version")
        journal.write_json(ledger_path, legacy)

        home, code = (self.state.parent / name for name in ("home", "code"))
        home.mkdir()
        code.mkdir()
        target = code / "staged"
        target.mkdir()
        config = home / "config.json"
        plugin = "plugin_checkout_fixture"
        journal.write_json(config, {"household": "Fixture", "provider": "oda", "hosted_oda_plugin": plugin})
        journal.write_json(home / "runtime.json", {"manager": "external", "hosted_oda_plugin": plugin, "code_root": str(code),
                                                   "paths": {"config": str(config), "state": str(self.state)}})
        with (patch.object(sys, "argv", ["install.py", "update", "--home", str(home)]),
              patch.object(journal, "DOT_WORKSPACE", self.state.parent), patch.object(install.os, "umask"),
              patch.object(install, "assert_stopped"), patch.object(install, "stage_release", return_value=target),
              patch.object(install, "offline", side_effect=RuntimeError("fixture reached publication"))):
            with self.assertRaisesRegex(ValueError, "checkout-recovery compatible"):
                install.main()
            (target / "hosted_oda_checkout.py").write_text("CHECKOUT_RECOVERY_JOURNAL_VERSION = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "fixture reached publication"):
                install.main()
        adopted = journal.read_private(ledger_path, journal.JOURNAL_LIMIT)
        self.assertTrue(adopted["current"]["checkout_recovery_deferred"])
        self.assertNotIn("maintenance_deferred", adopted["current"])
        self.assertEqual(adopted["checkout_recovery_version"], 2)
        self.assertEqual(adopted["write_generation"], legacy["write_generation"])
        self.assertEqual(adopted["current"]["build"], legacy["current"]["build"])
        stamp = self.clock.return_value.isoformat()
        completion = {"action": "observe_checkout_unsubmitted", "frame_id": read["hosted_request"]["frame_id"], "plugin_id": "fixture",
                      "addresses": ADDRESS, "cart": facts["cart"], "address_observed_at": stamp, "cart_observed_at": stamp,
                      "existing_orders": facts["existing_orders"], "orders_observed_at": stamp}
        with self.assertRaisesRegex(ValueError, "current retirement evidence"):
            journal.run(self.state, self.directory, self.identifier, completion, "fixture", {**BUILD, "source_commit": "d" * 40})

    def test_expired_review_never_dispatches_and_grouped_cart_can_close_error(self):
        reading = self.begin()
        reviewed = self.step("observe_delivery", **self.observation(reading))
        self.clock.return_value += timedelta(minutes=10)
        with self.assertRaisesRegex(ValueError, "quote or cutoff expired"):
            self.step("approve_delivery", review_digest=reviewed["review_digest"])
        self.step("abort")
        self.identifier = str(uuid.uuid4())
        write = self.step("observe_delivery", **self.observation(self.preflight()))
        read = self.acknowledge(write, error=True)
        self.clock.return_value += timedelta(minutes=3)
        cart = snapshot()
        cart["groups"][0]["groupType"] = "recipe"
        recovered = self.step("observe_delivery", **self.observation(read, cart=cart))
        self.assertEqual(recovered["status"], "delivery_inactive")
        self.assertFalse(recovered["original_cart_still_matches"])

    def test_reservation_can_be_read_back_across_cutoff_without_checkout_authority(self):
        self.clock.return_value = datetime.fromisoformat(CUTOFF) - timedelta(seconds=30)
        write = self.step("observe_delivery", **self.observation(self.preflight()))
        hold = selected_slot(expireAt="2026-11-27T19:40:00+00:00")
        self.clock.return_value += timedelta(seconds=31)
        read = self.acknowledge(write, slot=hold)
        result = self.verify(read, hold, full=True)
        self.assertEqual(result["status"], "delivery_reserved")
        self.assertTrue(result["reservation"]["booking_cutoff_passed"])
        self.assertFalse(result["dispatch"])

    def test_owner_archive_after_address_switch_requires_original_account_and_reports_drift(self):
        prepared = self.preflight()
        write = self.step("observe_delivery", **self.observation(prepared))
        self.assertTrue(write["dispatch"])
        self.clock.return_value += timedelta(minutes=3)
        frame = self.step("accept_delivery_current", review_digest=prepared["review_digest"],
                          owner_accepted_unknown=True, original_executor_stopped=True)
        other_cart = {**snapshot(), "deliveryAddress": "Different selected fixture address"}
        same_account = [{"id": 7, "isSelected": False}, {"id": 9, "isSelected": True}]
        observation = self.observation(frame, cart=other_cart, address_label=other_cart["deliveryAddress"],
                                       addresses=same_account)
        with self.assertRaisesRegex(ValueError, "original Oda account"):
            self.step("observe_delivery", **{**observation, "addresses": [{"id": 9, "isSelected": True}]})
        with self.assertRaisesRegex(ValueError, "original Oda account"):
            self.step("observe_delivery", **{**observation, "addresses": [{"id": 7, "isSelected": False}, {"id": 8, "isSelected": True}]})
        with self.assertRaisesRegex(ValueError, "original Oda account"):
            self.step("observe_delivery", **{**observation, "browser": {**observation["browser"], "tab_id": "other-tab"}})
        result = self.step("observe_delivery", **observation)
        self.assertEqual(result["status"], "delivery_archived_unknown")
        self.assertTrue(result["observed_address_changed"])
        self.assertFalse(result["original_cart_still_matches"])
        self.assertEqual(result["original_dispatch_outcome"], "unknown")

    def test_regular_recheck_can_settle_after_archive_stops_on_a_retired_tab(self):
        prepared = self.preflight()
        observation = self.observation(prepared)
        observation["browser"]["cart_url"] = "https://oda.com/no/cart"
        write = self.step("observe_delivery", **observation)
        self.assertTrue(write["dispatch"])
        self.clock.return_value += timedelta(minutes=3)
        archive = self.step("accept_delivery_current", review_digest=prepared["review_digest"],
                            owner_accepted_unknown=True, original_executor_stopped=True)
        moved = self.observation(archive)
        moved["browser"]["tab_id"] = "replacement-tab"
        with self.assertRaisesRegex(ValueError, "original Oda account"):
            self.step("observe_delivery", **moved)
        self.clock.return_value = datetime.fromisoformat(CUTOFF) + timedelta(seconds=1)
        read = self.step("recheck")
        observation = self.observation(read)
        observation["browser"]["tab_id"] = "replacement-tab"
        settled = self.step("observe_delivery", **observation)
        self.assertEqual(settled["status"], "delivery_inactive")
        self.assertEqual(settled["original_dispatch_outcome"], "unknown")
        self.assertNotIn("owner_accepted_unknown_at", settled)
        self.assertFalse(settled["dispatch"])


if __name__ == "__main__":
    unittest.main()
