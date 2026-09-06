"""Offline structured readers for the declared, sealed public-source snapshot.

This module never fetches URLs. Source wording is data, including attribution.
The shared recipe decoder remains the sole culinary representation contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urlsplit


THEMEALDB_TERMS = "https://www.themealdb.com/terms_of_use.php"
THEMEALDB_POLICY = {
    "redistribution_status": "permitted_with_attribution",
    "attribution_required": True,
    "preserve_copyright_and_trademark_notices": True,
    "api_resale_requires_separate_permission": True,
    "terms_url": THEMEALDB_TERMS,
}


class SourceParseError(ValueError):
    pass


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)

    def text(self) -> str:
        return " ".join(" ".join(c if isinstance(c, str) else c.text()
                                for c in self.children).split())

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()


class SourceHTML(HTMLParser):
    """Small inert HTML tree; scripts, styling and navigation are discarded."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.skip = 0
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        ignored = tag in {"script", "style", "iframe", "object", "nav"} or bool(classes & {"navbox", "toc", "mw-editsection", "noprint"})
        if self.skip or ignored:
            if tag not in self.VOID:
                self.skip += 1
            return
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            if len(self.stack) >= 100:
                raise SourceParseError("HTML nesting exceeds 100 levels")
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= 1
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, text):
        if not self.skip:
            self.stack[-1].children.append(text)


def plain(value: str) -> str:
    return SourceHTML(value).root.text()


def attribution_links(root: Node) -> list[str]:
    result = []
    for node in root.walk():
        url = node.attrs.get("href", "") if node.tag == "a" else ""
        if url.startswith("//"):
            url = "https:" + url
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and len(url) <= 2048:
            if url not in result:
                result.append(url)
    return result


def _ingredient(text: str, *, item=None, measure=None):
    from recipes import source_ingredient
    from recipe_quantities import UNITS, parse_measure

    punctuation_text = re.sub(r'\s+([,;:.])', r'\1', text)
    source_metric = False
    if item is None:
        # A source's printed metric equivalent is evidence for this ingredient
        # only. Do not infer cup conventions, densities or per-package totals.
        match = re.fullmatch(r'(.+?)\s*\(([^()]+)\)\s*(.+)', punctuation_text)
        if match and re.fullmatch(r'[\d\s.,/¼½¾⅓⅔⅛⅜⅝⅞]+\s*(?:cups?|tbsp|tsp|tablespoons?|teaspoons?|oz|ounces?|lb|pounds?|pints?|fl oz)', match[1], re.I):
            metric = re.fullmatch(r'\s*([\d\s.,/¼½¾⅓⅔⅛⅜⅝⅞]+)\s*(kg|g|ml|l)\s*(?:/\s*[\d\s.,¼½¾⅓⅔⅛⅜⅝⅞]+\s*(?:oz|ounces?|lb|pounds?|kg|g|ml|l)\s*)?', match[2], re.I)
            if metric:
                candidate = f'{metric[1].strip()} {metric[2]}'
                if parse_measure(candidate)[0] is not None:
                    measure, item, source_metric = candidate, match[3], True
    # Parse explicit compact metric spelling without changing original wording.
    if item is None:
        for unit in sorted(UNITS, key=len, reverse=True):
            match = re.fullmatch(r"(\d[\d\s.,/¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]*)\s*" + re.escape(unit) + r"\s+(.+)", text, re.I)
            if match and parse_measure(f"{match[1].strip()} {unit}")[0] is not None:
                measure, item = f"{match[1].strip()} {unit}", match[2]
                break
        if item is None:
            # These food nouns explicitly denote individual source items. A
            # package, cup, handful, range or alternative is not inferred here.
            match = re.fullmatch(r'(\d+)\s+((?:(?:small|medium|large|fresh)\s+)?(?:egg whites?|egg yolks?|salmon fillets?|garlic cloves?|bell peppers?|zucchini|sweet potatoes|sweet potato|eggs?|onions?|carrots?|potatoes|potato|tomatoes|tomato|lemons?|limes?|apples?|bananas?))(,.*)?', punctuation_text, re.I)
            if match:
                measure, item = f'{match[1]} count', match[2] + (match[3] or '')
    result = source_ingredient(text, item=item, measure=measure)
    if source_metric:
        for evidence in result['evidence'].values():
            evidence['conversion'] = 'Used the explicit metric amount printed in source parentheses; no density or general cup conversion inferred.'
    # Unsupported prose is retained in original_text, never promoted to a unit.
    if result['quantity'] is None:
        result['unit'] = None
    if re.search(r'\b(?:plus|and)\s+(?:extra|more|additional)\b|\b(?:or|plus)\s+\d', text, re.I):
        result.update(quantity=None, unit=None, scalable=False)
        result['evidence']['quantity']['assumptions'] = 'Alternative or additional source amount requires an explicit decision.'
    if re.search(r'\(\s*optional\s*\)', text, re.I):
        result['optional'] = True
        result['item'] = re.sub(r'\s*\(\s*optional\s*\)', '', result['item'], flags=re.I).strip()
    return result


