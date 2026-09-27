"""Conservative history clocks, reachability and explicit offline archival.

Recipe-bank data and external-operation journals are never archival candidates.
The installer owns the stopped-service, state-lock and complete-backup boundary.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re


PATHS = (
    ('recipe_usage',),
    *(('menu_planning', key) for key in ('history', 'locks', 'retired', 'applied', 'outcomes')),
    *(('batch_outcomes', key) for key in ('sources', 'leftovers')),
)
CLOCKS = 'history_retention'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def same_json(left, right):
    """Equality must preserve JSON number/bool types used by frozen digests."""
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        return left.hex() == right.hex()
    return left == right


def records(state):
    result = {}
    for path in PATHS:
        value = state
        for key in path:
            value = value[key]
        if not isinstance(value, dict):
            raise ValueError('history collection must be an object')
        result['/'.join(path)] = value
    return result


def clocks(state):
    value = state.get(CLOCKS, {'version': 1, 'records': {}})
    if not isinstance(value, dict) or set(value) != {'version', 'records'} or value['version'] != 1 or not isinstance(value['records'], dict):
        raise ValueError('unsupported history retention clocks')
    return value['records']


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('history event time must include a timezone')
    return parsed.astimezone(timezone.utc)


def snapshot_activity(state):
    """Copy mutable activity only; retained exact menu snapshots are immutable.

    Startup and offline preview verify their content fingerprints. Existing
    history keys need no copy/hash for an unrelated native state transaction.
    """
    if state.get('version') != 13:
        return None
    try:
        groups = records(state)
    except (KeyError, TypeError, ValueError):
        return None  # Startup can still be filling additive state defaults.
    return {group: frozenset(entries) if group == 'menu_planning/history' else deepcopy(entries)
            for group, entries in groups.items()}


def track_changes(state, *, before=None, now=None):
    """First observation and every changed record begin a conservative clock.

    Comparing content also detects edits made outside the current StateStore.
    Legacy records do not inherit an assumed last event from their menu date.
    """
    if state.get('version') != 13:
        raise ValueError('update the runtime/state before history maintenance')
    observed = (now or datetime.now(timezone.utc)).isoformat()
    previous = clocks(state)
    prior_records = before or {}
    tracked = {}
    for group, entries in records(state).items():
        old = previous.get(group, {})
        if not isinstance(old, dict):
            raise ValueError('invalid history retention clocks')
        tracked[group] = {}
        for key, value in entries.items():
            clock = old.get(key)
            valid = isinstance(clock, dict) and set(clock) == {'fingerprint', 'last_changed_at'}
            if valid:
                try:
                    timestamp(clock['last_changed_at'])
                except (TypeError, ValueError):
                    valid = False
            # An unrelated write reuses clocks without hashing every recipe
            # snapshot. Offline preview still verifies every fingerprint.
            if valid and key in prior_records.get(group, {}):
                if group == 'menu_planning/history' or same_json(value, prior_records[group][key]):
                    tracked[group][key] = clock
                    continue
            fingerprint = digest(value)
            tracked[group][key] = clock if valid and clock['fingerprint'] == fingerprint else {'fingerprint': fingerprint, 'last_changed_at': observed}
    state[CLOCKS] = {'version': 1, 'records': tracked}


def strings(value):
    if isinstance(value, str):
        yield value
        # Usage replay records can contain a canonical JSON request signature.
        if value.startswith(('{', '[')):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(parsed, (dict, list)):
                    yield from strings(parsed)
    elif isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def identities(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and (key in {'menu_id', 'slot_id', 'source_slot_id', 'order_id'} or key.endswith('_menu_id')):
                yield child
            yield from identities(child)
    elif isinstance(value, list):
        for child in value:
            yield from identities(child)


def latest_date(value):
    """Include future meal dates as well as explicitly recorded event times."""
    latest = date.min
    for text in strings(value):
        try:
            if re.fullmatch(r'\d{4}-W\d{2}', text):
                candidate = date.fromisocalendar(int(text[:4]), int(text[-2:]), 7)
            elif re.match(r'^\d{4}-\d{2}-\d{2}(?:$|T)', text):
                candidate = date.fromisoformat(text[:10])
            else:
                continue
        except ValueError:
            return date.max
        latest = max(latest, candidate)
    return latest


def plan(state, *, today=None):
    if state.get('version') != 13:
        raise ValueError('update the runtime/state before history maintenance')
    today = today or datetime.now(timezone.utc).date()
    cooldown = state['profile']['recipes']['repeat_cooldown_weeks']
    if type(cooldown) is not int or not 0 <= cooldown <= 260:
        raise ValueError('invalid repeat cooldown')
    weeks = max(52, cooldown)
    cutoff = today - timedelta(weeks=weeks)
    collections = records(state)
    events = clocks(state)
    nodes = {(group, key): value for group, entries in collections.items() for key, value in entries.items()}
    index = {}
    for node, value in nodes.items():
        tokens = {node[1], *identities(value)}
        if isinstance(value, dict) and isinstance(value.get('digest'), str):
            tokens.add(value['digest'])
        for token in tokens:
            index.setdefault(token, set()).add(node)
    edges = {node: set() for node in nodes}
    for node, value in nodes.items():
        for token in {node[1], *strings(value)}:
            for other in index.get(token, ()):
                edges[node].add(other)
                edges[other].add(node)
    roots = deepcopy(state)
    roots.pop(CLOCKS, None)
    for path in PATHS:
        parent = roots
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = {}
    keep = {node for token in strings(roots) for node in index.get(token, ())}
    # Prepared replans bind the complete planning-state digest, including
    # otherwise detached history. Preserve that authority until consumed.
    if state['menu_planning'].get('prepared'):
        keep.update(nodes)
    for node, value in nodes.items():
        event = events.get(node[0], {}).get(node[1])
        try:
            observed = timestamp(event['last_changed_at']).date()
            eligible = event['fingerprint'] == digest(value) and max(observed, latest_date(value)) < cutoff
        except (TypeError, ValueError, KeyError):
            eligible = False
        if not eligible:
            keep.add(node)
    pending = list(keep)
    while pending:
        node = pending.pop()
        for other in edges[node] - keep:
            keep.add(other)
            pending.append(other)
    removed = {group: sorted(key for key in entries if (group, key) not in keep) for group, entries in collections.items()}
    body = {'state_digest': digest(state), 'as_of_date': today.isoformat(), 'retention_weeks': weeks,
            'cutoff_date': cutoff.isoformat(), 'remove': removed}
    return {**body, 'preview': digest(body), 'eligible_records': sum(map(len, removed.values())),
            'retained_records': len(nodes) - sum(map(len, removed.values()))}


def compact(state, preview):
    result = deepcopy(state)
    collections = records(result)
    events = clocks(result)
    for group, keys in preview['remove'].items():
        for key in keys:
            collections[group].pop(key)
            events.get(group, {}).pop(key, None)
    return result


def archive(state, preview):
    collections = records(state)
    body = {'format': 'meal-concierge-history', 'version': 1,
            'household': state['household'], 'provider': state['provider'],
            'preview': preview,
            'records': {group: {key: deepcopy(collections[group][key]) for key in keys} for group, keys in preview['remove'].items()}}
    return {**body, 'sha256': digest(body)}


def recover(state, document, *, now=None):
    if state.get('version') != 13:
        raise ValueError('update the runtime/state before history maintenance')
    body = {key: value for key, value in document.items() if key != 'sha256'}
    if document.get('sha256') != digest(body) or body.get('format') != 'meal-concierge-history' or body.get('version') != 1:
        raise ValueError('history archive integrity check failed')
    if any(body.get(key) != state.get(key) for key in ('household', 'provider')):
        raise ValueError('history archive belongs to another household/store')
    result = deepcopy(state)
    collections = records(result)
    if set(body['records']) != set(collections):
        raise ValueError('history archive collections are invalid')
    for group, entries in body['records'].items():
        if not isinstance(entries, dict):
            raise ValueError('history archive entries are invalid')
        for key, value in entries.items():
            if key in collections[group] and not same_json(collections[group][key], value):
                raise ValueError('history recovery conflicts with current records; nothing restored')
            collections[group][key] = deepcopy(value)
    if any(len(entries) > 2000 for entries in collections.values()):
        raise ValueError('recovered history exceeds runtime capacity; inspect the full backup in a separate offline home')
    track_changes(result, now=now)
    return result
