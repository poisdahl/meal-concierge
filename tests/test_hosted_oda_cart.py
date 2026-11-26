"""Durable, host-supplied evidence for a one-item Oda MCP cart round trip."""
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hosted_oda
import hosted_oda_cart as cart


PRODUCT = 515


def snapshot(quantity=0, *, product=PRODUCT):
    amount = "20.00" if quantity else "0.00"
    breakdown = dict.fromkeys(hosted_oda.BREAKDOWN_FIELDS, "0.00")
    breakdown.update(subtotal=amount, total=amount)
    items = [{"product": {"id": product, "name": "Synthetic oats"}, "quantity": 1,
              "totalGrossAmount": amount}] if quantity else []
    return {"groups": [{"items": items}] if items else [], "productQuantityCount": quantity,
            "totalGrossAmount": amount, "totalBreakdown": breakdown, "url": "https://oda.com/no/cart/",
            "deliverySlot": None}


class HostedCartTest(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = folder / "state"
        self.state.mkdir(mode=0o700)
        self.registry = folder / cart.REGISTRY_NAME
        self.identifier = str(uuid.uuid4())
        self.plugin = "plugin_fixture"
        self.build = {"source_commit": "a" * 40, "source_sha256": "b" * 64,
                      "installed_runtime_sha256": "c" * 64}

    def run_step(self, action, **fields):
        return cart.run(self.state, self.registry, self.identifier,
                        {"action": action, **fields}, self.plugin, self.build)

    def begin(self):
        return self.run_step("begin", product_id=PRODUCT, isolated_new_cart=True,
                             authorized_addresses=[{"id": 7, "isSelected": True}])

    def observation(self, response, quantity=0, *, account=7, data=None):
        frame = response["hosted_request"]
        observed = cart.now().isoformat()
        return {"frame_id": frame["frame_id"], "plugin_id": frame["plugin_id"],
                "address_observed_at": observed, "cart_observed_at": observed,
                "addresses": [{"id": account, "isSelected": True}],
                "cart": snapshot(quantity) if data is None else data}

    def acknowledgement(self, response, quantity=1):
        frame = response["hosted_request"]
        return {**{key: frame[key] for key in ("frame_id", "plugin_id", "tool", "arguments")},
                "dispatched_at": frame["issued_at"], "observed_at": cart.now().isoformat(),
                "result_source": "original_attached_mutation_response", "is_error": False,
                "invocation_id": None, "cart": snapshot(quantity)}

    def dispatch_add(self):
        self.assertEqual(self.run_step("observe", **self.observation(self.begin()))["status"],
                         "baseline_verified")
        return self.run_step("observe", **self.observation(self.run_step("add")))

    def test_add_and_remove_require_separate_readback_and_original_product(self):
        write = self.dispatch_add()
        self.assertTrue(write["dispatch"])
        self.assertEqual(write["hosted_request"]["arguments"],
                         {"operations": [{"productId": PRODUCT, "quantity": 1}]})
        status = self.run_step("status")
        self.assertFalse(status["dispatch"])
        self.assertEqual(status["pending_response"]["frame_id"], write["hosted_request"]["frame_id"])
        self.assertNotIn("arguments", status["pending_response"])
        self.assertNotIn("hosted_request", self.begin())
        with self.assertRaisesRegex(ValueError, "does not allow"):
            self.run_step("remove")
        add_read = self.run_step("acknowledge", **self.acknowledgement(write))
        self.assertFalse(add_read["dispatch"])
        reread = self.run_step("retry_read")
        with self.assertRaisesRegex(ValueError, "different hosted Oda frame"):
            self.run_step("observe", **self.observation(add_read, 1))
        self.assertEqual(self.run_step("observe", **self.observation(reread, 1))["status"], "added")
        remove_read = self.run_step("remove")
        cleanup = self.run_step("observe", **self.observation(remove_read, 1))
        self.assertEqual(cleanup["hosted_request"]["arguments"],
                         {"operations": [{"productId": PRODUCT, "quantity": -1}]})
        with self.assertRaisesRegex(ValueError, "different hosted Oda frame"):
            self.run_step("acknowledge", **self.acknowledgement(write))
        final_read = self.run_step("acknowledge", **self.acknowledgement(cleanup, 0))
        restored = self.run_step("observe", **self.observation(final_read))
        self.assertEqual(restored["status"], "restored")
        self.assertTrue(restored["add_acknowledged"])
        self.assertTrue(restored["remove_acknowledged"])
        self.assertFalse(restored["dispatch"])
        self.assertNotIn('"isSelected"', (self.registry / "ledger.json").read_text())

    def test_lost_original_ack_remains_fenced_even_with_readback_and_new_home(self):
        write = self.dispatch_add()
        ledger = (self.registry / "ledger.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "no original read frame"):
            self.run_step("observe", **self.observation(write, 1))
        for action in ("add", "remove", "retry_read", "abort"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "does not allow"):
                self.run_step(action)
        second = self.state.parent / "other"
        second.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "original.*still owns"):
            cart.run(second, self.registry, str(uuid.uuid4()),
                     {"action": "begin", "product_id": PRODUCT, "isolated_new_cart": True,
                      "authorized_addresses": [{"id": 7, "isSelected": True}]},
                     "different_plugin", self.build)
        self.assertEqual(ledger, (self.registry / "ledger.json").read_bytes())

    def test_changed_account_grouped_items_and_selected_slot_stop_dispatch(self):
        baseline = self.begin()
        with self.assertRaisesRegex(ValueError, "account/address context changed"):
            self.run_step("observe", **self.observation(baseline, account=8))
        self.run_step("observe", **self.observation(baseline))
        checking = self.run_step("add")
        with self.assertRaisesRegex(ValueError, "account/address context changed"):
            self.run_step("observe", **self.observation(checking, account=8))
        with self.assertRaisesRegex(ValueError, "selected slot"):
            self.run_step("observe", **self.observation(checking, data={**snapshot(), "deliverySlot": {"id": 9}}))
        write = self.run_step("observe", **self.observation(checking))
        after = snapshot(1)
        after["groups"][0].update(id=23, title="Grouped recipe", groupType="recipes")
        ack = self.acknowledgement(write)
        ack["cart"] = after
        ending = self.run_step("acknowledge", **ack)
        self.assertEqual(ending["status"], "add_response_unverified")
        self.assertTrue(ending["add_acknowledged"])
        self.assertNotIn("hosted_request", ending)
        with self.assertRaisesRegex(ValueError, "does not allow"):
            self.run_step("remove")

    def test_original_response_requires_success_provenance_and_known_no_slot(self):
        write = self.dispatch_add()
        ack = self.acknowledgement(write)
        for bad in ({"result_source": "get_cart"},
                    {"arguments": {"operations": [{"productId": PRODUCT, "quantity": -1}]}}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "original hosted Oda mutation"):
                self.run_step("acknowledge", **{**ack, **bad})
        ack["cart"].pop("deliverySlot")
        with self.assertRaisesRegex(ValueError, "unreserved"):
            self.run_step("acknowledge", **ack)
        self.assertEqual(self.run_step("status")["status"], "awaiting_add_response")

    def test_terminal_provider_error_stays_fenced(self):
        write = self.dispatch_add()
        ack = {**self.acknowledgement(write), "is_error": True, "cart": None}
        ending = self.run_step("acknowledge", **ack)
        self.assertEqual(ending["status"], "add_response_unverified")
        self.assertTrue(ending["add_acknowledged"])
        self.assertFalse(ending["dispatch"])
        with self.assertRaisesRegex(ValueError, "does not allow"):
            self.run_step("abort")

    def test_stale_pre_dispatch_address_cannot_open_a_new_write_window(self):
        self.run_step("observe", **self.observation(self.begin()))
        checking = self.run_step("add")
        reading = self.observation(checking)
        after = hosted_oda.response_time(reading["address_observed_at"]) + timedelta(minutes=2, seconds=1)
        with patch.object(cart, "now", return_value=after):
            with self.assertRaisesRegex(ValueError, "pre-dispatch observation expired"):
                self.run_step("observe", **reading)
        self.assertEqual(self.run_step("status")["status"], "checking_add")

    def test_shared_workspace_has_one_registry_and_active_writes_guard_maintenance(self):
        with patch.object(cart, "DOT_WORKSPACE", self.state.parent):
            self.assertEqual(cart.workspace_registry(self.state), self.registry)
            second = self.state.parent / "other"
            second.mkdir(mode=0o700)
            self.assertEqual(cart.workspace_registry(second), self.registry)
            self.dispatch_add()
            with self.assertRaisesRegex(ValueError, "before changing its program"):
                cart.require_idle(self.state)
            (self.state / cart.BINDING_NAME).unlink()
            with self.assertRaisesRegex(ValueError, "before changing its program"):
                cart.require_idle(self.state)
            cart.require_idle(second)

    def test_expiry_freezes_writes_but_can_record_actual_original_response_after_restart(self):
        write = self.dispatch_add()
        expiry = hosted_oda.response_time(write["hosted_request"]["expires_at"])
        after_expiry = expiry + timedelta(seconds=5)
        ack = self.acknowledgement(write)
        with patch.object(cart, "now", return_value=after_expiry):
            when = after_expiry.isoformat()
            with self.assertRaisesRegex(ValueError, "dispatch time"):
                self.run_step("acknowledge", **{**ack, "dispatched_at": when, "observed_at": when})
            late = self.run_step("acknowledge", **{**ack, "observed_at": when})
            self.assertEqual(late["status"], "verify_add")
            self.assertFalse(late["dispatch"])
        self.build["source_sha256"] = "d" * 64
        self.assertEqual(self.run_step("status")["status"], "verify_add")
        with self.assertRaisesRegex(ValueError, "original active.*installed build"):
            self.run_step("retry_read")

    def test_registry_cannot_be_replaced_or_reinitialized_after_loss(self):
        self.begin()
        different = self.state / cart.REGISTRY_NAME
        with self.assertRaisesRegex(ValueError, "original hosted Oda cart registry"):
            cart.run(self.state, different, self.identifier, {"action": "status"}, self.plugin, self.build)
        (self.registry / "ledger.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.begin()
        self.registry.rmdir()
        second = self.state.parent / "other"
        second.mkdir(mode=0o700)
        with self.assertRaises(FileNotFoundError):
            cart.run(second, self.registry, str(uuid.uuid4()), {"action": "begin", "product_id": PRODUCT,
                     "isolated_new_cart": True, "authorized_addresses": [{"id": 7, "isSelected": True}]},
                     self.plugin, self.build)
        self.assertFalse(self.registry.exists())

    def test_restored_older_ledger_cannot_duplicate_original_dispatch(self):
        self.run_step("observe", **self.observation(self.begin()))
        checking = self.run_step("add")
        ledger = self.registry / "ledger.json"
        previous = ledger.read_bytes()
        self.assertTrue(self.run_step("observe", **self.observation(checking))["dispatch"])
        ledger.write_bytes(previous)
        with self.assertRaisesRegex(ValueError, "workspace write generation disagree"):
            self.run_step("observe", **self.observation(checking))

    def test_prewrite_abort_and_archived_id_never_issue_again(self):
        original = self.identifier
        self.begin()
        self.assertEqual(self.run_step("abort")["status"], "aborted")
        self.identifier = str(uuid.uuid4())
        self.assertEqual(self.begin()["status"], "reading_baseline")
        self.identifier = original
        self.assertEqual(self.begin()["status"], "aborted")
        self.assertNotIn("hosted_request", self.begin())
