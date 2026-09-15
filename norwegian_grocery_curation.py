"""Explicit publisher policy for an ordinary Norwegian grocery recipe pack.

This optional build-time policy deliberately contains no retailer integration,
catalog identifier, live-availability assumption, or household state. The
source document and its rights metadata remain the record of provenance.
"""

from copy import deepcopy
import re


POLICY = "ordinary-norwegian-grocery-v3"

# Only concrete ingredient blockers belong here. Broad cuisine, geography and
# publisher terms are intentionally absent. An unfamiliar name is not itself
# proof that an ingredient is difficult to buy. Inflections and hyphenated
# spellings are explicit so phrase boundaries cannot create accidental matches.
INGREDIENT_BLOCKERS = (
    "ackee", "achicha", "asafoetida", "banku", "bitter leaf", "breadfruit",
    "callaloo", "cassava", "chinese five spice", "cocoyam", "conch",
    "dried crayfish", "egusi", "fufu", "garri", "goat meat",
    "ground crayfish", "hare", "iru", "jerk seasoning", "kenkey", "kpomo",
    "locust bean", "manioc", "nigerian pepper soup spice", "ogbono",
    "oxtail", "palm nut", "palm nuts", "palm-nut", "palm-nuts", "palm oil",
    "palm-oil", "pandan", "pepper soup spice", "periwinkle", "pigeon peas",
    "pigeon", "plantain", "plantains", "ponmo", "rabbit", "ragi",
    "red palm oil", "red-palm oil", "scent leaf", "scotch bonnet", "snail", "snails",
    "stockfish", "taro", "toor daal", "tripe", "uda pod", "ube jam",
    "ugwu", "urad", "uziza", "venison", "waterleaf", "whole crayfish",
    "yaji", "yam", "yams",
)

ADAPT = frozenset({
    "themealdb:52802",  # Fish pie
    "themealdb:53161",  # Chicken & chorizo rice pot
})


def source_identity(recipe):
    source = recipe.get("source", {})
    kind, external_id = source.get("kind"), source.get("external_id")
    if not isinstance(kind, str) or not isinstance(external_id, str):
        raise ValueError("source identity is required for grocery curation")
    return f"{kind}:{external_id}"


class OrdinaryGroceryExcluded(Exception):
    """The reviewed recipe has a concrete grocery ingredient blocker."""


class OrdinaryGroceryPolicyError(Exception):
    """A source-bound editorial decision no longer matches its reviewed input."""


def _contains_phrase(text, phrase):
    pattern = r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)"
    return re.search(pattern, text) is not None


def excluded_reason(recipe):
    for row in recipe.get("ingredients", []):
        ingredient_text = row.get("item", "").casefold()
        for term in INGREDIENT_BLOCKERS:
            if _contains_phrase(ingredient_text, term):
                return f"ingredient blocker: {term}"
    return None


def _reviewed_estimate(input_text, assumptions, pack_version):
    return {
        "basis": "estimate",
        "input": input_text,
        "assumptions": assumptions,
        "project_review": {
            "publisher": "Meal Concierge",
            "pack_id": "wikibooks-themealdb-en",
            "pack_version": pack_version,
        },
    }


def _ingredient(recipe, item):
    matches = [row for row in recipe["ingredients"] if row["item"] == item]
    if len(matches) != 1:
        raise OrdinaryGroceryPolicyError(f"expected exactly one {item!r} ingredient")
    return matches[0]


def _reviewed_ingredient(recipe, item, amount, unit):
    from recipe_quantities import read_quantity
    row = _ingredient(recipe, item)
    try:
        matches = row.get("unit") == unit and read_quantity(row.get("quantity")) == amount
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise OrdinaryGroceryPolicyError(
            f"reviewed {item!r} quantity no longer matches {amount} {unit}"
        )
    return row


