"""Values inserted into page scripts are never searched for another placeholder."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import meny
from order_reductions import _spec, _ui_script


class RecordingMeny(meny.MenyClient):
    """Run the cart and checkout controls with synthetic browser replies."""
    def __init__(self, *replies):
        self.scripts = []
        self.replies = iter(replies)
        self.deadline = None

    def _eval(self, script):
        self.scripts.append(script)
        return next(self.replies)

    def _invoke(self, *arguments, **_options):
        return {"x": 600, "y": 400, "width": 81, "height": 41} if arguments[:2] == ("get", "box") else {}

    def _require_time(self, _seconds):
        pass

    def _sleep(self, _seconds):
        pass


class ScriptPlaceholderTests(unittest.TestCase):
    def test_meny_cart_control_compares_the_exact_label_for_any_product_name(self):
        for product, name in (("/varer/meieri/yoghurt/yoghurt-naturell-7038010000000", "Brokkoli"),
                              ("/varer/meieri/yoghurt/yoghurt-naturell-7038010000000", "Yoghurt Naturell 500g Tine"),
                              ("/varer/vask/x-tra-7038010000001", "X-tra Vaskemiddel"),
                              ("/varer/x/LABEL-__HIT_Y__-1", "LABEL PRODUCT XY __HIT_X__")):
            with self.subTest(name=name):
                label = f"Legg {name} i handlevognen"
                client, dispatched = RecordingMeny({"ready": True}, {"clear": True}), []
                client._click_cart_control(product, label, lambda: dispatched.append(True), lambda: True)
                ready, hit_test = client.scripts
                for script in (ready, hit_test):
                    self.assertIn(f"location.pathname === {json.dumps(product)} ", script)
                    self.assertIn(f"=== {json.dumps(label)}", script)
                self.assertIn("document.elementFromPoint(640, 420)", hit_test)
                self.assertEqual(dispatched, [True])

    def test_meny_cart_removal_and_readback_keep_the_reviewed_product_path(self):
        product = meny.normalize_product_ref("/varer/meieri/__QUANTITY__-__CODE__-__HIT_X__-7038010000000")
        client = RecordingMeny({"ready": True}, {"ready": True}, {"ready": True, "authenticated": True, "quantity": 1})
        dispatched = []
        client._click_cart_remove_control(product, 2, None, lambda: dispatched.append(True))
        self.assertEqual(client._wait_for_cart_quantity(product, 1, None), 1)
        ready, hit, readback = client.scripts
        for script in (ready, hit, readback):
            self.assertIn(f"expectedProduct = {json.dumps(product)},", script)
            self.assertIn("expectedCode = null", script)
        self.assertIn("expectedQuantity = 2", ready)
        self.assertIn("document.elementFromPoint(640, 420)", hit)
        self.assertEqual(dispatched, [True])

    def test_meny_checkout_step_keeps_the_reviewed_product_path(self):
        product = meny.normalize_product_ref("/varer/meieri/ost-__TARGET_CODE__-7038010000000")
        client = RecordingMeny({"ready": True}, {"ready": True}, {"ready": True})
        client._click_checkout_control("checkout-next", expected_items=[(product, 2)], target_code="TEST-1")
        self.assertEqual(len(client.scripts), 3)
        for script in client.scripts:
            self.assertIn(f'expectedItems = {json.dumps([[product, 2]])}, expectedCode = "TEST-1"', script)

    def test_order_removal_script_keeps_product_names_that_contain_placeholder_words(self):
        items = [{"product_id": 1, "name": "Kellogg's SPECIAL K", "quantity": 1, "previous_quantity": 2,
                  "credit_ore": 4990, "requested": True},
                 {"product_id": 2, "name": "BACKSTAGE Energidrikk ITEMS ORDER_ID PRODUCT_ID", "quantity": 1,
                  "previous_quantity": 1, "credit_ore": 0, "requested": False}]
        script = _ui_script("ORDER-1", items, "open", 1, "oda")
        expected = ("const orderId = " + json.dumps("ORDER-1") + ", items = " + json.dumps(items)
                    + ", stage = " + json.dumps("open") + ", productId = 1, spec = "
                    + json.dumps(_spec("oda"), ensure_ascii=False) + ";")
        self.assertIn(expected, script)
        self.assertEqual(script.count(json.dumps(items)), 1)


if __name__ == "__main__":
    unittest.main()
