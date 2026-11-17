"""One local stock review for the menu; explicit answers retain exact scope."""
from copy import deepcopy
from typing import Mapping

from core import HouseholdError
import menu_planning as mp
from product_planner import menu_requirements, _aggregation_identity
from recipe_quantities import UNITS, normalized_unit, read_quantity, quantity_json, display_amount


def _source_rows(menu):
    shopping = mp.shopping_menu(menu)
    return shopping, {
        mp.canonical({'collection': group, 'recipe_index': recipe_index, 'ingredient_index': ingredient_index}): raw
        for group in ('dishes', 'salads')
        for recipe_index, recipe in enumerate(shopping.get(group, []))
        for ingredient_index, raw in enumerate(recipe.get('shopping_requirements', []))
    }


def build_stock_check(state):
    menu = state.get('menu')
    if not menu:
        return None
    shopping, sources = _source_rows(menu)
    gross = {**shopping, 'available_ingredients': []}
    requirements, unknown = menu_requirements(gross)
    saved = state.get('menu_ingredient_decisions') or {}
    decisions = saved.get('decisions', []) if saved.get('menu_ref') == mp.menu_ref(menu) else []
    remaining, _ = menu_requirements(shopping, ingredient_decisions=decisions)
    needs = {row['requirement_id']: row for row in remaining}
    by_source = {mp.canonical(row['source']): row for row in decisions}
    pantry = (state.get('profile') or {}).get('pantry') or {}
    assumed = {_aggregation_identity(item) for item in pantry.get('assume', [])}
    rows = []
    for requirement in requirements:
        positions = requirement['sources']
        answers = [by_source.get(mp.canonical(position)) for position in positions]
        originals = [sources[mp.canonical(position)] for position in positions]
        need = needs.get(requirement['requirement_id'])
        rows.append({
            'requirement_id': requirement['requirement_id'], 'item': requirement['item'],
            'quantity': deepcopy(requirement['gross_quantity']), 'unit': requirement['unit'],
            'remaining_quantity': deepcopy(need['quantity']) if need else 0,
            'sources': deepcopy(positions), 'pantry': any(raw.get('pantry') is True for raw in originals),
            'optional': all(raw.get('optional') is True for raw in originals),
            'accepted_pantry_assumption': pantry.get('assumptions_accepted') is True and _aggregation_identity(requirement['identity']) in assumed,
            'status': ('answered' if all(answers) else 'partial' if any(answers)
                       else 'covered_by_available_ingredients' if need is None else 'unanswered'),
            'answered_sources': sum(answer is not None for answer in answers),
        })
    for issue in unknown:
        position = {key: issue[key] for key in ('collection', 'recipe_index', 'ingredient_index')}
        raw = sources[mp.canonical(position)]
        raw = raw if isinstance(raw, Mapping) else {}
        answer = by_source.get(mp.canonical(position))
        rows.append({'requirement_id': 'unresolved:' + mp.digest(position)[:24],
            'item': issue.get('item') or 'Unresolved ingredient', 'quantity': issue.get('quantity'),
            'unit': issue.get('unit'), 'sources': [position], 'pantry': raw.get('pantry') is True,
            'optional': raw.get('optional') is True, 'status': 'answered' if answer else 'unanswered',
            'answered_sources': int(answer is not None), 'unresolved_reason': issue['reason'],
            'accepted_pantry_assumption': False})
    return {'menu_ref': mp.menu_ref(menu), 'stock_ref': mp.digest({
                'menu_ref': mp.menu_ref(menu), 'sources': sources, 'available_ingredients': shopping.get('available_ingredients'),
                'decisions': decisions, 'pantry': pantry}),
            'items': rows, 'total_items': len(rows),
            'pending_count': sum(row['status'] in {'unanswered', 'partial'} for row in rows),
            'guidance': 'Resolve one combined stock check for this whole menu. Reuse answered sources and accepted ordinary pantry instructions; a recipe pantry flag or an unaccepted suggestion is not stock. Unknown optional amounts stay unresolved unless explicitly omitted or covered. A partial-stock answer is the total available amount for this ingredient, replacing prior source/request stock for it.'}


def stock_check_page(check, *, offset=0, limit=20, language=None):
    if check is None:
        return None
    result = {key: deepcopy(value) for key, value in check.items() if key != 'items'}
    rows = deepcopy(check['items'][offset:offset + limit])
    for row in rows:
        row['source_count'] = len(row['sources'])
        row['sources'] = row['sources'][:20]
        if (amount := display_amount(row.get('quantity'), row.get('unit'), language)) is not None:
            row['display_amount'] = amount
    result.update(items=rows, offset=offset, next_offset=offset + limit if offset + limit < len(check['items']) else None)
    return result


