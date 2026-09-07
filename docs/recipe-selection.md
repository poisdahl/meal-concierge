# Bounded recipe selection

`recipe_selection.py` supplies the compact projection, source-family grouping,
context queries, bounded retrieval and shortlist used by automatic selection.
Application owns source reads, trusted normalization, immutable references and
provider eligibility. Menu `plan` accepts `planner_input={week, dates?, portions?}`
without candidate references. Application collects the enabled local bank and
selected retailer, loads full details, shortlists and runs the planner. Installed
user/imported/bundled entries participate through `internal`, independently of
upstream pack API switches. Existing explicit candidate requests remain valid.

Summaries contain exact references, source/version identity, title, known
servings/time, tags and available bank metadata. Ingredients and steps are
explicitly `not_loaded`; missing servings/time are unknown. Full exact details
must be resolved before readiness, ingredient scoring and planning. A summary
cannot pass the planner. Discovery does not save personal recipes or favorites.

Retrieval allows 20 candidates per page, six page calls per enabled source,
80 detail attempts overall and one 30-second absolute deadline. Source adapters
must honor that deadline and provide continuation/exhaustion only when their
actual contract establishes it. Sources receive a turn before early completion;
there are no fixed per-source candidate quotas. Rejected, duplicate or stale
details cause bounded refill. A stale reference is not replaced with latest.
Measured page/detail counts and elapsed time accompany the result.

MENY's current search returns a bounded visible prefix and ignores `page`.
Its short nonempty result establishes neither continuation nor exhaustion.
Bounded context-derived queries can find further candidates; an unsupported
continuation remains a search limitation. Oda/Mathem native recipe detail and
pagination contracts remain unverified. This module does not invent them.

Disabled, empty, unsuitable, unavailable, timed-out, rate-limited and bounded
search results survive even with zero candidates. Automatic AI eligibility
requires zero suitable existing recipes, no unknown hard/readiness evidence,
no failed details, and completed bounded searches of enabled available sources.
A shortfall, outage, truncated result or planner work limit does not authorize
automatic generation. The calling agent remains responsible for an explicitly
marked generation workflow; there is no additional model service.

Known publisher/opaque source IDs and identity-preserving URLs join source
families, including saved and web versions. Tracking parameters and observed
retailer URL aliases do not split families; unknown identity and revision query
parameters remain significant. Similar titles or ingredient names do not join
families. Exact versions and local edits survive. Ready saved versions take
precedence within a family, but a draft cannot hide a usable web version.
Known family usage blockers are combined without rewriting history. The
request-local history index reads retained menu/order documents, then exact
local bank/discovery references where necessary. It never scans the catalog,
fetches latest source content or ignores an archived version's identity.
Historical builtin-library references retain their exact version; external
libraries are not fetched by this helper. Each historical key receives the
existing usage evaluation separately, so a retired/not-cooked alias cannot
clear another active alias. Work stops at 4000 retained documents or 2000 exact
local reads, with an explicit `history_work_limit`. Missing legacy identity or
expired unretained snapshots report partial coverage and receive no
never-used ranking bonus. Application applies this at candidate resolution and
save-time revalidation. Known recorded usage remains visible even when other
historical source identities are unresolved. Explicit feedback is applied before
shortlisting and refreshed through the same family keys before planning.

The shortlist is bounded by `math.perm(candidate_count, days) <= 250000`, as well
as the existing 12-candidate maximum. For seven days nine candidates fit
(181440 assignments); ten do not (604800). Automatic selection conservatively
uses at most eight (40320 assignments), after a local nine-candidate benchmark
took 43–53 seconds. This is an observed workload, not a portable latency promise.
Direct explicit inputs retain the
planner's explicit work-limit error. The shortlist is a bounded preference
ranking, not an exhaustive or globally optimal catalog search.

Known non-dinner tags and coarse culinary terms for desserts, beverages,
breakfast components, condiments and plain potato sides exclude those recipes
from dinner slots. This bounded culinary heuristic is not general meal-type
understanding; absent classifications do not establish meal completeness.
Unknown/non-scalable ingredient measures remain visible in saved menus with
`scaling_ready=false`, and products remain unresolved without invented amounts.

