# Offline source pack build

The builder reads a sealed source snapshot and writes a separate derived pack.
It does not fetch source APIs, read household data, call an LLM, or modify its
inputs. Acquisition completeness, successful parsing, shopping readiness and
redistribution eligibility are separate counts.

`build_recipe_pack.py` consumes the reviewed recipe schema and quantity helpers,
the managed asset contract, and the shared
[`recipe_portable.py`](../recipe_portable.py) archive codec. Use the same pinned
runtime dependencies as Meal Concierge. Code and dependency fingerprints are
part of the build identity; a schema, parser, image profile or rights-policy
change invalidates cached normalized results.

```sh
python build_recipe_pack.py \
  --snapshot /absolute/path/to/sealed-source-snapshot \
  --snapshot-sha256 EXPECTED_SNAPSHOT_JSON_SHA256 \
  --output /absolute/path/to/separate-build-directory \
  --pack-version 2026-09-06.4 \
  --covers-root /absolute/path/to/reviewed-cover-derivatives \
  --covers-manifest-sha256 EXPECTED_COVERS_MANIFEST_JSON_SHA256
```

The expected digest must come from the reviewed snapshot handoff. The builder
checks the seal, manifest checksums and every source body it consumes. Input and
output directories cannot overlap. One process owns an output directory at a
time. Each completed normalized record is cached with its source/build identity
and checksum. Rerunning validates cached records; interrupted builds resume
without modifying the sealed source. A complete ZIP replaces the output archive
only after the shared codec verifies it.

## Source mapping and unresolved content

Wikibooks parsing uses expanded, revision-bound HTML. Ingredient tables map
explicit source weight, volume or count columns; percentages never substitute
for quantities. The original row wording remains evidence. The recipe infobox
may establish person servings, including a yield explicitly naming people;
yield such as "2 loaves" cannot. Printed per-ingredient metric equivalents are
retained without inferring a general cup convention or density. Procedure
subsections retain all steps. Multiple or nested ingredient sections requiring
interpretation are reported as parse failures, rather than being merged into a
different ready recipe.

TheMealDB uses the original lookup fields and retains the upstream `strSource`
when present. Missing person servings remain unknown. Explicit supported
quantities use the shared exact-fraction parser. Cups, ranges and unverified
English spoon conventions remain unresolved or estimated under the common
contract; the builder never manufactures user acceptance.

Readable recipes with incomplete quantity decisions are drafts. Missing whole
ingredient/procedure sections, oversized fields and ambiguous recipe splitting
are reported as parse failures. Source IDs remain distinct even when their
content or cover hashes match. Complete source coverage includes redirects and
non-recipes; produced recipe counts must not be substituted for page counts.

### Editorial completion

The optional `--curation /absolute/path/to/editorial-amendments.json` and
`--curation-sha256 SHA256` inputs enable the offline completion pass. The JSON
has `schema: 1` and `records` keyed by stable source identity. Each amendment
binds the original content hash and lists its current source issues, changed
culinary fields and explanation. Identity, source attribution and covers cannot
be replaced by an amendment. A mismatch aborts the build. The input digest and
curation code fingerprint bind caches and build resumption.

The pass recovers source measures before applying documented cooking estimates.
Source wording remains available separately; changed active recipes carry their
current editorial instructions. Unrecognised foods remain unresolved. The
builder uses runtime scaling readiness, including every shopping requirement,
and never creates personal acceptance. Only the reviewed release introduces
publisher estimate markers through the verified bundled-import path. Culinary
review is required in addition to numeric completeness before publication.

### Reviewed dinner mappings

Five narrow mappings bind source page, revision and rendered SHA-256 after
whole-procedure review. They recover explicit mango, duck breast, star anise,
onion, sausage and tomato counts. The lamb dish selects the source's twelve
small potatoes, retaining the alternative ten medium potatoes and approximate
weight in original evidence. Acorn salmon selects the complete oven method
using the source's non-stick sheet alternative. Its oil-dependent stovetop
method remains in full attribution. These choices are recorded as adaptations;
no estimate receives user acceptance and no approximate weight becomes exact.

The following source recipes have complete quantified ingredient lists and
explicit person servings, and can be scaled without accepted estimates:

