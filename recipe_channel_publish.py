#!/usr/bin/env python3
"""Publish the verified Optional Recipe Collection pointer, never recipe content."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install import published_recipe_pack, stage_recipe_pack, validate_recipe_descriptor
from recipe_portable import preflight_archive, open_archive

REPOSITORY = 'https://api.github.com/repos/poisdahl/meal-concierge'
BRANCH = 'recipe-channel'
FILE = 'optional-recipes.json'


def api(token, route, *, data=None, method=None, missing=False):
    request = urllib.request.Request(REPOSITORY + route,
        data=json.dumps(data).encode() if data is not None else None,
        method=method, headers={'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json', 'User-Agent': 'meal-concierge-publisher'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if missing and exc.code == 404:
            return None
        raise RuntimeError(f'Publisher GitHub request failed (HTTP {exc.code}); channel not confirmed updated') from exc


def membership(path):
    with open_archive(path) as archive:
        return ({record['recipe_id'] for record in archive.records()},
                archive.manifest.get('withdrawals', {}))


def verify_withdrawals(previous, current):
    old_ids, old_reasons = previous
    new_ids, new_reasons = current
    removed = old_ids - new_ids
    if missing := removed - new_reasons.keys():
        raise RuntimeError(f'{len(missing)} removed recipe IDs have no explicit withdrawal declaration')
    # A skipped-version installation still needs explanations for old removals.
    for identity, reason in old_reasons.items():
        if identity not in new_ids and new_reasons.get(identity) != reason:
            raise RuntimeError('Retain cumulative withdrawal reasons for recipes not restored')
    if new_ids.intersection(new_reasons):
        raise RuntimeError('Withdrawn recipes cannot remain in the collection')
    if set(new_reasons) - set(old_reasons) - removed:
        raise RuntimeError('New withdrawal declarations must describe actual removals from the published collection')


def publish(token, *, initial_publication=False):
    expected = published_recipe_pack(token)
    with tempfile.TemporaryDirectory(prefix='recipe-channel-') as directory:
        archive = stage_recipe_pack(directory, expected=expected)
        preflight_archive(archive, expected)
        incoming = membership(archive)
        # Publication may have advanced while the archive was being checked.
        if published_recipe_pack(token) != expected:
            raise RuntimeError('Published collection changed during verification; rerun the channel workflow')
        payload = (json.dumps(expected, indent=2, sort_keys=True) + '\n').encode()
        branch = api(token, '/git/ref/heads/' + BRANCH, missing=True)
        current = api(token, f'/contents/{FILE}?ref={BRANCH}', missing=True) if branch else None
        if current is not None:
            previous_payload = base64.b64decode(current['content'])
            if previous_payload == payload:
                print('Recipe channel already matches ' + expected['pack_version'])
                return
            previous = validate_recipe_descriptor(json.loads(previous_payload))
            prior_archive = stage_recipe_pack(directory, expected=previous)
            preflight_archive(prior_archive, previous)
            verify_withdrawals(membership(prior_archive), incoming)
        elif not initial_publication:
            raise RuntimeError('No published baseline; explicit --initial-publication is required')
        if branch is None:
            main = api(token, '/git/ref/heads/main')
            api(token, '/git/refs', data={'ref': 'refs/heads/' + BRANCH, 'sha': main['object']['sha']})
        change = {'message': 'Point recipe channel to verified ' + expected['pack_version'],
                  'branch': BRANCH, 'content': base64.b64encode(payload).decode()}
        if current is not None:
            change['sha'] = current['sha']
        api(token, '/contents/' + FILE, data=change, method='PUT')
        result = api(token, f'/contents/{FILE}?ref={BRANCH}')
        if base64.b64decode(result['content']) != payload:
            raise RuntimeError('Recipe channel publication verification failed')
    print('Published verified recipe channel: ' + expected['pack_version'])


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initial-publication', action='store_true', help='explicitly publish without a prior channel baseline')
    args = parser.parse_args()
    publish(os.environ['GH_TOKEN'], initial_publication=args.initial_publication)