def stock_answer_decisions(state, stock_ref, answers):
    check = build_stock_check(state)
    if check is None or stock_ref != check['stock_ref']:
        raise HouseholdError('stock check changed; read the current shopping list before answering')
    if not isinstance(answers, list) or not 1 <= len(answers) <= 64:
        raise HouseholdError('stock_answers requires one to 64 explicit answers')
    shopping, sources = _source_rows(state['menu'])
    rows = {row['requirement_id']: row for row in check['items']}
    decisions, seen = [], set()
    for answer in answers:
        if (not isinstance(answer, Mapping) or set(answer) - {'requirement_id', 'action', 'quantity', 'unit'}
                or not isinstance(answer.get('requirement_id'), str) or answer['requirement_id'] not in rows
                or answer['requirement_id'] in seen or answer.get('action') not in {'have_all', 'have_quantity', 'include', 'omit'}):
            raise HouseholdError('stock answer requires one unique returned requirement_id and an explicit action')
        seen.add(answer['requirement_id'])
        row, action = rows[answer['requirement_id']], answer['action']
        available = None
        if action == 'have_quantity':
            conversion = UNITS.get(normalized_unit(answer.get('unit')))
            if row.get('unresolved_reason') or conversion is None or conversion[0] != row['unit']:
                raise HouseholdError('partial stock needs a known requirement and an exact compatible unit')
            try:
                available = read_quantity(answer.get('quantity')) * conversion[1]
            except ValueError as exc:
                raise HouseholdError(str(exc)) from exc
        elif 'quantity' in answer or 'unit' in answer:
            raise HouseholdError('only have_quantity accepts stock quantity and unit')
        for source in row['sources']:
            decision = {'source': deepcopy(source), 'action': action}
            if available is not None:
                raw = sources[mp.canonical(source)]
                amount = read_quantity(raw['quantity'], legacy_float=True) * UNITS[normalized_unit(raw['unit'])][1]
                allocated = min(amount, available)
                available -= allocated
                decision['action'] = 'have_all' if allocated == amount else 'include' if allocated == 0 else 'have_quantity'
                if decision['action'] == 'have_quantity':
                    decision.update(quantity=quantity_json(allocated), unit=row['unit'])
            decisions.append(decision)
    if len(decisions) > 512:
        raise HouseholdError('stock answers exceed the bounded source-decision limit')
    menu_requirements(shopping, ingredient_decisions=decisions)
    return decisions


def _exact_source_fingerprints(menu):
    """Only stable preparation slots can establish unchanged source identity."""
    if not menu.get('slots'):
        return {}
    shopping = mp.shopping_menu(menu)
    historical = set(menu.get('historical_slot_ids', []))
    by_recipe = {id(mp.recipe_for_slot(menu, slot)): slot for slot in mp.preparation_slots(menu)}
    result = {}
    for group in ('dishes', 'salads'):
        originals = [(recipe, by_recipe.get(id(recipe))) for recipe in menu.get(group, [])]
        originals = [(recipe, slot) for recipe, slot in originals if slot and slot['slot_id'] not in historical]
        for recipe_index, ((original, slot), prepared) in enumerate(zip(originals, shopping[group], strict=True)):
            source = {'slot_id': slot['slot_id'], 'owner': menu.get('slot_owners', {}).get(slot['slot_id'], menu['menu_id']),
                      'reference': slot.get('reference'), 'snapshot_digest': mp.recipe_snapshot_digest(original),
                      'shopping_snapshot_digest': mp.recipe_snapshot_digest(prepared), 'collection': group}
            for ingredient_index, raw in enumerate(prepared.get('shopping_requirements', [])):
                position = {'collection': group, 'recipe_index': recipe_index, 'ingredient_index': ingredient_index}
                result[mp.canonical(position)] = mp.digest({'source': source, 'ingredient_index': ingredient_index, 'requirement': raw})
    return result


def transfer_stock_answers(state, before, after):
    saved = state.get('menu_ingredient_decisions') or {}
    if not before or saved.get('menu_ref') != mp.menu_ref(before):
        return {}
    old, new = _exact_source_fingerprints(before), _exact_source_fingerprints(after)
    by_fingerprint = {}
    for position, fingerprint in new.items():
        by_fingerprint.setdefault(fingerprint, []).append(position)
    carried, dropped = [], []
    import json
    for decision in saved['decisions']:
        matches = by_fingerprint.get(old.get(mp.canonical(decision['source'])), [])
        if len(matches) == 1:
            carried.append({**deepcopy(decision), 'source': json.loads(matches[0])})
        else:
            dropped.append(deepcopy(decision['source']))
    if carried:
        menu_requirements(mp.shopping_menu(after), ingredient_decisions=carried)
        state['menu_ingredient_decisions'] = {'menu_ref': mp.menu_ref(after), 'decisions': carried}
    else:
        state.pop('menu_ingredient_decisions', None)
    return {'stock_answers_carried': len(carried), 'stock_answers_dropped': len(dropped),
            'stock_answers_dropped_sources': dropped[:20], 'stock_answers_dropped_sources_truncated': len(dropped) > 20}
