# Optional Recipe Collection build

The builder reads a sealed source snapshot and writes a separate derived pack
whose display name is **Optional Recipe Collection**.
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
  --pack-version UNIQUE_VERSION \
  --covers-root /absolute/path/to/reviewed-cover-derivatives \
  --covers-manifest-sha256 EXPECTED_COVERS_MANIFEST_JSON_SHA256
```

Use a date-based release version such as `2026-09-15.1`; increment the suffix
when publishing more than one collection on the same date. The display name does
not change between versions.

The expected digest must come from the reviewed snapshot handoff. The builder
requires `SEALED` to name that exact `snapshot.json` digest, then checks the
seal status/time against the snapshot, the snapshot's manifest checksums and
every source body it consumes. Input and
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

An optional second pass accepts `--reviewed-amendments` with its exact
`--reviewed-amendments-sha256`. It uses the same schema-1, source-identity-keyed
record shape after ordinary curation. Every record in this pass must bind the
stable source identity, complete consumed source-payload SHA-256, existing raw
source hash, and canonical SHA-256 of the entire curated recipe. The builder
checks all four before changing data. This permits reviewed active fields such
as `name`, `steps`, `notes`, `language`, `storage`, and `reheating` without a
parallel localization schema or any network or model call. Ingredient, source,
rights and recipe identity fields remain outside that amendment allowlist.
Review explanations are retained in attribution metadata; an explicit runtime
`notes` value, including null, is not replaced by that explanation.
For reviewed Wikibooks records, the raw revision ID, timestamp, content SHA-1
and wikitext must match the rendered revision and its exact `oldid` URL. Every
named reviewed record must be applied; parse failure, exclusion or other drift
aborts the build instead of silently omitting an amendment.

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
A Commons revision permalink pins the description; editor notes can come from
the separately displayed file
history, which MediaWiki does not freeze with the description revision.

A separate source compression job owns canonical managed renditions. The builder
checks the reviewed derivative manifest digest, complete recipe associations,
source revisions and original image hashes, then validates and reuses the exact
managed JPEG bytes. The supported profile selects the primary frame, applies
EXIF orientation and embedded ICC-to-sRGB conversion, resizes without upscaling
to at most 960 pixels and makes one JPEG quality-85 encoding. Separate ordinary
image notices survive in the artifact. No second lossy conversion occurs.
Omitting both cover arguments creates a text-only probe with visible pending
cover counts; it includes no managed images.

## Artifact and acceptance boundaries

The ZIP contains only the explicit inventory: `manifest.json`, `records.jsonl`,
`attribution.json`, `coverage.json`, and referenced managed assets. Records use
the shared envelope `{recipe_id, status, recipe}`. The manifest includes format,
pack/schema/normalizer versions, `display_name: "Optional Recipe Collection"`,
`membership_mode: "authoritative"`, source snapshot identity, measured counts,
build fingerprints and per-file byte counts/checksums. Authoritative membership
means an installer may permanently delete same-pack recipe identities absent
from a later complete record stream. Source responses, logs, absolute paths,
caches and private inputs are not archive members.

Records are staged individually and streamed into JSONL. The shared codec bounds
each normalized document to 256 KiB, its envelope to 512 KiB, records to 512 MiB,
each asset to 4 MiB, each report to 16 MiB, compressed ZIP to 1 GiB and total
expansion to 2 GiB. These are file-path bounds; ordinary RPC and user-import
limits are unchanged.

`build-report.json` records actual build time and compressed/expanded sizes.
Installer discovery, trusted bundled origin, database updates, conflict handling,
offline menu behavior, backup and relocated restore are separate integration
paths.

Recipes without an actionable source method are excluded by a source-hash-bound
curation decision, including previously completed placeholders. Short but real
preparation methods remain eligible. Changed recipe yields retain their original
value in attribution; covers that no longer represent an adaptation are omitted.
