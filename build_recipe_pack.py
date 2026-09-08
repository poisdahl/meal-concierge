"""Reproducible offline pack builder; never reads a household bank or fetches URLs."""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time
import unicodedata

from recipe_curation import SourceMethodExcluded
from recipe_pack_sources import THEMEALDB_POLICY, THEMEALDB_TERMS, SourceHTML, SourceParseError, attribution_links, mealdb_recipe, plain, readiness, wikibooks_recipe

FORMAT = 'meal-concierge-recipes'
NORMALIZER_VERSION = '2'
RIGHTS_POLICY = 'wikibooks-cc-themealdb-attribution-v3'
MAX_RECORD_BYTES = 512 * 1024
MAX_SOURCE_BODY = 32 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * MAX_ARCHIVE_BYTES


# Reviewed Commons description pages supplement absent machine-readable fields.
# Original image bytes and exact description URL bind each correction; the
# builder fingerprint also binds these reviewed data. Names retain source roles.
REVIEWED_IMAGE_CREDITS = {
    '1f7d2ad6c173c5bfd7aa9e739de16c3cbd5e2c4968f6d27b9b24b1cd1eb509be': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Feuerzangenbowle.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Feuerzangenbowle.jpg&oldid=1228706312',
        'creator': 'Soebe',
        'notice': 'Photographer: Soebe; photograph taken on 19 November 2004. Uploaded to Commons by Merkel.',
    },
    '41b10a1b230439bf3c76cef47bb1e5e94ca23fb9bbc6528781a48aa997e033da': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Concasse_de_tomate2.JPG',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Concasse_de_tomate2.JPG&oldid=1051694340',
        'creator': None,
        'notice': 'Source description: préparation du concassé de tomates. Par Antoine. The preparation credit is retained without assigning a photographer.',
    },
    'fd4ac7ccb2382994c1bce9ed630aa283b9f1fb3f07a2986f384f0e990b156530': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Semmelwuerfel_geroestet.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Semmelwuerfel_geroestet.jpg&oldid=1215282393',
        'creator': 'Hutschi',
        'notice': 'Author: Hutschi.',
    },
    'f2184f325e0af2f1bc3030f413f2aa2af21216284b80caed16681dbbe5d73f30': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Hartkeks_offen.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Hartkeks_offen.jpg&oldid=1221220238',
        'creator': 'Samuel Mellert',
        'notice': 'Photograph by Samuel Mellert; Commons upload by Schnee (Schneelocke).',
    },
    '3c61e75bbdd446d2abf3dabf6548137fc1c5f2ad4448e0b537234e643e13596c': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Sarmi.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Sarmi.jpg&oldid=1246493124',
        'creator': 'Kiril Kapustin',
        'notice': 'Author: Kiril Kapustin; source: imagesfrombulgaria.com.',
    },
    '88c0e8f257013db7b700c10a20ca0634a9e46fbc8701b3c22d30f1415fcf6b80': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Jelebi_close.jpg',
        'evidence_links': ['https://www.flickr.com/photos/cayce/19431431/'],
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Jelebi_close.jpg&oldid=1262719507',
        'creator': 'Cayce',
        'notice': 'Photograph by Cayce; cropped by Ranveig.',
    },
    'dc205083594434eb78acd9844142f9e4757188c80199632abfa115a669225eb2': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Tortilla_patatas.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Tortilla_patatas.jpg&oldid=1264492156',
        'creator': 'LLuisa Nunez',
        'notice': 'Photograph taken by LLuisa Nunez on 19 July 2005; levels adjusted by Hohum.',
    },
    'f0564a87cef5b3e893056098e7f9140e90b6d080aafbc78722456554ba6f3454': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:BLT_sandwich_(1).jpg',
        'evidence_links': ['https://www.flickr.com/photos/ollie_lizard/274401804/'],
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:BLT_sandwich_(1).jpg&oldid=1121478409',
        'creator': 'Ollylain',
        'notice': 'Photographer: Ollylain. Original Flickr title: Lucky Boy BLT. The Commons page records that Flickr later stopped distributing this image under CC; it retains the earlier CC license.',
    },
    '34c465df73cc704dd7970432f99bb2cafc8d2b95b46573db3fffe5a7bf9cfcc1': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Mushy_peas_19_july_05.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Mushy_peas_19_july_05.jpg&oldid=1228037729',
        'creator': 'Caroline Ford (Secretlondon)',
        'notice': 'Photographer: Caroline Ford (Secretlondon).',
    },
    'd04bcfed8ec7c4587c3c2ed4e9cb81621166337af49726a301e7b69447882063': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:23-pies_finished.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:23-pies_finished.jpg&oldid=766087552',
        'creator': 'Kellen',
        'notice': 'Photographer: Kellen; colours corrected by Doodledoo.',
    },
    'a8ecf25ada2e62c8a5e82509a9c4b8d4325336e92cd192fd2dc801377edec34c': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Aloo_gobi.jpg',
        'evidence_links': ['https://www.flickr.com/photos/pgoyette/339987980/'],
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Aloo_gobi.jpg&oldid=789586471',
        'creator': None,
        'notice': 'Originally posted to Flickr by paul goyette; named source attribution is retained without assigning a photographer.',
    },
    'd28b84660f9871f737163dcb983860036a1a553d4511f0d7108828392bd17d62': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Peru_Anticuchos.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Peru_Anticuchos.jpg&oldid=1185480208',
        'creator': 'Håkan Svensson (Xauxa)',
        'notice': 'Photograph taken on 31 July 2004 by Håkan Svensson (Xauxa).',
    },
    '9d835fbfcc89a8c2a55ad7345236695752c7ab2b34c9ea4cdec4bd710c1d3c47': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Potstickers_RTE.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Potstickers_RTE.jpg&oldid=702420813',
        'creator': None,
        'notice': 'Original photographer unresolved. Gene.arboit transferred the image from English Wikipedia; Nesnad later attempted an AI resize.',
    },
    '829e6b5efffb007089e90ac63d0fa006b32ef4923375e7c5ccfaa6fab91354ab': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Banana-Nut-Muffins-2005-Aug-24.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Banana-Nut-Muffins-2005-Aug-24.jpg&oldid=950756379',
        'creator': 'Mark Fickett',
        'notice': 'Prepared and photographed by Mark Fickett. Source permission requires attribution to Mark Fickett; notification of use is requested, not required.',
    },
    '1d3a2bc63fdfd2f3471c00bda7f6f4af0b6faf25d94671f4de7e03eaee6dcac8': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Rainbow-Jello-Cut-2004-Jul-30.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Rainbow-Jello-Cut-2004-Jul-30.jpg&oldid=1080202603',
        'creator': 'Mark Fickett',
        'notice': 'Prepared in part and photographed by Mark Fickett. Source permission requires attribution to Mark Fickett; notification of use is requested, not required.',
    },
    'bd2152534d66ab488d321243dd5d320e1d5013baedd7725fc197e4320c0a4599': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Chocolate-Cake-2006-Jan-04.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Chocolate-Cake-2006-Jan-04.jpg&oldid=856923875',
        'creator': 'Mark Fickett',
        'notice': 'Prepared and photographed by Mark Fickett.',
    },
    '30e97b1ff22292731a4934f1d7f0e821f1c725f8192f67a2726acb956c85197e': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Bowl%27o%27Coleslaw_modified.jpg',
        'evidence_links': ['https://www.flickr.com/photos/stuart_spivack/53197420/'],
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Bowl%27o%27Coleslaw_modified.jpg&oldid=831880976',
        'creator': None,
        'notice': 'Photo courtesy of Stu Spivack; modified by AlMare and colour/contrast adjusted by Rainer Zenz.',
    },
    '1e2196bd091dc814bccc749d280bfe6decd4cbc8535addf89d5a04454cb1b7a2': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Korean_food_7.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Korean_food_7.jpg&oldid=1082541169',
        'creator': None,
        'notice': 'Original upload by Feth includes the declaration “I took the picture”; retained as source context without assigning a named photographer.',
    },
    'f8a44ea375dd31fa7d450c71e540068c44ae55b34bec7aee78e2811a360b51ad': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:East-asian-food-spring-rolls-3.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:East-asian-food-spring-rolls-3.jpg&oldid=1051351393',
        'creator': None,
        'notice': 'Original upload by Anonymous Cow~commonswiki declares “Source: My mother. Photograph taken by me.”; retained as source context without assigning a named photographer.',
    },
    '215b62aa347dec47fd08ccbcfecc3a4510a1189c10a7b612d9d293a2f47e70d2': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Mapo_tofu.JPG',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Mapo_tofu.JPG&oldid=1124672819',
        'creator': None,
        'notice': 'Original upload by Yaoleilei declares “cook and photo by my self”; retained as source context without assigning a named photographer.',
    },
    'a112937aff379f32f7c6738432e02ca494c24f68089b845664becad452a2c732': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Mixed_spices_01_Pengo.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Mixed_spices_01_Pengo.jpg&oldid=951609928',
        'creator': None,
        'notice': 'Photo credit: Peter Halasz (User:Pengo).',
    },
    '760e8d0ebd6253a72808ed7cda5c6e8cd3c592f5e28bbd1db227b3318cf781e7': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:20000227--calzone.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:20000227--calzone.jpg&oldid=1239564291',
        'creator': 'Paul Vlaar',
        'notice': 'Author: Paul Vlaar; later cropped by Sebastian Wallroth.',
    },
    'baafaa2b8a1e951b7121919ad505139736edb29263a258532e16957c9fc32a3d': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Corn_chowder_bowl.jpg',
        'evidence_links': ['https://www.flickr.com/photos/stuart_spivack/121028785/'],
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Corn_chowder_bowl.jpg&oldid=1053342101',
        'creator': None,
        'notice': 'Photo courtesy of Stu Spivack.',
    },
    '407e730a1416382ccdca923899d1b55f2024914af11c1b7baa229f2239d6e296': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Chinese_fried_bread.jpg',
        'evidence_links': ['https://www.flickr.com/photos/stuart_spivack/121633136/'],
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Chinese_fried_bread.jpg&oldid=953572145',
        'creator': None,
        'notice': 'Photo courtesy of Stu Spivack.',
    },
    'a72fc1d5c25f5256df0c446a127fa19f46bf4f90b9f4d2211bf9087129b20a63': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Springerle_with_typical_foot_swabian_Fuessle.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Springerle_with_typical_foot_swabian_Fuessle.jpg&oldid=1056637064',
        'creator': 'Andreas Bauerle',
        'notice': 'Author: Andreas Bauerle.',
    },
    '78cc99f6ceb672f581c406ef864168598472348148aae8e9907f82663187c6a6': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Hot_chocolate_p1150797.jpg',
        'evidence_links': ['https://commons.wikimedia.org/wiki/User:David.Monniaux'],
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Hot_chocolate_p1150797.jpg&oldid=1142875522',
        'creator': None,
        'notice': 'Copyright © 2006 David Monniaux. This identifies the declared copyright holder; no separate photographer name is inferred.',
    },
    'f8ecd98c2380ec7d28f54fce44deba898fdeeb6d06803d665b85143039aead80': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Sweet_beef_07.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Sweet_beef_07.jpg&oldid=1192979998',
        'creator': None,
        'notice': 'The source explicitly lacks author information. Raul654 uploaded the image with a self-made declaration; retained as source context without assigning a named photographer.',
    },
    'c7e63d27d1be4214e037eab9e1169b1626de5e62722fb2c445501f612f992183': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Zippule.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Zippule.jpg&oldid=994861581',
        'creator': None,
        'notice': 'Original photographer unresolved. Uploaded by Marcuscalabresus; watermark fixed by FischX; cropped by Hohum.',
    },
    'd14d51a8acae294bf0c6b244faf09d5536ad66bc9ce0a8af674ef863b81d286c': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Marzipan_cake.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Marzipan_cake.jpg&oldid=1236874826',
        'creator': 'color line',
        'notice': 'Photograph by color line; cropped by Ranveig; levels adjusted by Hohum.',
    },
    '3014834cc9e28edd2d7a5a61e01d919b32dde14e6e883d575f0aa78b3e13a359': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Banana_pudding,_homemade.jpg',
        'evidence_links': ['https://www.flickr.com/photos/stuart_spivack/54200701/'],
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Banana_pudding,_homemade.jpg&oldid=1121841639',
        'creator': None,
        'notice': 'Photo courtesy of Stu Spivack; white balance adjusted by Belbury and brightened by ReneeWrites.',
    },
    'c6ee503448bea7f46154b5a7b4db5327c09d345c7d373ef0ba832395aa612fb5': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Libyan_Asida.jpg',
        'evidence_links': ['https://www.flickr.com/people/lovelytripoli/'],
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Libyan_Asida.jpg&oldid=1269901154',
        'creator': None,
        'notice': 'Photograph source: Hibo1976 (Flickr alias LovelyHibo).',
    },
    '684b7c174d85150615e52b5389bafdf35e91f22a7802f1529a6fd3cabae0b41e': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:Kitfo.jpg',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:Kitfo.jpg&oldid=914507267',
        'creator': None,
        'notice': 'The source explicitly lacks author information. Uploaded by Diádoco; later brightness/white balance adjusted by ReneeWrites. The Stu Spivack category remains context, not a photographer assignment.',
    },
    '75891d809dc62fe0cb457807ef270f299255e259384b08e748639b21f9b1e01b': {
        'description_url': 'https://commons.wikimedia.org/wiki/File:SN1.JPG',
        'revision_url': 'https://commons.wikimedia.org/w/index.php?title=File:SN1.JPG&oldid=844877492',
        'creator': None,
        'notice': 'Declared copyright holder and requested attribution: Serendipity1987 at English Wikibooks. Commons retains an unreviewed bot-transfer warning. Original Wikibooks description was deleted after transfer; its public upload log says own work and permission below, but the original permission text cannot be independently recovered. Optional cover not selected for this collection.',
        'omission_reason': 'source_cover_not_selected',
        'evidence_links': [
            'https://commons.wikimedia.org/w/index.php?title=File:SN1.JPG&oldid=255804028',
            'https://en.wikibooks.org/w/index.php?title=Special:Log&page=File:SN1.JPG',
        ],
    },
}

