"""Exact, bounded product/package selection for one menu."""

from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
import re
import time
from typing import Any, Mapping
import unicodedata

from core import HouseholdError


PRODUCT_PLAN_VERSION = "product-plan-v4"
MAX_REQUIREMENTS = 64
MAX_ALTERNATIVE_REQUIREMENTS = 3 * MAX_REQUIREMENTS
MAX_CANDIDATES_PER_REQUIREMENT = 5
MAX_PACKAGES_PER_REQUIREMENT = 100
MAX_COMBINATIONS = 10_000

from recipe_quantities import UNITS as _UNITS, read_quantity


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _ref_sort_key(value: str | int) -> bytes:
    return canonical(value).encode("utf-8")


def _valid_product_ref(value: Any) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and value > 0
    ) or (
        isinstance(value, str) and 1 <= len(value.encode("utf-8")) <= 500
    )


def _identity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not text or len(text.encode("utf-8")) > 300:
        return None
    text = text.casefold()
    # Preparation wording does not create another stock allocation. Keep food
    # form (dried/cooked, powder/fresh) distinct; only these culinary synonyms
    # are normalized, not arbitrary comma-separated source text.
    aliases = {
        'fersk brokkoli': 'fersk brokkoli', 'brokkoli, fersk': 'fersk brokkoli',
        'fresh broccoli': 'fresh broccoli', 'broccoli, fresh': 'fresh broccoli',
        'onion, finely chopped': 'onion', 'onion , finely chopped': 'onion',
        'garlic (cloves)': 'garlic cloves', 'garlic , minced (cloves)': 'garlic cloves',
        'green bell pepper , diced': 'green pepper',
        'red bell pepper , diced': 'red pepper',
    }
    return aliases.get(text, text)


def _aggregation_identity(value: Any) -> str | None:
    """Canonicalize only reviewed whole identities that share household stock."""
    identity = _identity(value)
    aliases = {
        "garlic": "hvitløk", "garlic cloves": "hvitløk", "hvidløg": "hvitløk",
        "spring onion": "vårløk", "spring onions": "vårløk", "forårsløg": "vårløk",
        "broccoli": "brokkoli",
        "cod fillet": "torskefilet", "torsk fillet": "torskefilet",
        "salmon fillet": "laksefilet",
        "onion": "gul løk", "løg": "gul løk",
        "black beans": "sorte bønner", "white beans": "hvite bønner",
        "hvide bønner": "hvite bønner", "chickpeas": "kikerter", "kikærter": "kikerter",
        "green pepper": "grønn paprika", "red pepper": "rød paprika",
        "tomato": "tomat", "tomatoes": "tomat", "tomater": "tomat",
        "potato": "potet", "potatoes": "potet", "poteter": "potet",
        "carrot": "gulrot", "carrots": "gulrot", "gulrøtter": "gulrot",
    }
    return aliases.get(identity, identity)


def ingredient_search(identity, provider):
    """Return a reviewed provider query without changing recipe identity."""
    normalized = _identity(identity) or identity
    if provider == 'mathem':
        normalized = _aggregation_identity(normalized) or normalized
        # Whole-identity mappings only. A missing or ambiguous Swedish mapping
        # stays visible for explicit candidate review instead of guessing from
        # individual Norwegian words.
        aliases = {
            'hvitløk': 'vitlök', 'vårløk': 'salladslök', 'brokkoli': 'broccoli',
            'sorte bønner': 'svarta bönor', 'hvite bønner': 'vita bönor',
            'kikerter': 'kikärter', 'gul løk': 'gul lök',
            'grønn paprika': 'grön paprika', 'rød paprika': 'röd paprika',
            'torskefilet': 'torskfilé', 'laksefilet': 'laxfilé',
            'maisstivelse': 'majsstärkelse', 'maismel': 'majsmjöl',
            'korianderblader': 'färska korianderblad',
            'ferske korianderblader': 'färska korianderblad',
            'korianderfrø': 'korianderfrön', 'chilipulver': 'chilipulver',
            'finkornet sukker': 'finkornigt strösocker', 'melis': 'florsocker',
            'selvhevende hvetemel': 'självjäsande vetemjöl',
            'kremfløte, minst 48 % fett': 'vispgrädde, minst 48 % fett',
            'kremfløte, 38 % fett': 'vispgrädde, 38 % fett',
            'kålrot': 'kålrot',
            'britisk fruktfyll til bakst': 'brittisk mincemeat-fruktfyllning',
            'kjøttfarse av storfe og svin': 'köttfärs av nöt och fläsk',
            'kjøttdeig av storfe': 'nötfärs', 'kjøttdeig av svin': 'fläskfärs',
            'kjøttdeig av kylling': 'kycklingfärs',
            'saltet smør': 'saltat smör', 'usaltet smør': 'osaltat smör',
            'helmelk': 'standardmjölk', 'skummet melk': 'skummjölk',
        }
        return aliases.get(normalized, normalized)
    # Oda/MENY recipe items are stable Norwegian semantic identities. Exact
    # legacy aliases remain supported, but composite or ambiguous wording falls
    # through intact for explicit candidate review.
    aliases = {
        'black beans': 'sorte bønner', 'sorte bønner': 'sorte bønner',
        'white beans': 'hvite bønner', 'hvide bønner': 'hvite bønner',
        'chickpeas': 'kikerter', 'kikærter': 'kikerter',
        'black pepper': 'svart pepper', 'celery': 'stangselleri',
        'fish stock': 'fiskebuljong', 'garlic': 'hvitløk',
        'garlic cloves': 'hvitløk', 'hvidløg': 'hvitløk',
        'spring onion': 'vårløk', 'spring onions': 'vårløk',
        'forårsløg': 'vårløk', 'broccoli': 'brokkoli',
        'cod fillet': 'torskefilet', 'torskefilet': 'torskefilet',
        'salmon fillet': 'laksefilet', 'laksefilet': 'laksefilet',
        'green pepper': 'grønn paprika', 'red pepper': 'rød paprika',
        'onion': 'gul løk', 'løg': 'gul løk',
        'ground cayenne pepper': 'cayennepepper', 'ground coriander': 'malt koriander',
        'ground cumin': 'spisskummen', 'ground paprika': 'paprikapulver',
        'vegetable oil': 'matolje', 'tomatoes, diced': 'tomater',
        'tomatoes , diced': 'tomater', 'salt': 'salt', 'water': 'vann',
    }
    if normalized.startswith('fish (e.g. tilapia'):
        return 'hvit fisk filet'
    if normalized.startswith('hot pepper (optional'):
        return 'chili'
    return aliases.get(normalized, normalized)


def nonfood_candidate(product):
    name = str(product.get('name') or '').casefold()
    return bool(re.search(r'\b(?:cat food|dog food|pet food|kattemat|hundemat|våtfôr|tørrfôr|whiskas|ansiktsservietter|lommetørklær|lommetørkler|tørkepapir|toalettpapir)\b', name))


def _semantic_features(text: str) -> dict[str, Any]:
    def one(patterns: Mapping[str, str]) -> str | None:
        matches = [name for name, pattern in patterns.items() if re.search(pattern, text)]
        return matches[0] if len(matches) == 1 else "conflicting" if matches else None

    dietary = {
        name for name, pattern in {
            "gluten_free": r"\b(?:glutenfri|gluten[- ]free)\b",
            "lactose_free": r"\b(?:laktosefri|lactose[- ]free)\b",
            "dairy_free": r"\b(?:melkefri|mælkefri|dairy[- ]free)\b",
            "vegetarian": r"\b(?:vegetarisk|vegetarian|plantebasert|plant[- ]based)\b",
            "vegan": r"\b(?:vegansk|vegan)\b",
            "nut_free": r"\b(?:nøttefri|nut[- ]free)\b",
            "peanut_free": r"\b(?:peanøttfri|peanut[- ]free)\b",
            "egg_free": r"\b(?:eggfri|egg[- ]free)\b",
            "soy_free": r"\b(?:soyafri|soya[- ]free|soy[- ]free)\b",
        }.items() if re.search(pattern, text)
    }
    return {
        "state": one({
            "fresh": r"\b(?:fersk(?:e)?|färsk(?:a)?|fresh)\b",
            "frozen": r"\b(?:fryst|frossen|fryst(?:a)?|frozen)\b",
            "dried": r"(?:tørr|tørket|torkad|dry|dried)",
            "instant": r"\b(?:instant|hurtig)\w*",
        }),
        "skin": one({
            "skinless": r"(?:uten\s+skinn|skinnfri|u\s*/\s*skinn|skinless)",
            "skin_on": r"(?:med\s+skinn|skin[- ]on|with\s+skin)",
        }),
        "salt": one({
            "unsalted": r"\b(?:usaltet|unsalted|osaltat)\b",
            "salted": r"\b(?:lettsaltet|saltet|salted|saltat)\b|med\s+salt|with\s+salt",
        }),
        "coriander_form": one({
            "leaf": r"(?:korianderblad\w*|coriander\s+lea(?:f|ves))",
            "seed": r"(?:korianderfrø\w*|korianderfrön|coriander\s+seeds?)",
            "ground": r"(?:malt\s+koriander|koriander\s+malt|ground\s+coriander|coriander\s+ground)",
            "whole": r"(?:hel\s+koriander|koriander\s+hel|whole\s+coriander|coriander\s+whole)",
        }),
        "chili_form": one({
            "powder": r"(?:chili|chilli)\s*(?:pulver|powder)|chilipulver",
            "flakes": r"(?:chili|chilli)\s*(?:flak|flakes)",
        }),
        "sugar_form": one({
            "caster": r"(?:finkornet\s+sukker|caster\s+sugar|finkornigt\s+strösocker)",
            "icing": r"\b(?:melis|icing\s+sugar|florsocker)\b",
        }),
        "flour_leavening": one({
            "self_raising": r"\b(?:selvhevende|self[- ]rais(?:ing|ed)|self[- ]rising|självjäsande)\b",
            "plain": r"\b(?:plain|vanlig)\s+(?:hvetemel|flour|vetemjöl)\b",
        }),
        "milk_type": one({
            "whole": r"\b(?:helmelk|whole\s+milk|standardmjölk)\b",
            "skim": r"\b(?:skummet\s+melk|skimmed\s+milk|skummjölk)\b",
            "low_fat": r"\b(?:lettmelk|semi[- ]skimmed\s+milk|lättmjölk)\b",
        }),
        "meat_form": one({
            "mince": r"\b(?:kjøttdeig|hakket\s+kjøtt|minced\s+meat|mince|nötfärs|fläskfärs|kycklingfärs)\b",
            "lean_mince": r"\b(?:karbonadedeig|lean\s+mince)\b",
            "forcemeat": r"\b(?:kjøttfarse|köttfärs|farse|forcemeat)\b",
            "sausage": r"\b(?:pølse|pølser|sausage|korv)\b",
        }),
        "cut": one({
            "breast": r"\b(?:kylling(?:bryst|filet)|chicken\s+(?:breast|fillet))s?\b",
            "thigh": r"\b(?:kyllinglår|chicken\s+thigh)s?\b",
            "wing": r"\b(?:kyllingvinge|chicken\s+wing)s?\b",
            "fillet": r"\b(?:(?!kyllingfilet\b)[a-zæøåöä]*filet|[a-zæøåöä]*filé|(?<!chicken\s)fillet|[a-zæøåöä]*loin)s?\b",
            "chop": r"\b(?:kotelett|chop)s?\b",
            "whole_fish": r"\b(?:(?:hel|whole)\s+(?:laks|salmon|torsk|cod)|(?:laks|salmon|torsk|cod)\s+(?:hel|whole))\b",
        }),
        "produce_form": one({
            "minced": r"\b(?:hakket|finhakket|minced|chopped|finely\s+chopped)\b",
            "diced": r"\b(?:terninger|diced)\b",
            "whole_tomato": r"\b(?:hele?\s+tomater?|whole\s+tomatoes?)\b",
        }),
        "grain_grade": one({
            "wholegrain": r"\b(?:fullkorn\w*|helkorn\w*|whole[- ]?grain\w*|brun\s+ris|brown\s+rice|grovt\s+brød)\b",
            "refined": r"\b(?:vanlig\s+pasta|hvit\s+ris|white\s+rice|jasminris|hvetetortilla|loff)\b",
        }),
        "root_variant": one({
            "swede": r"\b(?:kålrot|swede|rutabaga)\b",
            "turnip": r"\b(?:nepe|turnip)\b",
        }),
        "fruit_filling": one({
            "british_mincemeat": r"(?:britisk\s+fruktfyll|mincemeat[- ]frukt|fruit\s+mincemeat|\bmincemeat\b)",
        }),
        "treatment": one({
            "smoked": r"\b(?:røkt|rökt|smoked)\b",
            "cured": r"\b(?:gravet|gravad|cured)\b",
            "cooked": r"\b(?:kokt|kokte|cooked)\b",
            "raw": r"\b(?:rå|raw)\b",
            "roasted": r"\b(?:ristet|ristede|roasted)\b",
            "canned": r"\b(?:hermetisk|hermetiske|canned|tinned)\b",
        }),
        "bone": one({
            "boneless": r"\b(?:benfri|beinløs|boneless)\b",
            "bone_in": r"(?:med\s+(?:bein|ben)|bone[- ]in|with\s+bone)",
        }),
        "dietary": dietary,
    }


