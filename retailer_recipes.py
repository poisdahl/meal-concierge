"""Observed retailer recipe reads; no account, product or cart authority."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import unquote

from core import HouseholdError


MENY_ORIGIN = "https://meny.no"
MAX_DETAIL_BYTES = 256 * 1024
MENY_RECIPE_CAPABILITIES = {
    "detail": "website_jsonld",
    "native_portions": "unsupported",
    "product_associations": "unsupported",
    "native_cart": "unsupported",
}


def meny_recipe_path(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise HouseholdError("MENY recipe_id must be an exact recipe search path")
    decoded = unquote(value)
    if (
        re.fullmatch(r"/oppskrifter/[A-Za-z0-9._~%/-]+", value) is None
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
        or "//" in decoded or "\\" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
        or any(ord(character) < 32 for character in decoded)
        or any(character in decoded for character in "?#%")
    ):
        raise HouseholdError("MENY recipe_id must be an exact recipe search path")
    return value


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise HouseholdError(f"MENY recipe {field} changed")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise HouseholdError(f"MENY recipe {field} changed") from exc
    return value


def meny_recipe_jsonld(observation: Any, recipe_id: str) -> dict[str, Any]:
    """Validate one exact page observation before treating its data as source facts."""
    url = MENY_ORIGIN + meny_recipe_path(recipe_id)
    if (
        not isinstance(observation, Mapping)
        or observation.get("ready") is not True
        or observation.get("url") != url
        or observation.get("canonical_urls") != [url]
    ):
        raise HouseholdError("MENY recipe page identity changed")
    scripts = observation.get("scripts")
    if not isinstance(scripts, list) or not 1 <= len(scripts) <= 8:
        raise HouseholdError("MENY recipe structured data changed")
    if any(not isinstance(script, str) for script in scripts):
        raise HouseholdError("MENY recipe structured data changed")
    try:
        if sum(len(script.encode("utf-8")) for script in scripts) > MAX_DETAIL_BYTES:
            raise ValueError("oversized JSON-LD")
        documents = [json.loads(script, object_pairs_hook=_object, parse_constant=_invalid_constant) for script in scripts]
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HouseholdError("MENY recipe structured data changed") from exc
    pending = list(documents)
    recipes = []
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("@type") == "Recipe" or isinstance(item.get("@type"), list) and "Recipe" in item["@type"]:
                recipes.append(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    if len(recipes) != 1 or not any(recipes[0] is item for item in documents) or recipes[0].get("@type") != "Recipe":
        raise HouseholdError("MENY recipe structured data is missing or ambiguous")
    recipe = recipes[0]
    if recipe.get("@context") != "https://schema.org/" or recipe.get("url") != url:
        raise HouseholdError("MENY recipe structured identity changed")
    _text(recipe.get("name"), "name", 300)
    _text(recipe.get("recipeYield"), "yield", 500)
    _text(recipe.get("inLanguage"), "language", 20)
    for field, limit, text_limit in (("recipeIngredient", 200, 500), ("recipeInstructions", 100, 4000)):
        values = recipe.get(field)
        if not isinstance(values, list) or not 1 <= len(values) <= limit:
            raise HouseholdError(f"MENY recipe {field} changed")
        for value in values:
            _text(value, field, text_limit)
    for field, limit in (("description", 4000), ("dateModified", 100)):
        if recipe.get(field) is not None:
            _text(recipe[field], field, limit)
    author = recipe.get("author")
    if author is not None:
        if not isinstance(author, dict) or author.get("@type") not in ("Organization", "Person"):
            raise HouseholdError("MENY recipe author changed")
        _text(author.get("name"), "author", 300)
    return recipe


def meny_recipe_input(observation: Any, recipe_id: str, *, fetched_at: str | None = None) -> dict[str, Any]:
    """Map observed base quantities into the shared culinary input contract.

    The trusted Application boundary activates full private retailer decoding.
    No downloaded field supplies binding, estimates acceptance or dietary facts.
    """
    source = meny_recipe_jsonld(observation, recipe_id)
    from recipes import source_ingredient, source_yield
    from recipe_quantities import quantity_json, read_quantity
    original_yield = source["recipeYield"]
    yield_value, _ = source_yield(original_yield)
    portions = None
    match = re.fullmatch(r"Antall personer: ([1-9][0-9]{0,2})", original_yield)
    if match:
        portions = int(match[1])
        yield_value.update({"quantity": quantity_json(read_quantity(portions)), "unit": "personer"})
    evidence = {"basis": "source" if portions is not None else "unknown", "input": original_yield}
    encoded = json.dumps(source, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 2,
        "name": source["name"],
        "language": source["inLanguage"],
        "portions": portions,
        "portions_evidence": evidence,
        "yield": yield_value,
        "ingredients": [source_ingredient(text) for text in source["recipeIngredient"]],
        "steps": source["recipeInstructions"],
        "notes": source.get("description"),
        "source_provider": "meny",
        "source": {
            "kind": "meny", "publisher": "MENY", "title": source["name"],
            "author": (source.get("author") or {}).get("name"),
            "url": MENY_ORIGIN + recipe_id, "external_id": recipe_id, "relationship": "original",
        },
        "rights": {"storage": "full", "license": None, "license_url": None,
                   "credit": "Original recipe from MENY; retained for private household use."},
        "external_snapshot": {
            "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
            "content_hash": hashlib.sha256(encoded).hexdigest(),
            "source_revision_id": source.get("dateModified"), "permanent_url": None,
            "changes": "Base recipe text normalized from MENY page JSON-LD; no native scaling or product associations.",
        },
    }


def retail_web_recipe_input(source_recipe: dict[str, Any], provider: str, *, deadline: float | None = None) -> dict[str, Any]:
    """Bind one observed public Oda/Mathem recipe page to its exact search hit."""
    from copy import deepcopy
    from html import unescape
    from urllib.parse import urlsplit
    from recipes import source_ingredient
    from recipe_import_sources import fetch_public_webpage, TIMEOUT
    import time
    from recipe_quantities import quantity_json, read_quantity

    source = source_recipe["source"]
    url = source.get("url")
    parsed = urlsplit(url or "")
    hosts = {"oda": {"oda.com", "www.oda.com"}, "mathem": {"mathem.se", "www.mathem.se"}}
    locale = {"oda": "no", "mathem": "se"}.get(provider)
    match = re.fullmatch(r"/" + str(locale) + r"/recipes/([1-9][0-9]*)-[A-Za-z0-9._~-]+/", parsed.path)
    if (provider not in hosts or parsed.scheme != "https" or parsed.hostname not in hosts[provider]
            or parsed.netloc != parsed.hostname or parsed.query or parsed.fragment or not match
            or match[1] != str(source.get("external_id"))):
        raise HouseholdError("retailer recipe URL must match its exact provider and numeric search identity")
    if deadline is not None and time.monotonic() + TIMEOUT > deadline:
        raise HouseholdError("retailer recipe detail search time budget is exhausted")
    page = fetch_public_webpage(url)
    rows = page.get("recipes") if isinstance(page, dict) else None
    if not isinstance(page, dict) or page.get("requires_interpretation") or not isinstance(rows, list) or len(rows) != 1:
        raise HouseholdError("retailer recipe page must contain one complete structured recipe")
    candidate = deepcopy(rows[0].get("candidate"))
    if not isinstance(candidate, dict) or candidate.get("source", {}).get("url") != url:
        raise HouseholdError("retailer recipe page identity changed")
    candidate["name"] = unescape(candidate["name"])
    if candidate["name"] != unescape(source_recipe["name"]):
        raise HouseholdError("retailer recipe title changed; obtain a fresh search reference")
    # These exact /no/ and /se/ pages expose numeric recipeYield as portions,
    # matching the displayed Porsjoner/Portioner selector. Other yield shapes
    # stay unresolved rather than being guessed as servings.
    yield_text = candidate.get("yield", {}).get("original_text")
    if not isinstance(yield_text, str) or not re.fullmatch(r"[1-9][0-9]{0,2}", yield_text):
        raise HouseholdError("retailer recipe portions are not in the verified page format")
    evidence = {"basis": "source", "input": yield_text}
    candidate["portions"] = int(yield_text)
    candidate["portions_evidence"] = deepcopy(evidence)
    candidate["yield"] = {"original_text": yield_text, "quantity": quantity_json(read_quantity(yield_text)),
        "unit": "portioner" if provider == "mathem" else "porsjoner",
        "evidence": {"quantity": deepcopy(evidence), "unit": deepcopy(evidence)}}
    if provider == "mathem":
        # Verified Swedish metric measures. Preserve wording and keep e.g.
        # cloves/handfuls unresolved; a clove is not a whole garlic product.
        aliases = {"st": "stk", "tsk": "ts", "msk": "ss", "krm": "ml"}
        for index, ingredient in enumerate(candidate["ingredients"]):
            raw = ingredient.get("original_text", "")
            found = re.fullmatch(r"(.+?)\s+(st|tsk|msk|krm)\s+(.+)", raw)
            if found:
                candidate["ingredients"][index] = source_ingredient(raw, item=found[3], measure=found[1] + " " + aliases[found[2]])
    candidate["source"].update(kind=provider, publisher=provider.upper(),
        title=candidate["name"], external_id=source["external_id"], relationship="original")
    candidate["source_provider"] = provider
    candidate["rights"]["credit"] = f"Original recipe from {candidate['source']['publisher']}; retained for private household use."
    candidate["external_snapshot"] = {"fetched_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": hashlib.sha256(json.dumps(candidate, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "source_revision_id": None, "permanent_url": None,
        "changes": "Public structured recipe page; verified base portions and metric source measures, without native cart expansion."}
    return candidate