class PackBuildError(ValueError):
    pass


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def confined(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if not relative or parts.is_absolute() or str(parts) != relative or any(p in {'.', '..'} for p in parts.parts) or '\\' in relative:
        raise PackBuildError('invalid relative path')
    target = root
    for part in parts.parts:
        target = target / part
        if target.is_symlink():
            raise PackBuildError('symlink paths are unsupported')
    return target


def absolute_root(root: Path) -> Path:
    if not root.is_absolute():
        raise PackBuildError('input and output roots must be absolute')
    # macOS exposes its standard temporary paths through system symlinks. Resolve
    # only those fixed aliases, then reject all task-controlled symlink segments.
    for alias in (Path('/tmp'), Path('/var')):
        if (root == alias or alias in root.parents) and alias.is_symlink() and alias.resolve() == Path('/private') / alias.name:
            root = alias.resolve() / root.relative_to(alias)
    confined(Path('/'), str(root).lstrip('/'))
    return root


def read_file(root, relative, maximum, expected=None):
    path = confined(root, relative)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        mode = os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or mode.st_size > maximum:
            raise PackBuildError('input is not a bounded regular file')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise PackBuildError('input exceeded byte limit')
    if expected and (len(data) != expected['bytes'] or digest(data) != expected['sha256']):
        raise PackBuildError('source or output checksum mismatch')
    return data


def load_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PackBuildError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(PackBuildError('nonfinite JSON')))


