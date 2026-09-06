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
