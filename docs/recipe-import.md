# Recipe import boundaries

This is the shared reader and portable-file contract. The verified collection
consumer applies records through the bank API under installer ownership. Private
export and explicit offline restore preserve recipe history and assets. The
service exposes source preview, explicit save and managed covers through MCP/CLI. Successful file
inspection does not mean recipes have been saved.

## Native source extraction

### Service preview and explicit save

`meal_concierge_recipe_import` calls `recipes/import` with
`source_kind=transcript|url|library`. A transcript uses the quoted shape below.
A URL uses `url` and optional zero-based `record_index` for multiple JSON-LD
recipes. Text fallback returns the bounded page; the host submits `interpretation`
with page-1 quotes on a second URL read, and the service verifies those quotes
against the newly fetched text. Caller replacement source text is rejected.
Native reads use the exact configured `library_recipe_ref`.

The result contains `recipe`, `recipe_digest`, `discovery_ref`, `import_report`,
readiness and shopping requirements. Unknown amounts and estimates remain
visible; `suggested_status=draft` does not silently turn them into usable
quantities. The preview creates no personal entry. Explicit save uses the
existing `meal_concierge_recipe_write` with that exact discovery reference,
status and stable idempotency key. New source imports go to builtin.

Native identities include the configured provider/origin/account binding,
endpoint namespace and case-sensitive native ID. Supplied transcripts use their
content digest. The service stores this identity in existing bank metadata;
recipe content cannot choose it. Interpretation/cover/estimate changes produce
new technical snapshots of the same source. Reimport preserves local revisions
and reports a conflict rather than overwriting them. A legacy unqualified source
identity requires explicit reconciliation. Original attribution and private
provider restrictions remain independent from this duplicate identity.

Native snapshots retain their exact source revision in existing
`external_snapshot` metadata. Ordinary get/search never fetches image URLs.

