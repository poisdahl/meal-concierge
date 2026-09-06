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
