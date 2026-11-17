# Bounded recipe selection

Ordinary plans accept `planning_mode="ad_hoc"` and exact dates or
`period={"start_date":"2026-10-04","end_date":"2026-10-06"}`. They may cross
calendar weeks and include up to seven eating dates. Without an explicit week,
ad-hoc is the default; explicit weekly requests preserve calendar behavior.
The saved scope retains those dates and relative batch sessions. Short plans
do not automatically inherit full-week numeric quotas; relative day-number plans
shorter than the household's usual dinner count follow the same policy. Explicit strict targets
remain enforced and saved dietary preferences still guide the agent.

For ordinary open-ended planning, the host model compares a bounded candidate
page from the enabled saved bank and selected store, reusing current search results.
It reads exact full recipes before choosing. Disabled/unavailable sources and
explicit user recipe choices do not require extra searches. Dietary fit, practical
suitability and variety take priority; source diversity is a tie-breaker, not a
quota. A suitable plan may therefore use recipes from a single source.
[Source preferences](#source-preferences) decide between sources when recipes
fit comparably.
The host applies the actual saved dietary text; free-text guidelines are not
automatically enforced by the ranking score or recipe-readiness checks.
Send `selection_mode="agent"`, chronological dates and ordered exact references
to menu `plan`; this retains the model's order through the existing save handoff.
Use one recipe per cooking date, with leftover dates supplied by accepted batch
settings. Numeric goals are assessed but advisory unless named in `strict_targets`.
Dietary hard rules, cooldown, source binding and readiness remain enforced.
The automatic collection/ranking behavior below remains available in `ranked`
mode and to clients that omit `selection_mode`.

For a complete ranked period, saved positive numeric minima and the saved
leafy-green dinner range are checked before returning a save reference. Missing
evidence returns `needs_input`; a known infeasible set returns `no_plan`. Saving
the unchanged result uses the same minimum policy.

`recipe_selection.py` supplies the compact projection, source-family grouping,
context queries, bounded retrieval and shortlist used by automatic selection.
Application owns source reads, trusted normalization, immutable references and
provider eligibility. Menu `plan` accepts `planner_input={week, dates?, portions?}`
without candidate references. Application collects the enabled local bank and
selected retailer, loads full details, shortlists and runs the planner. Installed
user/imported/bundled/collection entries participate through `internal`, independently of
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
continuation remains a search limitation. Oda/Mathem use verified MCP
`page`/`size`/`hasMore` pagination and exact public recipe pages for details.
Missing URLs, incomplete responses or unresolved details remain limitations;
they do not establish exhaustion or eligibility for AI generation. Native
recipe cart expansion remains unsupported.

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
families. Exact versions and local edits survive. Ready versions come first within a family. Source priority and then saved status decide between equally usable versions.
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

Ranked planning draws only from active bank entries and active exact revisions.
Its bank window rotates within each source tier based on the planning dates and
saved menu history. Identical inputs reproduce the same proposal. When all tiers
have entries, each retrieval cycle examines three preferred, two normal and one
occasional entries. Enabled sources remain candidates under every preference.
Eligibility and dietary constraints apply before ranking. Equal scores use
source priority, then saved status, then exact reference. An unsaved v6 proposal
must be regenerated; saved menus retain their snapshots and assessments.

A retained original from one explicitly bound retailer with no secondary
attribution can be excluded from a different retailer's original-only family.
Missing references, user records without attribution, and cross-publisher
aliases keep coverage partial. The work-limit outcome applies to every family.

The shortlist is bounded by `math.perm(candidate_count, days) <= 250000`, as well
as the existing 12-candidate maximum. For seven days nine candidates fit
(181440 assignments); ten do not (604800). Automatic selection conservatively
uses at most eight (40320 assignments). Direct explicit inputs retain the
planner's explicit work-limit error. The shortlist is a bounded preference
ranking, not an exhaustive or globally optimal catalog search.

Canonical recipe categories guide dinner selection. `breakfast` plus `dinner`
can serve as dinner; desserts, drinks and condiments remain excluded even if
also labeled dinner. A meal-role classification without dinner, or bread alone,
is excluded. `baking` alone does not establish meal suitability, so the existing
tag/name checks still apply. With no usable category, known non-dinner tags and
coarse culinary terms for desserts, beverages, breakfast components, condiments
and plain potato sides remain the fallback. Absent classifications do not
establish meal completeness. Explicitly requested other meals use menu
`add_slot`, which carries their own date, meal type and portions.
Unknown/non-scalable ingredient measures remain visible in saved menus with
`scaling_ready=false`, and products remain unresolved without invented amounts.

Planner version `weekly-menu-v7` retains attributed leafy-green assessment to
category-aware dinner selection and retains exact recipe-tag matches for
`cuisine.wanted`/`flavours`, personal favorites, documented English/Norwegian
food-category matches for `diet.prioritise`, and a weekly range in
`diet.leafy_green_days`. `[4,5]` means four or five dinners on any days of
the week; the days can vary between menus. The agent judges whether actual
leafy greens form a meaningful part of dinner. Herbs, small garnishes and the
total weight of mixed products such as spinach pasta do not establish a leafy
serving. Whole grains and potatoes
are distinct for whole-grain preference scoring. Existing time, feedback,
variety and dinner/vegetable targets remain assessed; agent-mode numerical
goals are advisory unless explicitly strict.

Supply that judgment through the existing candidate facts, for example:

```json
{"leafy_green":{"source":"explicit","assessment":"substantial","ingredient_indices":[0],"basis":"The tatsoi in ingredient 0 is a vegetable component of the dinner, not a garnish."}}
```

`assessment` is `substantial`, `does_not_count` or `unknown`. Indices are
zero-based positions in the exact candidate recipe. A positive assessment needs
actual required, non-pantry ingredient lines with supported positive mass
quantities. The service calculates their listed amount per person; unsupported
units remain unknown rather than receiving an invented conversion. Ingredient
identity and culinary relevance are attributed to the agent, while the quantity
calculation is derived from the recipe. A rationale without those ingredient
references and usable quantities cannot establish a positive strict result.

The assessment travels with the existing frozen menu recipe/slot and is bound to
its content. Relevant edits require a fresh assessment; stale evidence must not
silently become a positive fallback result. Ordinary numeric goals remain
advisory in agent mode. An explicit strict leafy-day target uses the assessed
count and still rejects an unresolved result; it does not certify nutrition or
allergen safety.

Recipes without an agent assessment use a conservative, attributed heuristic.
Its existing 25 g per-person threshold is a fallback, not a universal definition
of a substantial serving or a veto on an explicit culinary assessment. Ambiguous
mixed ingredients and unrecognized possible greens cannot be credited with their
whole weight. Weekly evaluation tracks the interval from confirmed qualifying
dinners to confirmed plus unresolved dinners: it passes only when that entire
interval is inside the requested range.

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

Full discovery filters local cooldown/provider eligibility while scanning up to
200 ordered bank rows or eight seconds, rather than filtering only the first
source share. The bank can fill slots unused by other sources, up to the requested
total limit. Preferred/occasional group ordering is preserved; draft previews
remain visibly marked as drafts. The internal source reports `examined` and
`exhausted`, and `search_limit` when a bounded scan found no eligible results.
An empty bounded page is not a claim that the whole bank is empty.
Return its `next_cursor` unchanged with the same query/week to continue the local
bank only; optional source searches are not repeated. The cursor is bound to the
provider, group preferences and cooldown policy. Start a new search after changing
those settings or editing the bank, whose live offset order can change.

MENY detail results and client-assisted conversions use a bounded disposable
SQLite metadata memo keyed by exact source ref, digest, schema and transform
version. Immutable snapshots retain their existing expiry and storage limits;
expired details are not reused. Conversion action `convert` requires
`discovery_ref`, its `recipe_digest`, `source_schema_version`, and a schema-2
`recipe`. It preserves source attribution and cannot assert new source/user
quantity evidence. Usable estimates remain visibly estimated and need no separate approval for
planning; optional personal acceptance uses the exact `accept_estimates` operation. Accepted conversions remain reachable on the next
automatic request. No personal save/favorite or additional model service occurs.

## Source preferences

A household can mark a recipe source as `preferred` or `occasional`; every
other source is normal. The setting is `profile.recipes.preferred_sources` and
`occasional_sources`, two lists of source groups:

| Group | Recipes |
|---|---|
| `own` | The household's own saved recipes in the bank. |
| `collection:<pack_id>` | One installed collection in the bank. |
| `store` | The selected store's recipes. |
| `web` | Recipes found through recipe-website discovery. |
| `themealdb`, `wikibooks` | The two open English-language databases. |

A group can be in one list only, and the two lists hold at most 16 groups in
total. Preferences do not switch anything on or off; `recipes.sources` and
`recipes.web_search` still do that, and the household's own recipes and its
collections share the bank's one switch (`internal`). Setup `show`/`apply`
and profile `overview` return `recipe_source_overview`: one row per source
with `id`, `label`, `enabled`, `preference`, the bank groups' recipe counts
(null when the bank cannot be read) and the enabled websites. Change preferences with setup `apply` and
`changes.recipe_source_preferences={preferred:[...],occasional:[...]}`; a
supplied list replaces the saved one and a source moved to one level leaves
the other.

Where preferences apply:

- Bank pages (`projection=summary`, `source=internal`, and the automatic
  collector) list preferred groups first and occasional groups last, then by
  most recently updated as before. With no preference saved the order is
  unchanged.
- Candidate summaries carry `source_group`, and `source_preference` when it is
  not normal. Discovery results repeat the saved lists as `source_preferences`
  so the host model can follow them in `agent` mode.
- In `ranked` mode a preferred source adds 4 points per meal and an occasional
  source subtracts 4. This is a nudge between comparable dishes, not a filter:
  a dietary deviation (−30) outweighs it, and preferred against occasional
  (8 points) stays below the 10 points that separate a dish above the
  cooking-time maximum from one merely outside the weekday target. A recipe
  from an occasional source that fits the evening better can still win.
- Full cross-source discovery asks every enabled source for the same share and
  lists preferred sources first and occasional sources last, so the occasional
  ones are what the overall limit cuts.

The shortlist no longer drops every saved recipe in favour of store or web
discoveries when scores are equal. Preferences never override hard dietary
rules, cooldown, source binding or an explicit recipe choice.

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
## Supplemental web discovery

The host can add at most eight assessed full-recipe discovery references using
`planner_input.web_candidates`, with a current `web_search_result` from the
configured search scopes. Omit `candidates`: the normal collector evaluates
these alongside internal and selected-store recipes before source-identity
deduplication and bounded shortlisting. Exact candidates remain an explicit,
separate user-selected scope. Web failures are visible in `discovery.sources`;
missing search does not block local/store results or authorize AI fallback.
The resulting save reference contains only the frozen combined candidates,
not a request to repeat web search. See [import reference](recipe-import-reference.md)
for settings and storage decisions.

## Household cooking experience

Recipe search, discovery and detail return a compact `household_experience` when
there are explicit reports for that exact version. It includes the number of
reports and the most recent reported values, date and original portions where
known. Planner responses preserve this evidence. Reports do not replace source
times, rewrite preferences or automatically resize meals. Undo/reset removes
reports from future selection. A new recipe revision or adaptation does not
inherit reports from a merely similar title or source.

For a request that includes shopping, screen a small number of costly or unusual
ingredients before settling the meals. For a budget or complete estimate, use the
existing unsaved product preview. Revise material fit or package-cost problems;
missing store evidence stays explicitly unverified. A plan-only request needs no
product search, and a preview never authorizes a cart change.
