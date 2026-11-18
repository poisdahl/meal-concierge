"""Values inserted into page scripts are never searched for another placeholder."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import meny
from order_reductions import _spec, _ui_script


class RecordingMeny:
    """The real cart-control click with the browser replaced by a recorder."""
    _find_box = staticmethod(meny.MenyClient._find_box)

    def __init__(self):
        self.scripts = []

    def _eval(self, script):
        self.scripts.append(script)
        return {"ready": True} if len(self.scripts) == 1 else {"clear": True}

    def _invoke(self, *arguments, **_options):
        return {"x": 600, "y": 400, "width": 81, "height": 41} if arguments[:2] == ("get", "box") else {}

    def _require_time(self, _seconds):
        pass


class ScriptPlaceholderTests(unittest.TestCase):
    def test_meny_cart_control_compares_the_exact_label_for_any_product_name(self):
        for product, name in (("/varer/meieri/yoghurt/yoghurt-naturell-7038010000000", "Brokkoli"),
                              ("/varer/meieri/yoghurt/yoghurt-naturell-7038010000000", "Yoghurt Naturell 500g Tine"),
                              ("/varer/vask/x-tra-7038010000001", "X-tra Vaskemiddel"),
                              ("/varer/x/LABEL-__HIT_Y__-1", "LABEL PRODUCT XY __HIT_X__")):
            with self.subTest(name=name):
                label = f"Legg {name} i handlevognen"
                client, dispatched = RecordingMeny(), []
                meny.MenyClient._click_cart_control(client, product, label, lambda: dispatched.append(True))
                ready, hit_test = client.scripts
                for script in (ready, hit_test):
                    self.assertIn(f"location.pathname === {json.dumps(product)} ", script)
                    self.assertIn(f"=== {json.dumps(label)}", script)
                self.assertIn("document.elementFromPoint(640, 420)", hit_test)
                self.assertEqual(dispatched, [True])

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