def _ingredient_table(node: Node) -> list[dict]:
    from recipe_quantities import parse_measure
    rows = [n for n in node.walk() if n.tag == 'tr']
    if not rows:
        return []
    def cells(row):
        return [n.text() for n in row.children if isinstance(n, Node) and n.tag in {'td', 'th'}]
    headers = [re.sub(r'\s*\[.*?\]|\s*\(optional\)', '', x.casefold()).strip() for x in cells(rows[0])]
    headers = ['ingredient' if x == 'name' else x for x in headers]
    if 'ingredient' not in headers or set(headers) - {'ingredient', 'count', 'volume', 'weight', 'quantity', 'amount', "baker's %"}:
        raise SourceParseError('unsupported ingredient table structure')
    result = []
    group = None
    for row in rows[1:]:
        values = cells(row)
        row_nodes = [n for n in row.children if isinstance(n, Node) and n.tag in {'td', 'th'}]
        if len(row_nodes) == 1 and row_nodes[0].attrs.get('colspan') == str(len(headers)):
            group = values[0]
            continue
        if len(values) != len(headers):
            raise SourceParseError('ingredient table row shape differs from header')
        fields = dict(zip(headers, values))
        if fields['ingredient'].casefold().strip() == 'total':
            continue
        original = '; '.join(f'{label}: {value}' for label, value in zip(headers, values))
        measure = None
        for field_name in ('weight', 'volume', 'quantity', 'amount', 'count'):
            value = fields.get(field_name, '')
            if field_name == 'count' and re.fullmatch(r'\d+', value):
                value += ' count'
            if parse_measure(value)[0] is not None:
                measure = value
                break
        ingredient = _ingredient(original, item=fields['ingredient'], measure=measure)
        if group:
            ingredient['notes'] = group
        result.append(ingredient)
    return result


def _base(entry: dict) -> dict:
    wiki = entry["source"] == "wikibooks"
    return {
        "schema_version": 2, "name": entry["title"].removeprefix("Cookbook:"),
        "language": "en", "portions": None, "source_provider": None,
        "source": {"kind": entry["source"], "publisher": "Wikibooks Cookbook" if wiki else "TheMealDB",
                   "title": entry["title"], "author": "Wikibooks contributors" if wiki else None,
                   "url": entry["url"], "external_id": entry["source_id"], "relationship": "adapted"},
        "rights": {"storage": "full", "license": "CC BY-SA 4.0" if wiki else "TheMealDB Terms of Use",
                   "license_url": "https://creativecommons.org/licenses/by-sa/4.0/" if wiki else "https://www.themealdb.com/terms_of_use.php",
                   "credit": entry.get("credit") or "Recipe data sourced via TheMealDB. Recipe source listed by TheMealDB is retained separately."},
        "external_snapshot": {"fetched_at": entry["fetched_at"], "content_hash": entry["raw"]["sha256"],
                              "source_revision_id": str(entry["revision"]) if entry.get("revision") is not None else None,
                              "permanent_url": entry.get("permanent_url"),
                              "changes": "Structured offline extraction by Meal Concierge; original wording and unresolved quantities retained."},
    }


