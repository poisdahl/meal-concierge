"""Normalize recipe discovery from the selected grocery provider."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from core import DEFAULT_RECIPE_SOURCES, HouseholdError, RECIPE_SOURCE_IDS
from recipes import RecipeError, normalize_recipe, normalize_source_url


SOURCE_IDS = RECIPE_SOURCE_IDS
DEFAULT_SOURCES = DEFAULT_RECIPE_SOURCES
MAX_PROVIDER_RESULTS = 20


class RecipeSourceError(HouseholdError):
    pass


def _text(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.replace("\u0000", "").split())
    return cleaned[:maximum]


def provider_recipe_candidates(provider: str, value: Any, limit: int) -> list[dict[str, Any]]:
    if provider not in {"oda", "meny", "mathem"} or not isinstance(value, Mapping):
        raise RecipeSourceError("provider recipe response is invalid")
    rows = value.get("recipes")
    if not isinstance(rows, list) or len(rows) > MAX_PROVIDER_RESULTS:
        raise RecipeSourceError("provider recipe response is invalid")
    allowed_hosts = {"oda": {"oda.com", "www.oda.com"}, "meny": {"meny.no", "www.meny.no"}, "mathem": {"mathem.se", "www.mathem.se"}}[provider]
    results = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = _text(row.get("name") or row.get("title"), maximum=300)
        raw_id = row.get("recipe_id") or row.get("id")
        if type(raw_id) is int and 0 < raw_id < 10 ** 300:
            raw_id = str(raw_id)
        external_id = _text(raw_id, maximum=300)
        url_value = row.get("recipe_url") or row.get("url")
        try:
            url = normalize_source_url(url_value)
        except RecipeError:
            continue
        if not name or not external_id or urlsplit(url or "").hostname not in allowed_hosts:
            continue
        results.append(normalize_recipe({
            "schema_version": 2, "source_provider": provider,
            "name": name, "language": "sv-SE" if provider == "mathem" else "nb-NO", "tags": [provider.upper()],
            "source": {
                "kind": provider, "publisher": provider.upper(), "title": name,
                "author": None, "url": url, "external_id": external_id, "relationship": "original",
            },
            "rights": {
                "storage": "link_only", "license": None, "license_url": None,
                "credit": f"Original recipe at {provider.upper()}; open the source link for its terms.",
            },
        }))
        if len(results) == limit:
            break
    return results


def validate_source_settings(value: Any) -> dict[str, bool]:
    if isinstance(value, Mapping) and set(value) == set(SOURCE_IDS) - {"mathem"}:
        value = {**value, "mathem": False}
    if not isinstance(value, Mapping) or set(value) != set(SOURCE_IDS):
        raise RecipeSourceError("recipe sources must name exactly internal, oda, meny and mathem")
    if any(not isinstance(value[source], bool) for source in SOURCE_IDS):
        raise RecipeSourceError("recipe source settings must be true or false")
    return {source: value[source] for source in SOURCE_IDS}
