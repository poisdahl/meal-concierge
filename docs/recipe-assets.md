# Managed recipe covers

The built-in bank stores one optional managed cover per recipe version.
Local attachment import, bank origin metadata and frozen HTML/MIME rendering
use the same digest references. A remote sender needs access to the service's
managed assets to embed them; text-only destinations retain the attribution.

One optional `image` belongs to an exact recipe document version. It contains
`asset_id: "sha256:<64 lowercase hexadecimal characters>"`, plus nullable
`alt`, `source_url`, `creator`, `credit`, `license`, `license_url` and `changes`.
Text metadata is limited to 500 characters; URLs are credential-free HTTPS,
at most 2,048 characters. This is the shared recipe-v2 image shape. Image
credit and rights are independent of recipe-text credit and rights. Neither
an image URL nor license-looking text establishes public redistribution rights.

## Import and storage

`recipe_assets.RecipeAssets` stores immutable files named `<digest>.jpg`
beneath a caller-selected private asset directory. The installation uses
`recipe-assets` beside `recipes.sqlite3`, outside replaceable code packages.
Recipe documents carry only the digest reference, never image bytes or an
absolute path. Reading an image never downloads its source URL.

`import_file(import_root, relative_path)` reads an explicitly supplied local
attachment beneath its trusted import root. Relative traversal, symlink
components, nonregular files and oversized files are rejected. The root is
selected by the local operator, not by downloaded recipe content.
`import_bytes(data)` is the corresponding local in-memory ingestion primitive.

For an explicit local attachment, run:

```bash
python3 import_recipes.py --state-directory /path/to/state \
  --import-root /path/to/attachments --image cover.png
```

`--dry-run` decodes and reports the resulting `asset_id` without writing it.
Importing the image creates no recipe. Supply the returned ID in a schema-2
recipe's `image`, with independently supplied credit/license metadata, through
the ordinary recipe save/update or JSON import path. New and replaced covers
must resolve to valid managed files before the recipe transaction commits.
Completed retries and exact source duplicates return their existing identity
even if a cover later goes missing. Existing same-asset metadata edits, cover
removal and historical reads do not require the optional file to remain present.

Supported input is static JPEG, PNG or WebP, identified by actual decoding.
The input must be at most 12 MiB, 24 million pixels and 12,000 pixels on either
edge. Animated images and unsupported formats fail explicitly. JPEG decoding
uses strict errors before sanitization; this rejects incomplete scans that
Pillow alone can replace with invented pixels. Fatal decoding, malformed
format and supported decoder integrity failures are errors, not successful
imports. This is not a claim to detect every possible change to image pixels.

The importer applies EXIF orientation, downsizes to at most 1,600 pixels on
either edge, composites transparency on white, and encodes fresh RGB pixels
as baseline JPEG at quality 85. Fresh renditions exclude embedded EXIF/GPS,
device information, ICC, XMP, comments and PNG text. Explicit recipe image
credit/license fields remain in the recipe document. The identifier hashes
the exact rendition bytes. Repeating import with this installed encoder and
identical source bytes returns the same identifier. A different rendition
creates a different identifier; no existing asset is overwritten or collected.

`install_managed(asset_id, data)` is exclusively for a trusted private backup
or a verified recipe-pack artifact. The caller must establish that provenance;
matching a downloaded digest is not sufficient. It checks the digest, bounded
RGB dimensions, baseline JPEG markers and strict decoding, and preserves the
bytes exactly. Ordinary external attachments use fresh sanitization instead.
Managed checks reject embedded metadata markers and data after the final
image marker; they do not promise removal of arbitrary steganographic content.
Restoration must not recompress a managed rendition, which would change frozen
asset identities. Private retailer assets cannot enter public packs.

## Bank origin and pack imports

SQLite schema 6 adds entry metadata without rewriting culinary documents,
historical revisions or their digests. New explicit saves and ordinary imports
are `entry_origin: user`; existing schema-5 entries migrate to `unknown`.
Downloaded `entry_origin`, `pack` or `locally_modified` fields cannot grant an
origin. Discovery snapshots are not personal-bank entries.

Only the internal verified-pack consumer calls
`RecipeStore.import_pack_record(recipe, pack_id=..., recipe_id=..., version=...,
status="ready")`. It must verify archive provenance and install exact managed
assets first. The API accepts `ready` (active) or `draft` and commits one record
at a time. Its result has `outcome: created|unchanged|conflict`, the bank `recipe`,
and a conflict `reason` when applicable. This is not a normal RPC import mode.

Bundled entries expose `pack: {pack_id, recipe_id, version, baseline_hash}`.
The initial normalized document establishes the immutable baseline. Reimport
looks up the stable pack/recipe pair before mutable source metadata. Unchanged
content preserves the original ID, version, history, favorite and archive state.
Changed incoming content or a locally edited current document reports a
conflict. An existing user/unknown source duplicate retains its origin.
An interrupted import resumes by repeating the same per-record calls.

`locally_modified` compares the current document with that baseline, including
when reading a historical revision; archiving or favoriting is not a content
edit. Built-in search can filter `entry_origin=user|bundled|unknown` before its
limit and independently combine `favorites_only` or `include_archived`.
Pack origin does not authorize redistribution or override provider eligibility.

## Frozen email payloads

`recipe_email.prepare_recipe_media(menu_snapshot, assets,
images_supported=False)` reads only image references from the frozen menu's
`dishes` and `salads`. For a capable local sender it returns `image_cids`,
`inline_images` and `image_warnings`. Each inline descriptor has `asset_id`,
`content_id`, `filename` and `content_type: "image/jpeg"`. It contains no bytes
or local path. Unsupported destinations never read images. Missing/corrupt
assets produce a warning and leave the recipe usable as text.

The Application renderer supplies HTML with these CID references, a frozen
`html_without_images` fallback, and independently rendered image attribution.
The existing recipient, subject, provider, order and claim identity remain
under the existing email operation's ownership.

`recipe_email.build_message(payload, assets, sender=None)` constructs a local
`EmailMessage`; it does not send, contact a provider, or acknowledge a send.
It resolves every descriptor from the actual managed asset directory and
builds a multipart text/HTML message with inline JPEG parts. If any required
image becomes unavailable or the descriptors do not match the HTML, it uses
the frozen HTML fallback and omits all images. It never looks up the current
bank cover. This supports relocation between preparation and sender execution
when the same assets are restored beside the bank. A remote client cannot
resolve service-host assets merely by receiving these references; its sender
needs a supported host-local resolver or must use text-only presentation.

The helper's local MIME tests establish byte/CID resolution and readable
fallback. Actual Application preparation, send-protocol recovery and the
local sender integration are separate tests; no real delivery is implied.

## Backup and restore

SQLite-only backups exclude image files. A consistent private installation
backup must copy household state, the SQLite bank and the entire
`recipe-assets` directory while its writers are stopped through the existing
maintenance procedure. Retaining all managed files also retains historical
menu/order/email covers; this feature performs no garbage collection.

Restore the latest consistent state and assets together into a new empty
installation, preserving relative placement. Verify both current and frozen
historical references through the restored asset root. Do not restore older
order/email/library journals over possible subsequent external effects.
The existing installation backup copies the state tree; bank-only export
requires a separate copy of its assets and is not a whole-household rollback.
