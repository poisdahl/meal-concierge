"""Deterministic, bounded whole-week menu selection."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
from itertools import permutations
import json
import math
import re
from typing import Any, Mapping
import unicodedata

from core import HouseholdError
from product_planner import normalize_available_ingredients, available_ingredient_matches
from recipe_selection import candidate_groups, merge_family_usage
from recipes import RecipeError, scale_recipe
from recipe_quantities import UNITS, normalized_unit, read_quantity


PLANNER_VERSION = "weekly-menu-v6"
MAX_CANDIDATES = 12
MAX_DAYS = 7
MAX_ALTERNATIVES = 3
MAX_EXPLORED_STATES = 250_000
MAX_HISTORY_RECORDS = 2_000
MAX_FACT_TOKEN = 80

SUPPORTED_STRICT_TARGETS = {
    "active_minutes",
    "leafy_green_days",
    "minimum_fish_portions",
    "minimum_legume_dinners",
    "minimum_wholegrain_or_potato_dinners",
    "minimum_vegetable_types",
}
SAVED_MINIMUM_TARGETS = {
    "minimum_fish_portions",
    "minimum_legume_dinners",
    "minimum_wholegrain_or_potato_dinners",
    "minimum_vegetable_types",
}
DIETARY_FACETS = {"fish", "legume", "wholegrain_or_potato", "vegetable"}
PERISHABILITY = {"fresh", "shelf_stable", "unknown"}

FISH_TERMS = {
    "ansjos", "fisk", "hyse", "kveite", "laks", "makrell", "ørret", "sardiner",
    "sei", "sild", "torsk", "tunfisk", "fiskefilet", "hysefilet", "laksefilet",
    "seifilet", "torskefilet", "torskeloin", "ørretfilet",
    "fish", "salmon", "cod", "haddock", "herring", "trout", "tuna", "sardines",
}
LEGUME_TERMS = {
    "bønne", "bønner", "erte", "erter", "kikerter", "linse", "linser", "soyabønner",
    "beans", "bean", "peas", "chickpeas", "lentils", "lentil",
}
WHOLEGRAIN_OR_POTATO_TERMS = {
    "bygg", "fullkorn", "fullkornsris", "grov pasta", "havre", "potet", "poteter",
    "quinoa", "rug",
    "potato", "potatoes", "oats", "barley", "brown rice", "wholegrain", "wholewheat",
}
VEGETABLE_TERMS = {
    "agurk", "aubergine", "blomkål", "brokkoli", "gulrot", "grønnkål", "kål",
    "løk", "rødløk", "vårløk", "paprika", "pastinakk", "purre", "selleri", "spinat", "squash", "tomat",
    "carrot", "carrots", "onion", "onions", "tomato", "tomatoes", "broccoli", "spinach", "kale", "cabbage",
}
VEGETABLE_TYPE_TERMS = {
    "agurk": {"agurk", "cucumber"},
    "aubergine": {"aubergine", "eggplant"},
    "blomkål": {"blomkål", "cauliflower"},
    "brokkoli": {"brokkoli", "broccoli"},
    "gulrot": {"gulrot", "carrot", "carrots"},
    "grønnkål": {"grønnkål", "kale"},
    "kål": {"kål", "cabbage"},
    "løk": {"løk", "rødløk", "vårløk", "onion", "onions", "red onion", "spring onion", "scallion"},
    "paprika": {"paprika", "bell pepper"},
    "pastinakk": {"pastinakk", "parsnip"},
    "purre": {"purre", "leek"},
    "selleri": {"selleri", "celery"},
    "spinat": {"spinat", "spinach"},
    "squash": {"squash", "courgette", "zucchini"},
    "tomat": {"tomat", "tomato", "tomatoes"},
}

PRIORITY_FACETS = {
    "fish": "fish", "fisk": "fish", "legumes": "legume", "belgfrukter": "legume",
    "vegetables": "vegetable", "grønnsaker": "vegetable", "whole grains": "wholegrain", "fullkorn": "wholegrain",
}
LEAFY_TERMS = {
    "spinach", "spinat", "babyspinat", "kale", "grønnkål", "mangold", "chard",
    "lettuce", "romaine", "romanosalat", "hjertesalat", "salatblader",
    "arugula", "rocket", "ruccola", "rucola", "watercress", "brønnkarse",
    "pak choi", "bok choy",
}
MIN_LEAFY_GRAMS_PER_SERVING = 25
WHOLEGRAIN_TERMS = {"wholegrain", "wholewheat", "fullkorn", "brown rice", "havre", "oats", "quinoa"}


class PlannerError(HouseholdError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _text(value: Any) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))
    ).strip().casefold()


def _bounded_token(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PlannerError(f"{field} must be text")
    result = _text(value)
    if not result or len(result) > MAX_FACT_TOKEN or any(
        0xD800 <= ord(character) <= 0xDFFF for character in result
    ):
        raise PlannerError(f"{field} must be bounded text")
    return result


def _source(value: Any, field: str, allowed: set[str]) -> str:
    if not isinstance(value, Mapping) or set(value).difference({"source", *allowed}):
        raise PlannerError(f"{field} has unknown fields")
    if value.get("source") != "explicit":
        raise PlannerError(f"{field}.source must be explicit")
    return "explicit"


def _normalize_token_list(
    value: Any, field: str, *, allowed: set[str] | None = None, maximum: int = 50
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PlannerError(f"{field} must be a bounded list")
    result = sorted({_bounded_token(item, field) for item in value})
    if allowed is not None and any(item not in allowed for item in result):
        raise PlannerError(f"{field} contains an unsupported value")
    return result


def normalize_candidate_facts(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping) or set(value).difference(
        {"active_minutes", "dietary_facets", "variety_facets", "perishability", "batch_guidance", "leafy_green"}
    ):
        raise PlannerError("candidate facts have unknown fields")
    result: dict[str, Any] = {}
    if "leafy_green" in value:
        raw = value["leafy_green"]
        _source(raw, "facts.leafy_green", {"assessment", "ingredient_indices", "basis"})
        assessment = raw.get("assessment")
        indices = raw.get("ingredient_indices")
        basis = raw.get("basis")
        if assessment not in {"substantial", "does_not_count", "unknown"}:
            raise PlannerError("facts.leafy_green.assessment is invalid")
        if (not isinstance(indices, list) or len(indices) > 20
                or any(type(index) is not int or not 0 <= index < 200 for index in indices)
                or len(set(indices)) != len(indices)
                or (assessment == "substantial" and not indices)):
            raise PlannerError("substantial leafy assessment needs exact ingredient indices")
        if not isinstance(basis, str) or not 1 <= len(basis.strip()) <= 500:
            raise PlannerError("facts.leafy_green.basis must be bounded text")
        result["leafy_green"] = {"source": "explicit", "assessment": assessment,
                                 "ingredient_indices": indices, "basis": basis.strip()}
    if "batch_guidance" in value:
        guidance = value['batch_guidance']
        if not isinstance(guidance, dict) or set(guidance) != {'basis', 'suitability', 'storage', 'reheating'} or any(not isinstance(v, str) or not 1 <= len(v.strip()) <= 1000 for v in guidance.values()) or guidance['suitability'] not in {'suitable', 'unsuitable', 'unknown'}:
            raise PlannerError('batch guidance needs explicit basis, suitability, storage and reheating text')
        result['batch_guidance'] = deepcopy(guidance)
    if "active_minutes" in value:
        raw = value["active_minutes"]
        _source(raw, "facts.active_minutes", {"value"})
        minutes = raw.get("value")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 1_440:
            raise PlannerError("facts.active_minutes.value must be an integer from zero to 1440")
        result["active_minutes"] = {"source": "explicit", "value": minutes}
    if "dietary_facets" in value:
        raw = value["dietary_facets"]
        _source(raw, "facts.dietary_facets", {"values", "complete", "vegetable_types"})
        if not isinstance(raw.get("complete"), bool):
            raise PlannerError("facts.dietary_facets.complete must be true or false")
        result["dietary_facets"] = {
            "source": "explicit",
            "values": _normalize_token_list(
                raw.get("values"), "facts.dietary_facets.values", allowed=DIETARY_FACETS
            ),
            "complete": raw["complete"],
            "vegetable_types": _vegetable_types(_normalize_token_list(
                raw.get("vegetable_types", []),
                "facts.dietary_facets.vegetable_types",
                maximum=50,
            )),
        }
    if "variety_facets" in value:
        raw = value["variety_facets"]
        _source(raw, "facts.variety_facets", {"values"})
        result["variety_facets"] = {
            "source": "explicit",
            "values": _normalize_token_list(
                raw.get("values"), "facts.variety_facets.values", maximum=20
            ),
        }
    if "perishability" in value:
        raw = value["perishability"]
        _source(raw, "facts.perishability", {"value"})
        perishability = raw.get("value")
        if perishability not in PERISHABILITY:
            raise PlannerError(
                "facts.perishability.value must be fresh, shelf_stable or unknown"
            )
        result["perishability"] = {"source": "explicit", "value": perishability}
    return result


def _ingredient_identities(recipe: Mapping[str, Any]) -> list[tuple[str, str | None, bool]]:
    result = []
    values = recipe.get("ingredients")
    if not isinstance(values, list):
        return result
    for value in values:
        if not isinstance(value, Mapping):
            continue
        item = _text(value.get("item"))
        if not item:
            continue
        unit = _text(value.get("unit")) or None
        result.append((item, unit, bool(value.get("pantry") or value.get("optional"))))
    return result


def _contains_term(identity: str, terms: set[str]) -> bool:
    words = set(re.findall(r"[^\W\d_]+", identity, flags=re.UNICODE))
    return identity in terms or bool(words.intersection(terms)) or any(
        " " in term and term in identity for term in terms
    )


def _vegetable_types(values: list[str] | tuple[str, ...]) -> list[str]:
    """Collapse spelling and fresh-label variants into semantic vegetable types."""
    result: set[str] = set()
    for raw in values:
        identity = _text(raw)
        matches = {
            canonical_type for canonical_type, terms in VEGETABLE_TYPE_TERMS.items()
            if _contains_term(identity, terms)
        }
        result.update(matches or {identity})
    return sorted(item for item in result if item)


def _derived_dietary(recipe: Mapping[str, Any]) -> dict[str, Any]:
    facets: set[str] = set()
    vegetables: set[str] = set()
    for ingredient in recipe.get("ingredients", []):
        if ingredient.get("optional"):
            continue
        identity = _text(ingredient.get("item"))
        if _contains_term(identity, FISH_TERMS):
            facets.add("fish")
        if _contains_term(identity, LEGUME_TERMS):
            facets.add("legume")
        if _contains_term(identity, WHOLEGRAIN_OR_POTATO_TERMS):
            facets.add("wholegrain_or_potato")
        if _contains_term(identity, VEGETABLE_TERMS):
            facets.add("vegetable")
            vegetables.update(_vegetable_types([identity]))
    return {
        "source": f"derived:{PLANNER_VERSION}:ingredient-facets",
        "values": sorted(facets),
        "complete": False,
        "vegetable_types": sorted(vegetables),
    }


def _derived_active_minutes(recipe: Mapping[str, Any]) -> dict[str, Any]:
    times = recipe.get("times")
    if isinstance(times, Mapping):
        value = times.get("active_minutes")
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_440:
            return {"source": "structured_recipe", "value": value}
    return {"source": "unknown", "value": None}


def _derived_variety(recipe: Mapping[str, Any]) -> dict[str, Any]:
    identities = [
        identity for identity, _unit, excluded in _ingredient_identities(recipe) if not excluded
    ]
    return {
        "source": f"derived:{PLANNER_VERSION}:first-ingredient",
        "values": identities[:1],
    }


def _effective_facts(recipe: Mapping[str, Any], supplied: Any) -> dict[str, Any]:
    facts = normalize_candidate_facts(supplied)
    return {
        # V1 has no server-owned allergen evidence source. Caller assertions
        # cannot turn an unknown hard constraint into a pass.
        "safety": {
            "source": "unknown", "allergies_or_sensitivities": {}, "avoid": {},
        },
        "active_minutes": facts.get("active_minutes", _derived_active_minutes(recipe)),
        "dietary_facets": facts.get("dietary_facets", _derived_dietary(recipe)),
        "variety_facets": facts.get("variety_facets", _derived_variety(recipe)),
        "perishability": facts.get(
            "perishability", {"source": "unknown", "value": "unknown"}
        ),
        "leafy_green": facts.get("leafy_green", {"source": "heuristic"}),
    }


def _profile_rules(profile: Mapping[str, Any], field: str) -> list[str]:
    diet = profile.get("diet")
    values = diet.get(field, []) if isinstance(diet, Mapping) else []
    if not isinstance(values, list) or len(values) > 50:
        raise PlannerError(f"profile diet.{field} must be a bounded list")
    return sorted({_bounded_token(item, f"profile diet.{field}") for item in values})


def _non_dinner_role(recipe: Mapping[str, Any]) -> str | None:
    """Conservative culinary labels, never allergen/nutritional evidence."""
    name = str(recipe.get("name") or "").casefold()
    categories = set(recipe.get("categories", []))
    if categories.intersection({"dessert", "drink", "sauce", "dressing", "condiment", "preserve"}):
        return "category_non_dinner"
    if "dinner" in categories:
        return None
    # Baking alone describes preparation; it does not establish a meal role.
    if categories - {"baking"}:
        return "category_non_dinner"
    tags = {str(tag).casefold() for tag in recipe.get("tags", [])}
    if tags.intersection({"dessert", "desserts", "drink", "drinks", "beverage", "breakfast", "side dish", "condiment"}):
        return "source_tag_non_dinner"
    # Savory compound dish names override ambiguous words such as 'cakes' and
    # 'bread sauce'. This vocabulary is culinary, never safety evidence.
    if re.search(r"\b(fish|crab|salmon|tuna|cod|shrimp|prawn|chicken|duck|beef|pork|lamb|sausage|sausages|tofu|lentil|lentils|bean|beans)\b", name):
        return None
    if re.search(r"\b(cookies?|macaroons?|pudding|mousse|sorbet|ice cream|mush|confectionery|baklava|sachertorte|gingerbread|dessert|kake|kjeks|cinnamon bun|lassi|juice|grenadine|soda|smoothie)\b", name):
        return "dessert_or_beverage"
    if re.search(r"\b(chocolate|vanilla|sponge|birthday|fruit|carrot|lemon|coffee|pound) cakes?\b", name):
        return "dessert"
    if re.search(r"\b(porridge|pizza crust|grøt|soda bread|sandwich bread|white bread|brown bread|garlic bread)\b", name) and not re.search(r"\b(soup|stew|curry|casserole|salad|suppe|gryte)\b", name):
        return "breakfast_or_component"
    # A sauce alone is a condiment; 'chicken with sauce' remains a dish.
    if re.search(r"^(?:barbecue|bbq|tomato|hot|chocolate|caramel|hollandaise|béarnaise) sauce\b", name):
        return "condiment"
    if re.search(r"\bpotatoes?\b", name) and not re.search(r"\b(soup|stew|curry|casserole|hash|salad|cakes?)\b", name):
        ingredients = " ".join(str(item.get("item") or "").casefold() for item in recipe.get("ingredients", []))
        if not re.search(r"\b(chicken|duck|beef|lamb|pork|sausage|sausages|salmon|fish|cod|tuna|beans?|lentils?|chickpeas?|tofu|egg|eggs)\b", ingredients):
            return "potato_side_dish"
    return None


SPECIAL_EQUIPMENT = {
    'pressure cooker': r'pressure[- ]cook(?:er|ing)?|instant pot|trykkoker(?:en)?|tryckkokare(?:n)?',
    'blender': r'blender(?:en)?|liquidiser|liquidizer|stavmikser|immersion blender|hand blender',
    'food processor': r'food processor|foodprosessor|matprosessor|matberedare',
    'stand mixer': r'stand mixer|kitchen ?aid|kjøkkenmaskin(?:en)?|eltemaskin',
    'hand mixer': r'hand mixer|electric (?:hand )?(?:mixer|whisk)|håndmikser|elvisp',
    'air fryer': r'air[- ]?fryer|varmluftfrityrkoker',
    'slow cooker': r'slow[- ]cooker|crock[- ]?pot',
    'rice cooker': r'rice cooker|riskoker|riskokare',
    'deep fryer': r'deep fryer|frityrkoker|fritös',
    'sous vide': r'sous[- ]vide|immersion circulator',
    'ice cream maker': r'ice[- ]cream (?:maker|machine)|ismaskin',
    'waffle iron': r'waffle (?:iron|maker)|vaffeljern|våffeljärn',
    'pasta machine': r'pasta (?:machine|maker|roller)|pastamaskin',
    'bread machine': r'bread (?:machine|maker)|brødbakemaskin',
    'grill': r'(?:barbecue|barbeque)(?! sauce)|(?:outdoor|charcoal|gas|electric|the) grill|grill outdoors|utendørsgrill|grillen',
    'microwave': r'microwave|mikrobølgeovn(?:en)?|mikrovågsugn(?:en)?',
    'smoker': r'smoker|røykeovn|røykovn',
    'juicer': r'juicer|juice extractor|juicemaskin|saftpresse',
    'dehydrator': r'dehydrator|mattørker',
}


def equipment_conflicts(profile, recipe):
    """Require known specialist equipment, with explicit ordinary alternatives allowed.

    Pot, pan, oven and basic utensils need no inventory interview. Omitted
    specialist equipment is unknown/unavailable, not a claim the owner has it.
    """
    available = {str(v).casefold() for v in profile['meals'].get('equipment', ['pot', 'pan', 'oven'])}
    known = {name for name, aliases in SPECIAL_EQUIPMENT.items()
             if any(re.fullmatch(aliases, value) or value == name for value in available)}
    steps = recipe.get('steps') or []
    parts = [str(step.get('text', '') if isinstance(step, Mapping) else step) for step in steps]
    # Titles are useful when the imported recipe has no method yet.
    if not parts:
        parts = [str(recipe.get('name') or '')]
    missing = set()
    for part in parts:
        for sentence in re.split(r'[.!?\n]', part.casefold()):
            for name, aliases in SPECIAL_EQUIPMENT.items():
                if name in known:
                    continue
                for match in re.finditer(r'\b(?:' + aliases + r')\b', sentence):
                    before, after = sentence[:match.start()], sentence[match.end():]
                    if (re.search(r'(?:without|uten|no|ingen|not need|don.t need)\s+(?:an?\s+)?$', before)
                            or re.match(r'\s*(?:\(optional\)|(?:is\s+)?(?:optional|not needed|not required|trengs ikke))', after)):
                        continue
                    # An actual method alternative is required, not an invented substitution.
                    alternatives = re.split(r'\bor\b|\beller\b|alternatively', sentence)
                    if len(alternatives) > 1 and any(
                        not re.search(aliases, alt) and (
                            re.search(r'\b(?:by hand|for hånd|simmer|stovetop|ordinary pot|covered pot|vanlig kjele|småkok|bake in (?:an? |the )?oven|stek i (?:en |vanlig )?stekovn)\b', alt)
                            or any(re.search(SPECIAL_EQUIPMENT[k], alt) for k in known))
                        for alt in alternatives):
                        continue
                    missing.add(name)
    return sorted(missing)


def _hard_evaluation(
    candidate: Mapping[str, Any], profile: Mapping[str, Any], overrides: Mapping[str, str], *,
    meal_type: str = "dinner", meal_role_advisory: bool = False,
) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    status = "pass"
    if missing := equipment_conflicts(profile, candidate['recipe']):
        status = 'fail'
        reasons.append({'code': 'equipment_unavailable', 'status': 'fail', 'detail': missing})
    if meal_type == "dinner" and (role := _non_dinner_role(candidate["recipe"])):
        if not meal_role_advisory:
            status = "fail"
        reasons.append({"code": "meal_role:non_dinner", "status": "advisory" if meal_role_advisory else "fail", "detail": role})
    error = candidate.get("materialization_error")
    if error:
        status = "fail"
        reasons.append({"code": "not_materializable", "status": "fail", "detail": str(error)[:500]})
    if candidate.get("readiness_unknown"):
        if status == "pass":
            status = "unknown"
        reasons.append({"code": "readiness_needs_input", "status": "unknown", "detail": candidate["readiness_unknown"]})
    from dietary_assessment import assess
    findings = assess(profile, candidate['recipe'], recipe=True)
    for finding in findings:
        reasons.append({'code': 'dietary_assessment', 'status': 'fail' if finding['blocked'] else 'advisory', 'detail': finding})
        if finding['blocked']:
            status = 'fail'
    usage = candidate.get("usage")
    if isinstance(usage, Mapping) and usage.get("history_coverage") == "history_work_limit":
        if status == "pass":
            status = "unknown"
        reasons.append({"code": "history_work_limit", "status": "unknown", "detail": "source-family history search was bounded before completion"})
    eligible = bool(usage.get("eligible")) if isinstance(usage, Mapping) else False
    key = str(candidate["recipe_key"])
    if eligible:
        reasons.append({"code": "cooldown", "status": "pass", "detail": "eligible"})
    elif key in overrides:
        reasons.append({
            "code": "cooldown_override", "status": "pass",
            "detail": {"recipe_key": key, "reason": overrides[key]},
        })
    elif meal_role_advisory:
        reasons.append({"code": "cooldown", "status": "advisory",
                        "detail": deepcopy(usage.get("blocked_by", [])) if isinstance(usage, Mapping) else "unknown"})
    else:
        status = "fail"
        reasons.append({
            "code": "cooldown", "status": "fail",
            "detail": deepcopy(usage.get("blocked_by", [])) if isinstance(usage, Mapping) else "unknown",
        })
    return {"status": status, "reasons": reasons}


def prepare_candidate(candidate: Mapping[str, Any], profile: Mapping[str, Any], overrides: Mapping[str, str], portions: int | None = None, available_ingredients=None, *, meal_role_advisory: bool = False) -> dict[str, Any]:
    """Evaluate only a loaded, exact Application candidate; summaries cannot pass."""
    item = deepcopy(dict(candidate))
    recipe = item["recipe"]
    if recipe.get("representation") == "summary" or not isinstance(recipe.get("ingredients"), list) or not recipe.get("ingredients") or not recipe.get("steps"):
        item["materialization_error"] = "full recipe ingredients and steps are not loaded"
        item["readiness_unknown"] = "full recipe ingredients and steps are not loaded"
    else:
        try:
            scaled = scale_recipe(recipe, portions if portions is not None else recipe.get("portions"))
            unresolved = [need["item"] for need in scaled["shopping_requirements"]
                          if not need.get("optional") and (not need.get("scalable") or need.get("quantity") is None or not need.get("unit"))]
            if unresolved or not scaled["readiness"]["scaling_ready"]:
                item["readiness_unknown"] = {"ingredients": unresolved, "fields": scaled["readiness"]["missing_decisions"]}
        except RecipeError as exc:
            item["readiness_unknown"] = str(exc)[:500]
    item["facts"] = _effective_facts(recipe, item.get("supplied_facts", item.get("facts")))
    if not item.get("materialization_error"):
        item["facts"]["listed_fish_mass"] = _listed_fish_mass(recipe)
    item["hard_constraints"] = _hard_evaluation(item, profile, overrides, meal_role_advisory=meal_role_advisory)
    item.pop("available_ingredient_matches", None)
    if available_ingredients and not item.get("materialization_error"):
        item["available_ingredient_matches"] = available_ingredient_matches(recipe, available_ingredients)
    return item


def _preference_reasons(candidate: Mapping[str, Any], day: str, profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    recipe = candidate["recipe"]
    tags = {_text(tag) for tag in recipe.get("tags", [])}
    cuisine = profile.get("cuisine") or {}
    from dietary_assessment import assess
    reasons = [_reason('dietary_preference', -30, f) for f in assess(profile, recipe, recipe=True)
               if f['condition'] in {'preference_deviation', 'sensitivity_conflict'}]
    for field in ("wanted", "flavours"):
        requested = {_text(value) for value in cuisine.get(field, [])}
        matched = sorted(requested.intersection(tags))
        if requested:
            reasons.append(_reason("cuisine:" + field, min(12, len(matched) * 4),
                                   {"matched_tags": matched, "unmatched_or_unknown": sorted(requested - tags)}))
    if candidate.get("is_favorite"):
        reasons.append(_reason("recipe:favorite", 4, "current personal favorite"))
    identities = [_text(item.get("item")) for item in recipe.get("ingredients", []) if not item.get("optional")]
    facets = set(candidate["facts"]["dietary_facets"]["values"])
    if any(_contains_term(identity, WHOLEGRAIN_TERMS) for identity in identities):
        facets.add("wholegrain")
    diet = profile.get("diet") or {}
    for preference in sorted({_text(value) for value in diet.get("prioritise", [])}):
        facet = PRIORITY_FACETS.get(preference)
        reasons.append(_reason("diet:prioritise", 4 if facet in facets else 0,
                               {"preference": preference, "evidence": "positive_ingredient_match" if facet in facets else "unknown" if facet else "unsupported"}))
    if diet.get("leafy_green_days"):
        leafy = _leafy_dinner(candidate)
        reasons.append(_reason("diet:leafy_green_dinner", 5 if leafy["counts"] is True else 0,
                               {"day": day, **leafy}))
    return reasons


def _listed_fish_mass(recipe: Mapping[str, Any]) -> dict[str, Any]:
    """Positive listed fish mass per serving, never inferred nutritional safety."""
    grams = 0
    unknown = []
    modifiers = {"fresh", "frozen", "fillet", "fillets", "filet", "fileter", "fersk", "frossen", "skinless", "boneless"}
    try:
        scaled = scale_recipe(recipe, 1)
    except RecipeError:
        return {"grams_per_serving": None, "unknown": ["serving_or_quantity_evidence"]}
    for item in scaled["ingredients"]:
        if item.get("optional"):
            continue
        identity = _text(item.get("item"))
        if not _contains_term(identity, FISH_TERMS):
            continue
        words = set(re.findall(r"[^\W\d_]+", identity, flags=re.UNICODE))
        unit = UNITS.get(normalized_unit(item.get("unit")))
        if words - modifiers - FISH_TERMS or unit is None or unit[0] != "g":
            unknown.append(identity)
            continue
        try:
            grams += read_quantity(item.get("quantity"), legacy_float=recipe.get("schema_version") == 1) * unit[1]
        except ValueError:
            unknown.append(identity)
    return {"grams_per_serving": float(grams), "unknown": unknown}


def _listed_leafy_mass(recipe: Mapping[str, Any]) -> float | None:
    """Conservative legacy hint for unassessed, plainly named leaves."""
    try:
        scaled = scale_recipe(recipe, 1)
    except RecipeError:
        return None
    grams = 0.0
    found = False
    for item in scaled["ingredients"]:
        if item.get("optional") or item.get("pantry"):
            continue
        identity = _text(item.get("item"))
        if identity not in LEAFY_TERMS:
            # A mixed product such as spinach pasta is not its full weight in
            # leaves. Unfamiliar names require a culinary assessment too.
            if _contains_term(identity, LEAFY_TERMS):
                return None
            continue
        found = True
        unit = UNITS.get(normalized_unit(item.get("unit")))
        if unit is None or unit[0] != "g":
            return None
        try:
            grams += float(read_quantity(item.get("quantity"), legacy_float=recipe.get("schema_version") == 1) * unit[1])
        except ValueError:
            return None
    return grams if found else None


def _leafy_dinner(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Keep agent culinary judgment distinct from checked recipe arithmetic."""
    recipe = candidate["recipe"]
    fact = (candidate.get("facts") or {}).get("leafy_green") or {"source": "heuristic"}
    if fact["source"] == "unavailable":
        return {"source": "unavailable", "counts": None, "detail": "saved_assessment_missing_or_stale"}
    if fact["source"] == "heuristic":
        grams = _listed_leafy_mass(recipe)
        return {"source": "legacy_heuristic", "counts": True if grams is not None and grams >= MIN_LEAFY_GRAMS_PER_SERVING else None,
                "listed_grams_per_serving": grams,
                "detail": "plain_leaf_mass_hint" if grams is not None else "unassessed_or_ambiguous_ingredient"}
    assessment = fact["assessment"]
    result = {"source": "agent_assessment", "assessment": assessment,
              "ingredient_indices": deepcopy(fact["ingredient_indices"]), "basis": fact["basis"]}
    if any(index >= len(recipe.get("ingredients", [])) for index in fact["ingredient_indices"]):
        return {**result, "counts": None, "quantity_evidence": "ingredient_index_missing"}
    if assessment != "substantial":
        return {**result, "counts": False if assessment == "does_not_count" else None,
                "quantity_evidence": "not_needed_for_nonpositive_assessment"}
    try:
        scaled = scale_recipe(recipe, 1)
    except RecipeError:
        return {**result, "counts": None, "quantity_evidence": "unavailable"}
    checked = []
    for index in fact["ingredient_indices"]:
        if index >= len(scaled["ingredients"]):
            return {**result, "counts": None, "quantity_evidence": "ingredient_index_missing"}
        item = scaled["ingredients"][index]
        if item.get("optional") or item.get("pantry") or item.get("scalable") is not True:
            return {**result, "counts": None, "quantity_evidence": "ingredient_not_required_or_scalable"}
        unit = UNITS.get(normalized_unit(item.get("unit")))
        if unit is None or unit[0] != "g":
            return {**result, "counts": None, "quantity_evidence": "mass_unit_unavailable"}
        try:
            mass = read_quantity(item.get("quantity"), legacy_float=recipe.get("schema_version") == 1) * unit[1]
        except ValueError:
            return {**result, "counts": None, "quantity_evidence": "quantity_unavailable"}
        if mass <= 0:
            return {**result, "counts": None, "quantity_evidence": "quantity_unavailable"}
        grams = float(mass)
        checked.append({"ingredient_index": index, "item": item.get("item"), "listed_grams_per_serving": grams})
    grams = sum(item["listed_grams_per_serving"] for item in checked)
    return {**result, "counts": grams >= MIN_LEAFY_GRAMS_PER_SERVING,
            "listed_grams_per_serving": grams,
            "quantity_evidence": "calculated_from_listed_recipe"}


