"""Bounded, anonymous MENY catalog reads on a foreground cloud runtime."""
from decimal import Decimal, DecimalException
import json
from queue import Empty, SimpleQueue
import re
from threading import Thread
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from core import HouseholdError
from meny_catalog import CATALOG_HOST, _catalog_ore, _display_text, _unique_object
from retailer_recipes import (MENY_ORIGIN, MENY_RECIPE_SEARCH, meny_recipe_path,
                              read_meny_recipe_detail, read_meny_recipe_search)


PRODUCT_SEARCH = f"https://{CATALOG_HOST}/api/products/1300/0"
SEARCH_ENDPOINTS = {PRODUCT_SEARCH, MENY_RECIPE_SEARCH}
ACTIONS = ("products", "recipes", "recipe_get")
TIMEOUT = 10.0
MAX_BYTES = 1_000_000
_EAN = re.compile(r"(?:[0-9]{8}|[0-9]{13}|[0-9]{14})")
_PRODUCT_PATH = re.compile(r"(?:/[A-Za-z0-9_~-]+){1,12}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, *, maximum, timeout, accept):
    """Use the host's ordinary verified HTTPS transport for fixed public routes only."""
    parsed = urlsplit(url)
    endpoint = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if accept == "text/html" and not parsed.query and endpoint == MENY_ORIGIN + parsed.path:
        if meny_recipe_path(parsed.path) != parsed.path:
            raise ValueError("unexpected MENY public recipe path")
    elif accept != "application/json" or endpoint not in SEARCH_ENDPOINTS or not parsed.query:
        raise ValueError("unexpected MENY public catalog route")
    if parsed.fragment or not 0 < maximum <= MAX_BYTES or not 0 < timeout <= TIMEOUT:
        raise ValueError("unexpected MENY public request bounds")

    request = Request(url, headers={"Accept": accept, "Accept-Encoding": "identity"}, method="GET")
    transfer = SimpleQueue()

    def download():
        try:
            transfer.put(read_response(request, maximum=maximum, timeout=timeout, accept=accept))
        except Exception as exc:
            transfer.put(exc)

    # urllib's timeout applies per I/O. Bound the foreground command even
    # when the host's DNS, headers or response body never finish.
    Thread(target=download, daemon=True).start()
    try:
        ending = transfer.get(timeout=timeout)
    except Empty as exc:
        raise ValueError("MENY public response deadline passed") from exc
    if isinstance(ending, Exception):
        raise ValueError("could not read MENY public HTTPS response") from ending
    return ending


def read_response(request, *, maximum, timeout, accept):
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        lengths = response.headers.get_all("Content-Length", [])
        encodings = [value.strip().casefold() for value in response.headers.get_all("Content-Encoding", ["identity"])]
        if (response.status != 200 or response.url != request.full_url or content_type != accept
                or encodings != ["identity"]):
            raise ValueError("unexpected MENY public response")
        if len(lengths) > 1 or (lengths and (not re.fullmatch(r"[0-9]{1,10}", lengths[0])
                                             or not 0 < int(lengths[0]) <= maximum)):
            raise ValueError("unexpected MENY public response length")
        body = response.read(maximum + 1)
        if not body or len(body) > maximum or (lengths and len(body) != int(lengths[0])):
            raise ValueError("incomplete or oversized MENY public response")
    return bytes(body), content_type


def validate(request):
    action = request.get("action")
    common = {"operation", "action", "provider", "response_view"}
    if request.get("provider") != "meny" or request.get("response_view") != "agent" or action not in ACTIONS:
        raise ValueError("select a discovered public MENY catalog action")
    if action == "recipe_get":
        if set(request) != common | {"recipe_id"}:
            raise ValueError("open an exact MENY recipe path from the search")
        return meny_recipe_path(request["recipe_id"])
    query, limit = request.get("query"), request.get("limit", 5)
    if (set(request) - common - {"query", "limit"} or not isinstance(query, str)
            or not query.strip() or not query.isprintable() or len(query) > 200
            or type(limit) is not int or not 1 <= limit <= 20):
        raise ValueError("MENY public search requires a text query and a limit of 1 to 20")
    return query.strip(), limit


def product(row):
    if not isinstance(row, dict):
        raise HouseholdError("MENY public product row changed")
    ean, slug, name = row.get("ean"), row.get("slugifiedUrl"), _display_text(row.get("title"))
    if (not isinstance(ean, str) or not _EAN.fullmatch(ean)
            or not isinstance(slug, str) or len(slug) > 400 or not _PRODUCT_PATH.fullmatch(slug)
            or not slug.endswith("-" + ean)
            or not name or not name.isprintable() or len(name) > 300):
        raise HouseholdError("MENY public product identity changed")
    # MENY's catalog omits the /varer prefix on the shop path.
    path = "/varer" + slug
    result = {"product_id": path, "url": MENY_ORIGIN + path, "ean": ean, "name": name}
    subtitle = _display_text(row.get("subtitle"))
    if subtitle and len(subtitle) <= 300 and subtitle.isprintable():
        result["package_label"] = subtitle
    cents = _catalog_ore(row.get("pricePerUnit"))
    if cents is not None:
        result["public_price_per_unit_ore"] = cents
    return result


def products(query, limit):
    params = urlencode({"search": query, "page": 1, "page_size": limit, "fieldset": "maximal"})
    try:
        raw, _ = fetch(f"{PRODUCT_SEARCH}?{params}", maximum=MAX_BYTES, timeout=TIMEOUT, accept="application/json")
        document = json.loads(raw, parse_float=Decimal, parse_constant=Decimal, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError, DecimalException) as exc:
        raise HouseholdError("MENY public product search is unavailable") from exc
    hits = document.get("hits") if isinstance(document, dict) else None
    total = document.get("total") if isinstance(document, dict) else None
    if (not isinstance(hits, list) or type(total) is not int or total < 0 or len(hits) != min(total, limit)
            or document.get("searchType") != "normal"):
        raise HouseholdError("MENY public product search scope changed")
    rows = [product(row) for row in hits]
    if len({row["ean"] for row in rows}) != len(rows):
        raise HouseholdError("MENY public product search returned duplicate products")
    return rows, total


def read(request):
    arguments = validate(request)
    result = {"provider": "meny", "scope": "public_catalog"}
    if request["action"] == "recipe_get":
        return {**result, "recipe": read_meny_recipe_detail(arguments, timeout=TIMEOUT, fetch=fetch)}
    query, limit = arguments
    if request["action"] == "recipes":
        return {**result, "query": query, "recipes": read_meny_recipe_search(query, limit, timeout=TIMEOUT, fetch=fetch)}
    rows, total = products(query, limit)
    return {**result, "query": query, "products": rows, "total_matches": total}
