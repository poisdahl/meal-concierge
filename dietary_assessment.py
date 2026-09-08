"""Item-specific dietary findings; provider observations are evidence, never diagnoses."""
from copy import deepcopy
import hashlib
import json
import re
import unicodedata

from core import HouseholdError

KINDS = {'allergy', 'sensitivity', 'preference', 'never_buy', 'allergy_or_sensitivity'}


def text(value):
    return ' '.join(unicodedata.normalize('NFC', str(value)).casefold().split())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def validate_rules(value):
    if not isinstance(value, list) or len(value) > 50:
        raise HouseholdError('diet.rules must be a bounded list')
    seen = set()
    for rule in value:
        if not isinstance(rule, dict) or set(rule) != {'kind', 'term'} or rule['kind'] not in KINDS or not isinstance(rule['term'], str) or not 1 <= len(rule['term'].strip()) <= 200:
            raise HouseholdError('each dietary rule needs an explicit kind and ingredient/allergen term')
        key = (rule['kind'], text(rule['term']))
        if key in seen:
            raise HouseholdError('dietary rules must be distinct')
        seen.add(key)


def validate_permissions(value):
    if not isinstance(value, list) or len(value) > 100:
        raise HouseholdError('uncertainty permissions must be a bounded list')
    for permission in value:
        if not isinstance(permission, dict) or set(permission) != {'kind', 'term', 'product_ref', 'condition', 'accepted', 'notify'}:
            raise HouseholdError('uncertainty permission needs exact kind, term, product_ref, condition, accepted and notify')
        validate_rules([{k: permission[k] for k in ('kind', 'term')}])
        if permission['condition'] not in {'unknown', 'preference_deviation', 'sensitivity_conflict'} or permission['accepted'] is not True or permission['notify'] is not True or not isinstance(permission['product_ref'], (str, int)) or isinstance(permission['product_ref'], bool) or not 1 <= len(str(permission['product_ref'])) <= 500:
            raise HouseholdError('standing uncertainty permission must explicitly accept a specific product finding with notification')
        if permission['kind'] in {'allergy', 'never_buy', 'allergy_or_sensitivity'} and permission['condition'] != 'unknown':
            raise HouseholdError('known exclusions cannot be overridden')


def rules(profile):
    diet = profile.get('diet', profile)
    result = deepcopy(diet.get('rules', []))
    # Preserve old ambiguous statements and exclusions; never infer a diagnosis.
    result += [{'kind': 'allergy_or_sensitivity', 'term': term} for term in diet.get('allergies_or_sensitivities', [])]
    result += [{'kind': 'never_buy', 'term': term} for term in diet.get('avoid', [])]
    return result


def observation(raw):
    """Retain only literal, bounded fields returned by the retailer for this item."""
    result = {}
    for field in ('ingredients', 'allergens', 'may_contain', 'allergen_free_from'):
        value = raw.get(field)
        if isinstance(value, str) and value.strip() and len(value) <= 8000:
            result[field] = value
        elif isinstance(value, list) and len(value) <= 100 and all(isinstance(v, str) and len(v) <= 500 for v in value):
            result[field] = deepcopy(value)
    return result


def matches(term, value, *, milk_ambiguity=False):
    values = value if isinstance(value, list) else [value]
    for part in values:
        normalized = text(part)
        for match in re.finditer(r'(?<!\w)' + re.escape(text(term)) + r'(?!\w)', normalized):
            before = normalized[max(0, match.start() - 35):match.start()]
            after = normalized[match.end():match.end() + 12]
            if milk_ambiguity and re.search(r'(?:oat|almond|soy|soya|coconut|rice|havre|mandel|soja|kokos|ris)[- ]*$', before):
                continue
            if re.search(r'(?:does not contain|contains no|without|no|uten|fri for|inneholder ikke|innehåller inte)\s*$', before) or re.match(r'[- ]?(?:free|fri|fritt)\b', after):
                continue
            return True
    return False


def assess(profile, item, *, recipe=False):
    evidence = {'ingredients': [v.get('item', '') for v in item.get('ingredients', [])]} if recipe else item.get('dietary_evidence', {})
    findings = []
    for rule in rules(profile):
        term, kind = rule['term'], rule['kind']
        aliases = {'milk': ['milk', 'melk', 'mjölk', 'fløte', 'casein', 'whey'], 'melk': ['melk', 'milk', 'mjölk', 'fløte', 'casein', 'whey']}
        terms = aliases.get(text(term), [term]) if kind in {'allergy', 'allergy_or_sensitivity', 'sensitivity'} else [term]
        present = any(matches(t, evidence.get(field, ''), milk_ambiguity=text(term) in {'milk', 'melk'} and kind in {'allergy', 'allergy_or_sensitivity', 'sensitivity'}) for field in ('ingredients', 'allergens', 'may_contain') for t in terms)
        # An unlisted term is not an allergen-free claim (synonyms/compound ingredients).
        free = not recipe and any(text(v) == text(term) for v in evidence.get('allergen_free_from', []) if isinstance(v, str)) if isinstance(evidence.get('allergen_free_from', []), list) else False
        condition = ('preference_deviation' if kind == 'preference' else 'sensitivity_conflict' if kind == 'sensitivity' else 'conflict') if present else 'compatible_label' if free else 'unknown'
        finding = {**rule, 'condition': condition, 'product_ref': item.get('product_ref'), 'item': item.get('name'),
                   'source': 'recipe_ingredients' if recipe else 'retailer_fields' if any(k in evidence for k in ('ingredients', 'allergens', 'may_contain', 'allergen_free_from')) else 'unavailable',
                   'evidence': deepcopy(evidence), 'blocked': condition == 'conflict',
                   'unknown': 'Exact retail ingredient/allergen suitability remains unresolved.' if condition == 'unknown' else None}
        finding['finding_id'] = digest(finding)
        findings.append(finding)
    return findings