def write_file(root, relative, data):
    path = confined(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'path': relative, 'bytes': len(data), 'sha256': digest(data)}


def fingerprint():
    modules = ['recipes', 'recipe_quantities', 'recipe_assets', 'recipe_pack_sources', 'recipe_portable', 'recipe_curation']
    values = {name: digest(Path(importlib.import_module(name).__file__).read_bytes()) for name in modules}
    values['builder'] = digest(Path(__file__).read_bytes())
    for name in ['PIL', 'simplejpeg', 'numpy']:
        values[name] = importlib.import_module(name).__version__
    values['python'] = '.'.join(map(str, sys.version_info[:3]))
    values['unicode'] = unicodedata.unidata_version
    return values


def _mealdb_image_credit(image):
    notices = {
        'provider': 'TheMealDB', 'provider_url': 'https://www.themealdb.com/',
        'redistribution_policy': THEMEALDB_POLICY.copy(),
        'source_url': image.get('source_url'),
        'creator': image.get('creator'), 'source_license': image.get('license'),
        'source_image_credit': image.get('source_image_credit'),
        'source_creative_commons_flag': image.get('source_creative_commons_flag'),
    }
    credit = 'Artwork sourced via TheMealDB.'
    if image.get('source_image_credit'):
        credit += ' ' + plain(str(image['source_image_credit']))
    if len(credit) > 500:
        credit = 'Artwork sourced via TheMealDB. Complete image credit is preserved in attribution.json.'
    return {
        'alt': None, 'source_url': image.get('source_url'),
        'creator': image.get('creator'), 'credit': credit,
        'license': 'TheMealDB Terms of Use', 'license_url': THEMEALDB_TERMS,
        'changes': 'Image processed and compressed by Meal Concierge.',
    }, notices


