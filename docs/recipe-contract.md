# Versioned culinary document contract

Recipe document versions and SQLite versions are separate. This contract adds
document schema 2 without changing SQLite schema 5. Stored schema-1 documents,
revisions, discovery references and digests are decoded by their original rules;
reads do not migrate them. Unversioned legacy text-only requests retain schema 1.
New producers specify `schema_version: 2`; supplying a new typed field also
selects 2. A schema-1 request containing new fields is rejected. An update cannot
downgrade an existing schema-2 document and silently discard its evidence.

The culinary version retains existing name/language/tags/steps/times/storage,
source and rights fields. Bank ID/revision, favorite/archive state, entry origin,
pack metadata and technical creation fields remain bank metadata. Projected
`entry_origin`, `pack` and `locally_modified` never become culinary facts or
user-writable origin grants. Asset/origin storage and provider eligibility are
separate integration packages; this representation does not activate them.

## Quantities and matching

Schema-2 `ingredients[].quantity` is null or a reduced positive rational:

```json
{"numerator":350,"denominator":3}
```

Inputs also accept finite positive JSON numbers and exact decimal strings.
After reduction, numerator is at most 10^15 and denominator at most 10^12;
oversized inputs or scaling results fail explicitly. Arithmetic uses fractions
through scaling, requirement aggregation and product selection. Package counts
are rounded only after aggregate need is known. Twelve servings with 700 g
scaled to two require exactly 350/3 g; six such requirements aggregate to 700 g.
`portions` remains a positive finite numeric person count, or null, for existing
planner/client compatibility. Scaling reads its decimal representation exactly.
Time and cooking-temperature instructions are not mechanically scaled.

Legacy ingredient display numbers remain unchanged. Their newly calculated
shopping requirements use the same rational representation. When reading a
pre-existing frozen binary-float requirement outside the exact bounds, the sole
legacy recovery boundary accepts a rational with denominator at most 10^9 only
when its absolute difference is at most 10^-12. It does not rewrite the stored
document or historical digest. Schema-2 calculations never take this fallback.