Planner version `weekly-menu-v3` adds exact recipe-tag matches for
`cuisine.wanted`/`flavours`, personal favorites, documented English/Norwegian
food-category matches for `diet.prioritise`, and positive leafy-green evidence
on requested ISO weekdays or English weekday names. Whole grains and potatoes
are distinct for whole-grain preference scoring. Existing time, feedback,
variety and minimum dinner/vegetable targets remain active.

Recognized plain fish ingredients with usable mass/serving evidence contribute
listed grams per serving toward the weekly fish range. Mixed products and
unsupported quantities stay unknown; these observations do not establish
nutritional compliance or allergen safety. Free-text diet patterns, plate
ratios, nutrition prose, exceptions, legume preparation and cuisine style/quality
remain named unsupported settings. Cuisine synonyms/translation and absent
ingredient evidence are not inferred. Unknown allergy/avoid evidence still
requires input.

Compact discovery uses `projection=summary`, one `source` (default `internal`),
`limit` up to 20 and the unchanged returned `next_cursor`. Local pages search
active entries first, then drafts. Full discovery remains the default for existing
clients. Full details use the exact bank ID/revision or discovery ref, never a
summary masquerading as a recipe. Offset pages can change during concurrent
catalog edits; every selected revision is re-resolved and checked before use.

MENY detail results and client-assisted conversions use a bounded disposable
SQLite metadata memo keyed by exact source ref, digest, schema and transform
version. Immutable snapshots retain their existing expiry and storage limits;
expired details are not reused. Conversion action `convert` requires
`discovery_ref`, its `recipe_digest`, `source_schema_version`, and a schema-2
`recipe`. It preserves source attribution and cannot assert new source/user
quantity evidence. Estimates remain unaccepted until the separate exact
`accept_estimates` operation. Accepted conversions remain reachable on the next
automatic request. No personal save/favorite or additional model service occurs.

The runtime test exercises real MCP 2.1.1 over stdio, Unix Server/Application,
and CLI: no-ref seven-day planning, compact paging, complete handoff save,
idempotence, restart, provider outage and unchanged personal bank/favorites.
The focused Application test runs the production MENY detail extractor against
synthetic observations of its verified page contract. Native Oda/Mathem detail
and pagination acceptance remain unavailable; their failed details do not enable
AI fallback.

## Request-scoped available ingredients

`planner_input.available_ingredients` accepts up to 32 distinct exact item names:

```json
{"available_ingredients": [
  {"item": "brokkoli", "use_first": true},
  {"item": "ris", "quantity": 500, "unit": "g"}
]}
```

Quantities use the shared exact quantity format (integers, supported decimal
inputs or numerator/denominator objects). Omitted quantities or unsupported
units remain unknown. Only the current user's stock assertion belongs here;
this is not a stock database, freshness check or dietary assurance.

Within the existing six-page-per-source budget, the first stock query is followed
by an ordinary source query before additional names or continuation pages. These
first two queries receive a turn before early completion. Derived search queries
are bounded to 200 characters; full ingredient identities remain unchanged. Scoring uses exact case-insensitive, Unicode-
normalized names from loaded non-optional ingredients. No translation,
substitution, summary-derived ingredient or complete-coverage inference occurs.
A match adds three preference points per meal, six for `use_first`, capped at
18; hard restrictions and source deduplication still precede this ranking.
Without stock input, selection and calculation are unchanged.

The request is bound into the existing planner digest and frozen in the saved
menu. Product preparation aggregates compatible needs across all dishes, then
subtracts compatible quantified stock once per exact ingredient/unit identity.
Two 400 g rice meals and 500 g on hand therefore leave 300 g before package
rounding. Unknown quantity, incompatible unit or a distinct ingredient name
leaves the purchase unchanged. The returned requirements show gross quantity
and confirmed pantry quantity; the plan retains the original stock assertions.

Later source-position `ingredient_decisions` replace the request stock for that
entire ingredient, including `include` as an explicit buy choice. They never
add a second pool to the planning input. Allocate a newly confirmed total once
across those positions. Repeated preparation is deterministic and has no stock
side effects. Replanning takes the new request's stock assertion for its whole
remaining menu, including carried future meals; omission does not reuse old
stock after possible cooking. The earlier menu/history stays unchanged.