def _image_credit(image):
    metadata = image.get('license_metadata', {})
    values = {key: plain(str(metadata[key].get('value', ''))) for key in
              ('Artist', 'Credit', 'LicenseShortName', 'LicenseUrl', 'UsageTerms', 'Permission', 'Attribution',
               'Restrictions', 'Copyrighted', 'Copyright', 'License', 'ObjectName', 'AttributionRequired') if key in metadata}
    values['links'] = attribution_links(SourceHTML(' '.join(str(metadata[key].get('value', '')) for key in values)).root)
    # Preserve attribution and source-review context, without unrelated taxonomy.
    categories = plain(str(metadata.get('Categories', {}).get('value', ''))).split('|')
    relevant = [c for c in categories if re.search(r'author|attribut|copyright|photographs (?:by|and images by)|requiring review', c, re.I)]
    if relevant:
        values['Categories'] = '|'.join(relevant)
    reviewed = REVIEWED_IMAGE_CREDITS.get(image.get('file', {}).get('sha256'))
    if reviewed and image.get('description_url') != reviewed['description_url']:
        reviewed = None
    if reviewed:
        values['reviewed_source'] = reviewed.copy()
        values['links'] = sorted(set(values['links'] + [reviewed['revision_url']] + reviewed.get('evidence_links', [])))
    license_name = values.get('LicenseShortName', '')
    license_url = values.get('LicenseUrl') or None
    if license_url and license_url.startswith('http://creativecommons.org/'):
        license_url = 'https://' + license_url.removeprefix('http://')
    permitted = bool(re.fullmatch(r'CC BY(?:-SA)? (?:1\.0|2\.0|2\.5|3\.0|4\.0)(?: [a-z]{2})?', license_name)
                     and license_url and license_url.startswith('https://creativecommons.org/licenses/'))
    permitted |= license_name == 'CC0' and bool(license_url and license_url.startswith('https://creativecommons.org/publicdomain/zero/'))
    permitted |= license_name == 'Public domain' and values.get('Copyrighted', '').casefold() == 'false'
    if values.get('Restrictions') or (reviewed and reviewed.get('omission_reason')):
        permitted = False
    if not permitted:
        return None, values
    original_credit = values.get('Credit') or values.get('Attribution') or None
    # Preserve the existing bounds on original metadata eligibility. Combining
    # independently supplied notices must not newly exclude an eligible image.
    if any(len(v or '') > 500 for v in (values.get('Artist'), original_credit, license_name)):
        return None, values
    parts = list(dict.fromkeys(v for v in (values.get('Credit'), values.get('Attribution'),
                                         reviewed.get('notice') if reviewed else None) if v))
    credit = '; '.join(parts) or None
    if credit and len(credit) > 500:
        credit = 'Complete image credit and source notices are preserved in attribution.json.'
    record = {'alt': None, 'source_url': image.get('description_url') or image.get('source_url'),
              'creator': (reviewed.get('creator') if reviewed else None) or values.get('Artist') or None,
              'credit': credit, 'license': license_name, 'license_url': license_url,
              'changes': 'EXIF orientation applied; resized to at most 1600 pixels; JPEG quality 85; embedded metadata removed.'}
    return record, values


