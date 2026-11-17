"""Reviewed recipe text variants; canonical ingredients always drive shopping."""
from copy import deepcopy
import hashlib
import json
import re

TEXT_FIELDS = ('name', 'steps', 'notes', 'storage', 'reheating')


def language_tag(value):
    from recipes import RecipeError
    if not isinstance(value, str) or not re.fullmatch(r'[a-z]{2,3}(?:-[A-Z]{2})?', value):
        raise RecipeError('language must be a language tag such as en, nb or nb-NO')
    return value


def source_text_digest(recipe, *, legacy=False):
    """Bind reviewed prose to ordered food identities and the current method.

    Portion scaling changes quantities/raw strings, but never this binding.
    """
    value = {field: recipe.get(field) for field in (*TEXT_FIELDS, 'language')}
    if not legacy:
        value['source'] = recipe.get('source')
        snapshot = recipe.get('external_snapshot') or {}
        value['source_revision'] = [snapshot.get('content_hash'), snapshot.get('source_revision_id')]
    from recipe_quantities import read_quantity
    portions = recipe.get('portions')
    base = read_quantity(portions, legacy_float=True) if portions is not None else None
    value['ingredients'] = []
    for row in recipe.get('ingredients', []):
        ingredient = {key: row.get(key) for key in ('item', 'unit', 'optional', 'pantry', 'notes', 'scalable')}
        amount = row.get('quantity')
        amount = read_quantity(amount, legacy_float=True) if amount is not None else None
        ingredient['quantity_per_serving'] = str(amount / base if amount is not None and base and row.get('scalable') else amount)
        value['ingredients'].append(ingredient)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def normalize_translations(value, recipe):
    from recipes import RecipeError, _bounded_text, _single_line
    if not isinstance(value, dict) or not 1 <= len(value) <= 8:
        raise RecipeError('translations must contain one to eight language variants')
    if recipe['rights']['storage'] != 'full':
        raise RecipeError('link_only recipes cannot store translations')
    result = {}
    for language, variant in value.items():
        language_tag(language)
        if language == recipe['language']:
            raise RecipeError('translation language must differ from canonical language')
        allowed = {*TEXT_FIELDS, 'ingredients', 'source_text_digest'}
        if not isinstance(variant, dict) or set(variant) - allowed or not {'name', 'steps', 'ingredients', 'source_text_digest'} <= set(variant):
            raise RecipeError('translation requires name, steps, ingredients and source_text_digest; unknown fields are forbidden')
        if variant['source_text_digest'] not in (source_text_digest(recipe), source_text_digest(recipe, legacy=True)):
            raise RecipeError('translation source_text_digest is stale; review against the current recipe')
        labels, steps = variant['ingredients'], variant['steps']
        if not isinstance(labels, list) or len(labels) != len(recipe['ingredients']):
            raise RecipeError('translation ingredients must label every canonical ingredient in order')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
            raise RecipeError('translation steps must contain one to 100 entries')
        normalized = {'source_text_digest': variant['source_text_digest'],
                      'ingredients': [],
                      'steps': [_bounded_text(v, 'translation step', required=True) for v in steps]}
        for label, original in zip(labels, recipe['ingredients']):
            if not isinstance(label, dict) or set(label) - {'item', 'notes'} or 'item' not in label:
                raise RecipeError('translation ingredient requires item and optional notes only')
            normalized['ingredients'].append({
                'item': _single_line(label['item'], 'translation ingredient.item', required=True, maximum=300),
                'notes': _bounded_text(label.get('notes'), 'translation ingredient.notes', required=bool(original.get('notes')), maximum=500),
            })
        normalized['name'] = _single_line(variant.get('name'), 'translation.name', required=True, maximum=300)
        for field in ('notes', 'storage', 'reheating'):
            normalized[field] = _bounded_text(variant.get(field), 'translation.' + field,
                                               required=bool(recipe.get(field)), maximum=4000 if field == 'notes' else 1000)
        result[language] = normalized
    return result


def recipe_presentation(recipe, language=None):
    """Return separate display text without changing identity, evidence or queries."""
    requested = language_tag(language) if language is not None else recipe.get('language', 'nb-NO')
    canonical = recipe.get('language', 'nb-NO')
    from recipes import RecipeError
    variants = {}
    for tag, stored in (recipe.get('translations') or {}).items():
        try:
            variants.update(normalize_translations({tag: stored}, recipe))
        except RecipeError:
            # A previously stored variant can fail a newer label or digest check.
            continue
    # Older bindings omit source custody. Keep their bytes readable until a
    # reviewer supplies a variant bound to this source and revision.
    variants = {tag: v for tag, v in variants.items() if v['source_text_digest'] == source_text_digest(recipe)}
    available = [canonical, *variants]
    resolved = next((tag for tag in available if tag == requested), None)
    if resolved is None:
        family = requested.split('-')[0]
        resolved = next((tag for tag in available if tag.split('-')[0] == family), canonical)
    variant = variants.get(resolved)
    text = {field: deepcopy((variant or recipe).get(field)) for field in TEXT_FIELDS}
    from recipes import source_ingredient_identity
    text['ingredients'] = []
    for index, row in enumerate(recipe.get('ingredients', [])):
        label = {key: row.get(key) for key in ('item', 'notes')}
        if variant:
            proposed = variant['ingredients'][index]
            source_id, _ = source_ingredient_identity(row['item'], canonical)
            target_id, _ = source_ingredient_identity(proposed['item'], resolved)
            if source_id.casefold() == target_id.casefold():
                label = deepcopy(proposed)
            else:
                label['identity_fallback'] = True
            label['canonical_item'] = row['item']
        text['ingredients'].append(label)
    return {**text, 'requested_language': requested, 'resolved_language': resolved,
            'fallback': resolved.split('-')[0] != requested.split('-')[0]}


def menu_language(menu, language):
    result = deepcopy(menu)
    result['output_language'] = language_tag(language)
    for group in ('dishes', 'salads'):
        for recipe in result.get(group, []):
            presentation = recipe_presentation(recipe, language)
            recipe['presentation'] = {key: presentation[key] for key in ('name', 'requested_language', 'resolved_language', 'fallback')}
    return result


def display_recipe(recipe, language=None):
    from recipe_quantities import display_amount
    result = deepcopy(recipe)
    presentation = recipe_presentation(recipe, language)
    result['presentation'] = presentation
    for field in TEXT_FIELDS:
        result[field] = presentation[field]
    for row, label in zip(result.get('ingredients', []), presentation['ingredients']):
        translated = presentation['resolved_language'] != recipe.get('language', 'nb-NO')
        if translated:
            row['display_item'] = label['item']
            row['display_notes'] = label.get('notes')
            if row.get('quantity') is None:
                # Retain unstructured source quantities visibly, without inventing
                # a translated amount or mistaking a label for a complete amount.
                row['display_item'] += (' (quantity unspecified; source: ' if presentation['resolved_language'].split('-')[0] == 'en' else ' (mengde ikke angitt; kilde: ') + str(row.get('raw') or '') + ')'
        elif row.get('quantity') is not None:
            row['display_notes'] = label.get('notes')
        if row.get('quantity') is not None:
            shown = display_amount(row['quantity'], row.get('unit'), presentation['resolved_language'])
            if shown is not None:
                row['display_amount'] = shown
    return result