_REVIEWED_EXACT_PREPARED_TITLES = {
    "rød karripasta": "santa maria red curry paste",
    "søt chilisaus": "santa maria sweet chili sauce original",
}


def _prepared_signature(text: str) -> tuple[set[str], list[str]]:
    """Keep prepared form and every requested food-identity word distinct."""
    forms = {
        "juice": "juice", "jus": "juice", "pesto": "pesto", "aioli": "aioli",
        "dressing": "dressing", "saus": "sauce", "sauce": "sauce",
        "puré": "puree", "puree": "puree", "paste": "paste",
        "suppe": "soup", "soup": "soup", "ketchup": "ketchup",
        "chutney": "chutney", "salsa": "salsa", "brød": "bread",
        "bread": "bread", "pulver": "powder", "powder": "powder",
        "tortilla": "tortilla", "tortillas": "tortilla",
        "nudel": "noodle", "nudler": "noodle",
        "noodle": "noodle", "noodles": "noodle",
        "cracker": "cracker", "crackers": "cracker", "kjeks": "cracker",
        "mix": "mix",
    }
    compounds = ("chutney", "dressing", "nudler", "pulver", "pesto", "suppe", "juice", "brød", "saus", "kjeks")
    normalized = unicodedata.normalize("NFC", text).casefold()
    normalized = re.sub(
        r"(?:\s+\d+(?:[.,]\d+)?\s*(?:kg|g|ml|cl|l|stk|pk))+$", "", normalized,
    )
    categories: set[str] = set()
    identity: list[str] = []
    for word in re.findall(r"[a-zæøåöä]+|\d+(?:[.,]\d+)?", normalized):
        if word in forms:
            categories.add(forms[word])
        elif word == "karripasta":
            categories.add("paste")
            identity.append("karri")
        else:
            suffix = next(
                (part for part in compounds if word.endswith(part) and len(word) > len(part) + 1),
                None,
            )
            if suffix:
                categories.add(forms[suffix])
                identity.append(word[:-len(suffix)])
            elif word not in {"av", "of", "med", "with", "og", "and", "i", "in", "til", "to"}:
                identity.append(word)
    return categories, sorted(identity)


def _reviewed_exact_prepared_title_match(
    requirement: Mapping[str, Any], product: Mapping[str, Any],
) -> bool:
    wanted = _identity(requirement.get("item"))
    offered = _identity(product.get("name"))
    return bool(wanted and offered == _REVIEWED_EXACT_PREPARED_TITLES.get(wanted))