def mealdb_recipe(entry: dict, payload: dict) -> tuple[dict, dict]:
    from recipes import normalize_recipe
    meals = payload.get("meals")
    if not isinstance(meals, list) or len(meals) != 1 or meals[0].get("idMeal") != entry["source_id"]:
        raise SourceParseError("lookup identity mismatch")
    meal = meals[0]
    result = _base(entry)
    source = meal.get("strSource")
    if source:
        result["source"]["original"] = {"url": source}
    result["ingredients"] = []
    for n in range(1, 21):
        item = (meal.get(f"strIngredient{n}") or "").strip()
        measure = (meal.get(f"strMeasure{n}") or "").strip()
        if item:
            result["ingredients"].append(_ingredient(" ".join(x for x in (measure, item) if x), item=item, measure=measure))
    result["steps"] = [line.strip() for line in re.split(r"[\r\n]+", meal.get("strInstructions") or "") if line.strip()]
    result["tags"] = list(dict.fromkeys(x.strip() for x in [meal.get("strCategory") or "", meal.get("strArea") or ""] if x.strip()))
    return normalize_recipe(result), {
        "text_rights": "permitted_with_attribution",
        "redistribution_policy": THEMEALDB_POLICY.copy(),
        "provider": "TheMealDB", "provider_url": "https://www.themealdb.com/",
        "credit": result["rights"]["credit"],
        "original_source_label": "Recipe source listed by TheMealDB",
        "original_source": source, "terms_url": result["rights"]["license_url"],
    }


def _reviewed_dinner_mapping(entry: dict, recipe: dict) -> bool:
    """Apply whole-source-reviewed facts only to their sealed rendered revision.

    A selected explicit source alternative is recorded as an adaptation; it is
    never an accepted estimate, an inferred package weight or a general rule.
    """
    from recipes import source_ingredient

    mappings = {
        ('102173', 4509779): ('8da99cf480d05f44139c34814e3053d5649fd80032e5d3eedd672fbdb714d28c', {
            '2 mangoes , seeded, peeled, and sliced': ('2 count', 'mangoes, seeded, peeled, and sliced'),
        }),
        ('203935', 4509725): ('f18f4901df5230d964777c14a2ea3f2c25db877f5a35f7c17f0946a8a70c4356', {
            '4 ea . (28–32 oz / 800–900 g ) boneless duck breasts': ('4 count', 'boneless duck breasts'),
            '1 star anise pod, ground': ('1 count', 'star anise pod, ground'),
        }),
        ('479669', 4597437): ('60be8b028bc02821c8b6501585de8335678075bd25e593a2460dc965cf2956ce', {
            '¼ ea . medium onion': ('1/4 count', 'medium onion'),
        }),
        ('414427', 4524828): ('e0f9c27d7c6594a9da66c9ca5149870cbf1739e3413a7306e275d8395f928681', {
            '12 small potatoes or 10 medium sized ones weighing approximately 971g': ('12 count', 'small potatoes'),
            '8 lamb sausages': ('8 count', 'lamb sausages'),
            '12 cherry tomatoes': ('12 count', 'cherry tomatoes'),
        }),
        ('266778', 4512396): ('f2c4d7fb1d070047effa8c3dab71476caa41b124e1f79e9fa522f879daa566f3', {}),
    }
    key = (entry['source_id'], entry['revision'])
    if key not in mappings:
        return False
    expected_hash, quantities = mappings[key]
    if entry['rendered']['sha256'] != expected_hash:
        raise SourceParseError('reviewed dinner rendered digest mismatch')
    change = 'Mapped explicit source item counts; retained source preparation wording and any separate weight range without mass conversion.'
    if key[0] == '414427':
        change = 'Selected the source alternative of 12 small potatoes; the alternative 10 medium potatoes and approximate 971 g remain in original evidence, not an exact mass requirement.'
    for original, (measure, item) in quantities.items():
        indexes = [i for i, value in enumerate(recipe['ingredients']) if value['original_text'] == original]
        if len(indexes) != 1:
            raise SourceParseError('reviewed dinner ingredient wording mismatch')
        ingredient = source_ingredient(original, item=item, measure=measure)
        for evidence in ingredient['evidence'].values():
            evidence['conversion'] = change
        recipe['ingredients'][indexes[0]] = ingredient
    if key[0] == '266778':
        steps = recipe['steps']
        if (len(steps) != 9 or steps[3] != 'Transfer coated fillets to a slightly greased or non-stick baking sheet.'
                or not steps[6].startswith('Heat a few tablespoons of oil')):
            raise SourceParseError('reviewed dinner cooking branches mismatch')
        recipe['steps'] = steps[:6]
        recipe['steps'][3] = 'Transfer coated fillets to a non-stick baking sheet.'
        recipe['name'] += ' (Oven Method)'
        change = 'Selected the source oven method with a non-stick baking sheet. The alternative stovetop method requires unquantified oil and is retained only in full source attribution.'
    recipe['external_snapshot']['changes'] += ' ' + change
    recipe['notes'] = change + '\n' + (recipe.get('notes') or '')
    return True