| Recipe | Wikibooks page / revision | Source servings |
| --- | --- | --- |
| Frito Pie (Baked) | 59633 / 4518551 | 4 |
| Salmon with Rice and Sauce | 413652 / 4494430 | 3 |
| Jamaican Chicken Fingers with Honey-Mustard Sauce | 102173 / 4509779 | 4 |
| Asian Grilled Duck Breasts | 203935 / 4509725 | 4 |
| Langar Dal | 479669 / 4597437 | 4 |
| Acorn Crusted Salmon (Oven Method) | 266778 / 4512396 | 2 |
| Lamb Sausages and Grilled Potatoes | 414427 / 4524828 | 6 |

These are seven main-dish candidates, not a nutritional assessment or proof of
retailer matching. Langar dal is a legume main component; suggested side dishes
remain suggestions until separately quantified. Offline menu selection and
product matching must exercise the installed artifact through the runtime.

## Rights, attribution and images

Wikibooks text is distributed under CC BY-SA 4.0 with source, revision,
contributor/history attribution, source notices and change notes. Wikibooks
images use their own supported CC, CC0 or public-domain notices. Images outside
that policy are omitted. License labels do not grant trusted pack origin.

TheMealDB recipe text and artwork are distributed with attribution under the
project's `permitted_with_attribution` policy and the
[TheMealDB Terms of Use](https://www.themealdb.com/terms_of_use.php).
Credit identifies TheMealDB as the provider, retains `strSource` as “Recipe source
listed by TheMealDB”, and preserves copyright and trademark notices. Recipe
source links are separate from image credits. API resale requires separate
permission. Source image creator, license and `strCreativeCommons` values remain
as supplied, including null; the project policy does not assign a Creative
Commons license. Image changes are recorded separately from source credit.

Image notices retain the supplied title, attribution requirement, separate
credit and requested attribution, copyright notices, and attribution/source-review
categories. Missing machine-readable authors do not automatically disqualify an
image. Reviewed Commons description-page supplements bind the original image
SHA-256 and exact description URL and retain a revision permalink. They distinguish
photographers, copyright holders, uploaders and later editors; unknown creators
remain unknown. Complete notices survive in `attribution.json` even when their
combined display credit exceeds the recipe field limit.
The supplements were reviewed on 6 September 2026. A Commons revision permalink
pins the description; editor notes can come from the separately displayed file
history, which MediaWiki does not freeze with the description revision.

The optional `SN1.JPG` cover is not selected for this collection. Its recipe,
supplied Serendipity1987 attribution, declared licenses and original source
notices are retained in `attribution.json`.

A separate source compression job owns canonical managed renditions. The builder
checks the reviewed derivative manifest digest, complete recipe associations,
source revisions and original image hashes, then validates and reuses the exact
managed JPEG bytes. The supported profile selects the primary frame, applies
EXIF orientation and embedded ICC-to-sRGB conversion, resizes without upscaling
to at most 960 pixels and makes one JPEG quality-85 encoding. Separate ordinary
image notices survive in the artifact. No second lossy conversion occurs.
Omitting both cover arguments creates a text-only probe with visible pending
cover counts; it does not establish image acceptance.

## Artifact and acceptance boundaries

The ZIP contains only the explicit inventory: `manifest.json`, `records.jsonl`,
`attribution.json`, `coverage.json`, and referenced managed assets. Records use
the shared envelope `{recipe_id, status, recipe}`. The manifest includes format,
pack/schema/normalizer versions, source snapshot identity, measured counts,
build fingerprints and per-file byte counts/checksums. Source responses, logs,
absolute paths, caches and private inputs are not archive members.

Records are staged individually and streamed into JSONL. The shared codec bounds
each normalized document to 256 KiB, its envelope to 512 KiB, records to 512 MiB,
each asset to 4 MiB, each report to 16 MiB, compressed ZIP to 1 GiB and total
expansion to 2 GiB. These are file-path bounds; ordinary RPC and user-import
limits are unchanged.

`build-report.json` records actual build time and compressed/expanded sizes.
Installer discovery, trusted bundled origin, database updates, conflict handling,
offline menu behavior, backup and relocated restore are separate integration
paths. A passing builder test or a text-only archive does not certify them.

Recipes without an actionable source method are excluded by a source-hash-bound
curation decision, including previously completed placeholders. Short but real
preparation methods remain eligible. Changed recipe yields retain their original
value in attribution; covers that no longer represent an adaptation are omitted.

## Released .5 coverage and offline acceptance

The immutable `2026-09-06.5` archive contains 4,599 ready recipes (3,807
Wikibooks and 792 TheMealDB), with 1,570 managed JPEG assets referenced by
1,580 recipes. The remaining 3,019 recipes have a text-only presentation.
The coverage inventory accounts for 8,642 entries: 2,671 Wikibooks redirects,
1,351 non-recipe pages, 20 parse failures, the included recipes, and one
TheMealDB exclusion without a real source method. This accounts for the
captured scope; it does not assert successful parsing of every recipe or an
atomic, authoritative export of either upstream catalog.

The 20 retained Wikibooks failures are explicitly outside this release's
supported conversion shapes. They remain `failed_parse` in its immutable
coverage report, not invented recipes or silently reclassified non-recipes:

| Unsupported source shape | Count | Wikibooks page IDs |
| --- | ---: | --- |
| Nested ingredient lists needing grouping or an alternative choice | 11 | 14077, 25802, 30765, 33222, 34046, 108198, 119511, 180949, 462326, 464709, 465816 |
| Missing/multiple ingredient sections needing recipe splitting | 4 | 16997, 40635, 159967, 446276 |
| Incomplete ingredient or procedure section | 2 | 56657, 482477 |
| Notes exceed the supported field size | 1 | 83557 |
| Yield unit exceeds the supported field size | 1 | 470790 |
| Unsupported ingredient table | 1 | 471224 |

These classifications preserve the actual parser outcomes. Resolving a page
requires a separately reviewed source mapping or editorial choice; the release
does not guess missing instructions, flatten ambiguous alternatives or enlarge
runtime fields to conceal the failures. The source-hash-bound TheMealDB
missing-method exclusion remains unchanged.

A fresh isolated Linux ARM64 bank was exercised on 7 September 2026 using
Python 3.12.12 and the pinned runtime dependencies from public `fa63031`.
The production staging helper acquired and verified the exact public archive;
the production archive importer ran under its ordinary offline ownership lock.
All 4,599 records were created without failures or conflicts in 135.633 seconds
(excluding the 8.570-second download). Earlier accepted interrupted/resumed,
repeat-import, upgrade/conflict, frozen-history and relocated-restore checks
remain applicable to this unchanged archive.

| Measured artifact/storage | Bytes |
| --- | ---: |
| Downloaded ZIP | 186,678,225 |
| Expanded members | 234,658,369 |
| Recipe JSONL | 42,821,263 |
| Managed JPEGs | 179,997,024 |
| Attribution | 9,826,643 |
| Coverage | 1,731,767 |
| Installed bank, assets and metadata after menu acceptance | 289,328,114 |

Installation needs room for both the staged archive and installed bank, plus
runtime dependencies, temporary files and any retained backup. These are
measurements of this release, not constant storage guarantees.

With network connections disabled in the acceptance process, first/repeated
20-result Application summary reads took 0.122/0.116 seconds. The summary
response was 15,717 bytes; fetching the corresponding 20 exact full documents
returned 285,497 bytes in 0.330 seconds. “First” means a new Application process,
not a flushed operating-system disk cache. Full detail retained ingredients,
steps, source identity and exact versions. No source API or LLM conversion ran.

A plain seven-day request with no personal recipes selected seven local dishes
in 44.635 seconds: one local search page, 20 detail reads, 17 suitable candidates,
eight shortlisted candidates and 40,320 planner assignments. AI fallback stayed
disabled. The resulting 81 ingredient occurrences needed nine explicit optional
ingredient decisions before product preparation. Omitting those optional items
left 58 exact requirements and 57 distinct queries, producing a prepared plan
with controlled Oda product responses and exact candidate choices. No automatic
ownership, optional-ingredient choice or purchase was inferred.

Separately, the seven source-quantified dinner mappings listed above passed the
actual installed-bank -> dated menu -> product-preparation path for Oda, Mathem
and MENY. Each had 46 requirements, 46 searches per preparation and a prepared
58-package result with synthetic, explicitly approved offers. Both preparations
(before and after candidate approval) made 92 searches together. Socket access
was disabled; no provider API, cart or checkout ran. All 4,599 pack records are
provider-neutral. These tests establish shared runtime compatibility and offline
source independence; they do not establish live product availability or prices.

All included JPEGs passed the strict decoder during installation. Four sampled
renditions were also visually inspected at their distributed size: food edges,
garnish, bread texture and crumb detail remained legible without visible severe
compression artifacts. This is representative visual inspection, not manual
inspection of all 1,570 files. No new image rendition or recipe release was
needed for this acceptance.
