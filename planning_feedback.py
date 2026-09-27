"""Bounded explicit household planning feedback, never inferred preferences."""
from copy import deepcopy
from datetime import date
import secrets

from core import HouseholdError
from menu_planning import digest

POLICY_VERSION = "explicit-feedback-v1"
MAX_EVENTS = 500
RETENTION_DAYS = 180


def _visibility(events):
    disabled = set()
    suppressed = set()
    for event in reversed(events):
        if event['event_id'] in disabled:
            continue
        if event['kind'] == 'undo':
            disabled.update(event['targets'])
        elif event['kind'] == 'reset':
            suppressed.update((target, event.get('recipe_key')) for target in event['targets'])
    return disabled, suppressed


def experiences(events, recipe_keys=None):
    disabled, suppressed = _visibility(events)
    result = []
    for event in events:
        if event['kind'] != 'experience' or event['event_id'] in disabled:
            continue
        key = event['binding']['target']['recipe_key']
        if (event['event_id'], None) in suppressed or (event['event_id'], key) in suppressed or recipe_keys is not None and key not in recipe_keys:
            continue
        result.append({'event_id': event['event_id'], 'date': event['date'], 'recipe_key': key,
                       'experience': deepcopy(event['binding']['experience']), 'reason': event.get('reason'),
                       'target': deepcopy(event['binding']['target']),
                       'portion_context': deepcopy(event['binding'].get('portion_context'))})
    return result


def effective(events, as_of_date):
    today = date.fromisoformat(as_of_date)
    disabled, suppressed = _visibility(events)
    active = []
    totals = {}
    for event in events:
        if event['event_id'] in disabled or (event['event_id'], None) in suppressed or event['kind'] not in {'reject', 'swap'}:
            continue
        age = (today - date.fromisoformat(event['date'])).days
        strength = 2 if 0 <= age < 30 else 1 if 30 <= age < 60 else 0
        if not strength:
            continue
        for contribution in event['contributions']:
            key = contribution['recipe_key']
            if (event['event_id'], key) in suppressed:
                continue
            value = strength * contribution['direction']
            active.append({'event_id':event['event_id'], 'date':event['date'], 'recipe_key':key, 'weight':value})
            totals[key] = totals.get(key, 0) + value
    active.sort(key=lambda e:(e['event_id'], e['recipe_key']))
    result = {'policy_version':POLICY_VERSION, 'as_of_date':as_of_date, 'events':active,
              'signals':{key:max(-6,min(6,value)) for key,value in sorted(totals.items())}}
    result['feedback_digest'] = digest(result)
    return result


def compact(events, as_of_date):
    # Undo/reset dependencies form components. Drop only wholly expired components,
    # so no retained correction can dangle or resurrect a removed original.
    today = date.fromisoformat(as_of_date)
    groups = {e['event_id']:{e['event_id']} for e in events}
    for event in events:
        for target in event.get('targets', []):
            if target not in groups:
                raise HouseholdError('feedback correction has a missing dependency')
            merged = groups[event['event_id']] | groups[target]
            for member in merged:
                groups[member] = merged
    expired = {e['event_id'] for e in events if (today-date.fromisoformat(e['date'])).days >= RETENTION_DAYS}
    remove = {key for key, group in groups.items() if group <= expired}
    return [deepcopy(e) for e in events if e['event_id'] not in remove]


def prior(events, key, signature):
    for event in events:
        if event['idempotency_key'] == key:
            if event['request_digest'] != signature:
                raise HouseholdError('feedback idempotency key conflicts with its original request')
            return deepcopy(event)
    return None


def append(events, *, kind, binding, contributions, reason, key, signature, as_of_date, targets=None, recipe_key=None):
    event = {'event_id':'feedback_'+secrets.token_hex(16), 'kind':kind, 'date':as_of_date,
             'binding':deepcopy(binding), 'contributions':deepcopy(contributions), 'reason':reason,
             'idempotency_key':key, 'request_digest':signature}
    if targets is not None:
        event['targets'] = sorted(targets)
    if recipe_key is not None:
        event['recipe_key'] = recipe_key
    retained = compact([*events, event], as_of_date)
    if len(retained) > MAX_EVENTS:
        raise HouseholdError('feedback event limit reached; retained correction groups cannot be discarded')
    events[:] = retained
    return deepcopy(event)


def _version_identity(reference):
    """Normalize only exact built-in reference aliases, never source/title matches."""
    if not isinstance(reference, dict):
        return None
    bank = reference.get('recipe_ref')
    library = reference.get('library_recipe_ref')
    if bank:
        return ('builtin', bank.get('id'), str(bank.get('revision')))
    if library:
        return (library.get('library_id'), library.get('recipe_id'), str(library.get('version')))
    if reference.get('discovery_ref'):
        return ('discovery', reference['discovery_ref'])
    # Legacy feedback targets contain the inner reference.
    if 'library_id' in reference:
        return _version_identity({'library_recipe_ref': reference})
    if 'id' in reference and 'revision' in reference:
        return _version_identity({'recipe_ref': reference})
    return None


def household_experience(events, reference):
    identity = _version_identity(reference)
    if identity is None:
        return None
    reports = [event for event in experiences(events)
               if _version_identity(event['target'].get('reference')) == identity]
    if not reports:
        return None
    return {'basis': 'household_reports_for_exact_recipe_version', 'report_count': len(reports),
            'recent_reports': [{key: event[key] for key in ('date', 'experience', 'portion_context')}
                               for event in reports[-1:]]}


def attach_experience(recipe, events, *, discovery_ref=None):
    reference = {key: recipe[key] for key in ('recipe_ref', 'library_recipe_ref', 'discovery_ref') if key in recipe}
    if not reference and discovery_ref:
        reference = {'discovery_ref': discovery_ref}
    if not reference and recipe.get('id') and recipe.get('revision'):
        reference = {'recipe_ref': {'id': recipe['id'], 'revision': recipe['revision']}}
    summary = household_experience(events, reference)
    if summary:
        recipe['household_experience'] = summary
