"""Hosted Oda delivery approval and independent reservation readback."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hosted_oda
import hosted_oda_cart as journal


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
        stamp = self.clock.return_value.isoformat()
        return {"frame_id": frame["frame_id"], "plugin_id": "fixture", "addresses": ADDRESS,
                "address_observed_at": stamp, "cart_observed_at": stamp, "slots_observed_at": stamp,
                "cart": snapshot() if cart is None else cart,
                "address_label": "Fixture address", "browser": {"browser_id": "cloud", "tab_id": "dedicated",
                    "address_id": 7, "new_cart": True, "cart_url": "https://oda.com/no/cart/"},
                "slots": {"deliveryDate": DATE, "slots": [provider_slot() if slot is None else slot]},
                "slot_arguments": frame["tools"][-1]["arguments"], **overrides}

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
        self.assertFalse(result["dispatch"])
        with self.assertRaisesRegex(ValueError, "different delivery window or account"):
            self.step("begin_checkout", slot_ref=SLOT, authorized_addresses=ADDRESS, payment_method="saved_card")

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