def _leafy_week(selected: tuple[Mapping[str, Any], ...], profile: Mapping[str, Any]) -> dict[str, Any] | None:
    target = (profile.get("diet") or {}).get("leafy_green_days")
    if not target:
        return None
    assessments = []
    for candidate in selected:
        components = candidate.get("leafy_components") or [candidate]
        rows = [_leafy_dinner(component) for component in components]
        grams = sum(row.get("listed_grams_per_serving") or 0 for component, row in zip(components, rows)
                    if not component.get("partial_coverage") and
                    (row.get("assessment") == "substantial" or row.get("source") == "legacy_heuristic"))
        partial = any(component.get("partial_coverage") and row.get("counts") is True
                      for component, row in zip(components, rows))
        counts = (True if grams >= MIN_LEAFY_GRAMS_PER_SERVING else
                  None if partial else
                  None if any(row["counts"] is None for row in rows) else False)
        row = {**rows[0], "counts": counts, "listed_grams_per_serving": grams or rows[0].get("listed_grams_per_serving")}
        if len(rows) > 1:
            row["components"] = rows
        if partial:
            row["partial_side_coverage"] = True
        if candidate.get("date"):
            row["date"] = candidate["date"]
        assessments.append(row)
    counted = sum(item["counts"] is True for item in assessments)
    unknown = sum(item["counts"] is None for item in assessments)
    minimum, maximum = target
    status = ("fail" if counted > maximum or counted + unknown < minimum else
              "pass" if counted >= minimum and counted + unknown <= maximum else "unknown")
    return {"target_range": [minimum, maximum], "counted_dinners": counted,
            "unknown_dinners": unknown, "dinner_assessments": assessments,
            "listed_grams_per_serving": [item.get("listed_grams_per_serving") for item in assessments],
            "status": status}


