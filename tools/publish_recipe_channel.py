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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install import published_recipe_pack, stage_recipe_pack
from recipe_portable import preflight_archive

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


def publish(token):
    expected = published_recipe_pack(token)
    with tempfile.TemporaryDirectory(prefix='recipe-channel-') as directory:
        archive = stage_recipe_pack(directory, expected=expected)
        preflight_archive(archive, expected)
    # Publication may have advanced while the archive was being checked.
    if published_recipe_pack(token) != expected:
        raise RuntimeError('Published collection changed during verification; rerun the channel workflow')
    payload = (json.dumps(expected, indent=2, sort_keys=True) + '\n').encode()
    if api(token, '/git/ref/heads/' + BRANCH, missing=True) is None:
        main = api(token, '/git/ref/heads/main')
        api(token, '/git/refs', data={'ref': 'refs/heads/' + BRANCH, 'sha': main['object']['sha']})
    current = api(token, f'/contents/{FILE}?ref={BRANCH}', missing=True)
    if current is not None and base64.b64decode(current['content']) == payload:
        print('Recipe channel already matches ' + expected['pack_version'])
        return
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
    publish(os.environ['GH_TOKEN'])