def _semantic_product_conflict(
    requirement: Mapping[str, Any], product: Mapping[str, Any], *,
    exact_retailer_identity_approved: bool = False,
    reviewed_prepared_title_approved: bool = False,
) -> bool:
    """Reject explicit identity, form and variant contradictions fail-closed."""
    def semantic_text(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(unicodedata.normalize("NFC", value).split()).casefold()

    wanted = semantic_text(requirement.get("item"))
    offered = semantic_text(product.get("name"))
    if not wanted or not offered:
        return False
    reviewed_offered = _REVIEWED_EXACT_PREPARED_TITLES.get(wanted)
    if reviewed_offered is not None:
        if not reviewed_prepared_title_approved or offered != reviewed_offered:
            return True
        return False
    wanted_features = _semantic_features(wanted)
    offered_features = _semantic_features(offered)
    # Generic staples are a semantic trust boundary: a search hit containing
    # the word is not necessarily the ingredient (rice flour, garlic bread,
    # cookie butter). Accept only reviewed whole-identity forms. Product names
    # that do not expose one of these forms remain unresolved for manual choice.
    package_tail = r"(?:\s+\d+(?:[.,]\d+)?\s*(?:%|kg|g|l|ml|stk|pk))?"
    bare_identity_forms = (
        (r"^(?:smør|butter)$", rf"(?:^|\s)(?:(?:meieri|ekte|saltet|usaltet|lettsaltet)smør|smør){package_tail}$|^(?:(?:salted|unsalted|cultured|dairy)\s+)?butter{package_tail}$"),
        (r"^(?:mel|hvetemel|flour|vetemjöl)$", rf"(?:^|\s)(?:siktet\s+)?hvetemel(?:\s+siktet)?{package_tail}$|^(?:mel|flour){package_tail}$|(?:^|\s)(?:plain|wheat)\s+flour{package_tail}$|(?:^|\s)vetemjöl{package_tail}$"),
        (r"^salt$", rf"(?:^|\s)(?:(?:fint|grovt)\s+salt|havsalt|flaksalt|bordsalt|finsalt|grovsalt|salt)(?:\s+(?:fint|grovt|flak|med\s+jod))?{package_tail}$|^(?:sea\s+salt|table\s+salt){package_tail}$"),
        (r"^(?:ris|rice)$", rf"(?:^|\s)(?:jasminris|basmatiris|fullkornsris|villris|sushiris|grøtris|risottoris|ris){package_tail}$|(?:^|\s)(?:(?:jasmine|basmati|brown|white|wild|sushi|arborio|risotto|long[-\s]grain)\s+rice|rice){package_tail}$"),
        (r"^(?:melk|milk|mjölk)$", rf"(?:^|\s)(?:helmelk|lettmelk|skummet\s+melk|standardmjölk|lättmjölk|skummjölk|melk|mjölk)(?:\s+(?:lett|hel))?{package_tail}$|^(?:(?:whole|skimmed|semi[-\s]skimmed|dairy)\s+milk|milk){package_tail}$"),
        (r"^(?:hvitløk|garlic|vitlök)$", rf"^(?:(?:fersk|fresh)\s+|(?:upresset|opressad|unpressed)[-\s]+)?(?:hvitløk|garlic|vitlök)(?:\s+(?:kina|norsk|økologisk|løsvekt))?{package_tail}$"),
        (r"^(?:tomat|tomato|tomater|tomatoes)$", rf"(?:^|\s)(?:(?:ferske?|fresh|økologiske?|organic|norske?)\s+)?(?:tomat(?:er)?|(?:cherry|plomme|cocktail|klase)tomat(?:er)?)(?:\s+løsvekt)?{package_tail}$|(?:^|\s)(?:(?:cherry|plum|cocktail|cluster|vine)\s+)?tomato(?:es)?{package_tail}$"),
    )
    bare_identity_presence = (
        (r"^(?:smør|butter)$", r"\b(?:[a-zæøåöä]*smør|butter)\b"),
        (r"^(?:mel|hvetemel|flour|vetemjöl)$", r"\b(?:mel|hvetemel|flour|vetemjöl)\b"),
        (r"^salt$", r"\b[a-zæøåöä]*salt\b"),
        (r"^(?:ris|rice)$", r"\b(?:[a-zæøåöä]*ris|rice)\b"),
        (r"^(?:melk|milk|mjölk)$", r"\b(?:[a-zæøåöä]*melk|milk|mjölk)\b"),
        (r"^(?:hvitløk|garlic|vitlök)$", r"\b(?:hvitløk|garlic|vitlök)\b"),
        (r"^(?:tomat|tomato|tomater|tomatoes)$", r"\b(?:[a-zæøåöä]*tomat\w*|tomatoes?)\b"),
    )
    if any(
        re.fullmatch(base, wanted) and not re.search(identity, offered)
        for base, identity in bare_identity_presence
    ):
        return True
    retailer_identity_may_be_resolved = exact_retailer_identity_approved
    if (
        not retailer_identity_may_be_resolved
        and any(
            re.fullmatch(base, wanted) and not re.search(allowed, offered)
            for base, allowed in bare_identity_forms
        )
    ):
        return True
    pressed_garlic_form = (
        r"(?!(?:upresset|opressad|unpressed)\b)"
        r"\w*(?:presset|pressad|pressed)"
    )
    bare_compound_guards = (
        (r"^(?:smør|butter)$", r"\b(?:peanøtt|peanut|mandel|almond|cashew|hasselnøtt|hazelnut|pistasj|pistachio|sesam|sesame|solsikke|sunflower|kakao|cacao|cocoa|cookie)[-\s]*(?:smør|butter)\b"),
        (r"^(?:mel|hvetemel|flour|vetemjöl)$", r"(?:\b(?:mandel|almond|kokos|coconut|havre|oat|kikert|chickpea|mais|corn|ris|rice)[-\s]*(?:mel|flour|mjöl)\b|\bflour\s+tortillas?\b)"),
        (r"^(?:salt)$", r"(?:\b(?:hvitløk|garlic|vitlök|selleri|celery|løk|onion)s?[-\s]*salt\b|\bsalt(?:[-\s]+|\s*&\s*)(?:kjeks|crackers?|chips?|pepper\s+mix)\b)"),
        (r"^(?:ris|rice)$", r"(?:\b(?:blomkål|cauliflower|brokkoli|broccoli)[-\s]*(?:ris|rice)\b|\b(?:ris|rice)[-\s]*(?:nudler?|noodles?|kaker?|cakes?|grøt|pudding|flour)\b|\b(?:bygg|konjak|linse)ris\b)"),
        (r"^(?:melk|milk|mjölk)$", r"(?:\b(?:melke?|milk|mjölk)[-\s]*sjokolade|\b(?:chocolate|hemp|potato)[-\s]*milk\b|\b(?:havre|oat|soya?|soy|mandel|almond|kokos|coconut|ris|rice|ert|pea|hamp|hemp|potet|potato)[-\s]*(?:melk|milk|mjölk)\b)"),
        (r"^(?:hvitløk|garlic|vitlök)$", rf"(?:\b{pressed_garlic_form}\b|\b(?:hvitløk|garlic|vitlök)s?\s*[,/-]?\s*(?:pulver|powder|paste|puré|puree|saus|sauce|brød|bread|knust|crushed|hakket|minced|aioli|dressing|olje|oil)\b|\b(?:knust|crushed|hakket|minced)\s+(?:hvitløk|garlic|vitlök)\b)"),
        (r"^(?:tomat|tomato|tomater|tomatoes)$", r"\b(?:tomat|tomato)\w*\s*[,/-]?\s*(?:saus|sauce|puré|puree|paste|suppe|soup|ketchup|chutney|juice|jus|pesto|salsa)\b"),
    )
    if any(re.fullmatch(base, wanted) and re.search(compound, offered)
           for base, compound in bare_compound_guards):
        return True
    # Exact-ref selection can resolve arbitrary retailer brand/origin/packaging
    # prose around a recognizable identity. It cannot turn a prepared product
    # or a non-food use of that word back into the requested staple.
    wanted_forms, wanted_identity = _prepared_signature(wanted)
    offered_forms, offered_identity = _prepared_signature(offered)
    nonfood_context = re.compile(
        r"\b(?:body|kropps|cosmetic|kosmetisk|lotion|shampoo|sjampo|soap|såpe)\b"
    )
    if (
        (
            (wanted_forms or offered_forms)
            and (
                wanted_forms != offered_forms
                or bool(wanted_identity and wanted_identity != offered_identity)
            )
        )
        or nonfood_context.search(offered)
    ):
        return True
    for axis in (
        "state", "skin", "salt", "coriander_form", "chili_form",
        "sugar_form", "flour_leavening", "milk_type", "meat_form",
        "cut", "produce_form", "grain_grade", "root_variant",
        "fruit_filling", "treatment", "bone",
    ):
        required = wanted_features[axis]
        if required is not None and offered_features[axis] != required:
            return True
    if not wanted_features["dietary"].issubset(offered_features["dietary"]):
        return True
    wanted_broccoli = re.search(r"\b(?:brokkoli|broccoli)\b", wanted)
    wanted_sprouts = re.search(r"\b(?:spire|spirer|sprout|sprouts)\b", wanted)
    offered_broccoli_sprouts = re.search(
        r"\b(?:(?:brokkoli|broccoli)\s*(?:spire|spirer|sprout|sprouts)|"
        r"(?:spire|spirer|sprout|sprouts)\s*(?:av|of)?\s*(?:brokkoli|broccoli))\b", offered
    )
    if wanted_broccoli and not wanted_sprouts and offered_broccoli_sprouts:
        return True
    species = {
        "torsk": r"\b(?:torsk|cod)\w*", "laks": r"\b(?:laks|salmon)\w*",
        "sei": r"\b(?:sei|saithe)\w*", "ørret": r"\b(?:ørret|trout)\w*",
        "hyse": r"\b(?:hyse|haddock)\w*", "makrell": r"\b(?:makrell|mackerel)\w*",
    }
    wanted_species = {key for key, pattern in species.items() if re.search(pattern, wanted)}
    offered_species = {key for key, pattern in species.items() if re.search(pattern, offered)}
    if wanted_species and offered_species and wanted_species != offered_species:
        return True
    if wanted_species and not offered_species and re.search(r"\b(?:fiske?|fish)\s*(?:filet|fillet)\b", offered):
        return True
    legumes = {
        "chickpea": r"\b(?:kikert|kikerter|chickpea|chickpeas)\b",
        "white_bean": r"\b(?:hvite?\s+bønner?|white\s+beans?)\b",
        "black_bean": r"\b(?:(?:svarte?|sorte?)\s+bønner?|black\s+beans?)\b",
        "kidney_bean": r"\b(?:kidneybønner?|kidney\s+beans?)\b",
        "lentil": r"\b(?:linse|linser|lentil|lentils)\b",
    }
    wanted_legume = {key for key, pattern in legumes.items() if re.search(pattern, wanted)}
    offered_legume = {key for key, pattern in legumes.items() if re.search(pattern, offered)}
    if wanted_legume and offered_legume and wanted_legume != offered_legume:
        return True
    if re.search(r"\b(?:filet|loin)\w*", wanted) and re.search(
        r"\b(?:burger|glaze|krydder|sprøbakt|panert)\w*", offered
    ):
        return True
    contradictions = (
        (r"\b(?:maisstivelse|cornstarch|cornflour|majsstärkelse)\b", r"\b(?:maismel|corn flour|majsmjöl)\b"),
        (r"\b(?:maismel|corn flour|majsmjöl)\b", r"\b(?:maisstivelse|cornstarch|cornflour|majsstärkelse)\b"),
        (r"\b(?:korianderblader?|coriander leaves|korianderblad)\b", r"\b(?:korianderfrø|coriander seeds?|korianderfrön|malt koriander|ground coriander)\b"),
        (r"\b(?:korianderfrø|coriander seeds?|korianderfrön)\b", r"\b(?:korianderblader?|coriander leaves|korianderblad|malt koriander|ground coriander)\b"),
        (r"\b(?:malt koriander|ground coriander)\b", r"\b(?:korianderblader?|coriander leaves|korianderblad|korianderfrø|coriander seeds?|korianderfrön)\b"),
        (r"\b(?:finkornet sukker|caster sugar|strösocker)\b", r"\b(?:melis|icing sugar|florsocker)\b"),
        (r"\b(?:melis|icing sugar|florsocker)\b", r"\b(?:finkornet sukker|caster sugar|strösocker)\b"),
        (r"\b(?:selvhevende|self[- ]rais(?:ing|ed)|self[- ]rising|självjäsande)\b", r"\b(?:plain|vanlig)\s+(?:hvetemel|flour|vetemjöl)\b"),
        (r"\b(?:britisk fruktfyll|mincemeat-frukt|fruit mincemeat)\b", r"\b(?:kjøtt\w*|kött\w*|meat|minced meat|köttfärs)\b"),
        (r"\b(?:fersk|fresh)\b", r"(?:tørr|tørket|fryst|frossen|dry|dried|frozen)"),
        (r"(?:tørr|tørket|fryst|frossen|dry|dried|frozen)", r"\b(?:fersk|fresh)\b"),
        (r"\b(?:uten skinn|skinnfri|skinless)\b", r"\b(?:med skinn|skin[- ]on|with skin)\b"),
        (r"\b(?:med skinn|skin[- ]on|with skin)\b", r"\b(?:uten skinn|skinnfri|skinless)\b"),
    )
    if any(re.search(left, wanted) and re.search(right, offered) for left, right in contradictions):
        return True
    wanted_unsalted = bool(re.search(r"\b(?:usaltet|unsalted|osaltat)\b", wanted))
    offered_unsalted = bool(re.search(r"\b(?:usaltet|unsalted|osaltat)\b", offered))
    wanted_salted = not wanted_unsalted and bool(re.search(r"(?:lett)?saltet|salted|saltat", wanted))
    offered_salted = not offered_unsalted and bool(re.search(r"(?:lett)?saltet|salted|saltat", offered))
    if (wanted_unsalted and offered_salted) or (wanted_salted and offered_unsalted):
        return True
    raising = r"\b(?:selvhevende|self[- ]rais(?:ing|ed)|self[- ]rising|självjäsande)\b"
    if re.search(raising, wanted) and re.search(r"\b(?:hvetemel|flour|vetemjöl)\b", offered) and not re.search(raising, offered):
        return True
    wanted_minimum_percent = re.search(r"(?:minst|at least)\s*(\d+(?:[.,]\d+)?)\s*%", wanted)
    wanted_percent = re.search(r"(\d+(?:[.,]\d+)?)\s*%\s*(?:fett|fat)?", wanted)
    offered_percent = re.search(r"(\d+(?:[.,]\d+)?)\s*%", offered)
    wanted_explicit_fat = wanted_percent and (
        re.search(r"%\s*(?:fett|fat)\b", wanted)
        or re.search(r"(?:fløte|cream|grädde|yoghurt|yogurt|melk|milk)", wanted)
    )
    if wanted_explicit_fat:
        wanted_fat = float(wanted_percent.group(1).replace(",", "."))
        offered_fat = float(offered_percent.group(1).replace(",", ".")) if offered_percent else None
        if offered_fat is None or (
            offered_fat < wanted_fat if wanted_minimum_percent else offered_fat != wanted_fat
        ):
            return True
    meat_species = {
        "beef": r"\b(?:storfe|okse|beef|nöt)\b",
        "pork": r"\b(?:svin|pork|fläsk)\b",
        "chicken": r"\b(?:kylling|chicken|kyckling)\b",
        "turkey": r"\b(?:kalkun|turkey)\b",
    }
    wanted_meat = {key for key, pattern in meat_species.items() if re.search(pattern, wanted)}
    offered_meat = {key for key, pattern in meat_species.items() if re.search(pattern, offered)}
    if wanted_meat and offered_meat and wanted_meat != offered_meat:
        return True
    if wanted_meat and not offered_meat and re.search(r"\b(?:kjøttdeig|farse|köttfärs|mince|minced meat)\b", offered):
        return True
    return False


def semantic_product_conflict(requirement: Mapping[str, Any], product: Mapping[str, Any]) -> bool:
    """Reject explicit identity, form and variant contradictions fail-closed."""
    return _semantic_product_conflict(requirement, product)


def _ordinary_retailer_identity_uncertainty(
    requirement: Mapping[str, Any], product: Mapping[str, Any],
) -> bool:
    """Recognize identity plus retail metadata without interpreting food prose."""
    if _semantic_product_conflict(
        requirement, product,
        exact_retailer_identity_approved=True,
        reviewed_prepared_title_approved=True,
    ):
        return False
    if _reviewed_exact_prepared_title_match(requirement, product):
        return True
    wanted = str(requirement.get("item") or "").casefold()
    title = str(product.get("name") or "")
    identity_words = {
        r"(?:smør|butter)": r"(?:[a-zæøåöä]*smør|butter)",
        r"(?:mel|hvetemel|flour|vetemjöl)": r"(?:mel|hvetemel|flour|vetemjöl)",
        r"salt": r"[a-zæøåöä]*salt",
        r"(?:ris|rice)": r"(?:[a-zæøåöä]*ris|rice)",
        r"(?:melk|milk|mjölk)": r"(?:[a-zæøåöä]*melk|milk|mjölk)",
        r"(?:hvitløk|garlic|vitlök)": r"(?:hvitløk|garlic|vitlök)",
        r"(?:tomat|tomato|tomater|tomatoes)": r"(?:[a-zæøåöä]*tomat(?:er)?|tomatoes?)",
    }
    identity_pattern = next((
        pattern for base, pattern in identity_words.items()
        if re.fullmatch(base, wanted)
    ), None)
    if identity_pattern is None:
        return False
    word_matches = list(re.finditer(r"[A-Za-zÆØÅæøåÖÄöä]+", title))
    identity_indexes = {
        index for index, match in enumerate(word_matches)
        if re.fullmatch(identity_pattern, match.group(0).casefold())
    }
    if not identity_indexes:
        return False
    allowed_lower_metadata = {
        "fersk", "ferske", "fresh", "økologisk", "økologiske", "organic",
        "norsk", "norske", "klasse", "klase", "løsvekt", "siktet",
        "fint", "grovt", "lett", "hel", "med", "jod", "stk", "pk",
        "pakke", "kg", "g", "l", "ml", "cl", "vår", "laveste", "pris",
        "norge", "norway", "nederland", "netherlands", "spania", "spain",
        "kina", "china",
    }
    display = product.get("display")
    brand = display.get("brand") if isinstance(display, Mapping) else None
    brand_tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-zÆØÅæøåÖÄöä]+", brand or "")
    }
    for index, match in enumerate(word_matches):
        if index in identity_indexes:
            continue
        token = match.group(0)
        normalized = token.casefold()
        if (
            normalized in allowed_lower_metadata
            or normalized in brand_tokens
            or "gartneri" in normalized
        ):
            continue
        return False
    metadata_evidence = bool(
        re.search(r"\d+(?:[.,-]\d+)?\s*(?:kg|g|l|ml|cl|stk|pk|%)\b", title, re.I)
        or re.search(r"\b(?:vår\s+laveste\s+pris|økologisk\w*|organic|klasse|klase|løsvekt)\b", title, re.I)
        or re.search(r"\b(?:norge|norway|nederland|netherlands|spania|spain|kina|china)\b", title, re.I)
        or re.search(r"gartneri", title, re.I)
        or "/" in title
    )
    return metadata_evidence


