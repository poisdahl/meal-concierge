"""Explicit current-plan batch dependencies and exact portion accounting."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from core import HouseholdError
import menu_planning as mp
from recipe_quantities import read_quantity, quantity_json

CONFIRMATION_STATEMENT = "I confirm these exact batch portions, suitability and storage facts for this plan."


def fraction(value, *, zero=False):
    try:
        if isinstance(value, dict) and set(value) == {'numerator','denominator'}:
            n, d = value['numerator'], value['denominator']
            if type(n) is not int or type(d) is not int or not 0 < d <= 1_000_000 or abs(n) > 1_000_000_000:
                raise ValueError()
            result = Fraction(n,d)
        elif type(value) in {str,int} and len(str(value)) <= 30:
            decimal = Decimal(str(value))
            if not decimal.is_finite() or decimal < 0 or decimal > 1000 or not -30 <= decimal.as_tuple().exponent <= 30:
                raise ValueError()
            result = Fraction(decimal)
        else:
            raise ValueError()
        if result < 0 or (not zero and result == 0) or result > 1000 or result.denominator > 1_000_000:
            raise ValueError()
        return result
    except (ValueError, InvalidOperation, OverflowError, ZeroDivisionError):
        raise HouseholdError('portions require an exact bounded decimal string, integer or rational') from None


def rational(value):
    return {'numerator':value.numerator,'denominator':value.denominator}


def normalize(state, menu, value, today):
    expected={'source_slot_id','source_snapshot_digest','prepared_portions','consumed_at_source','suitability','storage','leftovers'}
    if not isinstance(value,dict) or set(value) != expected:
        raise HouseholdError('batch specification needs exact source, portions, suitability, storage and dependent slots')
    reserved = {b['source_slot_id'] for b in sources(menu)} | {s['slot_id'] for s in menu['slots'] if s.get('kind') == 'leftover'}
    if value['source_slot_id'] in reserved or (isinstance(value['leftovers'], list) and any(x.get('slot_id') in reserved for x in value['leftovers'] if isinstance(x, dict))):
        raise HouseholdError('batch component intersects an existing source or leftover')
    source=mp.slot_by_id(menu,value['source_slot_id'])
    if value['source_snapshot_digest'] != source['snapshot_digest'] or source.get('kind') == 'leftover':
        raise HouseholdError('batch source must be one exact fresh recipe snapshot')
    if source['date'] < today or mp.slot_outcome(state,menu,source) is not None:
        raise HouseholdError('batch source must be an unrecorded current/future meal')
    recipe=next(r for r in menu['dishes']+menu['salads'] if r['recipe_key']==source['recipe_key'])
    prepared=fraction(value['prepared_portions']); consumed=fraction(value['consumed_at_source'])
    if consumed != fraction(str(recipe['portions'])) or prepared <= consumed:
        raise HouseholdError('source consumption must match its exact meal portions, with explicit extra preparation')
    suitability=value['suitability']; storage=value['storage']
    if suitability != {'source':'current_user','value':'suitable'}:
        raise HouseholdError('structured current-user batch suitability is missing; prose/boolean inference is unsupported')
    if not isinstance(storage,dict) or set(storage).difference({'source','method','max_interval_days','use_by_date'}) or storage.get('source')!='current_user' or storage.get('method') not in {'refrigerated','frozen'}:
        raise HouseholdError('explicit structured refrigerated/frozen storage facts are required')
    days=storage.get('max_interval_days'); use_by=storage.get('use_by_date')
    if days is None and use_by is None:
        raise HouseholdError('an explicit maximum interval or use-by date is required')
    last=date.max
    if days is not None:
        if type(days) is not int or not 1 <= days <= 365:
            raise HouseholdError('maximum interval must be an explicit bounded day count')
        last=date.fromisoformat(source['date'])+timedelta(days=days)
    if use_by is not None:
        try:
            parsed=date.fromisoformat(use_by)
            if parsed.isoformat()!=use_by: raise ValueError()
        except (ValueError,TypeError):
            raise HouseholdError('use-by must be an exact ISO date') from None
        last=min(last,parsed)
    raw=value['leftovers']
    if not isinstance(raw,list) or not 1 <= len(raw) <= 6:
        raise HouseholdError('batch needs one to six exact dependent slots')
    dependents=[]; seen=set(); available=prepared-consumed
    for item in raw:
        if not isinstance(item,dict) or set(item)!={'slot_id','portions'}:
            raise HouseholdError('each leftover needs an exact target slot_id and portions')
        target=mp.slot_by_id(menu,item['slot_id'])
        if target['slot_id'] in seen or target['slot_id']==source['slot_id']:
            raise HouseholdError('leftover target is duplicated or equals its source')
        seen.add(target['slot_id'])
        if not source['date'] < target['date'] <= last.isoformat() or mp.slot_outcome(state,menu,target) is not None:
            raise HouseholdError('leftovers must be after their source, within the exact interval, and unrecorded')
        portions=fraction(item['portions']); available-=portions
        if available < 0:
            raise HouseholdError('leftover portions exceed exact source remainder')
        dependents.append({'replaces_slot_id':target['slot_id'],'date':target['date'],'meal_type':target['meal_type'],'portions':rational(portions)})
    locks=set(state['menu_planning']['locks'].get(mp.lock_key(menu),[]))
    if locks & (seen|{source['slot_id']}):
        raise HouseholdError('batch component intersects explicit locks; unlock the component first')
    spec={'source_slot_id':source['slot_id'],'source_snapshot_digest':source['snapshot_digest'],
          'prepared_portions':rational(prepared),'consumed_at_source':rational(consumed),
          'unallocated_portions':rational(available),'suitability':deepcopy(suitability),'storage':deepcopy(storage),
          'leftovers':sorted(dependents,key=lambda d:(d['date'],d['replaces_slot_id']))}
    spec['spec_digest']=mp.digest({'menu_ref':mp.menu_ref(menu),'spec':spec})
    for dependent in spec['leftovers']:
        dependent['slot_id']='slot_'+mp.digest({'spec':spec['spec_digest'],'target':dependent})[:32]
    return spec


def shopping(menu):
    result = deepcopy(menu)
    for batch in sources(menu):
        source = mp.slot_by_id(menu, batch['source_slot_id'])
        if source['slot_id'] in menu.get('historical_slot_ids', []):
            continue
        factor = fraction(batch['prepared_portions']) / fraction(batch['consumed_at_source'])
        for recipe in result['dishes'] + result['salads']:
            if recipe['recipe_key'] != source['recipe_key']:
                continue
            for requirement in recipe['shopping_requirements']:
                if requirement.get('scalable') is True:
                    try:
                        requirement['quantity'] = quantity_json(read_quantity(requirement['quantity'], legacy_float=True) * factor)
                    except (ValueError, ZeroDivisionError):
                        requirement['scalable'] = False
            recipe['batch_prepared_portions'] = deepcopy(batch['prepared_portions'])
    return result


def dependency_status(state, menu):
    statuses = []
    for batch in sources(menu):
        actual = state['batch_outcomes']['sources'].get(batch['source_slot_id'])
        for slot in menu['slots']:
            if slot.get('source_slot_id') != batch['source_slot_id']:
                continue
            recorded = state['batch_outcomes']['leftovers'].get(slot['slot_id'])
            status = recorded['outcome'] if recorded else ('needs_replan' if actual and (actual['outcome'] != 'cooked' or not actual['matches_plan']) else 'confirmed_source' if actual else 'planned_not_confirmed')
            statuses.append({'slot_id': slot['slot_id'], 'source_slot_id': batch['source_slot_id'], 'status': status, 'new_shopping_requirements': 0})
    return statuses


def record_outcome(state,menu,slot,request):
    batch = next((b for b in sources(menu) if b['source_slot_id'] in {slot['slot_id'], slot.get('source_slot_id')}), None)
    if not batch:
        if slot.get('kind')=='leftover':
            raise HouseholdError('historical leftover source context is unavailable; no outcome changed')
        return False
    source_id=batch['source_slot_id']; cooked=request['action']=='mark_cooked'
    data=state['batch_outcomes']
    if slot['slot_id']==source_id:
        if len(data['sources'])>=2000 and source_id not in data['sources']:
            raise HouseholdError('batch outcome limit reached')
        actual=request.get('actual_batch')
        if cooked:
            if not isinstance(actual,dict) or set(actual)!={'prepared_portions','consumed_at_source'}:
                raise HouseholdError('source cooking needs explicit actual prepared/consumed portions')
            prepared=fraction(actual['prepared_portions']); consumed=fraction(actual['consumed_at_source'],zero=True)
            if consumed>prepared: raise HouseholdError('actual consumed portions exceed preparation')
            remaining=prepared-consumed
            matches=prepared==fraction(batch['prepared_portions']) and consumed==fraction(batch['consumed_at_source'])
            data['sources'][source_id]={'outcome':'cooked','prepared_portions':rational(prepared),'consumed_at_source':rational(consumed),
                'confirmed_remaining':rational(remaining),'matches_plan':matches}
        else:
            data['sources'][source_id]={'outcome':'not_cooked','matches_plan':False}
        return False
    if slot.get('source_slot_id')==source_id:
        if len(data['leftovers'])>=2000 and slot['slot_id'] not in data['leftovers']:
            raise HouseholdError('batch outcome limit reached')
        actual=data['sources'].get(source_id)
        if cooked:
            if not actual or actual['outcome']!='cooked' or not actual['matches_plan']:
                raise HouseholdError('leftover needs an explicitly cooked matching source; replan after a mismatch')
            used=sum((fraction(value['portions']) for key,value in data['leftovers'].items()
                      if key!=slot['slot_id'] and value.get('source_slot_id')==source_id and value['outcome']=='cooked'),Fraction(0))
            if used+fraction(slot['portions'])>fraction(actual['confirmed_remaining'],zero=True):
                raise HouseholdError('leftover exceeds confirmed remaining portions')
        data['leftovers'][slot['slot_id']]={'source_slot_id':source_id,'outcome':'cooked' if cooked else 'not_cooked','portions':deepcopy(slot['portions'])}
        return True
    return False


def evaluate_plan(state, original, successor):
    from planner import _effective_facts, _hard_evaluation, _strict_evaluation
    handoff=original.get('planner_selection') or original.get('replan_selection')
    if not handoff:
        return {'status':'unknown','reason':'an exact structured planner selection is required'}
    facts={mp.canonical({k:v for k,v in c.items() if k!='facts'}):c.get('facts',{}) for c in handoff['request']['candidates']}
    recipes={r['recipe_key']:r for r in successor['dishes']}
    candidates=[]; hard=[]
    for slot in successor['slots']:
        recipe=recipes[slot['recipe_key']]
        candidate={'recipe':recipe,'recipe_key':slot['recipe_key'],'usage':{'eligible':True},'materialization_error':None,
                   'facts':_effective_facts(recipe,facts.get(mp.canonical(slot['reference']),{}))}
        check=_hard_evaluation(candidate,state['profile'],{})
        hard.append(check); candidates.append(candidate)
    strict=_strict_evaluation(tuple(candidates),handoff['request'].get('strict_targets',[]),state['profile'])
    return {'status':'pass' if all(c['status']=='pass' for c in hard) and strict['status']=='pass' else 'unknown',
            'hard':hard,'strict':strict}


def sources(menu):
    return menu.get('batches', []) or ([menu['batch']] if menu.get('batch') else [])


def recurring_layout(profile, eating_dates):
    """Allocate accepted preparation ranges without changing consumption or dates."""
    meals = profile['meals']
    if meals.get('meal_mode', 'fresh') == 'fresh':
        return None
    if not meals.get('recurring_batch_accepted'):
        raise HouseholdError('Accept the exact recurring batch settings before reusing them.')
    cooking = [day for day in eating_dates if date.fromisoformat(day).strftime('%A').casefold() in {d.casefold() for d in meals['cook_days']}]
    if not cooking or cooking[0] != eating_dates[0] or len(cooking) != meals['dishes']:
        raise HouseholdError('Cooking days must start on the first eating day and match dishes; propose explicit cooking-day/dish settings.')
    count = meals['batch_dishes']
    if not 1 <= count <= len(cooking) or (meals['meal_mode'] == 'batch' and count != len(cooking)):
        raise HouseholdError('Batch count must fit cooking days; batch mode requires every dish to be a batch.')
    low, high = meals['prepared_portion_range']
    portions = meals['portions']
    intervals = {day: [d for d in eating_dates if day < d < (cooking[i + 1] if i + 1 < len(cooking) else '9999-12-31')] for i, day in enumerate(cooking)}
    # Repeated eating dates determine which cooking sessions need batches.
    batch_days = {day for day in cooking if intervals[day]}
    if len(batch_days) != count:
        raise HouseholdError('batch_dishes must match cooking sessions with dependent eating days; adjust cooking days or batch count explicitly')
    result = []
    shortages = []
    for index, day in enumerate(cooking):
        next_day = cooking[index + 1] if index + 1 < len(cooking) else '9999-12-31'
        dependents = [d for d in eating_dates if day < d < next_day]
        is_batch = day in batch_days
        needed = portions * (1 + len(dependents))
        prepared = max(low, needed) if is_batch else portions
        if (is_batch and (not dependents or prepared > high)) or (dependents and not is_batch):
            shortages.append({'source_date': day, 'needed_portions': needed, 'available_portions': high if is_batch else portions,
                              'uncovered_dates': dependents, 'proposed_prepared_portions': needed,
                              'adjustment': 'Explicitly accept this prepared quantity or add a fresh cooking meal on the uncovered days.'})
        result.append({'source_date': day, 'eating_dates': [day] + dependents, 'batch': is_batch,
                       'prepared_portions': prepared, 'consumed_at_source': portions})
    return {'sources': result, 'shortages': shortages, 'required_portions': len(eating_dates) * portions,
            'available_portions': sum(min(s['prepared_portions'], high) if s['batch'] else portions for s in result),
            'accepted_settings': deepcopy(meals)}


def attach_recurring(menu, layout, resolved):
    original = deepcopy(menu['slots'])
    by_day = {s['date']: s for s in original}
    candidates = {c['recipe_key']: c for c in resolved}
    batches = []
    for allocation in layout['sources']:
        if not allocation['batch']:
            continue
        source = by_day[allocation['source_date']]
        candidate = candidates[source['recipe_key']]
        guidance = deepcopy(candidate.get('supplied_facts', {}).get('batch_guidance'))
        if guidance is None:
            guidance = {'basis': 'unknown', 'suitability': 'unknown', 'storage': 'Recipe-specific storage life not established; plan prompt cooling and freezing only if suitable.',
                        'reheating': 'Check recipe-specific reheating instructions before using leftovers.'}
        spec = {'source_slot_id': source['slot_id'], 'source_snapshot_digest': source['snapshot_digest'],
                'prepared_portions': rational(Fraction(allocation['prepared_portions'])),
                'consumed_at_source': rational(Fraction(allocation['consumed_at_source'])),
                'unallocated_portions': rational(Fraction(allocation['prepared_portions'] - len(allocation['eating_dates']) * allocation['consumed_at_source'])),
                'suitability': {'source': guidance['basis'], 'value': guidance['suitability']},
                'storage': guidance, 'leftovers': [], 'recurring_settings': deepcopy(layout['accepted_settings'])}
        for day in allocation['eating_dates'][1:]:
            slot = {**deepcopy(source), 'date': day, 'kind': 'leftover', 'source_slot_id': source['slot_id'],
                    'portions': rational(Fraction(allocation['consumed_at_source']))}
            slot['slot_id'] = 'slot_' + mp.digest({'source': source['slot_id'], 'date': day})[:32]
            menu['slots'].append(slot)
            spec['leftovers'].append({'slot_id': slot['slot_id'], 'date': day, 'portions': slot['portions'], 'meal_type': 'dinner'})
        spec['spec_digest'] = mp.digest(spec)
        batches.append(spec)
    menu['batches'] = batches
    menu['slots'].sort(key=lambda s: s['date'])
    menu['planning_scope'] = {'dates': [s['date'] for s in menu['slots']], 'portions': layout['accepted_settings']['portions']}
    names = {r['recipe_key']: r['name'] for r in menu['dishes']}
    menu['schedule'] = [{'day': s['date'], 'meal': names[s['recipe_key']] + (' (rester)' if s.get('kind') == 'leftover' else ''),
                         'slot_id': s['slot_id'], 'recipe_key': s['recipe_key']} for s in menu['slots']]
