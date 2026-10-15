"""General shopping lists over the same requirements used by product preparation."""
from copy import deepcopy
from fractions import Fraction
from typing import Mapping

from core import HouseholdError
import menu_planning as mp
from product_planner import canonical, menu_requirements
from recipe_quantities import read_quantity, quantity_json


_AUTO = object()

def effective_ingredient_decisions(state, menu, supplied=None, *, reference=_AUTO):
    """Current recorded stock wins over a repeated, older preparation request."""
    saved = state.get('menu_ingredient_decisions') or {}
    reference = mp.menu_ref(menu) if reference is _AUTO else reference
    if reference is None or saved.get('menu_ref') != reference:
        return supplied, None
    menu_requirements(mp.shopping_menu(menu), ingredient_decisions=supplied)
    merged = {canonical(d['source']): d for d in supplied or []}
    merged.update({canonical(d['source']): deepcopy(d) for d in saved['decisions']})
    return list(merged.values()), mp.digest(saved['decisions'])


def _list_scope(state, menu, annotations):
    """Checks belong to a shopping task; explicit replans continue that task."""
    saved = annotations.get('menu_ref') or {}
    current = menu
    seen = set()
    while current:
        if saved.get('menu_id') == current.get('menu_id'):
            return annotations.get('scope')
        ancestor = current.get('supersedes')
        if not ancestor:
            break
        key = f"{ancestor['menu_id']}:{ancestor['revision']}"
        if key in seen:
            break
        seen.add(key)
        if ancestor == saved:
            return annotations.get('scope')
        current = (state.get('menu_planning') or {}).get('history', {}).get(key)
        if current and mp.menu_ref(current) != ancestor:
            break
    return menu.get('menu_id') if menu else None


def _row_digest(row, scope):
    return mp.digest({'scope': scope, 'row': {
        key: value for key, value in row.items() if key not in {'sources', 'source_count'}}})


def _extras(value):
    if not isinstance(value, list) or len(value) > 100:
        raise HouseholdError('extra_items must be a list of at most 100 items')
    rows, ids = [], set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) - {'id', 'item', 'quantity', 'unit'}:
            raise HouseholdError('extra items accept id, item, optional quantity and unit')
        identity, name = item.get('id'), item.get('item')
        if (not isinstance(identity, str) or not 1 <= len(identity) <= 80
                or identity in ids or not isinstance(name, str) or not name.strip() or len(name) > 300):
            raise HouseholdError('extra items require unique bounded ids and nonempty names')
        row = {'id': identity, 'item': name.strip()}
        quantity, unit = item.get('quantity'), item.get('unit')
        if quantity is not None:
            try:
                row['quantity'] = quantity_json(read_quantity(quantity))
            except ValueError as exc:
                raise HouseholdError(str(exc)) from exc
            if not isinstance(unit, str) or not unit.strip() or len(unit) > 40:
                raise HouseholdError('a quantified extra item needs a unit')
        if unit is not None:
            if not isinstance(unit, str) or not unit.strip() or len(unit) > 40:
                raise HouseholdError('extra item unit must be bounded text')
            row['unit'] = unit.strip()
        ids.add(identity)
        rows.append(row)
    return rows


def build_list(state):
    menu = state.get('menu')
    reference = mp.menu_ref(menu) if menu else None
    annotations = state.get('shopping_list') or {}
    rows = []
    if menu:
        decisions, _ = effective_ingredient_decisions(state, menu)
        requirements, unresolved = menu_requirements(mp.shopping_menu(menu), ingredient_decisions=decisions)
        for requirement in requirements:
            rows.append({'id': requirement['requirement_id'], 'kind': 'ingredient',
                         **{key: deepcopy(requirement[key]) for key in
                            ('item', 'quantity', 'unit', 'gross_quantity', 'confirmed_pantry_quantity', 'sources')}})
        for issue in unresolved:
            position = {key: issue[key] for key in ('collection', 'recipe_index', 'ingredient_index')}
            rows.append({'id': 'unresolved:' + mp.digest(position)[:24], 'kind': 'unresolved',
                         'item': issue.get('item') or 'Unresolved ingredient',
                         'quantity': issue.get('quantity'), 'unit': issue.get('unit'),
                         'reason': issue['reason'], 'sources': [position]})
    for extra in annotations.get('extra_items', []):
        rows.append({**deepcopy(extra), 'id': 'extra:' + extra['id'], 'extra_id': extra['id'], 'kind': 'extra'})
    scope = _list_scope(state, menu, annotations)
    fingerprints = {row['id']: _row_digest(row, scope) for row in rows}
    checked = annotations.get('checked', {})
    baseline = annotations.get('baseline', {})
    changes = {'added': [], 'changed': [], 'removed': sorted(set(baseline) - set(fingerprints))}
    for row in rows:
        key = row['id']
        fingerprint = fingerprints[key]
        row['checked'] = checked.get(key) == fingerprint
        row['check_needs_review'] = key in checked and not row['checked']
        if key not in baseline:
            changes['added'].append(key)
        elif baseline[key] != fingerprint:
            changes['changed'].append(key)
    return {'menu_ref': reference, 'scope': scope, 'list_ref': mp.digest({'menu_ref': reference, 'rows': rows, 'annotations': annotations}),
            'items': rows, 'changes': changes, 'fingerprints': fingerprints,
            'cart_changed': False, 'provider': state.get('provider', 'oda'),
            'note': 'List checks and personal extras do not change pantry facts, product plans or carts. Changed quantities reopen checked rows. Record stock explicitly before preparing an order.'}