def _positive_int(profile: Mapping[str, Any], field: str, default: int) -> int:
    diet = profile.get("diet")
    value = diet.get(field, default) if isinstance(diet, Mapping) else default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlannerError(f"profile diet.{field} must be a non-negative integer")
    return value


def _active_window(profile: Mapping[str, Any]) -> tuple[int, int, int]:
    meals = profile.get("meals")
    if not isinstance(meals, Mapping):
        raise PlannerError("profile meals are invalid")
    target = meals.get("target_active_minutes", [0, 1_440])
    maximum = meals.get("maximum_active_minutes", 1_440)
    if (
        not isinstance(target, list) or len(target) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in target)
        or not 0 <= target[0] <= target[1] <= 1_440
        or isinstance(maximum, bool) or not isinstance(maximum, int)
        or not target[1] <= maximum <= 1_440
    ):
        raise PlannerError("profile active-minute targets are invalid")
    return target[0], target[1], maximum


def _strict_evaluation(
    selected: tuple[Mapping[str, Any], ...], strict_targets: list[str], profile: Mapping[str, Any]
) -> dict[str, Any]:
    results = []
    overall = "pass"
    dietary = [candidate["facts"]["dietary_facets"] for candidate in selected]
    incomplete = any(not facts["complete"] for facts in dietary)
    for target in strict_targets:
        if target == "active_minutes":
            low, high, maximum = _active_window(profile)
            values = [candidate["facts"]["active_minutes"]["value"] for candidate in selected]
            if any(value is None for value in values):
                result = {"target": target, "status": "unknown", "detail": "active minutes are missing"}
            elif any(not low <= value <= high for value in values):
                result = {
                    "target": target, "status": "fail",
                    "detail": {"target_range": [low, high], "soft_maximum": maximum, "values": values},
                }
            else:
                result = {
                    "target": target, "status": "pass",
                    "detail": {"target_range": [low, high], "values": values},
                }
        elif target == "leafy_green_days":
            leafy = _leafy_week(selected, profile)
            result = {"target": target, "status": leafy["status"] if leafy else "pass",
                      "detail": leafy or {"target_range": [], "counted_dinners": 0}}
        elif target == "minimum_vegetable_types":
            wanted = _positive_int(profile, target, 0)
            values = sorted({item for facts in dietary for item in facts["vegetable_types"]})
            if len(values) >= wanted:
                result = {"target": target, "status": "pass", "detail": {"minimum": wanted, "observed": values}}
            elif incomplete:
                result = {"target": target, "status": "unknown", "detail": {"minimum": wanted, "observed": values}}
            else:
                result = {"target": target, "status": "fail", "detail": {"minimum": wanted, "observed": values}}
        else:
            facet = {
                "minimum_fish_portions": "fish",
                "minimum_legume_dinners": "legume",
                "minimum_wholegrain_or_potato_dinners": "wholegrain_or_potato",
            }[target]
            wanted = _positive_int(profile, target, 0)
            observed = sum(facet in facts["values"] for facts in dietary)
            if observed >= wanted:
                result = {"target": target, "status": "pass", "detail": {"minimum": wanted, "observed": observed}}
            elif wanted > len(selected):
                result = {"target": target, "status": "fail", "detail": {"minimum": wanted, "observed": observed, "maximum_possible": len(selected)}}
            elif incomplete:
                result = {"target": target, "status": "unknown", "detail": {"minimum": wanted, "observed": observed}}
            else:
                result = {"target": target, "status": "fail", "detail": {"minimum": wanted, "observed": observed}}
        results.append(result)
        if result["status"] == "fail":
            overall = "fail"
        elif result["status"] == "unknown" and overall == "pass":
            overall = "unknown"
    return {"status": overall, "results": results}


