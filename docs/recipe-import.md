# Recipe import boundaries

This is the shared reader and portable-file contract. Bank installation, private
restore and client attachment integration remain pending their shared runtime
changes. Successful file inspection does not mean recipes have been saved.

## Native source extraction

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
explicitly. These offline readers do not yet establish the URL fetching or
actual host-attachment workflow.

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

## Trust and unfinished integration

`kind: bundled` is an untrusted manifest assertion. Only the dedicated verified
pack installation context may assign bundled entry origin. Ordinary imports
remain user-origin and preserve known store binding; null binding does not grant
public redistribution. A source instruction cannot change provider, favorite,
cart or save authorization. The codec neither changes origin nor writes the bank.

The bank importer will preserve exact source identities and existing favorites,
archive state and local edits, report explicit conflicts, and retain complete
committed records on interruption. Until that integration is implemented, this
codec is not evidence of resumable bank installation. It deliberately rejects
`kind: private`; the version-preserving private recipe export/restore contract
will be enabled with its actual bank and asset integration.

A portable recipe export is separate from a consistent installation backup.
It must not overwrite order/email/library outcome journals. The existing
installer's stopped-service database-plus-assets backup remains the full
installation recovery path; restoring old journals after possible external
effects is not a recipe-import operation.