def _page(value, request):
    offset, limit = request.get('offset', 0), request.get('limit', 12)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise HouseholdError('shopping list pages require offset >= 0 and limit from 1 to 20')
    result = {key: deepcopy(child) for key, child in value.items() if key not in {'items', 'fingerprints', 'changes', 'scope'}}
    result['changes'] = {key: {'count': len(ids), 'ids': ids[offset:offset + limit]} for key, ids in value['changes'].items()}
    result['changes_next_offset'] = (offset + limit if any(
        len(ids) > offset + limit for ids in value['changes'].values()) else None)
    rows = deepcopy(value['items'][offset:offset + limit])
    for row in rows:
        if 'sources' in row:
            row['source_count'] = len(row['sources'])
            row['sources'] = row['sources'][:20]
    result.update(items=rows, total_items=len(value['items']), offset=offset,
                  next_offset=offset + limit if offset + limit < len(value['items']) else None)
    lines = []
    for row in rows:
        quantity = row.get('quantity')
        if isinstance(quantity, Mapping):
            number = Fraction(quantity['numerator'], quantity['denominator'])
            quantity = str(number.numerator) if number.denominator == 1 else str(float(number))
        measure = ' '.join(str(part) for part in (quantity, row.get('unit')) if part is not None)
        lines.append(f"[{'x' if row['checked'] else ' '}] {row['item']}" + (f' — {measure}' if measure else ''))
    result['text'] = '\n'.join(lines)
    return result


def handle_shopping_list(app, request):
    action = request.get('action', 'get')
    if action not in {'get', 'update', 'refresh', 'match'}:
        raise HouseholdError('unknown shopping_list action')
    current = build_list(app.store.read())
    # Validate pagination before any local write or provider call.
    _page(current, request)
    if action == 'get':
        return _page(current, request)
    if request.get('list_ref') != current['list_ref']:
        raise HouseholdError('shopping list changed; read it again before updating or matching')
    if action in {'update', 'refresh'}:
        extras = _extras(request['extra_items']) if 'extra_items' in request else None
        checks = request.get('checked_items', {})
        if not isinstance(checks, Mapping) or any(type(value) is not bool for value in checks.values()):
            raise HouseholdError('checked_items must map returned item ids to booleans')
        if any(key not in current['fingerprints'] for key in checks):
            raise HouseholdError('checked_items contains an unknown list item')
        with app.store.locked() as state:
            if build_list(state)['list_ref'] != current['list_ref']:
                raise HouseholdError('shopping list changed; read it again before updating')
            annotations = state.setdefault('shopping_list', {'extra_items': [], 'checked': {}, 'baseline': {}})
            if extras is not None:
                annotations['extra_items'] = extras
            for key, value in checks.items():
                if value:
                    annotations['checked'][key] = current['fingerprints'][key]
                else:
                    annotations['checked'].pop(key, None)
            updated = build_list(state)
            annotations['checked'] = {key: value for key, value in annotations['checked'].items()
                                      if key in updated['fingerprints']}
            annotations['baseline'] = updated['fingerprints']
            annotations['menu_ref'] = updated['menu_ref']
            annotations['scope'] = updated['scope']
            result = build_list(state)
            result['changes'] = updated['changes']
        return _page(result, request)
    if app.provider == 'none':
        return {**_page(current, request), 'matching_status': 'not_configured',
                'next': 'The general list is available. Connect a store account for optional catalog matching; no order is required.'}
    if current['menu_ref'] is None:
        raise HouseholdError('save a menu before matching its ingredients; personal extras can be searched with catalog')
    arguments = {'operation': 'products', 'action': 'prepare', 'menu_ref': current['menu_ref'],
                 'include_recurring': False, 'response_view': 'agent', 'price_mode': request.get('price_mode', 'estimate'),
                 'offset': request.get('offset', 0), 'limit': request.get('limit', 8)}
    for key in ('candidate_approvals', 'product_plan_ref', 'continuation_mode'):
        if request.get(key) is not None:
            arguments[key] = request[key]
    try:
        if request.get('product_plan_ref') is not None:
            record = app._product_plan_record(app.store.read(), request['product_plan_ref'])
            if record.get('menu_ref') != current['menu_ref'] or record.get('include_recurring', True):
                raise HouseholdError('product review must match this menu and exclude recurring groceries')
            arguments.pop('menu_ref')
        matched = app.handle(arguments)
    except HouseholdError as exc:
        return {**_page(current, request), 'matching_status': 'unavailable', 'matching_error': str(exc),
                'next': 'The general list remains available. Resolve catalog access before requesting matching again.'}
    latest = build_list(app.store.read())
    return {'list_ref': current['list_ref'], 'menu_ref': current['menu_ref'], 'provider': app.provider,
            'matching_status': 'stale' if latest['list_ref'] != current['list_ref'] else matched.get('status', 'needs_input'),
            'general_list_arguments': {'action': 'get'},
            'product_result': matched, 'cart_changed': False,
            'unmatched_extra_items': sum(row['kind'] == 'extra' for row in current['items']),
            'price_scope': 'Menu ingredients only; personal extras, recurring items and delivery fees are excluded. Prices belong to the connected retailer, not another physical shop.',
            'next': 'Read further product pages with products.get; follow returned preparation continuations. Ordering requires the normal separately requested products.apply and checkout flow.'}