def covered(profile, finding):
    if finding['blocked']:
        return False
    if finding['condition'] == 'compatible_label':
        return True
    return any(p.get('accepted') is True and p.get('notify') is True and all(str(p.get(k)) == str(finding.get(k)) for k in ('kind', 'term', 'product_ref', 'condition')) for p in profile['diet'].get('uncertainty_permissions', []))


def parse_retail_product_page(raw, url, *, provider):
    """Read the observed visible product-information rows; scripts remain inert."""
    from recipe_import_readers import _WebpageText
    parser = _WebpageText()
    parser.feed(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
    lines = [line.strip() for line in ''.join(parser.parts).splitlines() if line.strip()]
    trace_label = 'Kan innehålla spår av' if provider == 'mathem' else 'Kan inneholde spor av'
    headings = {'Ingredienser', 'Allergener', trace_label}
    headings.update({'Tillverkningsland', 'Hanterad i', 'Leverantör', 'Förvaring', 'Kontaktuppgifter', 'Näringsinnehåll'}
                    if provider == 'mathem' else {'Produksjonsland', 'Leverandør', 'Oppbevaring', 'Næringsinnhold'})
    result = {'source_url': url}
    for label, field in (('Ingredienser', 'ingredients'), ('Allergener', 'allergens'), (trace_label, 'may_contain')):
        positions = [i for i, line in enumerate(lines) if line == label]
        if len(positions) != 1:
            continue
        start = positions[0] + 1
        end = next((i for i in range(start, len(lines)) if lines[i] in headings), None)
        if end is None:
            continue
        value = ' '.join(lines[start:end])
        if 0 < len(value) <= 8000:
            result[field] = value
    return result


def parse_oda_product_page(raw, url):
    return parse_retail_product_page(raw, url, provider='oda')


def read_oda_product_evidence(reference, deadline=None):
    return read_retail_product_evidence(reference, deadline, provider='oda')


def read_retail_product_evidence(reference, deadline=None, *, provider):
    """Unauthenticated public detail, bound to one provider product ID and origin."""
    import http.client
    import threading
    import time
    from urllib.parse import urlsplit, urljoin
    from recipe_import_sources import _PinnedConnection, _get_bytes, TIMEOUT, RecipeImportSourceError
    host, region = {'oda': ('oda.com', 'no'), 'mathem': ('www.mathem.se', 'se')}[provider]
    if not re.fullmatch(r'[1-9][0-9]{0,19}', str(reference)):
        raise HouseholdError(f'{provider} dietary detail needs an exact product ID')
    url = f'https://{host}/{region}/products/{reference}/'
    if deadline is not None and time.monotonic() + 2 * TIMEOUT > deadline:
        return {'source_url': url, 'unavailable': 'detail_time_budget_exhausted'}
    connection = _PinnedConnection(host, 443, tls=True, public_only=True)
    timer = threading.Timer(TIMEOUT, connection.abort)
    timer.daemon = True
    timer.start()
    try:
        # The observed numeric route redirects to its canonical slug. Resolve only
        # this exact identity, with no cookies, authorization, arbitrary origins or proxies.
        connection.request('HEAD', urlsplit(url).path)
        response = connection.getresponse()
        if response.status in {301, 302, 307, 308}:
            location = urljoin(url, response.getheader('Location', ''))
            parsed = urlsplit(location)
            if parsed.scheme != 'https' or parsed.netloc != host or parsed.query or parsed.fragment or not re.fullmatch('/' + region + r'/products/' + str(reference) + r'-[A-Za-z0-9._~-]+/', parsed.path):
                raise HouseholdError('Product detail redirect changed identity')
            url = location
        elif response.status != 200:
            raise HouseholdError('Oda product detail is unavailable')
        response.close()
        connection.close()
        timer.cancel()
        raw, content_type = _get_bytes(url, maximum=2 * 1024 * 1024)
        if content_type != 'text/html':
            raise HouseholdError('Product detail is not HTML')
        return parse_retail_product_page(raw, url, provider=provider)
    except (OSError, http.client.HTTPException, RecipeImportSourceError, ValueError, HouseholdError):
        return {'source_url': url, 'unavailable': 'exact_public_product_detail_unavailable'}
    finally:
        timer.cancel()
        connection.close()