def _authorized_semantic_difference(
    requirement: Mapping[str, Any], product: Mapping[str, Any]
) -> list[str] | None:
    """Return the narrow title-level differences a current user may authorize.

    Identity, form, explicit contradictory state, dietary and species checks
    remain in ``semantic_product_conflict`` and cannot be bypassed here.
    """
    wanted = str(requirement.get("item") or "").casefold()
    offered = str(product.get("name") or "").casefold()
    if not wanted or not offered:
        return None
    wanted_features = _semantic_features(wanted)
    offered_features = _semantic_features(offered)
    stripped = wanted
    differences: list[str] = []
    state_qualifiers = {
        "fresh": (r"\b(?:fersk(?:e)?|färsk(?:a)?|fresh)\b", "fresh_not_in_product_title"),
        "frozen": (r"\b(?:fryst|frossen|frysta|frozen)\b", "frozen_not_in_product_title"),
        "dried": (r"\b(?:tørr|tørket|torkad|dry|dried)\b", "dried_not_in_product_title"),
    }
    wanted_state = wanted_features["state"]
    if wanted_state in state_qualifiers and offered_features["state"] is None:
        pattern, difference = state_qualifiers[wanted_state]
        stripped = re.sub(pattern, " ", stripped)
        differences.append(difference)
    if wanted_features["treatment"] == "canned" and offered_features["treatment"] is None:
        stripped = re.sub(r"\b(?:hermetisk|hermetiske|canned|tinned)\b", " ", stripped)
        differences.append("canned_not_in_product_title")
    if wanted_features["produce_form"] == "minced" and offered_features["produce_form"] is None:
        stripped = re.sub(
            r"\b(?:hakket|finhakket|minced|chopped|finely\s+chopped)\b", " ", stripped,
        )
        differences.append("preparation_not_in_product_title")

    wanted_percent = re.search(r"(\d+(?:[.,]\d+)?)\s*%", wanted)
    offered_percent = re.search(r"(\d+(?:[.,]\d+)?)\s*%", offered)
    dairy_classes = {
        "sour_cream": r"\b(?:rømme|lettrømme|seterrømme|sour\s+cream)\b",
        "cream": r"\b(?:fløte|cream|grädde)\b",
        "milk": r"\b(?:melk|milk|mjölk)\b",
        "yogurt": r"\b(?:yoghurt|yogurt)\b",
    }
    wanted_dairy = next((name for name, pattern in dairy_classes.items() if re.search(pattern, wanted)), None)
    offered_dairy = next((name for name, pattern in dairy_classes.items() if re.search(pattern, offered)), None)
    if wanted_percent and offered_percent and wanted_dairy and wanted_dairy == offered_dairy:
        wanted_fat = float(wanted_percent.group(1).replace(",", "."))
        offered_fat = float(offered_percent.group(1).replace(",", "."))
        if wanted_fat != offered_fat and abs(wanted_fat - offered_fat) <= 2:
            stripped = re.sub(r"(?:minst|at\s+least)?\s*\d+(?:[.,]\d+)?\s*%\s*(?:fett|fat)?", " ", stripped)
            differences.append("nearby_dairy_fat_percentage")
    if not differences:
        return None
    def title_tokens(value: str) -> list[str]:
        tokens = re.findall(r"[a-zæøåöä]+|\d+(?:[.,]\d+)?|%", value)
        metadata = {
            "%", "g", "kg", "ml", "l", "cl", "stk", "pk", "pakke",
            "økologisk", "økologiske", "organic", "norsk", "norske",
        }
        return [token for token in tokens if token not in metadata and not token[0].isdigit()]

    def product_title_tokens() -> list[str]:
        tokens = title_tokens(offered)
        display = product.get("display")
        brand = display.get("brand") if isinstance(display, Mapping) else None
        brand_tokens = title_tokens(brand.casefold()) if isinstance(brand, str) else []
        # Brand metadata is presentation data, so only the exact audited
        # prefixes needed by observed safe candidates may be ignored here.
        # An arbitrary "brand" such as Chili or Hvitløk must remain part of
        # the semantic title and fail the whole-title comparison below.
        allowed_brand_prefixes = {("r",), ("kolonihagen",), ("tine",)}
        if (
            tuple(brand_tokens) in allowed_brand_prefixes
            and tokens[:len(brand_tokens)] == brand_tokens
        ):
            return tokens[len(brand_tokens):]
        return tokens

    # Fail closed on every residual title token. This makes the authority about
    # exactly one omitted qualifier or nearby fat value, never a prepared,
    # flavoured, compound or allergen-bearing addition.
    if any(name in differences for name in (
        "fresh_not_in_product_title", "frozen_not_in_product_title",
        "dried_not_in_product_title", "canned_not_in_product_title",
        "preparation_not_in_product_title",
    )):
        offered_title = product_title_tokens()
        wanted_title = title_tokens(stripped)
        offered_identity = _aggregation_identity(" ".join(offered_title))
        wanted_identity = _aggregation_identity(" ".join(wanted_title))
        if offered_title != wanted_title and (
            offered_identity is None or offered_identity != wanted_identity
        ):
            return None
    if "nearby_dairy_fat_percentage" in differences:
        allowed_dairy_titles = {
            "sour_cream": {("rømme",), ("lettrømme",), ("seterrømme",), ("sour", "cream")},
            "cream": {("fløte",), ("kremfløte",), ("matfløte",), ("vispgrädde",), ("grädde",), ("cream",)},
            "milk": {("melk",), ("lettmelk",), ("helmelk",), ("skummet", "melk"), ("milk",), ("mjölk",)},
            "yogurt": {("yoghurt",), ("yogurt",)},
        }
        dairy_class = wanted_dairy
        offered_title = product_title_tokens()
        if offered_title and offered_title[0] == "tine":
            offered_title = offered_title[1:]
        if tuple(offered_title) not in allowed_dairy_titles[dairy_class]:
            return None
        wanted_title = title_tokens(stripped)
        # The one reviewed subtype substitution is ordinary rømme to
        # lettrømme. Every other residual title, including flavors and named
        # dairy subtypes, must remain exact after removing the fat percentage.
        if wanted_title != offered_title and not (
            wanted_title == ["rømme"] and offered_title == ["lettrømme"]
        ):
            return None
    stripped_requirement = {**requirement, "item": " ".join(stripped.split())}
    return differences if not semantic_product_conflict(stripped_requirement, product) else None


def _ordinary_qualifier_omission(
    requirement: Mapping[str, Any], differences: list[str],
) -> bool:
    """Allow only reviewed, identity-specific title omissions without authority."""
    wanted = str(requirement.get("item") or "").casefold()
    reviewed = {
        "fresh_not_in_product_title": r"\b(?:brokkoli|broccoli)\b",
        "frozen_not_in_product_title": r"\b(?:rosenkål|brysselkål|brussels\s+sprouts?)\b",
        "dried_not_in_product_title": r"\boregano\b",
        "canned_not_in_product_title": r"\b(?:sorte\s+bønner|black\s+beans?)\b",
        "preparation_not_in_product_title": r"\b(?:gul\s+løk|løk|onions?)\b",
    }
    return bool(differences) and all(
        difference in reviewed and re.search(reviewed[difference], wanted)
        for difference in differences
    )


def _positive_fraction(value: Any) -> Fraction | None:
    try:
        return read_quantity(value, legacy_float=True)
    except ValueError:
        return None


