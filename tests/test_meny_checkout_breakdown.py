import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError
from meny import CHECKOUT_PRICE_ROWS_JS, MenyClient, normalize_checkout_payment_snapshot


PAYMENT = {
    "ready": True, "authenticated": True, "vipps_checked": True, "home_delivery": True,
    "submit_enabled": True, "submit_controls": 1, "total": 1010.0,
    "delivery": "torsdag 3. september kl. 10:00-12:00",
}
ROWS = [
    ["product_subtotal", 85000], ["deposits", 2000], ["delivery_price", 6900],
    ["picking_fee", 4900], ["weight_substitution_reserve", 3800],
    ["packaging_estimate", 1000], ["discounts", -1900], ["discounts", -700],
    ["reserved_total", 101000],
]


class MenyCheckoutBreakdownTests(unittest.TestCase):
    def test_verified_components_and_included_discount(self):
        for included in (False, True):
            with self.subTest(included=included):
                total_ore = 103600 if included else 101000
                result = normalize_checkout_payment_snapshot({
                    **PAYMENT, "total": total_ore / 100,
                    "price_rows": [False, [*ROWS[:-1], ["reserved_total", total_ore]]],
                })
                self.assertEqual(result["price_breakdown"], {
                    "status": "itemized", "product_subtotal_ore": 85000, "deposits_ore": 2000,
                    "delivery_price_ore": 6900, "picking_fee_ore": 4900,
                    "weight_substitution_reserve_ore": 3800, "packaging_estimate_ore": 1000,
                    "discounts_ore": -2600, "discounts_already_in_subtotal": included,
                })

    def test_missing_components_and_unexplained_arithmetic_stay_explicit(self):
        for unrecognized in (False, True):
            with self.subTest(unrecognized=unrecognized):
                rows = [row for row in ROWS if row[0] != "picking_fee"] if not unrecognized else ROWS
                result = normalize_checkout_payment_snapshot({**PAYMENT, "price_rows": [unrecognized, rows]})
                breakdown = result["price_breakdown"]
                self.assertEqual(breakdown["status"], "partially_itemized")
                self.assertEqual(breakdown["picking_fee_ore"], 4900 if unrecognized else None)
                self.assertIsNone(breakdown["discounts_already_in_subtotal"])

    def test_ambiguous_or_unbound_rows_keep_the_verified_payment_total(self):
        for rows in (
            None,
            [False, [*ROWS[:-1], ["reserved_total", 100990]]],
            [False, [*ROWS, ["delivery_price", 6900]]],
            [False, [*ROWS, ["discounts", True]]],
            [False, [*ROWS, ["discounts", 200]]],
            [False, [*ROWS, ["arbitrary", 200]]],
        ):
            with self.subTest(rows=rows):
                result = normalize_checkout_payment_snapshot({**PAYMENT, "price_rows": rows})
                self.assertEqual(result, {"total": PAYMENT["total"], "delivery": PAYMENT["delivery"]})

    @unittest.skipUnless(shutil.which("node"), "Node required to exercise the checkout DOM read")
    def test_reads_visible_vendor_rows_and_returns_only_allowlisted_values(self):
        harness = r"""
const {script, lines, summaryCount} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const norm = text => text.normalize('NFC').replace(/\s+/g, ' ').trim();
const visible = node => !node.hidden;
const row = ({type, title, price, hidden, packaging}) => ({
  hidden, classList: {contains: c => c === `ws-summary-line--${type}`},
  closest: selector => packaging && selector === '.ws-summary-line--fee-POSE' ? {} : null,
  querySelectorAll: selector => ({
    ':scope > .ws-summary-line__main > .ws-summary-line__price': [{innerText: price}],
    ':scope > .ws-summary-line__main > .ws-summary-line__header > .ws-summary-line__title': [{innerText: title}],
  })[selector] || [],
});
const summary = {querySelectorAll: selector => selector === '.ws-summary-line' ? lines.map(row) : []};
const root = {querySelectorAll: selector => selector === '.ws-price-summary.ws-checkout-summary__price-summary'
  ? Array(summaryCount).fill(summary) : []};
process.stdout.write(JSON.stringify(eval(script)(root)));
"""
        lines = [
            {"type": "calculator-total", "title": "Sum 15 varer", "price": "1\u00a0850,00"},
            {"type": "delivery-fee", "title": "Levering på døren", "price": "Gratis"},
            {"type": "picking-fee", "title": "Plukk og pakk", "price": "49,00 kr"},
            {"type": "buffer", "title": "Reservert beløp for vekt/erstatningsvarer", "price": "38,00"},
            {"type": "sum", "title": "Beløp som reserveres*", "price": "1\u202f923,90"},
            {"type": "static", "title": "Emballasje", "price": "ca. 10,00", "packaging": True},
            {"type": "discount", "title": "Synthetic campaign", "price": "− 23,10"},
            {"type": "picking-fee", "title": "Plukk og pakk", "price": "999,00", "hidden": True},
            {"type": "arbitrary", "title": "Synthetic unrelated text", "price": "811,23"},
        ]
        for count in (1, 2):
            with self.subTest(count=count):
                result = subprocess.run(
                    [shutil.which("node"), "-e", harness], input=json.dumps({
                        "script": CHECKOUT_PRICE_ROWS_JS, "lines": lines, "summaryCount": count,
                    }), text=True, capture_output=True, check=True, timeout=10,
                )
                self.assertNotIn("Synthetic", result.stdout)
                self.assertEqual(json.loads(result.stdout), [True, sorted([
                    ["product_subtotal", 185000], ["delivery_price", 0], ["picking_fee", 4900],
                    ["weight_substitution_reserve", 3800], ["reserved_total", 192390],
                    ["packaging_estimate", 1000], ["discounts", -2310],
                ])] if count == 1 else None)

    @unittest.skipUnless(shutil.which("node"), "Node required to exercise the dispatch gate")
    def test_dispatch_gate_rejects_same_total_when_the_reviewed_fees_change(self):
        client = MenyClient(instance="synthetic", binary="unused", executable="unused", profile="unused",
                            home="unused", socket_directory="unused", uid=1000, gid=1000,
                            vipps_phone_number="90000000")
        review = {"summary": {"total": 450.0, "delivery": {"display": PAYMENT["delivery"]}},
                  "payment_price_rows": [False, sorted([
                      ["product_subtotal", 35000], ["delivery_price", 6000], ["picking_fee", 4000],
                      ["reserved_total", 45000],
                  ])]}
        client._eval = mock.Mock(return_value={"ready": False})
        with self.assertRaisesRegex(HouseholdError, "payment control changed"):
            client._click_checkout_submit(review, mock.Mock())
        gate = client._eval.call_args.args[0]
        harness = r"""
const {gate, delivery, picking} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const checkoutDelivery = 'torsdag 3. september kl. 10:00-12:00';
const selector = '[data-meal-concierge-action="checkout-submit"]';
const element = (innerText = '', selectors = {}) => ({
  innerText, children: [], getBoundingClientRect: () => ({width: 10, height: 10}),
  querySelectorAll: key => selectors[key] || [],
  getAttribute: () => null, disabled: false,
});
const payment = element('Til betaling');
let marked = false;
payment.setAttribute = () => { marked = true; };
payment.removeAttribute = () => { marked = false; };
const total = element('Totalsum');
total.parentElement = element('Totalsum 450,00');
const deliveryTitle = element('Dato og tid');
deliveryTitle.parentElement = element('', {
  'button': [element('Endre dato og tid')], '*': [element(checkoutDelivery)],
});
const radio = label => ({...element(), checked: true, getAttribute: key => key === 'aria-label' ? label : null});
const line = (type, title, price) => ({
  ...element('', {
    ':scope > .ws-summary-line__main > .ws-summary-line__price': [element(price)],
    ':scope > .ws-summary-line__main > .ws-summary-line__header > .ws-summary-line__title': [element(title)],
  }),
  classList: {contains: c => c === `ws-summary-line--${type}`},
});
const prices = element('', {'.ws-summary-line': [
  line('calculator-total', 'Sum 1 vare', '350,00'),
  line('delivery-fee', 'Levering på døren', delivery),
  line('picking-fee', 'Plukk og pakk', picking),
  line('sum', 'Beløp som reserveres*', '450,00'),
]});
const root = element('', {
  'button': [payment], '*': [total], 'h2,h3': [deliveryTitle],
  'input[type="radio"], [role="radio"]': [radio('Vipps'), radio('Levert på døren')],
  '.ws-price-summary.ws-checkout-summary__price-summary': [prices],
});
global.location = new URL('https://meny.no/kassen');
global.getComputedStyle = () => ({display: 'block', visibility: 'visible'});
const document = element('', {'main': [root], 'button': [element('Brukermeny')]});
const query = document.querySelectorAll;
document.querySelectorAll = key => key === selector ? (marked ? [payment] : []) : query(key);
process.stdout.write(eval(gate));
"""
        for delivery, picking, ready in (("60,00", "40,00", True), ("70,00", "30,00", False)):
            with self.subTest(delivery=delivery, picking=picking):
                result = subprocess.run(
                    [shutil.which("node"), "-e", harness], input=json.dumps({
                        "gate": gate, "delivery": delivery, "picking": picking,
                    }), text=True, capture_output=True, check=True, timeout=10,
                )
                self.assertEqual(json.loads(result.stdout), {"ready": ready})