`recipe_import_readers.read_recipesage_export` accepts the UTF-8
`{"recipes": [...]}` JSON-LD export produced by RecipeSage v4.0.6. Its shape is
checked against the upstream
[export handler](https://github.com/julianpoy/recipesage/blob/v4.0.6/packages/util/server/src/general/queue/export/handlers/jsonldExportJobHandler.ts)
and [recipe converter](https://github.com/julianpoy/recipesage/blob/v4.0.6/packages/util/server/src/general/jsonLD.ts).
It preserves the title, original ingredient lines, ordered steps and section
headings, literal yield wording, notes, categories, timing text and attribution.
Image URLs remain inert candidates pending managed-asset import. Source rating,
nutrition and unrecognized fields are explicitly reported as unsupported.
Neither rating nor a tag is automatically interpreted as a favorite.

`read_webpage_jsonld` reads supplied HTML bytes and extracts every supported
schema.org Recipe from JSON-LD objects, arrays and `@graph`. Multiple recipes
remain separate choices. It does not execute scripts, fetch JSON-LD contexts or
follow source/image URLs. It accepts text ingredients and text, HowToStep or
HowToSection instructions; unsupported structured yield/ingredient shapes fail
explicitly. These offline readers parse supplied bytes; the service read and host-attachment
workflow use the separate boundaries described below.

Both readers return an extraction report with original fields, inert image
candidates and unsupported fields. `source_candidate` then uses the shared
schema-2 ingredient/yield parsers. Unknown servings remain unknown; two loaves
are not two people. The service must establish trusted source context separately
before accepting evidence, and must preserve the extraction report when showing
unresolved fields. There is no legacy-schema fallback when those parsers are
unavailable.

Source JSON is bounded, rejects duplicate keys/nonfinite values and never
honors origin, user-acceptance or provider assertions in its content.
Credential-bearing source URLs fail without echoing their contents. Connection
credentials and API requests are outside these offline readers.

`read_mealie_json` accepts one Mealie v3.24.0 native recipe, with either native
snake-case fields or documented API aliases. Duplicate aliases are rejected.
`open_mealie_export_zip` supports the single-recipe `<slug>.json` plus
`original.webp` layout, and the collection
`recipes/<slug>/<slug>.json` plus `recipes/<slug>/images/original.webp` layout.
These follow the upstream [native exporter](https://github.com/mealie-recipes/mealie/blob/v3.24.0/mealie/services/exporter/_abc_exporter.py)
and [single-recipe export](https://github.com/mealie-recipes/mealie/blob/v3.24.0/mealie/routes/recipe/shared_routes.py).
This is recipe import, not whole Mealie account/backup compatibility.

Structured native ingredient quantities, unit/food names and original wording
are retained. Mealie's person servings remain independent of physical yield.
An externally changed or incomplete native amount does not restore a stale
quantity from display text. Recipe notes, instruction titles/sections and
supported tags/categories survive conversion. Recipe IDs are retained as source
identities; account identifiers, settings and editable Meal Concierge sidecars
are not carried into the culinary candidate. Unsupported fields are reported.

Mealie cover bytes are read only by the dedicated `read_cover` method and still
require `RecipeAssets` decoding. Other attachments within a known recipe
directory are counted as unsupported and are not extracted or opened. These
reader tests prove the declared source shapes and shared normalization, not
authenticated API access or full bank migration.

## Network source readers

`recipe_import_sources.py` is an internal read boundary. The service supplies
reviewed connection configuration and credentials from its existing loader.
Never derive those arguments from imported recipe fields. No function saves a
recipe, imports an asset, sets a favorite, or writes to an external source.

### Public pages

```python
result = fetch_public_webpage("https://recipes.example/soup")
```

One unauthenticated HTTPS GET returns the shared `read_webpage` envelope:
`{mode: structured|text, recipes: [...], text, requires_interpretation, ...}`.
Every structured recipe has the offline extraction fields plus `candidate`
(schema 2) and `source_context`. A text fallback requires the host agent's
interpretation. Invalid JSON-LD fails explicitly. No embedded URL is fetched.

Public DNS results must all be public addresses. Connections use an already
validated numeric sockaddr, with TLS chain/hostname verification for the
original host. There are no proxy, redirect, cookie or authorization paths.
The new HTTP client has a ten-second I/O deadline, including closing responses
and chunk headers. DNS resolution itself uses the system resolver; a slow
resolver cannot be cancelled synchronously, but an expired connection budget
prevents subsequent connection attempts.

### Configured mapped GET APIs

```python
configuration = {
    "base_url": "https://library.example:8443",
    "endpoint_path": "/recipes",
    "records_path": ["data", "items"],
    "fields": {
        "id": ["id"], "name": ["title"],
        "ingredients": ["ingredients"], "steps": ["directions"],
        "yield": ["yield"], "notes": ["notes"],
    },
    "pagination": {
        "mode": "page", "parameter": "page",
        "page_size_parameter": "limit", "page_size": 50,
        "start": 1, "end_condition": "short_page",
    },
}
for record in MappedAPISource(configuration, credential=credential_from_service).records():
    candidate = record["candidate"]
    context = record["source_context"]
```

Credentials are optional and use the existing `{token: ...}` shape. A configured
origin may resolve privately; HTTP follows existing `allow_insecure_http`
rules. Authorization goes only to that exact configured origin. Source JSON
cannot supply another endpoint, headers or pagination URL.

Selectors are literal lists of object keys/list indexes, not expressions.
Required fields are id/name/ingredients/steps. Optional selectors are yield,
tags, notes, source_url, credit, image, description, language, prep_time,
cook_time, total_time. Ingredient and instruction interpretation uses the same
offline JSON-LD extractor and shared source parsers. Image URLs remain inert.

`mode: offset` increments by page size and also requires `end_condition` of
`empty` or `short_page`. `mode: cursor` requires `next_cursor_path`, omits the
cursor parameter when its initial state is null, and terminates only on an
explicit null next cursor. Cursor values remain encoded query values at the
fixed endpoint. Missing cursors/fields and repeated cursors/recipe IDs fail.

Bounds: 50 rows/page, 1000 pages, 10000 recipes, 2 MiB/page, 64 MiB total input,
600 seconds between iterator boundary checks. Errors never imply full import
completion. `source_context.page_state` is the current request state; resume by
setting pagination.start to that value and let the importer reconcile exact
already-saved IDs from the repeated boundary page. The importer must bind
configured_origin + endpoint + external_id into durable source identity before
persistence; a neutral title or publisher cannot establish identity.

### Existing native adapters

`read_native_recipe(configured_adapter, exact_library_recipe_ref)` reads one
recipe directly without a search or editable sidecar projection. It rejects a
different source or a supplied version that no longer matches. Favorite state
is unavailable unless separately observed; direct extraction does not infer it.
It shares the bulk iterator's native field conversion and record limits.

```python
for record in iter_native_recipes(configured_adapter, page_size=50, start_cursor=None):
    candidate = record["candidate"]
    observed = record["source_annotations"]
```

Only existing MealieAdapter/RecipeSageAdapter instances are accepted. Their
existing authentication, API version checks, pagination and exact native IDs
are reused. RecipeSage also verifies account ownership. Mealie raw structured
ingredients use read_mealie_json; RecipeSage raw fields are projected into its
pinned export shape and then use read_recipesage_export. Known RecipeSage
sidecar framing is stripped and reported; it is never decoded as authority.
Native label names come from native recipe relations. A favorite is returned
only when the existing adapter actually observed a boolean favorite. Otherwise
favorite_status is unavailable. No tag/rating is treated as favorite state.

Native calls have the existing adapter's timeouts; aggregate import bounds are
checked between those calls. Both native wire decoders reject duplicate JSON
keys, nonfinite numbers and numeric overflow before interpreting the response.
The extraction boundary separately validates retained field names and wording.

Retained read-only connections permit exact `reconcile_create` lookups while all
write capabilities stay disabled. Reconciliation searches existing owned markers
and verifies their original payload; it never resends a create. Edited, ambiguous,
malformed or unowned results cannot confirm an uncertain operation. This read
capability supports later retirement of external writes without losing recovery.

`source_context.page_cursor` can be passed back as start_cursor for a resumable
boundary page. The service must reconcile repeated boundary records by exact
identity/revision, not automatically replace local edits.

### Explicit native cover retrieval

```python
cover = fetch_native_cover(configured_adapter, exact_library_recipe_ref)
asset_id = recipe_assets.import_bytes(cover["bytes"])
```

The cover method re-reads the native recipe with the existing adapter and
rejects a supplied version that differs before fetching image bytes. Native
enumeration retains the actual fetched `library_recipe_ref` in source context;
cover results return that reference too. The importer must bind both before
attaching a cover. A source lacking versions cannot provide an atomic versioned
snapshot, and concurrent external changes are not automatically reconciled.
Mealie uses its verified fixed media route
`/api/media/recipes/<UUID>/images/original.webp` without a bearer header.
RecipeSage permits an exact native image.location on the independently configured
origin, including a self-hosted private source, or through the unauthenticated
public HTTPS path. Other private origins remain forbidden. Credentials are never
sent with image requests. When multiple images exist, pass `image_url` explicitly.
No image location is implicitly fetched during recipe enumeration.

The result is `{bytes, content_type, source_url, image_status, library_recipe_ref}`. It accepts only
JPEG/PNG/WebP media types, at most 12 MiB, and still says
requires_asset_sanitization. RecipeAssets must decode/sanitize the bytes before
the service can attach a managed asset. This helper's exact source URL can be
retained in private source context; managed image metadata must separately obey
the canonical image URL/attribution contract.

### Tested boundaries

`tests/test_recipe_import.py` uses the existing sanitized Mealie v3.24.0 and
RecipeSage v4.0.6 API fixtures with real adapters against ephemeral loopback HTTP
servers. It covers mapped pagination and authentication, redirect refusal,
response limits, native quantities and sidecar removal, favorite observations,
and Mealie cover retrieval followed by actual managed-image sanitization.
Public DNS/socket/TLS are exercised with synthetic boundaries; no live public
TLS or authenticated source-account acceptance is claimed.

The plain HTML fallback preserves paragraph/list/table-cell separation and
contiguous inline text. It ignores script/style/template and explicitly hidden
content, supports common omitted head/paragraph/list/table end tags, limits
nesting to 128 elements and output to 64 KiB, and rejects malformed structured
JSON-LD instead of silently falling back. It is a text extractor, not a browser
renderer: CSS layout, external resources and scripts are not interpreted.
The host agent must still identify the recipe and report ambiguity or missing
fields before the shared import/save workflow.

The service import/image path, controlled write retirement and private restore
are described here and in the reference. Actual model-driven attachment
acceptance remains specific to each client; parser or protocol tests alone do
not establish that a client read the original attachment.

## Supplied text and host transcriptions

`read_transcript` accepts `kind: pasted_text`, `photo_transcript` or
`pdf_transcript`, one to 20 uniquely numbered `pages`, and an `interpretation`.
Each page supplies `page` and `text`; an unreadable page supplies an explicit
`issue`. Combined source text and issues are limited to 64 KiB of UTF-8.
The reader does not open files or perform OCR. Photo/PDF text must come from
the host agent's actual access to the original attachment.

The interpretation contains a name, ingredient and step selections, and optional
yield, notes, tags and language. Selections use `{page, quote}`; each quote must
occur verbatim on that supplied page. The shared ingredient/yield parsers derive
source quantities. Ingredient and yield evidence retains its page number and
original wording. Unknown amounts or servings remain unknown.

An ingredient may add `estimated_amount: {quantity, unit, assumptions}`; a yield
selection may add `estimated_portions: {quantity, assumptions}`. These values
always become unaccepted estimates, independently of the physical yield. The
input cannot supply evidence, acceptance, rights, provider, managed image or
trusted source-context assertions. Optional attribution permits only URL,
publisher, title and author; known store attribution retains effective binding.
Full private recipe storage does not imply a redistribution license.

The result contains the candidate, source/page context, unreadable-page issues
and selected excerpts. Its source digest is stable for the same ordered pages
and attribution, even when the interpretation changes. No personal recipe entry,
favorite or managed image is created. The service still must establish its
qualified identity, show readiness, and process the requested save.

For photo/PDF imports, source evidence means explicit wording in the supplied
transcription. The service has not verified that wording against original pixels
or PDF bytes. Missing attribution remains unknown; it is never reported as a
verified absence of store binding. Actual client attachment and service-save
acceptance remain separate from these extraction tests.

## Offline collection file, version 1

The collection builder and importer use `recipe_portable.py`. The builder calls
`write_archive(destination, manifest, files)` with explicit local file paths;
it never recursively includes a directory. The destination must not exist.
The writer creates a private file, builds the inventory and checks the complete
result. Consumers use `open_archive(path)` as a context manager, call `verify()`
before writes, and stream `records()` from that same opened archive. Asset bytes
are available only through the dedicated `read_asset(asset_id)` method.
Member order, timestamps and permissions are fixed so unchanged inputs produce
identical archives with the same Python/zlib build.

ZIP members are exactly `manifest.json`, `records.jsonl`, optional
`attribution.json` and `coverage.json`, and `assets/<sha256hex>.jpg` files.
Directory entries, other paths, duplicate names, symlinks, encrypted entries,
multidisk/ZIP64 archives and unsupported compression are rejected. Nothing is
extracted to paths supplied by an archive.

The manifest fields are:

```json
{
  "format": "meal-concierge-recipes",
  "format_version": 1,
  "kind": "bundled",
  "pack_id": "wikibooks-themealdb-en",
  "pack_version": "2026-09-06.1",
  "recipe_schema_version": 2,
  "normalizer_version": "declared-by-builder",
  "records_count": 1,
  "files": [
    {"path": "records.jsonl", "bytes": 1234, "sha256": "64 lowercase hex characters"}
  ]
}
```

The inventory contains every other member and excludes the manifest itself.
`bytes` and `sha256` describe the exact uncompressed bytes. Extra manifest
metadata may describe source scope, snapshot identity and build configuration;
large acquisition/attribution reports belong in the separate report members.
These declarations are data, and cannot establish provenance or rights.

Every JSONL line is one envelope with exactly `recipe_id`, `status` and `recipe`.
`recipe_id` is the stable source identity chosen by the builder, such as
`wikibooks:123` or `themealdb:52772`. `status` is `ready` or `draft`; `recipe` is
the versioned culinary document. Each line, including the last, ends in a
newline. Duplicate identities and mismatched schema/counts fail inspection.
The common bank workflow must still validate the normalized document and actual
readiness before mapping `ready` to an active entry.

The framing limits are 1 GiB compressed, 2 GiB expanded, 10,000 records,
512 MiB of JSONL, 512 KiB per envelope and 256 KiB per culinary document.
Manifest size is at most 4 MiB; each attribution/coverage report is at most
16 MiB. A managed JPEG is at most 4 MiB, with its exact byte digest as filename.
These are scoped archive limits; native JSON import and ordinary RPC limits do
not change. Actual collection sizes and runtime installation measurements are
reported by the collection build, not inferred from these ceilings.

The codec checks archive integrity, framing and referenced asset presence.
`RecipeAssets` separately validates supported raster decoding and sanitization;
the source builder separately establishes image/text attribution and public
distribution suitability. `read_asset` does not return an image in an ordinary
recipe response. Managed JPEGs are installed without recompression so historical
references retain the same digest.

## Private recipe file, version 1

`export_private_archive(destination, store)` exports one coherent recipe snapshot
from an explicit `RecipeStore`. Its caller holds the installation's offline
ownership locks throughout the database and asset reads. It opens SQLite directly
in read-only mode and checks the exact household and schema 6 internally. It
does not migrate, clean up or checkpoint the source. The current rollback-journal
bank is supported; WAL mode or a nonempty journal fails explicitly because a
read-only WAL connection can create source-side shared-memory files.

The destination must not exist and is created with mode 0600. Temporary files
stay in a private directory beneath its selected parent and are removed on exit.
`open_private_archive(path)` and `write_private_archive(destination, manifest,
files)` select this format explicitly. The ordinary public archive APIs reject it.

The owner-facing command uses an existing, stopped installation:

```bash
python import_recipes.py --export-private /chosen/path/recipes.zip \
  --installation-home /chosen/installation
```

It reads that home's `runtime.json` and exports the exact manifest-selected state,
which may be in an adopted data directory. The same process holds the installer
lock and existing `install.offline` lifetime locks until export finishes. An
active service or occupied state/browser/listener target fails before export.
The command does not stop or restart services, adopt legacy paths, or use a
pending installation manifest. An optional `--state-directory` must match the
manifest. Normal JSON/image imports still require `--state-directory`.

The private manifest has exactly `format: meal-concierge-recipes`,
`format_version: 1`, `kind: private`, `private_schema_version: 1`, `recipes_count`,
`records_count` and `files`. Each canonical revision retains its own schema 1 or
2, so there is no single recipe schema version in the manifest. Members are
`records.jsonl` and managed `assets/<sha256>.jpg` only; shared ZIP framing,
inventory and path checks still apply.

Each recipe occupies one contiguous group in the JSONL stream:

- An entry row with `type: entry`, `id`, `revision`, `status`, `source_key`,
  `created_at`, `updated_at`, `created_via`, `entry_origin`, `pack` and `favorite`.
- All retained revision rows, in increasing revision order, with `type: revision`,
  `recipe_id`, `revision`, `status`, `document` and `created_at`.

`pack` is null or `{pack_id, recipe_id, version, baseline_hash}` for bundled
entries. `favorite` is null if there was never a favorite row; otherwise it holds
`is_favorite`, `favorite_revision`, `created_at` and `updated_at`. An explicit
false favorite retains its revision. The exact stored source key survives,
including migration identities. The final revision must match the entry's head,
status and timestamp. Revision gaps are preserved.
Recipe and pack identities must already use canonical bank text. Credential-bearing
URLs in source identities or retained recipe wording fail explicitly; export never
silently rewrites an identity or removes part of the stored recipe.

`archive.verify()` checks every record and managed raster before destination
writes. `archive.recipe_entries()` yields `{entry, revisions}` groups;
`archive.records()` yields their individual typed rows. The format allows at
most 10,000 entries, 100,000 total rows and 64 MiB of encoded history per entry,
within the shared 512 MiB JSONL and archive limits. Historical covers share the
existing member budget; missing or excessive history/assets fail explicitly.
Every referenced historical cover is included without recompression.

Documents retain source attribution, rights, provider binding, notes and accepted
evidence. Household identity, account principals, credentials, discovery tokens,
operation journals and installer pack reports are excluded. Recipe origin and
pack baseline remain recipe metadata; `locally_modified` can be recomputed.
A consistent full-installation backup is needed for history outside recipes,
operation journals and installation reports.

Restore an explicitly selected private archive with:

```sh
python import_recipes.py --restore-private /chosen/path/recipes.zip \
  --installation-home /chosen/installation
```

The command holds the same installer and stopped-runtime ownership as export.
It closes a private staging copy and verifies the whole archive before writing
bank entries or assets. The bank commits each complete entry and retained history
atomically, preserving exact IDs, timestamps, source identity, origin/pack
metadata and favorite CAS state. Identical entries are unchanged; different local
entries or occupied identities are conflicts. It does not merge histories,
rewrite journals or make restored store-bound recipes eligible elsewhere.
Historical managed JPEGs retain their exact bytes. A later failure reports
committed counts and supports replay; at most 100 outcome details are returned.
This is a recipe archive restore, not a whole-household backup or ordinary RPC.

## Trust and integration boundaries

`kind: bundled` is an untrusted manifest assertion. Only the dedicated verified
pack installation context may assign bundled entry origin. Ordinary imports
remain user-origin and preserve known store binding; null binding does not grant
public redistribution. A source instruction cannot change provider, favorite,
cart or save authorization. The framing codec neither changes origin nor writes
the bank; only the dedicated application API below does so.

`preflight_archive(path, expected_descriptor)` checks the entire archive against
an independently selected release descriptor: exact compressed bytes and SHA256,
format/version, recipe schema, pack identity/version and normalizer version.
It validates every normalized document and managed raster, rejects local estimate
acceptance and effective store bindings, and checks that ready records have
resolved scaling and shopping quantities. It makes no state writes. The read-only
CLI is `recipe_portable.py preflight --archive PATH --expected-json JSON`.
The shared provider resolver includes known-store source and upstream attribution
even when an explicit binding is null. This exclusion does not establish public
rights; the release builder must independently qualify source and image rights.

The installer calls `apply_archive(path, state_directory, household,
expected_descriptor)` in process while holding the installation's actual offline
ownership locks. It supplies a private, closed staging copy of the selected
archive; hashing an opened descriptor alone cannot prevent another writer from
changing that same inode. Ordinary uploaded files must not invoke this trusted
API or choose their own trusted descriptor. There is no standalone apply CLI.
The consumer repeats full preflight before making any bank or asset changes.

Each record uses `RecipeStore.import_pack_record` in its own transaction. Stable
pack/record identity makes reruns idempotent. Existing favorites, archive state,
local edits and source identities remain intact; differing records produce
explicit conflicts. Assets are installed before their referencing record.
Committed records remain available after interruption, and rerunning the same
verified collection continues without creating duplicates. Conflicts require
review and are not silently resolved by repeated application.

Exact manifest, attribution and coverage bytes are retained before bank writes
under `pack-metadata/<hash-of-pack-id-and-version>/` within the installation state.
A different artifact reusing that version fails instead of replacing its notices.
Directory and file creation are synchronized before committing recipe records.
`status.json` records progress; `results.json` contains the full per-record result
of the latest finished or cooperatively interrupted attempt. The returned JSON
contains counts and at most 100 record results, with an explicit truncation flag.
An abrupt process exit can leave `in_progress`; the committed bank records are
the authority when resuming. Report paths are relative, so a full state backup
and relocation preserve notices, reports and assets together. Storage failure
may prevent a final report; it does not undo committed recipes.

The public consumer deliberately rejects `kind: private`; private application
uses the separate explicit offline restore command above.

A portable recipe export is separate from a consistent installation backup.
It must not overwrite order/email/library outcome journals. The existing
installer's stopped-service database-plus-assets backup remains the full
installation recovery path; restoring old journals after possible external
effects is not a recipe-import operation.