# These sealed revisions failed parsing; none is an existing pack document.
# Keep repairs revision-bound until a future source shape is independently read.
# The count also checks that the demonstrated structural defect is still present.
_WIKIBOOKS_RECOVERY = {
    ("9016", 4510241): ("related_notice", 1),
    ("12749", 4518408): ("related_notice", 1),
    ("88223", 4541034): ("related_notice", 1),
    ("289230", 4501415): ("related_notice", 1),
    ("295837", 4518590): ("related_notice", 1),
    ("448083", 4523785): ("related_notice", 1),
    ("18354", 4617976): ("metric_notice", 1),
    ("23267", 4522414): ("vegetarian_notice", 1),
    ("38535", 4532021): ("nutrition_table", 1),
    ("33062", 4514693): ("heading_citations", 1),
    ("370360", 4518142): ("heading_citations", 2),
    ("372273", 4587429): ("heading_citations", 2),
    ("18111", 4636790): ("notes_headings", 1),
    ("415629", 4630845): ("notes_headings", 2),
    ("461702", 4511022): ("notes_headings", 1),
    ("476040", 4658398): ("notes_headings", 1),
    ("415349", 4517861): ("process_heading", 1),
    ("462355", 4613690): ("process_heading", 1),
    ("462387", 4508979): ("process_heading", 1),
    ("254237", 4509833): ("people_heading", 1),
    ("447194", 4535488): ("procedures_ingredients", 1),
    ("456925", 4587492): ("procedure_ingredients", 1),
    ("476395", 4522969): ("recipe_ingredients", 1),
    ("479750", 4601579): ("recipes_ingredients", 1),
    ("479752", 4601580): ("recipes_ingredients", 1),
    ("483186", 4634713): ("misspelled_ingredients", 1),
    ("412220", 4587440): ("recipe_procedure", 1),
    ("483114", 4634633): ("empty_list_wrapper", 1),
}


def _heading_without_citations(node: Node) -> str:
    if node.tag == "sup" and "reference" in node.attrs.get("class", "").split():
        return ""
    return " ".join(child if isinstance(child, str) else _heading_without_citations(child)
                    for child in node.children)


