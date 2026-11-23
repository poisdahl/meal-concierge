"""Read ordinary product evidence from MENY's store-scoped catalog.

The batch lookup follows Yngve Molnes's agenter-paa-menyen API research:
https://github.com/YngveMolnes/agenter-paa-menyen/tree/0b2cb53c1c051f5bad5359b3229b218887bb450e

MIT License

Copyright (c) 2026 Yngve Molnes

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from collections.abc import Mapping
from decimal import Decimal, DecimalException
import json
import re
import unicodedata
from urllib.parse import urlencode

from product_observations import _meny_ore
from recipe_import_sources import _get_bytes


CATALOG_HOST = "platform-rest-prod.ngdata.no"
MAX_BATCH = 50
_MAX_RESPONSE_BYTES = 1_000_000
_BARCODE = re.compile(r"(?:[0-9]{8}|[0-9]{13}|[0-9]{14})")


class MenyCatalogUnavailable(Exception):
    """The optional catalog read did not return a usable result."""


def is_store_gln(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9]{13}", value) is not None
        and value != "0000000000000"
    )


def regular_card_barcode(card):
    """Identify a fixed-price search card worth checking against the catalog."""
    if not isinstance(card, Mapping):
        return None
    path = card.get("product_id")
    if (
        not isinstance(path, str)
        or not path.startswith("/varer/")
        or card.get("available") is not True
        or _meny_ore(card.get("price")) is None
        or any(card.get(key) is not None for key in ("original_price", "campaign_tag", "campaign", "deposit"))
    ):
        return None
    barcode = path.rsplit("-", 1)[-1]
    return barcode if _BARCODE.fullmatch(barcode) else None


def read_catalog(store_gln, barcodes, *, timeout):
    """Return only exact, unique hits from one bounded anonymous request."""
    if (
        not is_store_gln(store_gln)
        or not 1 <= len(barcodes) <= MAX_BATCH
        or any(not isinstance(code, str) or not _BARCODE.fullmatch(code) for code in barcodes)
        or len(set(barcodes)) != len(barcodes)
    ):
        raise MenyCatalogUnavailable("invalid catalog lookup scope")
    query = urlencode({
        "product_ids": ",".join(barcodes),
        "page": 1,
        "page_size": len(barcodes),
        "fieldset": "maximal",
    })
    try:
        body, content_type = _get_bytes(
            f"https://{CATALOG_HOST}/api/products/1300/{store_gln}?{query}",
            maximum=_MAX_RESPONSE_BYTES, timeout=timeout, accept="application/json",
        )
        if content_type != "application/json":
            raise MenyCatalogUnavailable("catalog response was not JSON")
        value = json.loads(body, parse_float=Decimal, object_pairs_hook=_unique_object)
    except (ValueError, DecimalException, RecursionError) as exc:
        raise MenyCatalogUnavailable("catalog request failed") from exc

    hits = value.get("hits") if isinstance(value, dict) else None
    total = value.get("total") if isinstance(value, dict) else None
    if not isinstance(hits, list) or type(total) is not int or total != len(hits) or total > len(barcodes):
        raise MenyCatalogUnavailable("catalog response scope changed")
    requested = set(barcodes)
    rows = {}
    for hit in hits:
        barcode = hit.get("ean") if isinstance(hit, dict) else None
        if not isinstance(barcode, str) or barcode not in requested or barcode in rows:
            raise MenyCatalogUnavailable("catalog returned an unexpected or duplicate product")
        rows[barcode] = hit
    return rows


def _unique_object(fields):
    value = dict(fields)
    if len(value) != len(fields):
        raise ValueError("duplicate catalog field")
    return value


def _display_text(value):
    if not isinstance(value, str):
        return None
    return " ".join(unicodedata.normalize("NFC", value).split())


def _catalog_ore(value):
    if not (type(value) is int or isinstance(value, Decimal) and value.is_finite()):
        return None
    if not 0 <= value <= 1_000_000:
        return None
    cents = Decimal(value).quantize(Decimal("0.01"))
    return int(cents * 100) if cents == value else None


def regular_price_details(card, row):
    """Corroborate an authenticated regular card without inferring offers or stock."""
    barcode = regular_card_barcode(card)
    if barcode is None or not isinstance(row, Mapping):
        return None
    slug = row.get("slugifiedUrl")
    price_ore = _meny_ore(card["price"])
    inventory = row.get("availableInventory")
    if (
        row.get("ean") != barcode
        or not isinstance(slug, str)
        or "/varer" + slug != card["product_id"]
        or not _display_text(row.get("title"))
        or _display_text(row.get("title")) != _display_text(card.get("name"))
        or _display_text(row.get("subtitle")) != _display_text(card.get("package"))
        or row.get("isForSale") is not True
        or row.get("isOutOfStock") is not False
        or row.get("isRevoked") is not False
        or row.get("isOffer") is not False
        or row.get("usesPromotionPrice") is not False
        or row.get("usesAvtalepris", False) is not False
        or row.get("mustOrderFromSupplier") is not False
        or row.get("promotions") != []
        or "coupon" not in row
        or row["coupon"] is not None
        or row.get("unit") != "stk"
        or type(row.get("unitRule")) is not int
        or row["unitRule"] != 1
        or "unitWeight" not in row
        or row["unitWeight"] is not None
        or row.get("productSoldByWeight") is not False
        or row.get("productByWeightSoldAsItem") is not False
        or "availableInventory" not in row
        or inventory is not None and (type(inventory) is not int or inventory <= 0)
        or _catalog_ore(row.get("recycleValue")) != 0
        or _catalog_ore(row.get("pricePerUnit")) != price_ore
        or _catalog_ore(row.get("pricePerUnitOriginal")) != price_ore
    ):
        return None
    kroner, ore = divmod(price_ore, 100)
    return {"detail_price": f"{kroner},{ore:02d} kroner.", "deposit_status": "none"}
