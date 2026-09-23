# Recipe sources and import

Use the installed recipe tools for source reading, preview and explicit save.
Import creates a frozen technical discovery, not a personal recipe-bank entry.
Use its returned `discovery_ref` with `recipe_write` only when saving is wanted;
retain the exact reference and stable idempotency key on recovery. A draft or
link-only bookmark is not a complete shopping recipe.

## Source choice and storage

For URL/transcript import, supply `storage_decision` before fetching/persistence:
`{storage:"full",basis:"own_recipe"|"permission"|"license"|"private_use",evidence:"concrete assessment"}`.
Use `own_recipe` only for supplied text identified as the user's own. Public
access or an enabled search domain does not establish storage permission. If
unresolved, `{storage:"link_only"}` retains a URL bookmark without fetching its
content. Full private storage does not grant redistribution rights.

- `source_kind="url"`: pass the exact URL. Structured sources are read first;
  use returned bounded text for a second read with top-level `interpretation`
  only when requested by the result. Quotes must match the newly fetched page.
- `source_kind="library"`: pass the exact configured `library_recipe_ref`.
- `source_kind="transcript"`: first read the actual supplied attachment/text,
  then put `interpretation` **inside** `transcript`, as below.

```json
{"source_kind":"transcript","transcript":{"kind":"pasted_text","pages":[{"page":1,"text":"Rice\n100 g rice\nBoil the rice."}],"interpretation":{"name":"Rice","ingredients":[{"page":1,"quote":"100 g rice"}],"steps":[{"page":1,"quote":"Boil the rice."}]}}}
```

The example shows the transcript shape; supply the actual storage assessment
separately. Kinds are `pasted_text`, `photo_transcript`, `pdf_transcript`.
Preserve original page numbers and exact quotes. Use at most 20 pages and 64 KiB
of text/issues per transcript; unreadable pages carry an explicit `issue`.
Missing amounts/servings stay unknown, not zero. Physical yield (two loaves)
is distinct from person portions. An ingredient may carry
`estimated_amount={quantity,unit,assumptions}`; a yield selection may carry
`estimated_portions={quantity,assumptions}`. These remain honest estimates and
need no separate acceptance ceremony. Do not fabricate evidence or acceptance.

## PDF and photo attachments

Prefer the host's working native reader. If PDF reading is incomplete and local
execution/image reading is available, use this skill's `scripts/read_pdf.py`:

```sh
python3 /absolute/installed/skill/scripts/read_pdf.py /actual/source.pdf --output /new/private/page-directory
```

Resolve these to the actual installed skill and attachment paths. The helper
uses the installation's private renderer; no system PDF package is needed.
For longer PDFs, use `--pages FIRST-LAST`, at most 20 pages per batch, with a new
output directory each time. Read the resulting page images before transcribing.
The manifest proves rendering, not reading or recipe import. Report unreadable
content and partial-book coverage honestly. A denied read is not authorization
for a fallback. NanoClaw templates omit this helper: use their native reader or
source page images. A remote service cannot open a client-only attachment.

## Adaptation and retailer provenance

Keep schema-2 original text, structured quantities, portions, method, attribution,
rights and amount evidence distinct. Scale amounts as exact fractions, not times
or temperatures. Ordinary brand/package choices belong in product preparation.
For a culinary change, use discovery `adapt` with exactly one original
`recipe_ref={id,revision}` or `discovery_ref`, its original `recipe_digest` and
`source_schema_version`. Prefer bounded `changes`: ingredient edits contain
zero-based `index`, replacement `item`, and concrete `assumptions`; optional
`quantity`/`unit` replace the amount. Include the complete coherent `steps` when
editing ingredients. Optional top-level `portions` scales the original before
these edits. Read all relevant recipe pages first. The service preserves original
text, attribution, rights, provider binding and untouched evidence, and labels
changed amounts as estimates. It removes stale product hints only on changed
ingredients. The returned discovery is separate from the original; saving a
personal variant remains a separate choice.

A complete schema-2 `recipe` is still supported instead of `changes`. Preserve
source attribution/rights and original text, set `source.relationship="adapted"`,
and retain estimate assumptions. Never copy a scaled display's calculated
provenance into a reconstructed original. `convert` changes representation while
preserving source facts; it is not a substitute for adaptation.

Oda, Mathem and MENY snapshots retain their original provider binding, even
after adaptation. Do not relabel store content as neutral to shop elsewhere.
Neutral personal/external sources work with the selected store. Oda product
hints are candidates only; fresh product observations establish usable package,
price, availability and dietary facts. Mathem/MENY have no invented equivalents.
Web search is optional: omit `backend` to use the installation's selected search
provider. Check returned coverage; candidate links are not recipe evidence.
Automatic web-discovery imports use `web_discovery=true` and a storage decision;
manual supplied URLs remain independent of those search settings.