Supported canonical dimensions are grams, millilitres and count. Shared aliases
include g/gram(s), kg/kilogram(s), ml/cl/dl/l and common litre spellings,
stk/stykk/count/piece(s), Norwegian ts/teskje(er) = 5 ml and ss/spiseskje(er) =
15 ml. The internal metric culinary aliases tsp/teaspoon(s) and
tbsp/tablespoon(s) have the same 5/15 ml meanings. Source readers mark that
English spoon convention as an estimate when its locale is unverified; it
requires explicit acceptance. US customary and metric culinary spoon measures
differ, as shown by [NIST's conversion table](https://www.nist.gov/pml/owm/metric-si/unit-conversion/approximate-conversions-us-customary-measures-metric).

Explicit `metric cup` = 250 ml and `us cup` = 236.5882365 ml are distinct;
unqualified cup/fluid-ounce/pinch/free-text quantities stay unresolved.
Mass oz = 28.349523125 g and lb = 453.59237 g use the international avoirdupois
definition ([NIST weight conversion factors](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=200329)).
There is no automatic volume-to-mass conversion. A reader may convert only when
it has ingredient-specific evidence and retains that input/conversion basis.

`item` is the supplied consistent ingredient matching name. Preserve its input
in `original_text`; `raw`/`amount` are normalized display text in schema 2.
Readers parse only explicit supported measures and otherwise keep source text
unresolved. The calling normalization workflow supplies any needed translation
into the household's matching language. Similar titles/names do not establish
ingredient or recipe identity. Product requirements merge only exact normalized
item identities in compatible unit dimensions. `pantry` is a suggestion, not
proof of stock; existing explicit pantry decisions still apply.

## Yield, evidence and acceptance

`yield` is null or:

```json
{
  "original_text":"2 loaves",
  "quantity":{"numerator":2,"denominator":1},
  "unit":"loaves",
  "evidence":{
    "quantity":{"basis":"source","input":"2 loaves","assumptions":null,"conversion":null},
    "unit":{"basis":"source","input":"2 loaves","assumptions":null,"conversion":null}
  }
}
```

Yield never supplies person servings implicitly. `portions_evidence` describes
person servings separately; ingredient `evidence.quantity` and `evidence.unit`
describe each calculation field. Evidence is:

```json
{"basis":"estimate","input":"2 loaves","assumptions":"Six slices per loaf; one slice per person.","conversion":null}
```

`basis` is `source`, `user`, `estimate` or `unknown`. Input, assumptions and
conversion are nullable strings of at most 1000 characters. Deterministic
scaling adds an optional `calculation` record containing
`operation: "portion_scale"`, exact `input_quantity` and exact `factor`, retaining
the original basis/input/conversion. Ingredient calculations additionally carry
exact `input_portions` and the original `portions_evidence`, with no nested
calculation. Scaled person servings retain their original numeric input in their
own calculation. Repeated scaling keeps the original inputs and combines exact
factors. The decoder verifies that the arithmetic matches the derived values.
A calculation depending on an estimate remains estimated: readiness and display
inspect both its quantity and serving evidence, retaining each acceptance and
assumption. Original ingredient/yield wording is bounded to 500 characters.

Normalization is a version-aware decoder, not an authority check. At actual
caller/import boundaries, source claims require trusted source context or the
unchanged exact prior field. A culinary payload cannot create acceptance,
create or change service-owned calculation provenance,
relabel a known estimate, downgrade its version, remove known provider binding
or replace original attribution. Generated new input is estimated. Editable
external-library metadata cannot establish local user acceptance.

Recipes can be read and saved with unknown servings or unaccepted estimates.
Automatic scaling rejects unresolved relevant evidence. Product preparation
returns explicit unresolved ingredient/serving reasons rather than zero or
invented quantities. A direct inline menu never fills missing source servings
from the household profile. Explicit authored person servings remain supported.
Existing allergy/avoid/strict-time/nutrition evidence rules are unchanged; these
quantity records do not establish safety or nutritional compliance.

Recipe get/discovery responses expose `recipe_digest`. After the calling agent
shows the exact estimates and obtains explicit current-user acceptance, recipe
write `accept_estimates` takes either `recipe_id` plus `expected_revision`, or
`discovery_ref`; the digest; exact `estimate_fields`; and this statement:

```text
I accept these exact recipe estimates and their stated assumptions.
```

There is no replacement recipe payload. Only existing estimated fields can be
accepted. A saved recipe requires an idempotency key and creates a new revision;
an unsaved discovery creates an idempotent new frozen snapshot without a bank
entry/favorite. Acceptance is stored on each selected evidence record as
`{recipe_digest, statement}`. It retains the estimated basis and assumptions.
Copies, changed values and source content cannot invent or transplant it.
Menu/chat/email continue to show estimate labels, including after restart.

## Source identity and staged integrations

Existing source kind/publisher/author/title/url/external_id/relationship remain.
Optional `source.original` has nullable url/external_id/publisher/author/title,
preserving upstream attribution separately from an aggregator. TheMealDB
`strSource` is retained without fetching it. Primary source and permanent URLs
retain query order/encoding and unknown identifying parameters; only `utm_*`,
`fbclid`, `gclid` and `msclkid` are removed for new normalization. Old stored URL
rules remain unchanged. HTTPS credential-free URLs are required; an upstream
attribution URL may also retain HTTP honestly. No attribution field triggers a
network fetch. `external_snapshot` retains fetched timestamp, hash, source
revision, permanent URL and change notes; a hash does not replace input wording.

`rights.storage` (`full`/`link_only`) describes retained content. License/credit
strings do not authorize public redistribution. `source_provider` is nullable
`oda`/`meny`/`mathem` and remains distinct from existing operation-journal
`provider_binding`. This stage preserves known bindings and retains the current
full-original-store restriction; provider-private saves/eligibility await their
trusted integration.

Optional `image` is null or one exact versioned record with required
`asset_id: "sha256:<64 lowercase hex>"` and nullable `alt`, `source_url`,
`creator`, `credit`, `license`, `license_url`, `changes`. Text is bounded to
500 characters, credential-free HTTPS URLs to 2048. There are no embedded bytes
or filesystem paths. Image attribution is separate from recipe-text rights.
Representation and historical decoding preserve it, but new/replaced/removed
managed-image writes report unsupported until the asset importer is installed.
An unchanged prior image stays readable even if the local asset is missing.

Mealie/RecipeSage native source readers preserve original text and distinguish
yield from servings without requiring a Meal Concierge sidecar. Supported
explicit source quantities are parsed; residual text stays unresolved. Schema-2
external-library writes and edited schema-2 sidecars report unsupported before
external dispatch rather than discarding fields. Existing supported schema-1
text-only write/reconciliation paths remain. Ordinary native JSON imports reject
privileged evidence/acceptance or new image writes before committing any rows;
dedicated trusted source import/private restore handles those separately.

The focused behavioral contract is exercised by
[the recipe contract tests](../tests/test_meal_concierge_recipe_contract.py).
The suite uses synthetic/local data and actual Application/menu/product code;
it does not certify live provider availability, image importing or public-pack
redistribution.