def wikibooks_recipe(entry: dict, payload: dict) -> tuple[dict, dict]:
    from recipes import normalize_recipe, source_yield
    parsed = payload.get("parse", {})
    if str(parsed.get("pageid")) != entry["source_id"] or parsed.get("revid") != entry["revision"]:
        raise SourceParseError("rendered revision identity mismatch")
    tree = SourceHTML(parsed["text"])
    result = _base(entry)
    ingredients, steps, headings, notes = [], [], [], []
    recovery, expected_repairs = _WIKIBOOKS_RECOVERY.get((entry["source_id"], entry["revision"]), (None, 0))
    repairs = 0
    section, section_level = None, None
    servings, yields = [], []
    infobox_rows = [row for box in tree.root.walk()
                    if box.tag == 'table' and 'infobox' in box.attrs.get('class', '').split()
                    for row in box.walk() if row.tag == 'tr']
    for node in infobox_rows:
        if node.tag == "tr":
            cells = [n for n in node.children if isinstance(n, Node) and n.tag in {"td", "th"}]
            if len(cells) == 2:
                label, value = cells[0].text().casefold().strip(":"), cells[1].text()
                if label in {"servings", "serves", "portions"} and value:
                    servings.append(value)
                elif label == "yield" and value:
                    yields.append(value)

    def visit(node: Node):
        nonlocal section, section_level, repairs
        if 'infobox' in node.attrs.get('class', '').split():
            return
        if node.tag == 'table':
            classes = set(node.attrs.get('class', '').split())
            text = node.text()
            if ((recovery == 'related_notice' and 'mbox-side-notice' in classes
                 and text.startswith('Wikipedia has related information at '))
                or (recovery == 'metric_notice' and 'box-Metricate' in classes
                    and text.startswith('This article or section exclusively uses non- SI units'))
                or (recovery == 'nutrition_table' and text.startswith('NUTRITION FACTS Serving Size:'))
                or (recovery == 'vegetarian_notice' and text ==
                    'vg This recipe is vegetarian ; it contains no meat. Milk is present.')):
                # The original tree is retained intact for source attribution.
                repairs += 1
                return
            if section == 'ingredients':
                ingredients.extend(_ingredient_table(node))
            elif section == 'steps':
                raise SourceParseError('procedure table requires explicit structured mapping')
            return
        if re.fullmatch(r"h[1-6]", node.tag):
            level, heading = int(node.tag[1]), node.text()
            kind = heading.casefold().strip(" :")
            if recovery == 'heading_citations':
                clean = " ".join(_heading_without_citations(node).split()).casefold().strip(" :")
                if clean != kind and clean in {'ingredients', 'procedure', 'preparation'}:
                    kind = clean
                    repairs += 1
            if (recovery == 'notes_headings' and section == 'notes' and level > section_level
                    and kind in {'ingredients', 'method'}):
                repairs += 1
                return
            renamed = {
                'process_heading': ('process', 'procedure'),
                'people_heading': ('ingredients for 4 people', 'ingredients'),
                'procedures_ingredients': ('procedures', 'ingredients'),
                'procedure_ingredients': ('procedure', 'ingredients'),
                'recipe_ingredients': ('recipe', 'ingredients'),
                'recipes_ingredients': ('recipes', 'ingredients'),
                'misspelled_ingredients': ('ingrdients', 'ingredients'),
                'recipe_procedure': ('recipe', 'procedure'),
            }.get(recovery)
            if renamed and kind == renamed[0]:
                kind = renamed[1]
                repairs += 1
            if re.fullmatch(r"ingredients?(?:\s*\(.*\))?", kind):
                headings.append(heading)
                section, section_level = "ingredients", level
            elif re.fullmatch(r"(?:procedures?|directions?|instructions?|method|preparation)(?:\s*\(.*\))?", kind):
                if section != 'steps' or level <= section_level:
                    section, section_level = "steps", level
            elif re.match(r'^(?:notes?|warnings?|tips|conversion notes)(?:\b|,)', kind):
                section, section_level = 'notes', level
            elif section_level is not None and level <= section_level:
                section, section_level = None, None
            return
        if recovery == 'empty_list_wrapper' and section == 'ingredients' and node.tag == 'li':
            if len(node.children) == 1 and isinstance(node.children[0], Node) and node.children[0].tag == 'ul':
                repairs += 1
                visit(node.children[0])
                return
        if section and node.tag in {"li", "p"}:
            if section == 'ingredients' and any(child is not node and child.tag in {'li', 'table', 'dl'} for child in node.walk()):
                raise SourceParseError('nested ingredient list requires explicit grouping or alternatives')
            text = node.text()
            if text:
                (ingredients if section == "ingredients" else steps if section == 'steps' else notes).append(text)
            return
        for child in node.children:
            if isinstance(child, Node):
                visit(child)
    visit(tree.root)
    if repairs != expected_repairs:
        raise SourceParseError("reviewed source structure changed")
    if len(headings) != 1:
        raise SourceParseError("missing or multiple ingredient sections; requires explicit recipe splitting")
    if not ingredients or not steps:
        raise SourceParseError("missing complete ingredient or procedure section")
    result["ingredients"] = [_ingredient(x) if isinstance(x, str) else x for x in ingredients]
    result["steps"] = steps
    if len(servings) == 1:
        # The rendered infobox label establishes people, never a bare yield.
        if re.fullmatch(r"\d+(?:[.,]\d+)?", servings[0]):
            result["portions"] = float(servings[0].replace(",", "."))
            result["portions_evidence"] = {"basis": "source", "input": f"Servings: {servings[0]}"}
        else:
            serving_text = re.sub(r'^serves\s+(\d+(?:[.,]\d+)?)$', r'\1 servings', servings[0], flags=re.I)
            _, result["portions"] = source_yield(serving_text)
            result["portions_evidence"] = {"basis": "source", "input": servings[0]}
    if len(yields) == 1:
        result["yield"], yield_portions = source_yield(yields[0])
        if not servings and yield_portions is not None:
            result['portions'] = yield_portions
            result['portions_evidence'] = {'basis': 'source', 'input': yields[0]}
    # Preserve the complete readable source, including notices, in attribution;
    # do not duplicate all instructions into the culinary notes field.
    result["notes"] = '\n'.join(notes) or None
    result["tags"] = ["Wikibooks Cookbook"]
    reviewed_dinner = _reviewed_dinner_mapping(entry, result)
    issues = []
    if 'this recipe is incomplete' in tree.root.text().casefold():
        issues.append('source_marks_recipe_incomplete')
        result['notes'] = 'Source marks this recipe incomplete.\n' + (result['notes'] or '')
    # Revision-bound defects found by reading the entire cooking procedure.
    # Never promote a quantified ingredient subset while omitting required food.
    procedure_gaps = {
        ('162377', 4514080): 'Procedure requires oil absent from the ingredient list; its amount is unresolved.',
        ('99627', 4503985): 'Procedure requires a filling absent from the quantified ingredient list.',
        ('266778', 4512396): 'Stovetop branch requires unquantified oil; branch-specific shopping needs explicit selection.',
        ('85421', 4515970): 'Procedure requires glaze and frosting described separately in notes; finishing ingredient quantities need explicit mapping.',
    }
    if (entry['source_id'], entry['revision']) in procedure_gaps and not reviewed_dinner:
        issues.append('procedure_ingredient_quantity_unresolved')
        result['notes'] = procedure_gaps[(entry['source_id'], entry['revision'])] + '\n' + (result['notes'] or '')
    if (entry['source_id'], entry['revision']) == ('461702', 4511022):
        issues.append('source_quantity_guidance_conflict')
        result['notes'] = ('Source gives conflicting ratio guidance ("about 1:10 or 5% w/w") '
                           'and mentions optional rinsing milk without a quantity. '
                           'Keep draft until clarified; original ingredients and instructions are unchanged.\n'
                           + (result['notes'] or ''))
    return normalize_recipe(result), {
        "text_rights": "CC-BY-SA-4.0", "history_url": entry.get("history_url"),
        "normalization_issues": issues,
        "rendered_sha256": entry["rendered"]["sha256"],
        "rendered_at": entry["rendered"]["fetched_at"],
        "source_notices": tree.root.text(), "source_links": attribution_links(tree.root),
    }


def readiness(recipe: dict) -> tuple[str, list[str]]:
    """Use the runtime evidence/quantity contract for bundled eligibility too."""
    from recipes import scale_recipe
    scaled = scale_recipe(recipe)
    reasons = list(scaled['readiness']['missing_decisions'])
    for index, requirement in enumerate(scaled['shopping_requirements']):
        if not requirement['scalable']:
            reason = f'ingredients.{index}.quantity_or_unit_unresolved'
            if reason not in reasons:
                reasons.append(reason)
    return ('draft' if reasons else 'ready'), reasons
