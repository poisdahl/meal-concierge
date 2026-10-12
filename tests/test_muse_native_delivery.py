"""Synthetic original broker/core delivery path; no live retailer qualification."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import sys
import unittest

from core import HouseholdError
from muse_browser import consume_request, digest, end_request, read_json
from muse_native_provider import MuseNativeReadProvider
from retail_mcp import oda_cart_delivery_window
import test_muse_native_cart as cart_fixture
from test_muse_native_provider import ACCOUNT, observation


def picker(day=None, held="16-21"):
    day = day or (datetime.now(ZoneInfo("Europe/Oslo")).date() + timedelta(days=1))
    weekday = ("man", "tir", "ons", "tor", "fre", "lør", "søn")[day.weekday()]
    month = ("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des")[day.month-1]
    label = f"{weekday}. {day.day}. {month}"
    start, end = held.split("-")
    return {"url": "https://oda.com/no/cart/?modal=delivery", "signed_in": True, "complete": True,
        "account": ACCOUNT, "heading": "Velg leveringstid", "dismissed": True,
        "held_delivery_text": f"Hjemlevering mellom kl {start} og {end}, {day.day}. {month}",
        "cells": [{"date_text": label, "window_text": window,
            "button": {"label": price, "enabled": True}, "unavailable_text": None, "expanded": expanded}
            for window, price, expanded in (("16-21", "99 kr", False), ("14-17", "19,00 kr", True))]}


class NativeDeliveryFactTests(unittest.TestCase):
    def parse(self, facts, instant="2026-12-31T08:00:00Z"):
        return MuseNativeReadProvider.validate_read_facts("get_delivery_slots", {}, facts,
            {"observed_at": instant}, {"issued_at": instant})

    def test_year_rollover_raw_provenance_and_selection_correlates_cart_not_expanded(self):
        day = datetime.fromisoformat("2027-01-01").date()
        facts = picker(day)
        result = self.parse(facts)
        self.assertEqual([s["selected"] for s in result["slots"]], [True, False])
        self.assertTrue(all(s["provider_slot_id"] is None for s in result["slots"]))
        self.assertTrue(all(s["start_at"].startswith("2027-01-01") for s in result["slots"]))
        raw = result["native_slot_observations"][result["slots"][0]["slot_ref"]]
        self.assertEqual(raw["raw"], facts["cells"][0])
        self.assertEqual(raw["year_basis"], "next_nonpast_date")
        facts["held_delivery_text"] = None
        self.assertFalse(any(s["selected"] for s in self.parse(facts)["slots"]))

    def test_unavailable_cell_is_diagnostic_not_selectable_unknown_price(self):
        facts = picker(datetime.fromisoformat("2027-01-01").date())
        cell = deepcopy(facts["cells"][1])
        cell.update(window_text="10-12", button=None, unavailable_text="Ikke tilgjengelig", expanded=None)
        facts["cells"].append(cell)
        result = self.parse(facts)
        self.assertEqual(len(result["slots"]), 2)
        self.assertEqual(result["unavailable_cells"], [cell])

    def test_weekday_duplicate_unknown_enabled_and_from_price_refuse(self):
        original = picker(datetime.fromisoformat("2027-01-01").date())
        for mutate in (
                lambda f: f["cells"][0].update(date_text="tor. 1. jan"),
                lambda f: f["cells"].append(deepcopy(f["cells"][0])),
                lambda f: f["cells"][0]["button"].update(enabled=None),
                lambda f: f["cells"][0]["button"].update(label="fra 99 kr")):
            facts = deepcopy(original); mutate(facts)
            with self.assertRaises(HouseholdError): self.parse(facts)

    def test_native_unknown_slot_id_does_not_relax_existing_mcp_default(self):
        raw = {"slot_id": None, "display": "Hjemlevering mellom kl 16 og 21, i morgen"}
        with self.assertRaises(HouseholdError): oda_cart_delivery_window(raw)
        actual = oda_cart_delivery_window(raw, today=datetime.fromisoformat("2026-12-31").date(),
                                         allow_unknown_slot_id=True)
        self.assertEqual(actual, {"slot_id": None, "date": "2027-01-01", "start": "16:00", "end": "21:00"})


class NativeDeliveryFlowTests(unittest.TestCase):
    transport, runner = cart_fixture.NativeCartTests.transport, cart_fixture.NativeCartTests.runner
    rpc, produce, finish, quantities = cart_fixture.NativeCartTests.rpc, cart_fixture.NativeCartTests.produce, cart_fixture.NativeCartTests.finish, cart_fixture.NativeCartTests.quantities

    def setUp(self):
        self.held = "16-21"
        self.delivery_outcome = "selected_once"
        self.delivery_records = []
        cart_fixture.NativeCartTests.setUp(self)
        self.extra_quantities = {"9998": 2, "9999": 1}

    def observation(self, tool, arguments):
        if tool == "get_delivery_slots": return picker(held=self.held)
        facts = observation(tool, arguments)
        if tool == "get_cart":
            facts["delivery_text"] = picker(held=self.held)["held_delivery_text"]
            facts["amount_rows"] = []
        return facts

    def delivery_effect(self, record):
        self.delivery_records.append(record)
        self.seen.append("delivery_select")
        self.assertEqual(record["payload"]["before_quantities"], self.quantities())
        if self.delivery_outcome == "not_dispatched": return {"dispatch": "not_dispatched"}, "completed"
        consume_request(self.broker, record["request_id"], record["task_id"])
        self.held = "14-17"
        if self.delivery_outcome == "external_drift":
            self.extra_quantities["9997"] = 1
        return ({"dispatch": "selected_once"}, "waiting_for_information"
                if self.delivery_outcome == "waiting" else "completed")

    def target(self):
        result = self.rpc({"operation": "delivery", "action": "list"})
        return next(s["slot_ref"] for s in result["slots"] if not s["selected"])

    def test_select_readback_preserves_all_goods_and_revalidation_uses_local_reference(self):
        reference = self.target(); before = self.quantities()
        result = self.rpc({"operation": "delivery", "action": "select", "slot_ref": reference})
        self.assertEqual(result["selected"]["slot_ref"], reference, result)
        self.assertIsNone(result["selected"]["provider_slot_id"])
        self.assertEqual(self.quantities(), before)
        self.assertIsNone(self.rpc({"operation": "cart", "action": "get"})["total"])
        self.assertEqual(len(self.delivery_records), 1)
        state = self.store.read()
        self.assertEqual(state["delivery_selection"]["origin"], "explicit")
        if not sys.platform.startswith("linux"):
            choice = self.app._current_delivery_choice(occurrence=None, deadline=None)
            self.assertEqual(choice["selected"]["slot_ref"], reference)
        self.rpc({"operation": "delivery", "action": "select", "slot_ref": reference})
        self.assertEqual(len(self.delivery_records), 1)
        self.assertFalse(self.action_records)

    def test_refusal_and_external_cart_drift_preserve_goods_without_repair(self):
        reference = self.target(); before = self.quantities()
        self.delivery_outcome = "not_dispatched"
        self.rpc({"operation": "delivery", "action": "select", "slot_ref": reference}, ok=False)
        self.assertEqual(self.quantities(), before)
        original = self.delivery_records[-1]
        self.assertFalse((self.broker / "consumed" / (original["request_id"] + ".json")).exists())
        self.assertEqual(next(s["slot_ref"] for s in self.rpc({"operation": "delivery", "action": "list"})["slots"]
                              if not s["selected"]), reference)
        self.delivery_outcome = "external_drift"
        self.rpc({"operation": "delivery", "action": "select", "slot_ref": reference}, ok=False)
        self.assertEqual(self.quantities(), {**before, "9997": 1})
        self.assertFalse(self.action_records)
        self.assertIsNone(self.store.read().get("delivery_selection"))

    def test_unresolved_selection_blocks_replacement_reads_until_actual_original_ending(self):
        reference = self.target(); self.delivery_outcome = "waiting"
        self.rpc({"operation": "delivery", "action": "select", "slot_ref": reference}, ok=False)
        original = self.delivery_records[0]
        records = list((self.broker / "requests").glob("*.json"))
        self.rpc({"operation": "delivery", "action": "list"}, ok=False)
        self.assertEqual(len(list((self.broker / "requests").glob("*.json"))), len(records))
        end_request(self.broker, original["request_id"], original["task_id"], {
            "request_id": original["request_id"], "request_digest": digest(original), "task_id": original["task_id"],
            "observed_at": datetime.now(timezone.utc).isoformat(), "task_state": "completed", "facts": {"dispatch": "selected_once"}})
        result = self.rpc({"operation": "delivery", "action": "list"})
        self.assertEqual(next(s["slot_ref"] for s in result["slots"] if s["selected"]), reference)
        self.assertEqual(len(self.delivery_records), 1)
        self.assertFalse(self.action_records)