class Covers:
    """Read exact managed bytes from an explicitly reviewed derivative handoff."""

    def __init__(self, root, expected, snapshot_sha256, entries):
        root = absolute_root(root)
        raw = read_file(root, 'covers-manifest.json', MAX_SOURCE_BODY)
        if not expected or digest(raw) != expected:
            raise PackBuildError('reviewed cover manifest checksum mismatch')
        manifest = load_json(raw)
        if manifest.get('schema') != 'meal-concierge-derived-covers/1' or manifest.get('status') != 'complete' or manifest.get('source_snapshot_sha256') != snapshot_sha256:
            raise PackBuildError('cover manifest is incomplete or belongs to another snapshot')
        if manifest.get('profile', {}).get('id') != 'managed-jpeg-960-q85-v1':
            raise PackBuildError('cover processing profile is unsupported')
        associations = manifest.get('recipes')
        if not isinstance(associations, list) or len(associations) > MAX_ENTRIES:
            raise PackBuildError('cover association count exceeds bounds')
        self.rows = {}
        for row in associations:
            key = (row['source'], row['source_id'])
            if key in self.rows:
                raise PackBuildError('duplicate cover recipe identity')
            self.rows[key] = row
        recipes = {(e['source'], e['source_id']): e for e in entries if e['classification'] == 'recipe'}
        if self.rows.keys() != recipes.keys():
            raise PackBuildError('cover association scope differs from recipe snapshot')
        self.root = root
        self.manifest = manifest
        self.manifest_sha256 = expected
        self.files = {}
        for key, entry in recipes.items():
            row = self.rows[key]
            if row.get('revision') != entry.get('revision') or row.get('source_raw_sha256') != entry['raw']['sha256'] or row.get('source_rendered_sha256') != entry.get('rendered', {}).get('sha256'):
                raise PackBuildError('cover association source revision differs')
            if not entry.get('image'):
                if row.get('status') != 'no_source_cover':
                    raise PackBuildError('cover association invents a source image')
                continue
            original = entry['image']['file']['sha256']
            if row.get('status') != 'complete' or row.get('source_original_sha256') != original:
                raise PackBuildError('cover original image differs or is incomplete')
            asset = manifest['assets'][original]
            if any(row.get(k) != asset.get(k) for k in ('asset_id', 'path', 'sha256', 'bytes')):
                raise PackBuildError('cover asset and association differ')
            if row['asset_id'] != 'sha256:' + row['sha256'] or row['path'] != 'assets/' + row['sha256'] + '.jpg' or not re.fullmatch('[0-9a-f]{64}', row['sha256']):
                raise PackBuildError('cover asset identity is invalid')
            self.files[row['asset_id']] = row

    def read(self, asset_id):
        from recipe_assets import validate_managed, MAX_ASSET_BYTES
        row = self.files[asset_id]
        data = read_file(self.root, row['path'], MAX_ASSET_BYTES, row)
        validate_managed(data, asset_id)
        return data