def _normalized_unit(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def normalize_available_ingredients(value: Any) -> list[dict[str, Any]]:
    """An explicit planning-request assertion, never an inferred inventory."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise HouseholdError("available_ingredients must contain at most 32 items")
    result, seen = [], set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) - {"item", "quantity", "unit", "use_first"}:
            raise HouseholdError("available ingredient fields are invalid")
        identity = _identity(raw.get("item"))
        if identity is None or identity in seen:
            raise HouseholdError("available ingredients need distinct exact item names")
        seen.add(identity)
        unit = raw.get("unit")
        if unit is not None and (not isinstance(unit, str) or not unit.strip() or len(unit) > 50):
            raise HouseholdError("available ingredient unit must be bounded text")
        quantity = raw.get("quantity")
        if quantity is not None:
            quantity = _positive_fraction(quantity)
            if quantity is None:
                raise HouseholdError("available ingredient quantity must be exact and positive")
        use_first = raw.get("use_first", False)
        if not isinstance(use_first, bool):
            raise HouseholdError("available ingredient use_first must be true or false")
        result.append({"item": " ".join(unicodedata.normalize("NFC", raw["item"]).split()),
                       "quantity": _fraction_json(quantity) if quantity is not None else None,
                       "unit": _normalized_unit(unit) or None, "use_first": use_first})
    return sorted(result, key=lambda item: (not item["use_first"], _identity(item["item"])))


def available_ingredient_matches(recipe, available):
    """Exact loaded ingredient names only; no substitutions or coverage claims."""
    identities = {_identity(item.get("item")) for item in recipe.get("ingredients", [])
                  if isinstance(item, Mapping) and not item.get("optional")}
    return [deepcopy(item) for item in available if _identity(item["item"]) in identities]


def _legacy_scalable(recipe: Mapping[str, Any], index: int, requirement: Mapping[str, Any]) -> bool:
    """Recover only an omitted flag from the same frozen scaled ingredient."""

    if "scalable" in requirement:
        return False
    ingredients = recipe.get("ingredients")
    if not isinstance(ingredients, list) or index >= len(ingredients):
        return False
    ingredient = ingredients[index]
    if not isinstance(ingredient, Mapping) or ingredient.get("scalable") is not True:
        return False
    return (
        _identity(ingredient.get("item")) == _identity(requirement.get("item"))
        and _identity(ingredient.get("item")) is not None
        and _positive_fraction(ingredient.get("quantity")) == _positive_fraction(requirement.get("quantity"))
        and _positive_fraction(ingredient.get("quantity")) is not None
        and _normalized_unit(ingredient.get("unit")) == _normalized_unit(requirement.get("unit"))
        and _normalized_unit(ingredient.get("unit")) != ""
        and ingredient.get("optional", False) is requirement.get("optional", False)
        and ingredient.get("pantry", False) is requirement.get("pantry", False)
    )


def _fraction_json(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _read_fraction(value: Any, *, positive: bool = False) -> Fraction:
    if not isinstance(value, Mapping) or set(value) != {"numerator", "denominator"}:
        raise HouseholdError("product plan fraction is invalid")
    numerator = value.get("numerator")
    denominator = value.get("denominator")
    if (
        isinstance(numerator, bool) or not isinstance(numerator, int)
        or isinstance(denominator, bool) or not isinstance(denominator, int)
        or denominator < 1 or (positive and numerator < 1) or (not positive and numerator < 0)
    ):
        raise HouseholdError("product plan fraction is invalid")
    return Fraction(numerator, denominator)


def menu_requirements(menu: Any, *, maximum: int | None = MAX_REQUIREMENTS, ingredient_decisions: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Aggregate only exact compatible recipe requirements."""

    if not isinstance(menu, Mapping):
        raise HouseholdError("product preparation needs one exact menu")
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    unresolved = []
    available = menu.get("available_ingredients") or []
    stock: dict[str, dict[str, Any]] = {}
    for available_item in available:
        if not isinstance(available_item, Mapping):
            continue
        stock_identity = _aggregation_identity(available_item.get("item"))
        stock_quantity = _positive_fraction(available_item.get("quantity"))
        stock_conversion = _UNITS.get(_normalized_unit(available_item.get("unit")))
        if stock_identity is None or stock_quantity is None or stock_conversion is None:
            continue
        stock_unit, stock_factor = stock_conversion
        amount = stock_quantity * stock_factor
        existing = stock.get(stock_identity)
        if existing is not None and existing["unit"] != stock_unit:
            raise HouseholdError("available ingredient aliases use incompatible units")
        if existing is None:
            stock[stock_identity] = {"quantity": _fraction_json(amount), "unit": stock_unit}
        else:
            existing["quantity"] = _fraction_json(_read_fraction(existing["quantity"]) + amount)
    explicit_stock = set()
    decisions = ingredient_decisions or []
    if not isinstance(decisions, list) or len(decisions) > 512:
        raise HouseholdError("ingredient_decisions must be a bounded list")
    by_position = {}
    for decision in decisions:
        if not isinstance(decision, Mapping) or not set(decision).issubset({"source", "action", "quantity", "unit"}):
            raise HouseholdError("ingredient decision fields are invalid")
        position = decision.get("source")
        if not isinstance(position, Mapping) or set(position) != {"collection", "recipe_index", "ingredient_index"} or position["collection"] not in {"dishes", "salads"} or any(type(position[k]) is not int or position[k] < 0 for k in ("recipe_index", "ingredient_index")):
            raise HouseholdError("ingredient decision requires an exact returned source position")
        key = canonical(position)
        if key in by_position or decision.get("action") not in {"have_all", "have_quantity", "include", "omit"}:
            raise HouseholdError("ingredient decisions must be unique explicit choices")
        if decision["action"] != "have_quantity" and ("quantity" in decision or "unit" in decision):
            raise HouseholdError("only have_quantity accepts a quantity and unit")
        by_position[key] = decision
    used = set()
    for collection in ("dishes", "salads"):
        recipes = menu.get(collection)
        if not isinstance(recipes, list):
            raise HouseholdError("menu recipes are invalid")
        for recipe_index, recipe in enumerate(recipes):
            if not isinstance(recipe, Mapping) or not isinstance(recipe.get("shopping_requirements"), list):
                raise HouseholdError("menu recipe shopping requirements are invalid")
            for ingredient_index, raw in enumerate(recipe["shopping_requirements"]):
                position = {
                    "collection": collection,
                    "recipe_index": recipe_index,
                    "ingredient_index": ingredient_index,
                }
                if not isinstance(raw, Mapping):
                    unresolved.append({**position, "reason": "invalid_requirement"})
                    continue
                item = raw.get("item")
                identity = _identity(item)
                quantity = _positive_fraction(raw.get("quantity"))
                unit = _normalized_unit(raw.get("unit"))
                conversion = _UNITS.get(unit)
                scalable = raw.get("scalable") is True or _legacy_scalable(
                    recipe, ingredient_index, raw
                )
                decision = by_position.get(canonical(position))
                action = decision.get("action") if decision else None
                if decision:
                    used.add(canonical(position))
                    explicit_stock.add(_aggregation_identity(identity))
                # Plain cooking water is a preparation input, not inferred stock.
                # An explicit include still permits a requested water purchase.
                if identity in {'water', 'vann', 'tap water', 'springvann', 'kranvann'} and action != 'include':
                    continue
                if action == "omit" and raw.get("optional") is not True:
                    raise HouseholdError("only an optional ingredient can be omitted")
                if action == "omit" or (action == "have_all" and (quantity is None or conversion is None or identity is None or not scalable)):
                    continue
                reason = None
                if raw.get("unresolved_reason"):
                    reason = str(raw["unresolved_reason"])
                elif not scalable:
                    reason = "non_scalable_quantity_unresolved"
                elif identity is None:
                    reason = "ingredient_identity_unresolved"
                elif quantity is None or conversion is None:
                    reason = "quantity_or_unit_unresolved"
                if reason is not None:
                    unresolved.append({
                        **position, "item": str(item or "")[:300],
                        "quantity": raw.get("quantity"), "unit": raw.get("unit"), "reason": reason,
                    })
                    continue
                canonical_unit, factor = conversion
                exact_quantity = quantity * factor
                gross_quantity = exact_quantity
                pantry_quantity = Fraction(0)
                if action == "have_all":
                    pantry_quantity = exact_quantity
                    exact_quantity = Fraction(0)
                if action == "have_quantity":
                    available = _positive_fraction(decision.get("quantity"))
                    available_unit = _UNITS.get(_normalized_unit(decision.get("unit")))
                    if available is None or available_unit is None or available_unit[0] != canonical_unit:
                        raise HouseholdError("pantry quantity must have an exact compatible unit")
                    pantry_quantity = min(exact_quantity, available * available_unit[1])
                    exact_quantity -= pantry_quantity
                aggregate_identity = _aggregation_identity(identity)
                key = (aggregate_identity, canonical_unit)
                requirement = aggregated.setdefault(key, {
                    "identity": identity,
                    "item": " ".join(unicodedata.normalize("NFC", str(item)).split()),
                    "unit": canonical_unit,
                    "quantity_fraction": Fraction(0),
                    "gross_fraction": Fraction(0),
                    "pantry_fraction": Fraction(0),
                    "sources": [],
                    "product_hints": [],
                })
                if requirement["identity"] != identity:
                    requirement["identity"] = aggregate_identity
                    requirement["item"] = aggregate_identity
                requirement["quantity_fraction"] += exact_quantity
                requirement["gross_fraction"] += gross_quantity
                requirement["pantry_fraction"] += pantry_quantity
                requirement["sources"].append(position)
                hint = raw.get("_store_product_hint")
                if isinstance(hint, Mapping) and not any(
                    canonical(existing) == canonical(hint)
                    for existing in requirement["product_hints"]
                ):
                    requirement["product_hints"].append(deepcopy(dict(hint)))
    requirements = []
    for (aggregate_identity, unit), value in sorted(aggregated.items(), key=lambda pair: (pair[0][0].encode("utf-8"), pair[0][1])):
        # Explicit source-position decisions replace this ingredient's request
        # stock, rather than counting the same household assertion a second time.
        supplied = stock.get(aggregate_identity)
        if supplied and aggregate_identity not in explicit_stock:
            amount = _positive_fraction(supplied.get("quantity"))
            stock_unit = _UNITS.get(_normalized_unit(supplied.get("unit")))
            if amount is not None and stock_unit is not None and stock_unit[0] == unit:
                used_stock = min(value["quantity_fraction"], amount * stock_unit[1])
                value["quantity_fraction"] -= used_stock
                value["pantry_fraction"] += used_stock
        if value["quantity_fraction"] == 0:
            continue
        requirement_id = "req:" + hashlib.sha256(canonical({"identity": aggregate_identity, "unit": unit}).encode()).hexdigest()[:24]
        requirements.append({
            "requirement_id": requirement_id,
            "identity": value["identity"],
            "item": value["item"],
            "search": value["item"],
            "quantity": _fraction_json(value["quantity_fraction"]),
            "gross_quantity": _fraction_json(value["gross_fraction"]),
            "confirmed_pantry_quantity": _fraction_json(value["pantry_fraction"]),
            "unit": unit,
            "sources": value["sources"],
            **({"product_hints": value["product_hints"]} if value["product_hints"] else {}),
        })
    if set(by_position) != used:
        raise HouseholdError("ingredient decision does not name a source in this exact menu")
    if maximum is not None and len(requirements) + len(unresolved) > maximum:
        raise HouseholdError(f"product preparation supports at most {maximum} menu requirements")
    return requirements, unresolved


def normalize_approvals(value: Any, requirement_ids: set[str]) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, list) or len(value) > MAX_ALTERNATIVE_REQUIREMENTS:
        raise HouseholdError("candidate_approvals must be a bounded list")
    approvals = {}
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw).difference({
            "requirement_id", "candidate_refs", "max_excess", "search_query",
            "package_count", "quantity_basis", "semantic_authorization", "shared_package",
        }):
            raise HouseholdError("candidate approval has unknown fields")
        requirement_id = raw.get("requirement_id")
        refs = raw.get("candidate_refs")
        if requirement_id not in requirement_ids:
            raise HouseholdError("candidate approval requirement_id is not in this menu")
        if requirement_id in approvals:
            raise HouseholdError("candidate approval requirement_id is duplicated")
        if (
            not isinstance(refs, list) or not 1 <= len(refs) <= MAX_CANDIDATES_PER_REQUIREMENT
            or any(not _valid_product_ref(ref) for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise HouseholdError("candidate approval needs one to five exact product refs")
        approval: dict[str, Any] = {
            "requirement_id": requirement_id,
            "candidate_refs": sorted(refs, key=_ref_sort_key),
            "source": "selected_exact_candidate_scope",
        }
        if raw.get("search_query") is not None:
            query = raw["search_query"]
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
                raise HouseholdError("search_query must be a short ingredient search")
            approval["search_query"] = query.strip()
        if "package_count" in raw or "quantity_basis" in raw:
            count, basis = raw.get("package_count"), raw.get("quantity_basis")
            if len(refs) != 1 or type(count) is not int or not 1 <= count <= MAX_PACKAGES_PER_REQUIREMENT or not isinstance(basis, str) or not 1 <= len(basis.strip()) <= 600:
                raise HouseholdError("practical package choice needs one exact candidate, bounded package_count and quantity_basis")
            if raw.get("max_excess") is not None:
                raise HouseholdError("practical package coverage cannot claim an exact maximum excess")
            approval.update(package_count=count, quantity_basis=basis.strip())
        if raw.get("max_excess") is not None:
            maximum = _read_fraction(raw["max_excess"])
            if maximum > 100:
                raise HouseholdError("candidate approval max_excess is too large")
            approval["max_excess"] = _fraction_json(maximum)
        if raw.get("semantic_authorization") is not None:
            authority = raw["semantic_authorization"]
            if (
                not isinstance(authority, Mapping)
                or set(authority) != {"candidate_ref", "authorized_by", "reason"}
                or authority.get("authorized_by") != "current_user"
                or authority.get("candidate_ref") not in refs
                or not isinstance(authority.get("reason"), str)
                or not 1 <= len(authority["reason"].strip()) <= 600
            ):
                raise HouseholdError(
                    "semantic_authorization needs one selected candidate_ref, authorized_by=current_user and a bounded reason"
                )
            approval["semantic_authorization"] = {
                "candidate_ref": authority["candidate_ref"],
                "authorized_by": "current_user",
                "reason": authority["reason"].strip(),
            }
        if raw.get("shared_package") is not None:
            shared = raw["shared_package"]
            if (
                not isinstance(shared, Mapping)
                or set(shared) != {"requirement_ids", "package_count", "quantity_basis", "authorized_by"}
                or shared.get("authorized_by") != "current_user"
                or not isinstance(shared.get("requirement_ids"), list)
                or not 2 <= len(shared["requirement_ids"]) <= MAX_REQUIREMENTS
                or any(member not in requirement_ids for member in shared["requirement_ids"])
                or len(set(shared["requirement_ids"])) != len(shared["requirement_ids"])
                or requirement_id not in shared["requirement_ids"]
                or len(refs) != 1
                or type(shared.get("package_count")) is not int
                or not 1 <= shared["package_count"] <= MAX_PACKAGES_PER_REQUIREMENT
                or not isinstance(shared.get("quantity_basis"), str)
                or not 1 <= len(shared["quantity_basis"].strip()) <= 600
                or "package_count" in raw or "quantity_basis" in raw or "max_excess" in raw
            ):
                raise HouseholdError(
                    "shared_package needs every exact requirement_id, one candidate, authorized_by=current_user, package_count and quantity_basis"
                )
            approval["shared_package"] = {
                "requirement_ids": sorted(shared["requirement_ids"]),
                "package_count": shared["package_count"],
                "quantity_basis": shared["quantity_basis"].strip(),
                "authorized_by": "current_user",
            }
            # Reuse the established practical-package path, but the allocation
            # is validated and counted atomically below across every member.
            approval["package_count"] = shared["package_count"]
            approval["quantity_basis"] = shared["quantity_basis"].strip()
        approvals[requirement_id] = approval
    shared_members: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for approval in approvals.values():
        shared = approval.get("shared_package")
        if isinstance(shared, Mapping):
            shared_members.setdefault(tuple(shared["requirement_ids"]), []).append(approval)
    for members, group in shared_members.items():
        if len(group) != len(members) or {approval["requirement_id"] for approval in group} != set(members):
            raise HouseholdError("shared_package must be repeated unchanged by every member requirement")
        if len({canonical(approval["shared_package"]) for approval in group}) != 1:
            raise HouseholdError("shared_package member authority differs")
        if len({canonical(approval["candidate_refs"]) for approval in group}) != 1:
            raise HouseholdError("shared_package members must select the same exact candidate")
    return approvals


def _normalize_hard_product_constraints(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value).difference({
        "allergies_or_sensitivities", "avoid",
    }):
        raise HouseholdError("hard product constraints are invalid")
    normalized = {}
    for key in ("allergies_or_sensitivities", "avoid"):
        rules = value.get(key, [])
        if not isinstance(rules, list) or len(rules) > 50:
            raise HouseholdError("hard product constraints are invalid")
        cleaned = []
        for rule in rules:
            if not isinstance(rule, str):
                raise HouseholdError("hard product constraints are invalid")
            text = " ".join(unicodedata.normalize("NFC", rule).split())
            if not text or len(text.encode("utf-8")) > 300:
                raise HouseholdError("hard product constraints are invalid")
            cleaned.append(text)
        if cleaned:
            normalized[key] = sorted(set(cleaned), key=lambda item: item.encode("utf-8"))
    return normalized


def _option_costs(product: Mapping[str, Any], maximum: int) -> list[dict[str, Any] | None]:
    maximum = min(maximum, product.get("package_limit", {}).get("count", maximum))
    options = product.get("purchase_options")
    if not isinstance(options, list) or not options:
        raise HouseholdError("candidate has no purchase options")
    bundles = []
    for index, option in enumerate(options):
        if not isinstance(option, Mapping):
            raise HouseholdError("candidate purchase option is invalid")
        packages = option.get("package_count")
        fields = (option.get("merchandise_ore"), option.get("mandatory_deposit_ore"), option.get("total_payable_ore"))
        if (
            option.get("price_kind") != "exact" or option.get("eligibility") != "confirmed"
            or isinstance(packages, bool) or not isinstance(packages, int) or not 1 <= packages <= 20
            or any(isinstance(amount, bool) or not isinstance(amount, int) or amount < 0 for amount in fields)
            or fields[0] + fields[1] != fields[2]
        ):
            raise HouseholdError("candidate has an inexact or ineligible purchase option")
        bundles.append({
            "option_index": index,
            "package_count": packages,
            "merchandise_ore": fields[0],
            "mandatory_deposit_ore": fields[1],
            "total_payable_ore": fields[2],
            "offer_kind": str(option.get("offer_kind") or "regular"),
        })
    costs: list[dict[str, Any] | None] = [None] * (maximum + 1)
    costs[0] = {"merchandise_ore": 0, "mandatory_deposit_ore": 0, "total_payable_ore": 0, "bundles": []}
    promotion_limit = max(
        (
            bundle["package_count"] for bundle in bundles
            if bundle["offer_kind"] != "regular"
        ),
        default=None,
    )
    for count in range(maximum + 1):
        current = costs[count]
        if current is None:
            continue
        for bundle in bundles:
            if bundle["offer_kind"] != "regular" and any(
                existing["option_index"] == bundle["option_index"]
                for existing in current["bundles"]
            ):
                continue
            target = count + bundle["package_count"]
            if target > maximum or (
                promotion_limit is not None and target > promotion_limit
            ):
                continue
            candidate = {
                "merchandise_ore": current["merchandise_ore"] + bundle["merchandise_ore"],
                "mandatory_deposit_ore": current["mandatory_deposit_ore"] + bundle["mandatory_deposit_ore"],
                "total_payable_ore": current["total_payable_ore"] + bundle["total_payable_ore"],
                "bundles": [*current["bundles"], {
                    "option_index": bundle["option_index"],
                    "package_count": bundle["package_count"],
                    "offer_kind": bundle["offer_kind"],
                }],
            }
            rank = (
                candidate["total_payable_ore"], canonical(candidate["bundles"]),
            )
            previous = costs[target]
            previous_rank = None if previous is None else (
                previous["total_payable_ore"], canonical(previous["bundles"]),
            )
            if previous_rank is None or rank < previous_rank:
                costs[target] = candidate
    return costs


def _package_quantity(package, unit):
    if not isinstance(package, Mapping):
        return None
    if package.get("unit") == unit:
        try:
            return _read_fraction(package.get("quantity"), positive=True)
        except HouseholdError:
            return None
    if unit == "count" and type(package.get("contained_count")) is int and package["contained_count"] > 0:
        return Fraction(package["contained_count"])
    return None


def _select_requirement(
    requirement: Mapping[str, Any], observation: Mapping[str, Any], approval: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None, int]:
    products = observation.get("products")
    if not isinstance(products, list):
        return None, "provider_search_invalid", 0
    by_ref = {
        product.get("product_ref"): product
        for product in products
        if isinstance(product, Mapping) and _valid_product_ref(product.get("product_ref"))
    }
    approved_refs = approval["candidate_refs"]
    missing = [ref for ref in approved_refs if ref not in by_ref]
    if missing:
        return None, "approved_candidate_scope_changed", 0
    approved = [by_ref[ref] for ref in approved_refs]
    required = _read_fraction(requirement["quantity"], positive=True)
    candidates = []
    for product in approved:
        if product.get("availability") != "available":
            return None, "candidate_availability_unresolved", 0
        package = product.get("package")
        package_quantity = _package_quantity(package, requirement["unit"])
        if re.search(r'\b(?:drained|avrent)\b', requirement['item'], re.I):
            package_quantity = None  # Retail net mass is not edible drained mass.
        if package_quantity is None:
            return None, "candidate_package_incompatible", 0
        options = product.get("purchase_options")
        if not isinstance(options, list) or not options:
            return None, "candidate_price_unresolved", 0
        if any(
            not isinstance(option, Mapping)
            or option.get("price_kind") != "exact"
            or option.get("eligibility") != "confirmed"
            or option.get("total_payable_ore") is None
            for option in options
        ):
            return None, "candidate_price_or_eligibility_unresolved", 0
        candidates.append((product, package_quantity))
    if not candidates:
        return None, "candidate_scope_empty", 0
    maximum_bundle = max(
        option["package_count"]
        for product, _quantity in candidates
        for option in product["purchase_options"]
    )
    minimum_quantity = min(quantity for _product, quantity in candidates)
    maximum_packages = math.ceil(required / minimum_quantity) + maximum_bundle - 1
    if maximum_packages > MAX_PACKAGES_PER_REQUIREMENT:
        return None, "package_limit_exceeded", len(candidates)
    costs = [
        _option_costs(product, maximum_packages)
        for product, _quantity in candidates
    ]
    maximum_excess = _read_fraction(approval["max_excess"]) if approval.get("max_excess") is not None else None
    work = 0
    best: tuple[Any, dict[str, Any]] | None = None

    def visit(index: int, quantities: list[int], covered: Fraction, merchandise: int, deposit: int, payable: int, bundles: list[Any]) -> bool:
        nonlocal work, best
        if index == len(candidates):
            work += 1
            if work > MAX_COMBINATIONS:
                return False
            if covered < required:
                return True
            excess = (covered - required) / required
            if maximum_excess is not None and excess > maximum_excess:
                return True
            selected_products = [
                {
                    "product_ref": candidates[position][0]["product_ref"],
                    "name": candidates[position][0]["name"],
                    "dietary_assessments": deepcopy(candidates[position][0].get("dietary_findings", [])),
                    "quantity": count,
                    "purchase_options": bundles[position],
                    "merchandise_ore": sum(
                        candidates[position][0]["purchase_options"][bundle["option_index"]]["merchandise_ore"]
                        for bundle in bundles[position]
                    ),
                    "mandatory_deposit_ore": sum(
                        candidates[position][0]["purchase_options"][bundle["option_index"]]["mandatory_deposit_ore"]
                        for bundle in bundles[position]
                    ),
                    "total_payable_ore": sum(
                        candidates[position][0]["purchase_options"][bundle["option_index"]]["total_payable_ore"]
                        for bundle in bundles[position]
                    ),
                }
                for position, count in enumerate(quantities) if count
            ]
            package_count = sum(quantities)
            stable_refs = [[item["product_ref"], item["quantity"]] for item in selected_products]
            selection = {
                "products": selected_products,
                "coverage": _fraction_json(covered),
                "required": _fraction_json(required),
                "unit": requirement["unit"],
                "excess_score": _fraction_json(excess),
                "package_count": package_count,
                "merchandise_ore": merchandise,
                "mandatory_deposit_ore": deposit,
                "total_payable_ore": payable,
                "tie_break": stable_refs,
            }
            dietary_rank = sum(10 if f['condition'] in {'preference_deviation', 'sensitivity_conflict'} else 1 if f['condition'] == 'unknown' else 0 for product in selected_products for f in product.get('dietary_assessments', []))
            rank = (dietary_rank, payable, excess, package_count, canonical(stable_refs))
            if best is None or rank < best[0]:
                best = (rank, selection)
            return True
        product, package_quantity = candidates[index]
        for count, cost in enumerate(costs[index]):
            if cost is None:
                continue
            if not visit(
                index + 1, [*quantities, count], covered + package_quantity * count,
                merchandise + cost["merchandise_ore"], deposit + cost["mandatory_deposit_ore"],
                payable + cost["total_payable_ore"], [*bundles, cost["bundles"]],
            ):
                return False
        return True

    if not visit(0, [], Fraction(0), 0, 0, 0, []):
        return None, "combination_work_limit_exceeded", len(candidates)
    if best is None:
        return None, "quantity_or_excess_limit_unmet", len(candidates)
    return best[1], None, len(candidates)


def _canonical_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    # Provider order is useful relevance evidence for the caller. Canonical
    # ordering belongs at the digest boundary, not in the returned observation.
    return deepcopy(dict(value))


def _without_presentation(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_presentation(child)
            for key, child in value.items()
            if key not in {"product_plan_digest", "observed_at", "display", "display_ore_per_unit"}
        }
    if isinstance(value, list):
        return [_without_presentation(child) for child in value]
    return value


def product_plan_digest(value: Mapping[str, Any]) -> str:
    authoritative = deepcopy(dict(value))
    requirements = authoritative.get("requirements")
    if isinstance(requirements, list):
        for requirement in requirements:
            if isinstance(requirement, dict) and requirement.get("status") == "selected":
                # Bind the chosen candidate's observed facts, but not unrelated
                # ranked results whose membership can change between searches.
                selected_refs = {
                    product.get("product_ref")
                    for product in requirement.get("selection", {}).get("products", [])
                    if isinstance(product, Mapping)
                }
                observation = requirement.get("observation")
                if isinstance(observation, dict):
                    selected = [
                        product for product in observation.get("products", [])
                        if isinstance(product, Mapping) and product.get("product_ref") in selected_refs
                    ]
                    requirement["observation"] = {"products": sorted(
                        selected, key=lambda product: _ref_sort_key(product["product_ref"])
                    )}
                requirement.pop("eligible_candidate_count", None)
                requirement.pop("dietary_assessments", None)
    return hashlib.sha256(canonical(_without_presentation(authoritative)).encode()).hexdigest()


def partial_product_plan_digest(value: Mapping[str, Any]) -> str | None:
    """Bind only selected lines so unrelated unresolved search churn cannot block them."""
    if value.get("budget_status") == "exceeded":
        return None
    selected = []
    for requirement in value.get("requirements", []):
        if not isinstance(requirement, Mapping) or requirement.get("status") != "selected":
            continue
        selected.append({
            key: deepcopy(requirement[key])
            for key in (
                "requirement_id", "identity", "item", "quantity", "unit",
                "candidate_approval", "selection",
            ) if key in requirement
        })
    if not selected:
        return None
    payload = {
        key: deepcopy(value.get(key))
        for key in (
            "product_plan_version", "provider", "binding", "hard_product_constraints",
            "ingredient_decisions", "budget_ore", "price_mode",
        )
    }
    payload["selected_requirements"] = selected
    return hashlib.sha256(canonical(_without_presentation(payload)).encode()).hexdigest()


def _estimated_single_product(requirement, observation, approval):
    """Quantity-ready explicit selection with an honestly non-final price."""
    refs = approval["candidate_refs"]
    if len(refs) != 1:
        return None
    matches = [p for p in observation.get("products", []) if p.get("product_ref") == refs[0]]
    if len(matches) != 1:
        return None
    product = matches[0]
    package = product.get("package")
    options = product.get("purchase_options", [])
    size = _package_quantity(package, requirement["unit"])
    if product.get("availability") != "available" or size is None or len(options) != 1:
        return None
    option = options[0]
    price_kind = option.get("price_kind")
    amount = option.get("merchandise_ore") if price_kind == "exact" else option.get("estimated_merchandise_ore")
    if price_kind not in {"exact", "estimate"} or option.get("eligibility") != "confirmed" or option.get("offer_kind") != "regular" or option.get("package_count") != 1 or type(amount) is not int or amount < 0:
        return None
    needed = _read_fraction(requirement["quantity"], positive=True)
    count = math.ceil(needed / size)
    if not 1 <= count <= min(MAX_PACKAGES_PER_REQUIREMENT, product.get("package_limit", {}).get("count", MAX_PACKAGES_PER_REQUIREMENT)):
        return None
    excess = (count * size - needed) / needed
    quantity_kind = package.get("quantity_kind")
    variable = quantity_kind in {"minimum", "expected"}
    if variable and approval.get("max_excess") is not None:
        return None
    if approval.get("max_excess") is not None and excess > _read_fraction(approval["max_excess"]):
        return None
    merchandise = count * amount
    observed_quantity = _fraction_json(count * size)
    return {"products": [{"product_ref": refs[0], "name": product["name"], "quantity": count,
                          "dietary_assessments": deepcopy(product.get("dietary_findings", [])),
                          "purchase_options": [{
                              "option_index": 0, "package_count": count,
                              "offer_kind": "regular", "price_kind": price_kind,
                          }],
                          "merchandise_ore": merchandise,
                          "price_status": "estimate" if price_kind == "estimate" else "exact",
                          "mandatory_deposit_ore": None, "total_payable_ore": None}],
            "coverage": None if variable else observed_quantity,
            **({"expected_coverage": observed_quantity} if quantity_kind == "expected" else {}),
            **({"minimum_coverage": observed_quantity} if quantity_kind == "minimum" else {}),
            "required": _fraction_json(needed),
            "unit": requirement["unit"], "excess_score": None if variable else _fraction_json(excess), "package_count": count,
            "coverage_status": ("variable_weight_" + package["quantity_kind"]) if variable else "exact",
            "quantity_basis": ("declared_" + package["quantity_kind"] + "_package_weight") if variable else "fixed_package",
            "observed_package": deepcopy(package),
            "merchandise_ore": merchandise, "mandatory_deposit_ore": None, "total_payable_ore": None}


def _form_conflict(requirement, product):
    # Never use a package estimate as a dry/cooked legume substitution.
    dry = r"\b(?:dry|dried|tørre|tørket|tørkede)\b"
    cooked = r"\b(?:cooked|canned|jarred|kokte|ferdigkokte|hermetiske|hermetisk|avrent|drained)\b"
    wanted, offered = str(requirement['item']).casefold(), str(product.get('name', '')).casefold()
    return bool((re.search(dry, wanted) and re.search(cooked, offered)) or
                (re.search(cooked, wanted) and re.search(dry, offered)))


def _practical_packages(requirement, observation, approval, price_mode):
    """Select observed whole packages without claiming a physical conversion."""
    refs = approval['candidate_refs']
    if 'package_count' not in approval or len(refs) != 1:
        return None
    products = [p for p in observation['products'] if p['product_ref'] == refs[0]]
    if len(products) != 1:
        return None
    product = products[0]
    count = approval['package_count']
    package = product.get('package')
    if (product.get('availability') != 'available'
            or _form_conflict(requirement, product)
            or count > product.get('package_limit', {}).get('count', MAX_PACKAGES_PER_REQUIREMENT)):
        return None
    size = _package_quantity(package, requirement['unit'])
    if requirement['unit'] in {'g', 'ml'} and size is not None and count * size < _read_fraction(requirement['quantity'], positive=True):
        return None  # Even the full observed package cannot cover this amount.
    options = product.get('purchase_options', [])
    if not options:
        return None
    try:
        cost = _option_costs(product, count)[count]
    except HouseholdError:
        cost = None
    if cost is None and price_mode == 'estimate' and len(options) == 1:
        option = options[0]
        if (option.get('price_kind') == 'exact' and option.get('eligibility') == 'confirmed'
                and option.get('offer_kind') == 'regular' and option.get('package_count') == 1
                and type(option.get('merchandise_ore')) is int and option['merchandise_ore'] >= 0
                and option.get('mandatory_deposit_ore') is None):
            cost = {'merchandise_ore': count * option['merchandise_ore'],
                    'mandatory_deposit_ore': None, 'total_payable_ore': None, 'bundles': []}
    if cost is None:
        return None
    amounts = {k: cost[k] for k in ('merchandise_ore', 'mandatory_deposit_ore', 'total_payable_ore')}
    return {'products': [{'product_ref': refs[0], 'name': product['name'], 'quantity': count,
                         'dietary_assessments': deepcopy(product.get('dietary_findings', [])),
                         'purchase_options': cost['bundles'], **amounts}],
            'coverage_status': 'practical_estimate', 'quantity_basis': approval['quantity_basis'],
            'observed_package': deepcopy(package),
            'observed_package_description': product.get('display', {}).get('package'), 'coverage': None,
            'required': deepcopy(requirement['quantity']), 'unit': requirement['unit'],
            'excess_score': None, 'package_count': count, **amounts}


def _candidate_diagnostics(requirement, observation, approval):
    """Explain observed size/unit/price blockers without inventing conversions."""
    result = []
    for product in observation["products"]:
        if product["product_ref"] not in approval["candidate_refs"]:
            continue
        package = product.get("package")
        detail = {"product_ref": product["product_ref"], "required_unit": requirement["unit"]}
        if not isinstance(package, Mapping) or (package.get("unit") == requirement["unit"]
                                               and _package_quantity(package, requirement["unit"]) is None):
            detail["reason"] = "package_size_unresolved"
        elif _package_quantity(package, requirement["unit"]) is None:
            detail.update(reason="unit_conversion_required", observed_unit=package.get("unit"))
        elif (product.get("package_limit") and math.ceil(_read_fraction(requirement["quantity"], positive=True)
                / _package_quantity(package, requirement["unit"])) > product["package_limit"]["count"]):
            detail.update(reason="package_limit_exceeded", package_limit=deepcopy(product["package_limit"]))
        elif any(option.get("price_kind") == "exact" and option.get("eligibility") == "confirmed"
                 and option.get("mandatory_deposit_ore") is None
                 for option in product.get("purchase_options", [])):
            detail["reason"] = "deposit_unobserved"
        else:
            continue
        result.append(detail)
    return result


def build_product_plan(
    *, provider: str, binding: Mapping[str, Any], menu: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Any]], candidate_approvals: Any,
    hard_product_constraints: Any = None,
    dietary_profile: Any = None,
    ingredient_decisions: Any = None,
    budget_ore: int | None = None,
    price_mode: str = "exact",
    deadline: float | None = None,
) -> dict[str, Any]:
    if price_mode not in {"exact", "estimate"}:
        raise HouseholdError("price_mode must be exact or estimate")
    if budget_ore is not None and (type(budget_ore) is not int or not 1 <= budget_ore <= 100_000_000):
        raise HouseholdError("product budget_ore must be a positive integer")
    requirements, structural_unresolved = menu_requirements(menu, ingredient_decisions=ingredient_decisions)
    approvals = normalize_approvals(candidate_approvals, {item["requirement_id"] for item in requirements})
    hard_constraints = _normalize_hard_product_constraints(hard_product_constraints)
    ref_owners: dict[str | int, set[str]] = {}
    for requirement_id, approval in approvals.items():
        for reference in approval["candidate_refs"]:
            ref_owners.setdefault(reference, set()).add(requirement_id)
    reused_refs = {
        reference for reference, owners in ref_owners.items() if len(owners) > 1
    }
    shared_groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for approval in approvals.values():
        shared = approval.get("shared_package")
        if not isinstance(shared, Mapping):
            continue
        members = tuple(shared["requirement_ids"])
        shared_groups[members] = deepcopy(dict(shared))
    planned = []
    unresolved = deepcopy(structural_unresolved)
    for requirement in requirements:
        requirement_id = requirement["requirement_id"]
        observation = observations.get(requirement_id)
        item = deepcopy(requirement)
        if not isinstance(observation, Mapping) or observation.get("unavailable_reason"):
            reason = observation["unavailable_reason"] if isinstance(observation, Mapping) else "provider_search_unavailable"
            unresolved.append({"requirement_id": requirement_id, "item": requirement["item"], "reason": reason})
            item["status"] = "needs_input"
            planned.append(item)
            continue
        approval = approvals.get(requirement_id)
        authority = approval.get("semantic_authorization") if approval else None
        authority_ref = authority.get("candidate_ref") if isinstance(authority, Mapping) else None
        semantic_mismatches = {}
        identity_unverified = {}
        authority_differences = None
        for product in observation.get("products", []):
            if not isinstance(product, Mapping):
                continue
            product_ref = product.get("product_ref")
            if product_ref == authority_ref:
                authority_differences = _authorized_semantic_difference(requirement, product)
            exact_candidate_approved = bool(
                approval is not None
                and product_ref in approval["candidate_refs"]
            )
            if _semantic_product_conflict(
                requirement, product,
                reviewed_prepared_title_approved=exact_candidate_approved,
            ):
                exact_identity_only = _ordinary_retailer_identity_uncertainty(
                    requirement, product,
                )
                if exact_identity_only:
                    identity_unverified[product_ref] = [
                        "retailer_title_identity_verified_by_exact_candidate_approval"
                    ]
                else:
                    semantic_mismatches[product_ref] = _authorized_semantic_difference(
                        requirement, product
                    )
        authorized_semantic_ref = (
            authority_ref
            if isinstance(authority, Mapping)
            and authority_differences
            else None
        )
        ordinary_omissions = {
            product_ref: differences
            for product_ref, differences in semantic_mismatches.items()
            if approval is not None
            and product_ref in approval["candidate_refs"]
            and differences
            and _ordinary_qualifier_omission(requirement, differences)
        }
        allowed_semantic_refs = set(ordinary_omissions)
        if authorized_semantic_ref is not None:
            allowed_semantic_refs.add(authorized_semantic_ref)
        excluded_semantic_refs = set(semantic_mismatches) - allowed_semantic_refs
        safe_observation = deepcopy(dict(observation))
        safe_observation["products"] = [
            product for product in safe_observation.get("products", [])
            if product.get("product_ref") not in excluded_semantic_refs
        ]
        if excluded_semantic_refs:
            safe_observation["excluded_candidate_count"] = len(excluded_semantic_refs)
            safe_observation["excluded_candidate_reason"] = "candidate_semantic_mismatch"
        if identity_unverified:
            item["identity_unverified_candidate_refs"] = sorted(
                identity_unverified, key=_ref_sort_key,
            )
        for product in safe_observation.get("products", []):
            product["candidate_approval"] = {
                "requirement_id": requirement_id,
                "candidate_refs": [product["product_ref"]],
                "search_query": product["name"][:150],
            }
        item["observation"] = _canonical_observation(safe_observation)
        if deadline is not None and time.monotonic() >= deadline:
            unresolved.append({"requirement_id": requirement_id, "item": requirement["item"], "reason": "product_planning_deadline"})
            item["status"] = "needs_input"
            planned.append(item)
            continue
        if approval is None:
            unresolved.append({"requirement_id": requirement_id, "item": requirement["item"], "reason": "exact_candidate_scope_needs_selection"})
            item["status"] = "needs_input"
            planned.append(item)
            continue
        item["candidate_approval"] = deepcopy(approval)
        if authorized_semantic_ref is not None:
            item["semantic_authorized_differences"] = deepcopy(
                semantic_mismatches[authorized_semantic_ref]
            )
        selected_ordinary = [
            {"product_ref": product_ref, "differences": deepcopy(differences)}
            for product_ref, differences in sorted(
                {**ordinary_omissions, **identity_unverified}.items(),
                key=lambda item: _ref_sort_key(item[0]),
            )
            if product_ref in approval["candidate_refs"]
        ]
        if authority is not None and authorized_semantic_ref is None:
            unresolved.append({
                "requirement_id": requirement_id,
                "item": requirement["item"],
                "reason": "semantic_authorization_not_applicable",
                "candidate_refs": [authority["candidate_ref"]],
            })
            item["status"] = "needs_input"
            planned.append(item)
            continue
        if excluded_semantic_refs.intersection(approval["candidate_refs"]):
            unresolved.append({
                "requirement_id": requirement_id,
                "item": requirement["item"],
                "reason": "candidate_semantic_mismatch",
                "candidate_refs": sorted(
                    excluded_semantic_refs.intersection(approval["candidate_refs"]),
                    key=_ref_sort_key,
                ),
            })
            item["status"] = "needs_input"
            planned.append(item)
            continue
        from dietary_assessment import assess
        product_findings = {p['product_ref']: assess(dietary_profile or {'diet': hard_constraints}, p) for p in safe_observation['products']}
        item['dietary_assessments'] = [f for values in product_findings.values() for f in values]
        filtered = deepcopy(approval)
        nonfood = {p['product_ref'] for p in safe_observation['products'] if nonfood_candidate(p) or _form_conflict(requirement, p)}
        filtered['candidate_refs'] = [ref for ref in approval['candidate_refs'] if ref not in nonfood and not any(f['blocked'] for f in product_findings.get(ref, []))]
        evaluated_observation = deepcopy(safe_observation)
        for product in evaluated_observation['products']:
            product['dietary_findings'] = product_findings[product['product_ref']]
        selection, reason, eligible_count = _select_requirement(requirement, evaluated_observation, filtered)
        if not filtered['candidate_refs']:
            reason = 'dietary_conflict_no_compatible_candidate'

        if reason == "candidate_price_or_eligibility_unresolved" and price_mode == "estimate":
            estimated = _estimated_single_product(requirement, evaluated_observation, filtered)
            if estimated is not None:
                selection, reason, eligible_count = estimated, None, 1
        if 'package_count' in filtered and filtered['candidate_refs']:
            practical = _practical_packages(requirement, evaluated_observation, filtered, price_mode)
            selection, reason, eligible_count = (practical, None, 1) if practical is not None else (None, 'practical_package_choice_unavailable', 0)
        item["eligible_candidate_count"] = eligible_count
        if reason is not None:
            problem = {"requirement_id": requirement_id, "item": requirement["item"], "reason": reason}
            diagnostics = _candidate_diagnostics(requirement, evaluated_observation, filtered)
            if diagnostics:
                problem["candidate_diagnostics"] = diagnostics
            unresolved.append(problem)
            item["status"] = "needs_input"
        else:
            item["status"] = "selected"
            item["selection"] = selection
            selection["surplus_quantity"] = None if selection["coverage"] is None else _fraction_json(_read_fraction(selection["coverage"]) - _read_fraction(selection["required"]))
            selected_refs = {product["product_ref"] for product in selection.get("products", [])}
            selected_differences = [
                difference for difference in selected_ordinary
                if difference["product_ref"] in selected_refs
            ]
            if selected_differences:
                item["semantic_equivalent_differences"] = selected_differences
        planned.append(item)
    planned_by_id = {item.get("requirement_id"): item for item in planned}
    allocated_refs: set[str | int] = set()
    for members, shared in shared_groups.items():
        rows = [planned_by_id.get(member) for member in members]
        selections = [row.get("selection") if isinstance(row, Mapping) else None for row in rows]
        reference = approvals[members[0]]["candidate_refs"][0]
        valid_group = all(
            isinstance(row, Mapping)
            and row.get("status") == "selected"
            and isinstance(selection, Mapping)
            and len(selection.get("products", [])) == 1
            and selection["products"][0].get("product_ref") == reference
            and selection["products"][0].get("quantity") == shared["package_count"]
            for row, selection in zip(rows, selections)
        )
        valid_group = valid_group and ref_owners.get(reference) == set(members)
        if valid_group:
            selected_products = [selection["products"][0] for selection in selections]
            comparable_products = [
                {key: value for key, value in product.items() if key != "dietary_assessments"}
                for product in selected_products
            ]
            valid_group = len({canonical(product) for product in comparable_products}) == 1
        if valid_group:
            valid_group = len({
                canonical(selection.get("observed_package"))
                for selection in selections
            }) == 1
        # When the package exposes the same physical dimension as every need,
        # the declared shared count must cover their sum. Other dimensions rely
        # on the user's explicit bounded culinary quantity_basis, just like the
        # existing practical-package contract.
        if valid_group:
            units = {row.get("unit") for row in rows}
            package = selections[0].get("observed_package")
            if len(units) == 1:
                unit = next(iter(units))
                size = _package_quantity(package, unit)
                if size is not None:
                    required = sum(
                        (_read_fraction(row["quantity"], positive=True) for row in rows),
                        Fraction(0),
                    )
                    valid_group = shared["package_count"] * size >= required
        if not valid_group:
            for row in rows:
                if isinstance(row, dict) and row.get("status") == "selected":
                    row["status"] = "needs_input"
                    row.pop("selection", None)
            unresolved.append({
                "reason": "shared_package_group_unavailable",
                "requirement_ids": list(members),
                "candidate_ref": reference,
            })
            continue
        owner = min(members)
        allocation = {
            **deepcopy(shared),
            "candidate_ref": reference,
            "owner_requirement_id": owner,
        }
        for row, selection in zip(rows, selections):
            selection["shared_package_allocation"] = deepcopy(allocation)
            selection["counts_toward_cart_and_totals"] = row["requirement_id"] == owner
        allocated_refs.add(reference)

    # An exact SKU selected for several compatible requirements is one stock
    # allocation. Recalculate against their combined quantity so per-line
    # rounding cannot overbuy it. Explicit cross-unit culinary allocations
    # remain on the current-user shared_package path above.
    selected_ref_owners: dict[str | int, list[dict[str, Any]]] = {}
    for row in planned:
        selection = row.get("selection") if isinstance(row, Mapping) else None
        products = selection.get("products") if isinstance(selection, Mapping) else None
        if row.get("status") == "selected" and isinstance(products, list) and len(products) == 1:
            reference = products[0].get("product_ref")
            if _valid_product_ref(reference):
                selected_ref_owners.setdefault(reference, []).append(row)
    for reference, rows in selected_ref_owners.items():
        if len(rows) < 2 or reference in allocated_refs:
            continue
        members = tuple(sorted(row["requirement_id"] for row in rows))
        group_approvals = [approvals[row["requirement_id"]] for row in rows]
        if any(
            approval.get("candidate_refs") != [reference]
            or any(key in approval for key in ("package_count", "max_excess", "shared_package"))
            for approval in group_approvals
        ):
            continue
        units = {row.get("unit") for row in rows}
        if len(units) != 1:
            continue
        unit = next(iter(units))
        products = []
        for row in rows:
            observed = row.get("observation", {}).get("products", [])
            matches = [product for product in observed if product.get("product_ref") == reference]
            if len(matches) != 1:
                products = []
                break
            product = deepcopy(matches[0])
            product.pop("candidate_approval", None)
            products.append(product)
        if not products or len({canonical(_without_presentation(product)) for product in products}) != 1:
            continue
        required = sum(
            (_read_fraction(row["quantity"], positive=True) for row in rows),
            Fraction(0),
        )
        combined_requirement = {
            "item": " + ".join(row["item"] for row in rows),
            "quantity": _fraction_json(required),
            "unit": unit,
        }
        combined_observation = {"products": [products[0]]}
        combined_approval = {"candidate_refs": [reference]}
        combined, reason, _eligible = _select_requirement(
            combined_requirement, combined_observation, combined_approval,
        )
        if reason == "candidate_price_or_eligibility_unresolved" and price_mode == "estimate":
            combined = _estimated_single_product(
                combined_requirement, combined_observation, combined_approval,
            )
            reason = None if combined is not None else reason
        if reason is not None or combined is None:
            continue
        combined["products"][0]["dietary_assessments"] = deepcopy(
            rows[0]["selection"]["products"][0].get("dietary_assessments", [])
        )
        combined["surplus_quantity"] = (
            None if combined["coverage"] is None
            else _fraction_json(_read_fraction(combined["coverage"]) - required)
        )
        owner = min(members)
        allocation = {
            "source": "internal_shared_allocation",
            "requirement_ids": list(members),
            "candidate_ref": reference,
            "package_count": combined["package_count"],
            "quantity_basis": "combined_compatible_requirement_quantities",
            "combined_required": _fraction_json(required),
            "unit": unit,
            "owner_requirement_id": owner,
        }
        for row in rows:
            row["selection"] = deepcopy(combined)
            row["selection"]["shared_package_allocation"] = deepcopy(allocation)
            row["selection"]["counts_toward_cart_and_totals"] = row["requirement_id"] == owner
        allocated_refs.add(reference)

    # Complex reuse (mixed dimensions, different observed facts, multiple
    # selected SKUs or explicit per-line counts) stays reviewable and unresolved.
    for reference in reused_refs - allocated_refs:
        rows = [
            row for row in planned
            if row.get("status") == "selected"
            and any(product.get("product_ref") == reference for product in row.get("selection", {}).get("products", []))
        ]
        if len(rows) < 2:
            continue
        for row in rows:
            row["status"] = "needs_input"
            row.pop("selection", None)
            unresolved.append({
                "requirement_id": row["requirement_id"],
                "item": row["item"],
                "reason": "candidate_ref_reused_across_requirements",
            })

    # Shared allocations expose the selection on every requirement for review,
    # while exactly one deterministic owner contributes packages and money.
    merchandise = deposit = payable = packages = 0
    exact_known_minimum = 0
    estimated_merchandise = False
    payable_known = True
    excess = Fraction(0)
    for row in planned:
        selection = row.get("selection") if isinstance(row, Mapping) else None
        if not isinstance(selection, Mapping) or selection.get("counts_toward_cart_and_totals") is False:
            continue
        merchandise += selection["merchandise_ore"]
        selection_has_estimate = any(
            product.get("price_status") == "estimate"
            for product in selection.get("products", [])
        )
        estimated_merchandise = estimated_merchandise or selection_has_estimate
        for product in selection.get("products", []):
            if product.get("price_status") != "estimate":
                exact_known_minimum += product["merchandise_ore"]
                if isinstance(product.get("mandatory_deposit_ore"), int):
                    exact_known_minimum += product["mandatory_deposit_ore"]
        if selection["total_payable_ore"] is None:
            payable_known = False
        else:
            deposit += selection["mandatory_deposit_ore"]
            payable += selection["total_payable_ore"]
        packages += selection["package_count"]
        if selection["excess_score"] is not None:
            excess += _read_fraction(selection["excess_score"])
    stock_covers_menu = bool(menu.get("available_ingredients")) and any(
        recipe.get("shopping_requirements") for collection in ("dishes", "salads") for recipe in menu[collection]
    )
    status = "prepared" if (requirements or ingredient_decisions or stock_covers_menu) and not unresolved else "needs_input"
    coverage_kinds = {
        r.get("selection", {}).get("coverage_status")
        for r in planned if r.get("selection", {}).get("coverage_status") not in {None, "exact"}
    }
    estimated_coverage = bool(coverage_kinds)
    plan: dict[str, Any] = {
        "product_plan_version": PRODUCT_PLAN_VERSION,
        "provider": provider,
        "binding": deepcopy(dict(binding)),
        "hard_product_constraints": hard_constraints,
        "ingredient_decisions": deepcopy(ingredient_decisions or []),
        **({"available_ingredients": deepcopy(menu["available_ingredients"])} if menu.get("available_ingredients") else {}),
        "budget_ore": budget_ore,
        "price_mode": price_mode,
        "coverage_status": (
            "practical_estimate" if coverage_kinds == {"practical_estimate"}
            else "variable_weight_estimate" if coverage_kinds and all(
                str(kind).startswith("variable_weight_") for kind in coverage_kinds
            ) else "estimated" if estimated_coverage else "exact"
        ),
        "cost_status": "exact_product_payable" if payable_known and status == "prepared" else "merchandise_estimate_only" if status == "prepared" else "unresolved",
        "status": status,
        "scope": {
            "search_semantics": "bounded_relevance_ranked",
            "candidate_semantics": "exact_current_user_approved_refs_per_requirement",
            "maximum_requirements": MAX_REQUIREMENTS,
            "maximum_candidates_per_requirement": MAX_CANDIDATES_PER_REQUIREMENT,
            "maximum_combinations_per_requirement": MAX_COMBINATIONS,
        },
        "requirements": planned,
        "unresolved_requirements": unresolved,
        "comparison_claim": (
            f"best dietary fit, then lowest verified total payable amount among the approved, exactly priced candidates observed for {len(requirements)} bounded {provider.upper()} searches"
            if status == "prepared" and payable_known and not estimated_coverage else None
        ),
        "excluded_costs": ["delivery", "cart_level_bags", "cart_level_fees", "checkout_price_drift"],
    }
    exact_floor_exceeded = budget_ore is not None and exact_known_minimum > budget_ore
    if exact_floor_exceeded and status != "prepared":
        plan["budget_status"] = "exceeded"
        plan["unresolved_requirements"].append({
            "reason": "product_budget_exceeded", "budget_ore": budget_ore,
            "known_minimum_ore": exact_known_minimum, "total_payable_ore": None,
        })
    if status == "prepared":
        plan["totals"] = {
            "merchandise_ore": merchandise,
            "mandatory_deposit_ore": deposit if payable_known else None,
            "total_payable_ore": payable if payable_known else None,
            "excess_score": None if estimated_coverage else _fraction_json(excess),
            "package_count": packages,
        }
        plan["budget_status"] = (
            "not_set" if budget_ore is None
            else "exceeded" if exact_floor_exceeded
            else "unverified" if estimated_merchandise
            else "unverified" if not payable_known
            else "exceeded" if merchandise + deposit > budget_ore
            else "within_budget"
        )
        if plan["budget_status"] == "exceeded":
            plan["status"] = "needs_input"
            plan["unresolved_requirements"].append({"reason": "product_budget_exceeded", "budget_ore": budget_ore, "known_minimum_ore": exact_known_minimum, "total_payable_ore": payable if payable_known else None})
    partial_digest = partial_product_plan_digest(plan)
    if partial_digest is not None and status != "prepared":
        plan["partial_product_plan_digest"] = partial_digest
    plan["product_plan_digest"] = product_plan_digest(plan)
    return plan


