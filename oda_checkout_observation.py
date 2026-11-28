"""Read-only Oda checkout observations shared by native browser hosts."""
from collections.abc import Mapping
from datetime import datetime, timedelta
import re
import unicodedata
from zoneinfo import ZoneInfo

from core import HouseholdError, validate_delivery_slot
from oda_browser import (ODA_CHECKOUT_AMOUNT_KEYS, ODA_CHECKOUT_AMOUNT_LABELS, _oda_checkout_amounts_minor,
                         checkout_delivery_matches, oda_checkout_amount_minor)


def text(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise HouseholdError("Native browser text is unavailable")
    return " ".join(unicodedata.normalize("NFC", value).split())


def native_amount_minor(label, value):
    # Native AX joins adjacent number/currency nodes on the observed page.
    value = re.sub(r"(?<=\d)(kr|NOK)$", r" \1", text(value), flags=re.IGNORECASE)
    return oda_checkout_amount_minor(label, value)


def checkout_amounts(rows, product_count, expected_total, *, require_discounted_subtotal=True):
    """Validate the complete native summary from its observed label/value rows.

    The host supplies each visible row. Some same-order retry pages omit the
    intermediate discounted subtotal, which is computed from the visible rows.
    """
    if (type(product_count) is not int or not 0 < product_count <= 1_000_000
            or type(expected_total) is not int or expected_total < 0
            or not isinstance(rows, list) or not 3 <= len(rows) <= 9):
        raise HouseholdError("Oda checkout amount section is unavailable")
    product_label = "1 vare" if product_count == 1 else f"{product_count} varer"
    labels = {product_label: "product_subtotal",
              **{label: key for key, label in ODA_CHECKOUT_AMOUNT_LABELS.items()}}
    observed = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"label", "value"}:
            raise HouseholdError("Oda checkout amount row changed")
        label = "" if row["label"] is None else text(row["label"])
        value = text(row["value"])
        if not label:
            # One observed zero with no label has no financial effect. Preserve
            # it in the raw surface without inventing a delivery or fee meaning.
            if "unlabeled_zero" in observed or native_amount_minor("Total inkl. MVA", value) != 0:
                raise HouseholdError("Oda unlabeled checkout amount is nonzero or ambiguous")
            observed["unlabeled_zero"] = 0
            continue
        key = labels.get(label)
        if key is None or key in observed:
            raise HouseholdError("Oda checkout amount rows are unknown or ambiguous")
        observed[key] = native_amount_minor(label, value)
    required = {"product_subtotal", "provider_total"}
    if require_discounted_subtotal:
        required.add("discounted_subtotal")
    if not required <= observed.keys():
        raise HouseholdError("Oda checkout required amount row is missing")
    subtotal = observed["product_subtotal"] + observed.get("discounts", 0)
    observed.setdefault("discounted_subtotal", subtotal)
    fee_keys = ("other_fee", "weight_reservation")
    payable = (subtotal + observed.get("delivery_price", 0)
               + observed.get("bags", 0) + observed.get("deposits", 0)
               + sum(observed.get(key, 0) for key in fee_keys))
    if (observed["discounted_subtotal"] != subtotal
            or observed["provider_total"] != payable
            or payable != expected_total):
        raise HouseholdError("Oda checkout amount arithmetic or cart total changed")
    amounts = {key: observed[key] / 100 if key in observed else None
               for key in ODA_CHECKOUT_AMOUNT_KEYS}
    amounts["other_fees"] = {ODA_CHECKOUT_AMOUNT_LABELS[key]: observed[key] / 100
                             for key in fee_keys if key in observed} or None
    _oda_checkout_amounts_minor(amounts)
    return amounts


def receipt_delivery_matches(slot, display, observed_at: datetime) -> bool:
    """Compare the dated slot with the receipt displayed at an aware instant."""
    slot = validate_delivery_slot(slot)
    oslo = ZoneInfo("Europe/Oslo")
    start, end = (datetime.fromisoformat(slot[key].replace("Z", "+00:00")).astimezone(oslo)
                  for key in ("start_at", "end_at"))
    if start.date() != end.date() or start.second or end.second or start.microsecond or end.microsecond:
        return False
    local_date = observed_at.astimezone(oslo).date()
    display = text(display)
    years = re.findall(r"\b[12][0-9]{3}\b", display)
    if years and years != [str(start.year)]:
        return False
    relative = re.findall(r"\bi (dag|morgen)\b", display.casefold())
    if relative:
        if len(relative) != 1 or local_date + timedelta(days=relative[0] == "morgen") != start.date():
            return False
    months = ("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des")
    expected = f"{start:%H:%M}-{end:%H:%M}, {start.day}. {months[start.month - 1]}"
    return checkout_delivery_matches(expected, [display], today=local_date)