def _build(snapshot: Path, output: Path, *, snapshot_sha256: str, pack_version: str, stop_after=None, covers_root=None, covers_manifest_sha256=None, curation=None, curation_sha256=None):
    from recipe_assets import RecipeAssetError
    from recipe_portable import canonical_bytes, write_archive
    from recipes import RecipeError, normalize_recipe, categories_from_tags
    started = time.monotonic()
    # Resolve only after rejecting symlinks in every root component.
    for root in (snapshot, output):
        if not root.is_absolute():
            raise PackBuildError('snapshot and output must be absolute')
        confined(Path('/'), str(root).lstrip('/'))
    if snapshot == output or snapshot in output.parents or output in snapshot.parents:
        raise PackBuildError('input and output must be disjoint')
    if not re.fullmatch(r'[a-f0-9]{64}', snapshot_sha256):
        raise PackBuildError('expected snapshot SHA-256 is required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', pack_version):
        raise PackBuildError('invalid pack version')
    read_file(snapshot, 'SEALED', 4096)
    source_bytes = read_file(snapshot, 'snapshot.json', MAX_SOURCE_BODY)
    if digest(source_bytes) != snapshot_sha256:
        raise PackBuildError('sealed snapshot digest mismatch')
    source = load_json(source_bytes)
    entries = []
    for name in ('wikibooks', 'themealdb'):
        filename = name + '-manifest.json'
        rows = load_json(read_file(snapshot, filename, MAX_SOURCE_BODY, source['files'][filename]))
        if not isinstance(rows, list) or len(rows) > MAX_ENTRIES:
            raise PackBuildError('source manifest exceeds record bound')
        for row in rows:
            if row.get('source') != name or not re.fullmatch(r'\d{1,20}', row.get('source_id', '')):
                raise PackBuildError('source identity mismatch')
        entries.extend(rows)
    identities = [(e['source'], e['source_id']) for e in entries]
    if len(entries) > MAX_ENTRIES or len(set(identities)) != len(entries):
        raise PackBuildError('duplicate or excessive source identities')
    covers = Covers(covers_root, covers_manifest_sha256, snapshot_sha256, entries) if covers_root else None
    if covers_manifest_sha256 and covers is None:
        raise PackBuildError('cover checksum requires its root')
    amendments = None
    if curation is not None:
        curation = Path(curation)
        if not curation.is_absolute() or not re.fullmatch(r'[a-f0-9]{64}', curation_sha256 or ''):
            raise PackBuildError('curation requires an absolute file and pinned SHA-256')
        data = read_file(curation.parent, curation.name, 16 * 1024 * 1024)
        if digest(data) != curation_sha256:
            raise PackBuildError('curation digest mismatch')
        document = load_json(data)
        if not isinstance(document, dict) or set(document) != {'schema', 'records'} or document['schema'] != 1 or not isinstance(document['records'], dict):
            raise PackBuildError('unsupported curation input')
        amendments = document['records']
        if set(amendments) - {f'{source}:{identity}' for source, identity in identities}:
            raise PackBuildError('curation contains unknown source identities')
    elif curation_sha256:
        raise PackBuildError('curation checksum requires its file')
    versions = fingerprint()
    if amendments is not None:
        versions['curation_input_sha256'] = curation_sha256
    if covers:
        versions['covers_manifest_sha256'] = covers.manifest_sha256
    run_key = digest(encoded({'snapshot': snapshot_sha256, 'versions': versions, 'policy': RIGHTS_POLICY, 'pack_version': pack_version}))
    output.mkdir(parents=True, exist_ok=True)
    cache = f'cache/{run_key}'
    coverage, attribution_paths, record_paths = [], [], []
    attribution_bytes = records_bytes = coverage_bytes = 0
    counts = Counter()
    asset_ids = set()
    reused = 0
    for index, entry in enumerate(sorted(entries, key=lambda e: (e['source'], int(e['source_id'])))):
        identity = entry['source'] + ':' + entry['source_id']
        base = {'source': entry['source'], 'source_id': entry['source_id'], 'url': entry['url'], 'classification': entry['classification']}
        if entry['classification'] != 'recipe':
            base['status'] = 'excluded_' + entry['classification']
            coverage_bytes += len(encoded(base)) + 1
            if coverage_bytes > 16 * 1024 * 1024:
                raise PackBuildError('coverage exceeds portable report bound')
            coverage.append(base)
            counts[(entry['source'], base['status'])] += 1
            continue
        # Verify every consumed original each run, even when normalized output is cached.
        payloads = {kind: load_json(read_file(snapshot, entry[kind]['path'], MAX_SOURCE_BODY, entry[kind])) for kind in ('raw', 'rendered') if kind in entry}
        key = digest(encoded({'entry': entry, 'run': run_key}))
        filename = f'{cache}/{entry["source"]}-{entry["source_id"]}.json'
        cached = None
        try:
            stored = load_json(read_file(output, filename, 2 * MAX_RECORD_BYTES))
            if not isinstance(stored, dict) or not isinstance(stored.get('result'), dict):
                raise ValueError('invalid cache object')
            if stored['key'] == key and stored['sha256'] == digest(encoded(stored['result'])):
                cached = stored['result']
                cached_status = cached.get('status')
                if not isinstance(cached_status, str):
                    raise ValueError('invalid cached status type')
                if cached_status in {'ready', 'draft'}:
                    if not isinstance(cached.get('recipe'), dict) or not isinstance(cached.get('credit'), dict) or not isinstance(cached.get('reasons'), list) or not isinstance(cached.get('image_status'), str):
                        raise ValueError('incomplete cached recipe')
                elif cached_status not in {'failed_parse', 'excluded_missing_source_method'}:
                    raise ValueError('invalid cached status')
                if cached.get('image_status') == 'invalid_derivative':
                    cached = None
                else:
                    reused += 1
        except (FileNotFoundError, ValueError, KeyError):
            cached = None
        if cached is None:
            try:
                reader = wikibooks_recipe if entry['source'] == 'wikibooks' else mealdb_recipe
                recipe, credit = reader(entry, payloads.get('rendered', payloads['raw']))
                if amendments is not None:
                    from recipe_curation import curate
                    try:
                        recipe, credit = curate(recipe, credit, pack_version=pack_version, amendments=amendments)
                    except (RecipeError, KeyError, TypeError, ValueError) as exc:
                        raise PackBuildError('curation failed for '+entry['source']+':'+entry['source_id']+': '+str(exc)) from exc
                recipe = normalize_recipe({**recipe, "categories": categories_from_tags(recipe.get("tags", []))})
                status, reasons = readiness(recipe)
                reasons.extend(credit.get('normalization_issues', []))
                if reasons:
                    status = 'draft'
                image_status = entry.get('image_status') or 'no_candidate'
                if entry.get('image'):
                    image_reader = _mealdb_image_credit if entry['source'] == 'themealdb' else _image_credit
                    image_record, notices = image_reader(entry['image'])
                    credit['image_notices'] = notices
                    credit['image_source_url'] = entry['image'].get('description_url') or entry['image'].get('source_url')
                    image_status = notices.get('reviewed_source', {}).get('omission_reason') or 'not_selected_by_image_policy'
                    if credit.get('image_omission'):
                        image_record = None
                        image_status = 'omitted_after_adaptation'
                    if image_record:
                        image_status = 'awaiting_reviewed_derivative'
                        if covers:
                            row = covers.rows[(entry['source'], entry['source_id'])]
                            try:
                                covers.read(row['asset_id'])
                                image_record['asset_id'] = row['asset_id']
                                image_record['changes'] = 'Primary frame selected; EXIF orientation applied and embedded ICC converted to sRGB when present; at most 960 pixels; JPEG quality 85; embedded metadata removed.'
                                recipe['image'] = image_record
                                recipe = normalize_recipe(recipe)
                                credit['image_processing'] = covers.manifest['assets'][row['source_original_sha256']]['processing']
                                credit['image_profile_sha256'] = covers.manifest['profile_sha256']
                                image_status = 'included'
                            except (OSError, PackBuildError, RecipeAssetError, RecipeError):
                                recipe['image'] = None
                                image_status = 'invalid_derivative'
                                credit['image_error'] = 'managed_cover_unavailable_or_invalid'
                cached = {'recipe': recipe, 'status': status, 'reasons': reasons, 'credit': credit, 'image_status': image_status}
            except SourceMethodExcluded as exc:
                cached = {'status': 'excluded_missing_source_method', 'reason': str(exc)}
            except PackBuildError:
                raise
            except (SourceParseError, RecipeError, KeyError, TypeError, ValueError) as exc:
                cached = {'status': 'failed_parse', 'error': str(exc)}
            cached_bytes = encoded({'key': key, 'sha256': digest(encoded(cached)), 'result': cached})
            if len(cached_bytes) > 2 * MAX_RECORD_BYTES:
                raise PackBuildError('normalized source entry exceeds cache bound')
            write_file(output, filename, cached_bytes)
        status = cached['status']
        counts[(entry['source'], status)] += 1
        base.update({k: v for k, v in cached.items() if k not in {'recipe', 'credit'}})
        if 'recipe' in cached:
            counts[(entry['source'], 'image_' + cached['image_status'])] += 1
            public = ((entry['source'] == 'wikibooks' and cached['credit']['text_rights'] == 'CC-BY-SA-4.0')
                      or (entry['source'] == 'themealdb' and cached['credit']['text_rights'] == 'permitted_with_attribution'))
            base['distribution'] = 'included' if public else 'excluded_rights'
            counts[(entry['source'], base['distribution'])] += 1
            if public:
                envelope = {'recipe_id': identity, 'status': status, 'recipe': cached['recipe']}
                if len(encoded(envelope)) > MAX_RECORD_BYTES:
                    raise PackBuildError('normalized envelope exceeds limit')
                envelope_bytes = encoded(envelope)
                records_bytes += len(envelope_bytes)
                if records_bytes > 512 * 1024 * 1024:
                    raise PackBuildError('records exceed portable stream bound')
                record_path = f'{cache}/records/{entry["source"]}-{entry["source_id"]}.json'
                write_file(output, record_path, envelope_bytes)
                record_paths.append(record_path)
                attribution_bytes += len(encoded(cached['credit'])) + len(encoded(identity)) + 2
                if attribution_bytes > 16 * 1024 * 1024:
                    raise PackBuildError('attribution exceeds portable report bound')
                attribution_paths.append((identity, filename))
                if cached['recipe'].get('image'):
                    asset_ids.add(cached['recipe']['image']['asset_id'])
            else:
                base['rights_reason'] = cached['credit']['text_rights']
        coverage_bytes += len(encoded(base)) + 1
        if coverage_bytes > 16 * 1024 * 1024:
            raise PackBuildError('coverage exceeds portable report bound')
        coverage.append(base)
        if stop_after is not None and index + 1 >= stop_after:
            return {'complete': False, 'processed': len(coverage), 'cache_reused': reused}
    release = f'{cache}/release'
    files = []
    def put(name, data):
        info = write_file(output, f'{release}/{name}', data)
        info['path'] = name
        files.append(info)
    records_path = confined(output, release + '/records.jsonl')
    records_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='records-', dir=records_path.parent)
    record_hash = hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as stream:
            for relative in record_paths:
                data = read_file(output, relative, MAX_RECORD_BYTES)
                stream.write(data)
                record_hash.update(data)
        os.replace(temporary, records_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    files.append({'path': 'records.jsonl', 'bytes': records_bytes, 'sha256': record_hash.hexdigest()})
    attribution_path = confined(output, release + '/attribution.json')
    fd, temporary = tempfile.mkstemp(prefix='attribution-', dir=attribution_path.parent)
    attribution_hash = hashlib.sha256()
    attribution_size = 0
    try:
        with os.fdopen(fd, 'wb') as stream:
            def emit(data):
                nonlocal attribution_size
                attribution_size += len(data)
                if attribution_size > 16 * 1024 * 1024:
                    raise PackBuildError('attribution exceeds portable report bound')
                stream.write(data)
                attribution_hash.update(data)
            emit(b'{')
            for n, (identity, relative) in enumerate(attribution_paths):
                cached = load_json(read_file(output, relative, 2 * MAX_RECORD_BYTES))
                emit((b',' if n else b'') + encoded(identity).rstrip(b'\n') + b':' + encoded(cached['result']['credit']).rstrip(b'\n'))
            emit(b'}\n')
        os.replace(temporary, attribution_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    files.append({'path': 'attribution.json', 'bytes': attribution_size, 'sha256': attribution_hash.hexdigest()})
    put('coverage.json', encoded(coverage))
    expanded = sum(f['bytes'] for f in files)
    for asset_id in sorted(asset_ids):
        data = covers.read(asset_id)
        expanded += len(data)
        if expanded > MAX_EXPANDED_BYTES:
            raise PackBuildError('pack exceeds expanded limit before staging asset')
        put('assets/' + asset_id.removeprefix('sha256:') + '.jpg', data)
    manifest = {'format': FORMAT, 'format_version': 1, 'kind': 'bundled',
                'pack_id': 'wikibooks-themealdb-en', 'pack_version': pack_version,
                'recipe_schema_version': 2, 'normalizer_version': NORMALIZER_VERSION,
                'source_snapshot': {'id': source['snapshot_id'], 'sha256': snapshot_sha256},
                'scope': source['scope'],
                # Acquisition policy is superseded by this pack's explicit source policy.
                'source_limitations': [item for item in source['source_limitations'] if not item.startswith('Image rights')],
                'rights_policy': RIGHTS_POLICY, 'build_versions': versions,
                'records_count': len(record_paths), 'counts': {f'{k[0]}.{k[1]}': v for k, v in sorted(counts.items())},
                'files': files}
    if sum(f['bytes'] for f in files) > MAX_EXPANDED_BYTES:
        raise PackBuildError('pack exceeds expanded limit')
    archive_name = f'meal-concierge-recipes-{pack_version}.zip'
    with tempfile.TemporaryDirectory(prefix='archive-', dir=output) as staging:
        temp = Path(staging) / archive_name
        manifest = write_archive(temp, manifest, {f['path']: confined(output, f'{release}/{f["path"]}') for f in files})
        os.replace(temp, confined(output, archive_name))
    result = {'complete': True, 'archive': archive_name, 'archive_bytes': (output / archive_name).stat().st_size,
              'expanded_bytes': sum(f['bytes'] for f in files) + len(canonical_bytes(manifest)),
              'records': len(record_paths), 'assets': len(asset_ids), 'counts': manifest['counts'],
              'cache_reused': reused, 'seconds': round(time.monotonic() - started, 3)}
    write_file(output, 'build-report.json', encoded(result))
    return result


def build(snapshot: Path, output: Path, **options):
    snapshot, output = absolute_root(snapshot), absolute_root(output)
    if snapshot == output or snapshot in output.parents or output in snapshot.parents:
        raise PackBuildError('input and output must be disjoint')
    covers_root = options.get('covers_root')
    if covers_root:
        covers_root = options['covers_root'] = absolute_root(covers_root)
    if covers_root and (covers_root == output or covers_root in output.parents or output in covers_root.parents):
        raise PackBuildError('cover input and output must be disjoint')
    output.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(confined(output, '.build.lock'), os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PackBuildError('another builder owns this output root') from exc
        return _build(snapshot, output, **options)
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--snapshot-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pack-version', required=True)
    parser.add_argument('--covers-root', type=Path)
    parser.add_argument('--covers-manifest-sha256')
    parser.add_argument('--curation', type=Path)
    parser.add_argument('--curation-sha256')
    args = parser.parse_args()
    print(json.dumps(build(args.snapshot, args.output, snapshot_sha256=args.snapshot_sha256, pack_version=args.pack_version,
                           covers_root=args.covers_root, covers_manifest_sha256=args.covers_manifest_sha256,
                           curation=args.curation, curation_sha256=args.curation_sha256), indent=2))


if __name__ == '__main__':
    main()