def validate_product_plan(value: Any, supplied_digest: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HouseholdError("apply needs the complete server-returned product_plan")
    plan = deepcopy(dict(value))
    digest = plan.get("product_plan_digest")
    if (
        not isinstance(supplied_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", supplied_digest)
        or digest != supplied_digest or product_plan_digest(plan) != supplied_digest
    ):
        raise HouseholdError("product_plan payload or digest changed")
    if plan.get("product_plan_version") != PRODUCT_PLAN_VERSION or plan.get("status") != "prepared":
        raise HouseholdError("only one complete prepared product_plan can be applied")
    return plan


def cart_requirements(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    quantities: dict[str | int, dict[str, Any]] = {}
    for requirement in plan.get("requirements", []):
        selection = requirement.get("selection") if isinstance(requirement, Mapping) else None
        if not isinstance(selection, Mapping):
            raise HouseholdError("prepared product plan has an incomplete selection")
        if selection.get("counts_toward_cart_and_totals") is False:
            continue
        for product in selection.get("products", []):
            if not isinstance(product, Mapping):
                raise HouseholdError("prepared product plan product is invalid")
            reference = product.get("product_ref")
            quantity = product.get("quantity")
            name = product.get("name")
            if (
                not _valid_product_ref(reference) or not isinstance(name, str) or not name
                or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1
            ):
                raise HouseholdError("prepared product plan product is invalid")
            current = quantities.setdefault(reference, {"product_id": reference, "product_name": name, "quantity": 0})
            if current["product_name"] != name:
                raise HouseholdError("prepared product plan product name conflicts")
            current["quantity"] += quantity
    return [quantities[reference] for reference in sorted(quantities, key=_ref_sort_key)]


def partial_cart_requirements(plan: Mapping[str, Any], supplied_digest: Any) -> list[dict[str, Any]]:
    digest = partial_product_plan_digest(plan)
    if (
        not isinstance(supplied_digest, str)
        or re.fullmatch(r"[a-f0-9]{64}", supplied_digest) is None
        or digest != supplied_digest
        or plan.get("partial_product_plan_digest") != supplied_digest
    ):
        raise HouseholdError("partial product selection or digest changed")
    selected_plan = {
        "requirements": [
            requirement for requirement in plan.get("requirements", [])
            if isinstance(requirement, Mapping) and requirement.get("status") == "selected"
        ]
    }
    requirements = cart_requirements(selected_plan)
    if not requirements:
        raise HouseholdError("partial apply needs at least one selected product requirement")
    return requirements