def saved_menu_minimum_evaluation(menu: Any, profile: Mapping[str, Any]) -> dict[str, Any]:
    """Report saved minima truthfully and separately identify enforced results."""
    diet = profile.get("diet") if isinstance(profile, Mapping) else None
    meals = profile.get("meals") if isinstance(profile, Mapping) else None
    targets = sorted(
        target for target in SAVED_MINIMUM_TARGETS
        if isinstance(diet, Mapping) and type(diet.get(target)) is int and diet[target] > 0
    )
    if isinstance(diet, Mapping) and diet.get("leafy_green_days"):
        targets.append("leafy_green_days")
    planner_selection = menu.get("planner_selection") if isinstance(menu, Mapping) else None
    planner_request = planner_selection.get("request") if isinstance(planner_selection, Mapping) else None
    scope = menu.get("planning_scope") if isinstance(menu, Mapping) else None
    request = scope if isinstance(scope, Mapping) else planner_request
    agent_mode = isinstance(request, Mapping) and request.get("selection_mode") == "agent"
    enforced_targets = (
        set(request.get("strict_targets", [])) & set(targets)
        if agent_mode and isinstance(request.get("strict_targets"), list) else set(targets)
    )

    def with_policy(evaluation: dict[str, Any]) -> dict[str, Any]:
        enforced = [row["status"] for row in evaluation["results"]
                    if row["target"] in enforced_targets]
        return {
            **evaluation,
            "enforced_status": "fail" if "fail" in enforced else "unknown" if "unknown" in enforced else "pass",
        }

    if not targets:
        return with_policy({"status": "pass", "complete_menu": True, "results": []})
    if not isinstance(menu, Mapping) or not isinstance(menu.get("dishes"), list):
        return with_policy({"status": "unknown", "complete_menu": False, "results": [{"target": target, "status": "unknown", "detail": "menu dishes are unavailable"} for target in targets]})
    import menu_planning as mp
    import batch_planning as bp
    recipes = {recipe.get("recipe_key"): recipe for recipe in menu["dishes"]
               if isinstance(recipe, Mapping) and isinstance(recipe.get("recipe_key"), str)}
    has_explicit_slots = isinstance(menu.get("slots"), list)
    dinner_slots = [
        slot for slot in menu.get("slots", [])
        if isinstance(slot, Mapping) and slot.get("meal_type") == "dinner"
    ] if has_explicit_slots else []
    dinner_by_date = {slot.get("date") if isinstance(slot.get("date"), str) else f"__undated_{index}": slot
                      for index, slot in enumerate(reversed(dinner_slots))}
    selected_slots = [dinner_by_date[day] for day in sorted(dinner_by_date)] if has_explicit_slots else None
    selected_recipes = ([mp.recipe_for_slot(menu, slot, allow_stale=True) if slot.get("slot_id")
                         else recipes.get(slot.get("recipe_key")) for slot in selected_slots]
                        if has_explicit_slots else list(recipes.values()))
    selected_slots = selected_slots if has_explicit_slots else [None] * len(selected_recipes)
    expected = meals.get("dinner_days") if isinstance(meals, Mapping) else None
    if type(expected) is not int or len(selected_recipes) != expected or any(recipe is None for recipe in selected_recipes):
        return with_policy({"status": "unknown", "complete_menu": False, "results": [{
            "target": target, "status": "unknown",
            "detail": {"expected_dinners": expected, "observed_dinners": len(selected_recipes)},
        } for target in targets]})
    new_assessment_slots = set()
    planned_by_occurrence = {}
    planned_by_key = {}
    for field in ("planner_selection", "replan_selection"):
        planner_selection = menu.get(field)
        selection = planner_selection.get("selection") if isinstance(planner_selection, Mapping) else None
        if not isinstance(selection, Mapping):
            continue
        for fact_slots in (selection.get("slots"), selection.get("source_slots")):
            if not isinstance(fact_slots, list):
                continue
            for selected_slot in fact_slots:
                if not isinstance(selected_slot, Mapping):
                    continue
                if planner_selection.get("planner_version") == PLANNER_VERSION:
                    new_assessment_slots.add((selected_slot.get("date"), selected_slot.get("recipe_key")))
                facets = selected_slot.get("dietary_facets")
                if not (isinstance(facets, Mapping) and isinstance(facets.get("values"), list)
                        and isinstance(facets.get("vegetable_types"), list)
                        and isinstance(facets.get("complete"), bool)):
                    continue
                key = selected_slot.get("recipe_key")
                if isinstance(key, str):
                    planned_by_key[key] = deepcopy(dict(facets))
                    if isinstance(selected_slot.get("date"), str) and isinstance(selected_slot.get("reference"), Mapping):
                        planned_by_occurrence[(selected_slot["date"], key,
                                               mp.canonical(selected_slot["reference"]))] = deepcopy(dict(facets))
    def candidate_for(slot, recipe):
        facets = None
        if isinstance(slot, Mapping):
            exact_snapshot = slot.get("snapshot_digest") == mp.recipe_snapshot_digest(recipe)
            if "dietary_facets" in slot:
                if isinstance(slot["dietary_facets"], Mapping) and exact_snapshot:
                    facets = slot["dietary_facets"]
            elif exact_snapshot and isinstance(slot.get("reference"), Mapping) and isinstance(slot.get("date"), str):
                facets = planned_by_occurrence.get((slot["date"], slot.get("recipe_key"),
                                                    mp.canonical(slot["reference"])))
            elif slot.get("reference") is None and sum(
                    dinner.get("recipe_key") == slot.get("recipe_key") for dinner in dinner_slots) == 1:
                facets = planned_by_key.get(slot.get("recipe_key"))
        elif not has_explicit_slots:
            facets = planned_by_key.get(recipe.get("recipe_key"))
        return {"recipe": recipe, "date": slot.get("date") if isinstance(slot, Mapping) else None, "facts": {
        "dietary_facets": deepcopy(facets) if facets is not None else _derived_dietary(recipe),
        "leafy_green": (
            deepcopy(slot["leafy_green"])
            if isinstance(slot, Mapping) and isinstance(slot.get("leafy_green"), Mapping)
            and slot.get("snapshot_digest") == mp.recipe_snapshot_digest(recipe)
            else {"source": "heuristic"}
            if isinstance(slot, Mapping) and "leafy_green" not in slot
            and slot.get("snapshot_digest") == mp.recipe_snapshot_digest(recipe)
            and slot.get("meal_type") == "dinner"
            and (slot.get("date"), slot.get("recipe_key")) not in new_assessment_slots
            else {"source": "unavailable"} if has_explicit_slots else {"source": "heuristic"}
        ),
    }}
    selected = []
    for slot, recipe in zip(selected_slots, selected_recipes, strict=True):
        candidate = candidate_for(slot, recipe)
        if isinstance(slot, Mapping):
            sides = [side for side in menu["slots"] if isinstance(side, Mapping)
                     and side.get("date") == slot.get("date") and side.get("meal_type") == "side"
                     and side.get("served_with") == "dinner"]
            components = [candidate]
            for side in sides:
                component = candidate_for(side, mp.recipe_for_slot(menu, side, allow_stale=True)
                    if side.get("slot_id") else recipes.get(side.get("recipe_key")))
                try:
                    component["partial_coverage"] = bp.fraction(side.get("portions")) < bp.fraction(slot.get("portions"))
                except HouseholdError:
                    component["partial_coverage"] = True
                components.append(component)
            candidate["leafy_components"] = components
        selected.append(candidate)
    selected = tuple(selected)
    return with_policy({"complete_menu": True, **_strict_evaluation(selected, targets, profile)})