def _replace_words(recipe, old, new):
    changed = False
    for index, step in enumerate(recipe["steps"]):
        if old in step:
            recipe["steps"][index] = step.replace(old, new)
            changed = True
    if not changed:
        raise OrdinaryGroceryPolicyError(f"source method did not contain {old!r}")


def _note(recipe, text):
    recipe["notes"] = "\n".join(part for part in (recipe.get("notes"), text) if part)


def _fish_pie(recipe, pack_version):
    celeriac = _reviewed_ingredient(recipe, "Jerusalem Artichokes", 200, "g")
    celeriac.update({
        "item": "Celeriac",
        "raw": "200 g Celeriac",
        "notes": "Editorial adaptation: grate celeriac in place of the source Jerusalem artichokes.",
    })
    cheese = _reviewed_ingredient(recipe, "Gruyère", 25, "g")
    cheese.update({
        "item": "Jarlsberg",
        "raw": "25 g grated Jarlsberg",
        "notes": "Editorial adaptation: grate Jarlsberg in place of the source Gruyère.",
    })
    _replace_words(recipe, "artichokes", "celeriac")
    _replace_words(recipe, "the cheese", "the Jarlsberg")
    _note(recipe, "Editorial adaptation: celeriac and Jarlsberg replace the two source specialty ingredients; quantities and equipment are unchanged.")


def _chicken_chorizo_pot(recipe, pack_version):
    wine = _reviewed_ingredient(recipe, "White Wine", 150, "ml")
    wine.update({
        "item": "Cider vinegar",
        "quantity": {"numerator": 10, "denominator": 1},
        "unit": "ml",
        "raw": "10 ml Cider vinegar",
        "notes": "Editorial adaptation: replaces the source white wine.",
        "evidence": {
            key: _reviewed_estimate(
                "150 ml White Wine",
                "Replaced the source wine with 10 ml cider vinegar for acidity; the remaining liquid is supplied by stock.",
                pack_version,
            ) for key in ("quantity", "unit")
        },
    })
    stock = _reviewed_ingredient(recipe, "Chicken Stock", 800, "ml")
    stock.update({
        "quantity": {"numerator": 940, "denominator": 1},
        "unit": "ml",
        "raw": "940 ml Chicken Stock",
        "notes": "Editorial adaptation: increased from 800 ml so the dish keeps the source 950 ml total cooking liquid before the vinegar substitution.",
        "evidence": {
            key: _reviewed_estimate(
                "800 ml Chicken Stock plus 150 ml White Wine",
                "Increased stock to 940 ml after replacing 150 ml wine with 10 ml cider vinegar, preserving 950 ml total cooking liquid.",
                pack_version,
            ) for key in ("quantity", "unit")
        },
    })
    _replace_words(recipe, "white wine and stock", "stock and cider vinegar")
    _note(recipe, "Editorial adaptation: cider vinegar replaces wine; stock is adjusted so the cooking-liquid quantity stays coherent.")


def apply(recipe, credit, *, pack_version, reviewed_source_hash=None):
    """Return a retained/adapted recipe or raise for a concrete blocker."""
    identity = source_identity(recipe)
    recipe, credit = deepcopy(recipe), deepcopy(credit)
    classification = "keep"
    if identity in ADAPT:
        current_hash = (recipe.get("external_snapshot") or {}).get("content_hash")
        if reviewed_source_hash != current_hash or not re.fullmatch(r"[0-9a-f]{64}", current_hash or ""):
            raise OrdinaryGroceryPolicyError(
                f"{identity} adaptation is not bound to its reviewed source hash"
            )
        if identity == "themealdb:52802":
            _fish_pie(recipe, pack_version)
        else:
            _chicken_chorizo_pot(recipe, pack_version)
        classification = "adapt"
    if reason := excluded_reason(recipe):
        raise OrdinaryGroceryExcluded(reason)
    credit.setdefault("curation", {})["ordinary_grocery_selection"] = {
        "policy": POLICY,
        "classification": classification,
    }
    return recipe, credit, classification
