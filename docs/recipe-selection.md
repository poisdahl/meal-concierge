# Bounded recipe selection

`recipe_selection.py` supplies the compact projection, source-family grouping,
context queries, bounded retrieval and shortlist used by automatic selection.
Application owns source reads, trusted normalization, immutable references and
provider eligibility. The initial helper implementation does not itself expose
automatic discovery or change the MCP contract; that shared wiring follows the
private source-binding integration.

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
never-used ranking bonus. Application must wire this into both candidate
resolution and save-time planner revalidation; that integration remains pending.

The shortlist is bounded by `math.perm(candidate_count, days) <= 250000`, as well
as the existing 12-candidate maximum. For seven days nine candidates fit
(181440 assignments); ten do not (604800). Automatic selection conservatively
uses at most eight (40320 assignments), after a local nine-candidate benchmark
took 43–53 seconds. This is an observed workload, not a portable latency promise.
Direct explicit inputs retain the
planner's explicit work-limit error. The shortlist is a bounded preference
ranking, not an exhaustive or globally optimal catalog search.

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

Verification must include the real Application/MCP automatic request path,
pack-only and selected-provider paths after shared wiring lands. Helper fixtures
and a direct explicit-reference planner call alone do not complete that gate.