def _reason(code: str, weight: int, detail: Any) -> dict[str, Any]:
    return {"code": code, "weight": int(weight), "detail": detail}


def _slot_reasons(
    candidate: Mapping[str, Any], day: str, index: int, count: int, profile: Mapping[str, Any]
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = _preference_reasons(candidate, day, profile)
    matches = candidate.get("available_ingredient_matches")
    if matches:
        reasons.append(_reason("pantry:explicit_request", min(18, sum(6 if item["use_first"] else 3 for item in matches)),
                               {"matched_ingredients": deepcopy(matches), "coverage": "not_established",
                                "basis": "user_stock_assertion_and_loaded_exact_ingredient_names"}))
    explicit_feedback = candidate.get("planning_feedback")
    if explicit_feedback is not None:
        reasons.append(_reason("feedback:explicit-v1", explicit_feedback["weight"], deepcopy(explicit_feedback)))
    active = candidate["facts"]["active_minutes"]
    low, high, maximum = _active_window(profile)
    value = active["value"]
    weekday = date.fromisoformat(day).weekday() < 5
    if value is None:
        reasons.append(_reason("active_minutes:unknown", 0, "not scored"))
    elif low <= value <= high:
        reasons.append(_reason(
            "active_minutes:weekday_target" if weekday else "active_minutes:target",
            8, {"value": value, "range": [low, high], "weekday": weekday},
        ))
    elif value <= maximum:
        reasons.append(_reason(
            "active_minutes:weekday_outside_target" if weekday else "active_minutes:weekend_capacity",
            -2 if weekday else 2,
            {"value": value, "range": [low, high], "weekday": weekday},
        ))
    else:
        reasons.append(_reason("active_minutes:above_maximum", -12, {"value": value, "maximum": maximum}))
    perishable = candidate["facts"]["perishability"]["value"]
    if perishable == "fresh":
        reasons.append(_reason("perishability:fresh_early", (count - index - 1) * 2, {"day": day}))
    elif perishable == "shelf_stable":
        reasons.append(_reason("perishability:shelf_stable_late", index, {"day": day}))
    else:
        reasons.append(_reason("perishability:unknown", 0, "not scored"))
    usage = candidate.get("usage") if isinstance(candidate.get("usage"), Mapping) else {}
    if usage.get("history_coverage", "complete") != "complete":
        reasons.append(_reason("recency:history_incomplete", 0, usage["history_coverage"]))
    elif not any(usage.get(field) for field in ("last_planned_week", "last_ordered_week", "last_cooked_week")):
        reasons.append(_reason("recency:no_recorded_use", 5, "no matching recorded use"))
    if any(usage.get(field) for field in ("last_planned_week", "last_ordered_week", "last_cooked_week")):
        weeks_since = None
        recorded_weeks = []
        for field in ("last_cooked_week", "last_ordered_week", "last_planned_week"):
            recorded_week = usage.get(field)
            if not isinstance(recorded_week, str) or re.fullmatch(r"\d{4}-W\d{2}", recorded_week) is None:
                continue
            try:
                monday = date.fromisocalendar(int(recorded_week[:4]), int(recorded_week[6:]), 1)
            except ValueError:
                continue
            recorded_weeks.append((monday, recorded_week))
        latest = max(recorded_weeks, default=None)
        last_week = latest[1] if latest is not None else None
        if latest is not None:
            last_monday = latest[0]
            weeks_since = max(0, (date.fromisoformat(day) - last_monday).days // 7)
        cooldown = usage.get("cooldown_weeks")
        weight = (
            min(5, max(0, weeks_since - cooldown))
            if isinstance(weeks_since, int) and isinstance(cooldown, int) else 0
        )
        reasons.append(_reason("recency:recorded_use", weight, {
            "last_week": last_week, "weeks_since": weeks_since, "cooldown_weeks": cooldown,
        }))
    return reasons


def _plan_reasons(
    selected: tuple[Mapping[str, Any], ...], profile: Mapping[str, Any]
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    leafy = _leafy_week(selected, profile)
    if leafy:
        distance = max(0, leafy["target_range"][0] - leafy["counted_dinners"],
                       leafy["counted_dinners"] - leafy["target_range"][1])
        reasons.append(_reason("weekly_target:leafy_green_days",
                               10 if leafy["status"] == "pass" else -min(10, distance * 3) if leafy["status"] == "fail" else 0,
                               leafy))
    fish_range = (profile.get("diet") or {}).get("fish_grams_per_person")
    if isinstance(fish_range, list) and len(fish_range) == 2:
        mass = [candidate["facts"].get("listed_fish_mass", {"grams_per_serving": None, "unknown": ["not_loaded"]}) for candidate in selected]
        known = sum(item["grams_per_serving"] or 0 for item in mass)
        incomplete = any(item["grams_per_serving"] is None or item["unknown"] for item in mass)
        met = fish_range[0] <= known <= fish_range[1]
        reasons.append(_reason("weekly_target:listed_fish_mass", 8 if met and not incomplete else 0,
                               {"listed_grams_per_serving": known, "target_range": fish_range,
                                "coverage": "partial_positive_evidence" if incomplete else "listed_recognized_ingredients",
                                "nutritional_compliance": "not_established"}))
    variety = [
        value for candidate in selected for value in candidate["facts"]["variety_facets"]["values"]
    ]
    distinct = len(set(variety))
    duplicates = max(0, len(variety) - distinct)
    reasons.append(_reason("variety:distinct_facets", distinct * 3, {"distinct": distinct}))
    if duplicates:
        reasons.append(_reason("variety:monotony", -10 * duplicates, {"duplicates": duplicates}))

    ingredient_counts: dict[tuple[str, str], int] = {}
    for candidate in selected:
        identities = {
            (identity, unit)
            for identity, unit, excluded in _ingredient_identities(candidate["recipe"])
            if not excluded and unit is not None
        }
        for identity, unit in identities:
            ingredient_counts[(identity, unit)] = ingredient_counts.get((identity, unit), 0) + 1
    reusable = sorted(
        {f"{identity}|{unit}": count for (identity, unit), count in ingredient_counts.items() if count > 1}.items()
    )
    reuse_weight = min(16, sum(min(count - 1, 2) * 4 for _key, count in reusable))
    reasons.append(_reason("ingredients:exact_reuse", reuse_weight, dict(reusable)))
    monotony = min(24, sum(max(0, count - 2) * 6 for _key, count in reusable))
    if monotony:
        reasons.append(_reason("ingredients:monotony", -monotony, dict(reusable)))

    dietary = [candidate["facts"]["dietary_facets"] for candidate in selected]
    for target, facet in (
        ("minimum_fish_portions", "fish"),
        ("minimum_legume_dinners", "legume"),
        ("minimum_wholegrain_or_potato_dinners", "wholegrain_or_potato"),
    ):
        wanted = _positive_int(profile, target, 0)
        if not wanted:
            continue
        observed = sum(facet in facts["values"] for facts in dietary)
        met = observed >= wanted
        reasons.append(_reason(
            f"weekly_target:{target}", 10 if met else -min(10, (wanted - observed) * 3),
            {"minimum": wanted, "positive_evidence": observed, "met_by_positive_evidence": met},
        ))
    wanted_vegetables = _positive_int(profile, "minimum_vegetable_types", 0)
    if wanted_vegetables:
        vegetables = sorted({item for facts in dietary for item in facts["vegetable_types"]})
        reasons.append(_reason(
            "weekly_target:minimum_vegetable_types",
            10 if len(vegetables) >= wanted_vegetables else -min(10, wanted_vegetables - len(vegetables)),
            {"minimum": wanted_vegetables, "positive_evidence": vegetables,
             "met_by_positive_evidence": len(vegetables) >= wanted_vegetables},
        ))
    return reasons


def _selection(
    selected: tuple[Mapping[str, Any], ...], dates: list[str], profile: Mapping[str, Any],
    input_digest: str, scope: list[dict[str, Any]], strict: Mapping[str, Any], portions: int, recurring=None,
) -> dict[str, Any]:
    slots = []
    for index, (candidate, day) in enumerate(zip(selected, dates, strict=True)):
        reasons = _slot_reasons(candidate, day, index, len(dates), profile)
        slots.append({
            "date": day,
            "reference": deepcopy(candidate["reference"]),
            "reference_key": candidate["reference_key"],
            "recipe_key": candidate["recipe_key"],
            "content_digest": candidate["content_digest"],
            "name": str(candidate["recipe"].get("name") or "")[:300],
            "portions": portions,
            "hard_constraints": deepcopy(candidate["hard_constraints"]),
            "dietary_facets": deepcopy(candidate["facts"]["dietary_facets"]),
            "leafy_green": deepcopy(candidate["facts"]["leafy_green"]),
            "reason_contributions": reasons,
            "score": sum(reason["weight"] for reason in reasons),
        })
    plan_reasons = _plan_reasons(selected, profile)
    relaxations = {
        item
        for candidate in selected
        for item in (
            ["active_minutes"] if candidate["facts"]["active_minutes"]["value"] is None else []
        ) + (
            ["perishability"] if candidate["facts"]["perishability"]["value"] == "unknown" else []
        ) + (
            ["dietary_completeness"] if not candidate["facts"]["dietary_facets"]["complete"] else []
        )
    }
    diet = profile.get("diet") if isinstance(profile.get("diet"), Mapping) else {}
    cuisine = profile.get("cuisine") if isinstance(profile.get("cuisine"), Mapping) else {}
    for field in ("patterns", "plate", "nutrition", "exceptions", "legumes"):
        value = diet.get(field)
        if field == "plate" and isinstance(value, Mapping):
            value = any(value.values())
        if value:
            relaxations.add("unsupported:diet." + field)
    leafy = _leafy_week(selected, profile)
    if leafy and leafy["unknown_dinners"]:
        relaxations.add("leafy_green_quantity_unknown")
    if any(cuisine.get(field) for field in ("base_style", "quality")):
        relaxations.add("cuisine_free_text")
    if any(cuisine.get(field) for field in ("wanted", "flavours")):
        relaxations.add("cuisine_exact_tags_only")
    payload = {
        "candidate_scope": scope,
        "slots": slots,
        "strict_targets": deepcopy(strict),
        "soft_relaxations": sorted(relaxations),
        "plan_reason_contributions": plan_reasons,
        "total_score": sum(slot["score"] for slot in slots)
        + sum(reason["weight"] for reason in plan_reasons),
        "tie_break": [slot["reference_key"] for slot in slots],
    }
    if recurring:
        payload['source_slots'] = deepcopy(slots)
        payload['slots'] = []
        payload['batches'] = []
        for candidate, slot, allocation in zip(selected, slots, recurring['sources'], strict=True):
            for day in allocation['eating_dates']:
                payload['slots'].append({**deepcopy(slot), 'date': day, 'source_date': slot['date'],
                                         'kind': 'fresh' if day == slot['date'] else 'leftover',
                                         'new_shopping_requirements': day == slot['date']})
            if allocation['batch']:
                payload['batches'].append({**deepcopy(allocation), 'recipe_key': slot['recipe_key'], 'name': slot['name'],
                    'guidance': deepcopy(candidate.get('supplied_facts', {}).get('batch_guidance') or {'basis': 'unknown', 'suitability': 'unknown', 'storage': 'Recipe-specific storage life must be checked before preparation.', 'reheating': 'Recipe-specific reheating guidance remains unknown.'})})
    payload["selection_digest"] = digest({
        "planner_version": PLANNER_VERSION,
        "input_digest": input_digest,
        "selection": payload,
    })
    return payload


def _validate_request(value: Any, *, allow_discovery: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value).difference({
        "week", "dates", "portions", "candidates", "strict_targets",
        "cooldown_overrides", "alternatives", "as_of_date", "available_ingredients", "recurring_batch", "prepared_portion_range", "meal_mode", "selection_mode",
    }):
        raise PlannerError("planner input has unknown fields")
    selection_mode = value.get("selection_mode", "ranked")
    if not isinstance(selection_mode, str) or selection_mode not in {"agent", "ranked"}:
        raise PlannerError("selection_mode must be agent or ranked")
    available = normalize_available_ingredients(value.get("available_ingredients"))
    week = str(value.get("week") or "")
    if re.fullmatch(r"\d{4}-W\d{2}", week) is None:
        raise PlannerError("planner week must use YYYY-Www")
    try:
        week_year, week_number = int(week[:4]), int(week[6:])
        date.fromisocalendar(week_year, week_number, 1)
    except ValueError as exc:
        raise PlannerError("planner week is invalid") from exc
    raw_dates = value.get("dates")
    if not isinstance(raw_dates, list) or not 1 <= len(raw_dates) <= MAX_DAYS:
        raise PlannerError(f"planner dates must contain one to {MAX_DAYS} dates")
    dates = []
    for raw in raw_dates:
        if not isinstance(raw, str):
            raise PlannerError("planner dates must be ISO dates")
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise PlannerError("planner dates must be ISO dates") from exc
        iso = parsed.isocalendar()
        if (iso.year, iso.week) != (week_year, week_number):
            raise PlannerError("every planner date must belong to planner week")
        dates.append(parsed.isoformat())
    if selection_mode == "agent" and dates != sorted(dates):
        raise PlannerError("agent selection dates must be chronological")
    dates = sorted(dates)
    if len(set(dates)) != len(dates):
        raise PlannerError("planner dates must be unique")
    portions = value.get("portions")
    if isinstance(portions, bool) or not isinstance(portions, int) or not 1 <= portions <= 100:
        raise PlannerError("planner portions must be an integer from one to 100")
    candidates = value.get("candidates")
    if selection_mode == "agent" and candidates is None:
        raise PlannerError("agent selection requires exact candidates")
    if not (allow_discovery and candidates is None) and (not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES):
        raise PlannerError(f"planner candidates must contain one to {MAX_CANDIDATES} entries")
    strict = value.get("strict_targets", [])
    if not isinstance(strict, list) or len(strict) > len(SUPPORTED_STRICT_TARGETS):
        raise PlannerError("strict_targets must be a bounded list")
    strict = sorted(set(strict))
    if any(not isinstance(item, str) or item not in SUPPORTED_STRICT_TARGETS for item in strict):
        raise PlannerError("strict_targets contains an unsupported target")
    raw_overrides = value.get("cooldown_overrides", {})
    if not isinstance(raw_overrides, Mapping) or len(raw_overrides) > MAX_CANDIDATES:
        raise PlannerError("cooldown_overrides must be a bounded object")
    overrides = {}
    for key, reason in raw_overrides.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 1_024:
            raise PlannerError("cooldown override recipe keys are invalid")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise PlannerError("cooldown override reasons must be bounded text")
        overrides[key] = reason.strip()
    alternatives = value.get("alternatives", 1)
    if isinstance(alternatives, bool) or not isinstance(alternatives, int) or not 1 <= alternatives <= MAX_ALTERNATIVES:
        raise PlannerError(f"alternatives must be from one to {MAX_ALTERNATIVES}")
    if selection_mode == "agent" and alternatives != 1:
        raise PlannerError("agent selection returns exactly one arrangement")
    as_of_date = value.get("as_of_date")
    if not isinstance(as_of_date, str):
        raise PlannerError("as_of_date must be an ISO date")
    try:
        as_of_date = date.fromisoformat(as_of_date).isoformat()
    except ValueError as exc:
        raise PlannerError("as_of_date must be an ISO date") from exc
    return {
        "planner_version": PLANNER_VERSION,
        **({"selection_mode": selection_mode} if "selection_mode" in value else {}),
        **({"recurring_batch": deepcopy(value["recurring_batch"])} if value.get("recurring_batch") else {}),
        **({"prepared_portion_range": deepcopy(value["prepared_portion_range"])} if value.get("prepared_portion_range") is not None else {}),
        **({"meal_mode": value['meal_mode']} if value.get('meal_mode') is not None else {}),
        **({"available_ingredients": available} if available else {}),
        "week": week,
        "dates": dates,
        "portions": portions,
        "candidates": deepcopy(candidates),
        "strict_targets": strict,
        "cooldown_overrides": dict(sorted(overrides.items())),
        "alternatives": alternatives,
        "as_of_date": as_of_date,
    }


def plan_week(
    request: Any, *, profile: Mapping[str, Any], candidates: list[Mapping[str, Any]],
    history: Mapping[str, Any], feedback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a byte-stable ranking for already-resolved exact candidates."""
    checked = _validate_request(request)
    meals = profile.get("meals") if isinstance(profile, Mapping) else None
    dinner_days = meals.get("dinner_days") if isinstance(meals, Mapping) else None
    diet = profile.get("diet") if isinstance(profile, Mapping) else None
    agent_mode = checked.get("selection_mode") == "agent"
    if not agent_mode and type(dinner_days) is int and len(checked["dates"]) == dinner_days and isinstance(diet, Mapping):
        saved_minima = {
            target for target in SAVED_MINIMUM_TARGETS
            if type(diet.get(target)) is int and diet[target] > 0
        }
        checked["strict_targets"] = sorted(set(checked["strict_targets"]) | saved_minima)
    if not isinstance(history, Mapping) or len(history) > MAX_HISTORY_RECORDS:
        raise PlannerError(f"planner history exceeds {MAX_HISTORY_RECORDS} records")
    if len(candidates) != len(checked["candidates"]):
        raise PlannerError("resolved planner candidates do not match planner input")
    prepared = []
    reference_keys = set()
    for candidate in candidates:
        required = {
            "reference", "reference_key", "recipe", "recipe_key", "dedupe_key", "content_digest",
            "usage", "facts", "materialization_error",
        }
        if not isinstance(candidate, Mapping) or not required.issubset(candidate):
            raise PlannerError("resolved planner candidate is invalid")
        reference_key = str(candidate["reference_key"])
        if reference_key in reference_keys:
            raise PlannerError("planner candidates contain a duplicate exact reference")
        reference_keys.add(reference_key)
        item = deepcopy(dict(candidate))
        usage = item.get("usage")
        if isinstance(usage, Mapping):
            item["usage"] = deepcopy(dict(usage))
            blocked_by = item["usage"].get("blocked_by")
            if isinstance(blocked_by, list):
                item["usage"]["blocked_by"] = sorted(blocked_by, key=canonical)
        item = prepare_candidate(item, profile, checked["cooldown_overrides"], checked["portions"], checked.get("available_ingredients"), meal_role_advisory=agent_mode)
        prepared.append(item)
    status_priority = {"pass": 0, "unknown": 1, "fail": 2}
    for group in candidate_groups(prepared):
        usage = merge_family_usage(group)
        for item in group:
            item["usage"] = deepcopy(usage)
            item["hard_constraints"] = _hard_evaluation(item, profile, checked["cooldown_overrides"], meal_role_advisory=agent_mode)
        ordered = sorted(group, key=lambda candidate: (
            status_priority[candidate["hard_constraints"]["status"]],
            "recipe_ref" not in candidate["reference"],
            not candidate.get("locally_modified", False),
            candidate["reference_key"],
        ))
        for item in group:
            item["dedupe_key"] = "family:" + ordered[0]["reference_key"]
        for item in ordered[1:]:
            item["hard_constraints"]["status"] = "fail"
            item["hard_constraints"]["reasons"].append({
                "code": "duplicate_recipe_identity",
                "status": "fail",
                "detail": {"same_as_reference_key": ordered[0]["reference_key"]},
            })
    if not agent_mode:
        prepared.sort(key=lambda item: item["reference_key"])
    unknown_override_keys = set(checked["cooldown_overrides"]).difference(
        item["recipe_key"] for item in prepared
    )
    if unknown_override_keys:
        raise PlannerError("cooldown override does not name an exact candidate recipe key")
    unnecessary_override_keys = {
        item["recipe_key"] for item in prepared
        if item["recipe_key"] in checked["cooldown_overrides"]
        and isinstance(item.get("usage"), Mapping)
        and item["usage"].get("eligible") is True
    }
    if unnecessary_override_keys:
        raise PlannerError("cooldown override is valid only for a currently blocked candidate")

    public_request = {
        key: deepcopy(value) for key, value in checked.items() if key != "planner_version"
    }
    public_request["candidates"] = [
        {
            **deepcopy(item["reference"]),
            "facts": normalize_candidate_facts(item.get("supplied_facts")),
        }
        for item in prepared
    ]

    evaluations = [{
        "reference": deepcopy(item["reference"]),
        "reference_key": item["reference_key"],
        "recipe_key": item["recipe_key"],
        "dedupe_key": item["dedupe_key"],
        "content_digest": item["content_digest"],
        "facts": deepcopy(item["facts"]),
        "usage": deepcopy(item["usage"]),
        "hard_constraints": deepcopy(item["hard_constraints"]),
    } for item in prepared]
    canonical_input = {
        "planner_version": PLANNER_VERSION,
        "request": public_request,
        "profile": deepcopy(dict(profile)),
        "history": {
            str(key): deepcopy(value)
            for key, value in sorted(history.items(), key=lambda item: str(item[0]))
        },
        "candidates": evaluations,
    }
    if feedback is not None:
        canonical_input["feedback"] = deepcopy(dict(feedback))
    input_digest = digest(canonical_input)
    base_result = {
        "planner_version": PLANNER_VERSION,
        "request": public_request,
        "input_digest": input_digest,
        "canonical_input": canonical_input,
        "candidate_evaluations": evaluations,
        "work_limits": {
            "maximum_candidates": MAX_CANDIDATES,
            "maximum_days": MAX_DAYS,
            "maximum_alternatives": MAX_ALTERNATIVES,
            "maximum_explored_states": MAX_EXPLORED_STATES,
            "maximum_orders_per_candidate_set": MAX_ALTERNATIVES,
            "maximum_history_records": MAX_HISTORY_RECORDS,
        },
    }
    eligible = [item for item in prepared if item["hard_constraints"]["status"] == "pass"]
    unknown = [item for item in prepared if item["hard_constraints"]["status"] == "unknown"]
    layout = checked.get('recurring_batch')
    if layout and layout['shortages']:
        return {**base_result, 'status': 'needs_input', 'issues': [{'code': 'batch_portion_shortfall', **layout}], 'selections': []}
    source_dates = [s['source_date'] for s in layout['sources']] if layout else checked['dates']
    count = len(source_dates)
    if agent_mode and source_dates != sorted(source_dates):
        raise PlannerError("agent selection cooking dates must be chronological")
    if agent_mode and len(prepared) != count:
        raise PlannerError("agent selection needs exactly one candidate per cooking date")
    if len(eligible) < count:
        status = "needs_input" if len(eligible) + len(unknown) >= count else "no_plan"
        return {
            **base_result,
            "status": status,
            "issues": [{
                "code": "insufficient_hard_constraint_candidates",
                "required": count,
                "eligible": len(eligible),
                "unknown": [item["reference_key"] for item in unknown],
            }],
            "selections": [],
        }
    complete_states = math.perm(len(eligible), count) if not agent_mode else 1
    scope = [{
        "reference": deepcopy(item["reference"]),
        "reference_key": item["reference_key"],
        "content_digest": item["content_digest"],
    } for item in prepared]
    scored_positions = (
        ((candidate, index, day) for index, (candidate, day) in enumerate(zip(prepared, source_dates, strict=True)))
        if agent_mode else
        ((candidate, index, day) for candidate in eligible for index, day in enumerate(source_dates))
    )
    slot_scores = {
        (candidate["reference_key"], index): sum(
            reason["weight"] for reason in _slot_reasons(
                candidate, day, index, len(source_dates), profile
            )
        )
        for candidate, index, day in scored_positions
    }

    def strict_prefix_signature(
        selected: tuple[Mapping[str, Any], ...],
    ) -> tuple[Any, ...]:
        """Partition equal candidate sets by order-sensitive batch contributions."""
        if not layout:
            return tuple()
        evaluated = tuple(
            candidate
            for candidate, allocation in zip(
                selected, layout["sources"][:len(selected)], strict=True,
            )
            for _ in allocation["eating_dates"]
        )
        signature: list[Any] = []
        for target in checked["strict_targets"]:
            if target == "active_minutes":
                low, high, _maximum = _active_window(profile)
                values = [
                    candidate["facts"]["active_minutes"]["value"]
                    for candidate in evaluated
                ]
                signature.append((
                    target,
                    any(value is None for value in values),
                    any(value is not None and not low <= value <= high for value in values),
                ))
            elif target == "leafy_green_days":
                leafy = _leafy_week(evaluated, profile)
                signature.append((target, leafy["counted_dinners"], leafy["unknown_dinners"]))
            elif target == "minimum_vegetable_types":
                # Duplicate batch servings do not change a set of vegetable types.
                continue
            else:
                facet = {
                    "minimum_fish_portions": "fish",
                    "minimum_legume_dinners": "legume",
                    "minimum_wholegrain_or_potato_dinners": "wholegrain_or_potato",
                }[target]
                wanted = _positive_int(profile, target, 0)
                observed = sum(
                    facet in candidate["facts"]["dietary_facets"]["values"]
                    for candidate in evaluated
                )
                signature.append((target, min(wanted, observed)))
        return tuple(signature)

    if agent_mode:
        explored_states = 1
        candidate_sequences = iter((tuple(prepared),))
        search_strategy = "agent_selection"
    elif complete_states <= MAX_EXPLORED_STATES:
        explored_states = complete_states
        candidate_sequences = permutations(eligible, count)
        search_strategy = "exhaustive"
    else:
        # Slot scores depend on order, while plan reasons and strict targets
        # depend only on the selected candidate set. Keep the best requested
        # number of orderings for every exact set instead of globally pruning
        # low-scoring prefixes: this remains bounded for 12 candidates and
        # cannot discard the only set that satisfies a strict weekly target.
        frontier: list[tuple[Mapping[str, Any], ...]] = [tuple()]
        explored_states = 0
        for index in range(count):
            by_candidate_set: dict[
                tuple[tuple[str, ...], tuple[Any, ...]],
                list[tuple[int, tuple[str, ...], tuple[Mapping[str, Any], ...]]],
            ] = {}
            for prefix in frontier:
                for candidate in eligible:
                    selected = (*prefix, candidate)
                    if len({item["recipe_key"] for item in selected}) != len(selected):
                        continue
                    if len({item["dedupe_key"] for item in selected}) != len(selected):
                        continue
                    if len({item["content_digest"] for item in selected}) != len(selected):
                        continue
                    explored_states += 1
                    if explored_states > MAX_EXPLORED_STATES:
                        raise PlannerError("planner bounded search exhausted unexpectedly")
                    if (
                        layout and layout["sources"][index]["batch"]
                        and candidate.get("supplied_facts", {}).get("batch_guidance", {}).get("suitability") == "unsuitable"
                    ):
                        continue
                    score = sum(reason["weight"] for reason in _plan_reasons(selected, profile)) + sum(
                        slot_scores[(item["reference_key"], slot_index)]
                        for slot_index, item in enumerate(selected)
                    )
                    ranked_prefix = (
                        -score,
                        tuple(item["reference_key"] for item in selected),
                        selected,
                    )
                    state_key = (
                        tuple(sorted(ranked_prefix[1])),
                        strict_prefix_signature(selected),
                    )
                    retained = by_candidate_set.setdefault(state_key, [])
                    retained.append(ranked_prefix)
                    retained.sort(key=lambda item: (item[0], item[1]))
                    del retained[checked["alternatives"]:]
            frontier = [
                item[2]
                for state_key in sorted(by_candidate_set)
                for item in by_candidate_set[state_key]
            ]
            if not frontier:
                break
        candidate_sequences = iter(frontier)
        search_strategy = "bounded_dynamic_programming"

    ranked: list[dict[str, Any]] = []
    strict_unknowns: dict[str, dict[str, Any]] = {}
    strict_failures = 0
    for selected in candidate_sequences:
        if len({item["recipe_key"] for item in selected}) != count:
            continue
        if len({item["dedupe_key"] for item in selected}) != count:
            continue
        if len({item["content_digest"] for item in selected}) != count:
            continue
        if layout and any(s['batch'] and c.get('supplied_facts', {}).get('batch_guidance', {}).get('suitability') == 'unsuitable' for c, s in zip(selected, layout['sources'])):
            continue
        evaluated = tuple(c for c, allocation in zip(selected, layout['sources']) for _ in allocation['eating_dates']) if layout else selected
        strict = _strict_evaluation(evaluated, checked["strict_targets"], profile)
        if strict["status"] == "unknown":
            for item in strict["results"]:
                if item["status"] == "unknown":
                    strict_unknowns.setdefault(canonical(item), item)
            continue
        if strict["status"] == "fail":
            strict_failures += 1
            continue
        tie_break = tuple(item["reference_key"] for item in selected)
        ranked.append({
            "selected": selected,
            "strict": strict,
            "total_score": sum(
                reason["weight"] for reason in _plan_reasons(selected, profile)
            ) + sum(
                slot_scores[(item["reference_key"], index)]
                for index, item in enumerate(selected)
            ),
            "tie_break": tie_break,
        })
        # Exact references are unique within every permutation, so tie_break is
        # itself unique. The selection digest can be computed only for the
        # retained winners without changing the deterministic ordering.
        if not agent_mode:
            ranked.sort(key=lambda item: (-item["total_score"], item["tie_break"]))
            del ranked[checked["alternatives"]:]
    if not ranked:
        if strict_unknowns:
            issues = sorted(strict_unknowns.values(), key=canonical)
            status = "needs_input"
        else:
            issues = [{"code": "strict_targets_infeasible", "evaluated": strict_failures}]
            status = "no_plan"
        return {**base_result, "status": status, "issues": issues, "selections": []}
    selections = []
    for rank in ranked:
        selection = _selection(
            rank["selected"], source_dates, profile, input_digest, scope,
            rank["strict"], checked["portions"], recurring=layout,
        )
        if (
            selection["total_score"] != rank["total_score"]
            or tuple(selection["tie_break"]) != rank["tie_break"]
        ):
            raise PlannerError("planner ranking and selection materialization disagree")
        selections.append(selection)
    handoffs = [{
        "planner_version": PLANNER_VERSION,
        "input_digest": input_digest,
        "selection_digest": selection["selection_digest"],
        "request": deepcopy(public_request),
        "selection": deepcopy(selection),
    } for selection in selections]
    return {
        **base_result,
        "status": "planned",
        "explored_states": explored_states,
        "search_strategy": search_strategy,
        "selection": deepcopy(selections[0]),
        "selection_digest": selections[0]["selection_digest"],
        "selections": selections,
        "save_handoff": deepcopy(handoffs[0]),
        "save_handoffs": handoffs,
    }
