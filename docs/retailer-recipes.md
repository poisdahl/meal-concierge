# Retailer recipe details

The MENY adapter has a bounded `recipe_detail` read taking only the exact
`recipe_id` path returned by recipe search. It uses the same dedicated browser,
login checks, operation lock and deadline as other MENY reads. It never presses
a recipe shopping control. The requested page, its sole canonical URL and its
sole JSON-LD Recipe URL must agree. Embedded context, author and image URLs are
not fetched, and structured text is never executed.

The result contains `provider: "meny"`, the base culinary `recipe`, and separate
`capabilities`. The reader derives `source_provider` and identity from its fixed
MENY context. Original source text stays original; private storage does not
grant redistribution rights. The shared private-recipe boundary decodes full
original recipes and enforces provider eligibility together. It does not relabel
a recipe as adapted to bypass that boundary. Application discovery persists the
selected version without creating a personal bank entry or favorite.

Existing MENY `recipe_search` consumes `query` and `size` (1–20); it does not
implement `page` or continuation. It returns
`{provider: "meny", query, recipes: [...]}` from a bounded prefix of currently
visible, identity-checked cards. It exposes no total, next cursor or exhaustion
flag. A short batch cannot establish that the source is exhausted; even a
confirmed empty query is not proof that other useful queries have no results.
Bounded refill must preserve this limitation rather than inventing pagination.

## Observed contract and remaining acceptance

| Provider | Detail evidence | Native portions, associations and bulk recipe cart | Fallback |
|---|---|---|---|
| MENY | Authenticated adapter probe/search/detail and Application source-bound detail passed in Bob on 2026-09-07; a repeat search timed out rendering | No integration contract established; adapter reports unsupported | Base ingredients, shared exact scaling and ordinary product matching through the integrated private boundary |
| Oda | Authenticated MCP 1.1.0 discovery and recipe search; two returned public pages passed Application on 2026-09-07. Subsequent probe/search returned internal error -32603 | `manipulate_cart` accepts `recipeId` and `fromRecipePortions`, but no exact product-expansion preview tool is exposed | Exact public page, shared scaling, product matching and journalled product deltas; unresolved details retain a source link |
| Mathem | Authenticated MCP 1.1.0 search with integer IDs and exact links; two corresponding public recipe pages verified through Application on 2026-09-07 | `manipulate_cart` accepts `recipeId` and `fromRecipePortions`, but no exact product-expansion preview tool is exposed | Exact public page, shared scaling, product matching and journalled product deltas; unresolved details retain a source link |

Both MCP discoveries returned 25 tools through the existing authenticated
provider client and its ordinary provider lock. Native recipe addition is an
observed write capability, **not a safe approved-plan expansion**: its schema
cannot establish the product identities and quantities before dispatch. It is
therefore not used by recipe planning or cart application. Separate provider
credentials and household locks remain in place. The successful Mathem read
must not be represented as a successful Oda recipe response.

Oda/Mathem compact discovery consumes their observed `page` (1–50), `size`
(1–20), and boolean `hasMore` contract. Continuations bind provider, query and
page size. Only explicit `hasMore: false` establishes query exhaustion; missing
or malformed metadata, unrepresentable rows (including nullable recipe URLs),
oversized pages, and the page-50 bound do not. These incomplete results cannot
authorize automatic AI generation. Search metadata alone does not make a
link-only result menu-ready; details must resolve successfully. Automatic
selection retains its six-page, 80-detail and 30-second source-search budgets.

Oda/Mathem details reuse the existing unauthenticated HTTPS reader with pinned
public DNS, TLS verification, size/time limits and no redirects. No credentials,
embedded links, images or contexts are fetched. The exact provider host, locale,
numeric recipe ID in the URL and searched title must agree. One structured
Recipe is required. Observed numeric `recipeYield` supplies base portions on
these retailer pages; other shapes fail explicitly. Swedish `st`, `tsk`, `msk`
and `krm` are interpreted as count, 5 ml, 15 ml and 1 ml respectively while
preserving source wording. Cloves, handfuls and other unresolved amounts remain
unknown; they are never guessed into whole-product counts.

The observed pages include [Mathem 2713](https://www.mathem.se/se/recipes/2713-mari-bergman-pasta-allamatriciana/),
[Mathem 6953](https://www.mathem.se/se/recipes/6953-samarbete-pasta-amatriciana/)
and [Oda 5050](https://oda.com/no/recipes/5050-silje-feiring-kremet-pasta-med-sopp/).
All expose four base portions. The Application read verified exact private
source binding, scaling to two portions, replay from the same cached snapshot
and zero personal saves. Mathem used links from a fresh authenticated search.
The initial Oda detail used a known public URL. A later authenticated Oda
search returned 3330 (creamy salmon pasta) and 4122 (pasta al limone); both
corresponding exact public pages passed the same Application checks, with four
base portions and respectively four/11 ingredients and three/four steps.
Subsequent Oda probe/search calls returned MCP internal error -32603. The
successful search establishes the response contract; it does not establish
continuous service availability or explain the provider error. Website login
is separate from MCP OAuth, and no additional Oda website login was used.
This is public-page detail acceptance, not authenticated website or purchase
acceptance.
The Swedish measures agree with [Mathem's measuring-set specification](https://www.mathem.se/se/products/7170-gastromax-mattsats/).
Tests retain only invented text with these observed shapes.

The exact [MENY reference page](https://meny.no/oppskrifter/pasta/hjemmelaget-lasagne)
returned one `application/ld+json` Recipe object with `recipeIngredient` and
`recipeInstructions` as arrays of strings, an exact `url`, `inLanguage`, author
metadata, `dateModified`, and `recipeYield: "Antall personer: 4"`. Tests use
invented recipe text in these observed shapes. No store recipe collection or
private account/basket response is included. This public HTTP observation is
not evidence of an authenticated browser read. A separate 2026-09-07 run
after owner login exercised the actual MENY adapter through Bob's existing
reserved browser wrapper: authenticated probe, two search hits and detail from
this reference page (four portions, 16 ingredients, six steps) passed.
The searched lasagne with salsiccia also passed Application detail, exact source
binding, scaling from four portions to two, cache replay and zero personal saves
(17 ingredients, nine steps). Only command transport was routed through the
wrapper; adapter login checks, lock, deadlines and parsing were unchanged.
A second search timed out waiting for rendering; detail acceptance then reused
the prior successful search result. No recipe or cart control was submitted.

For MENY, only the observed explicit person-count format supplies person servings.
Other yield text remains preserved with unresolved servings. Ingredient text
uses the shared source parser: supported explicit measures become exact
quantities; residual text stays unresolved. Base quantities are never adjusted
by a website control, so the shared scaling path runs once. The page's
`totalTime` is not promoted to exact cooking-time evidence: the observed visible
time was a range. No dietary, pantry or product facts are inferred.

[MENY's shopping guide](https://meny.no/kundefordeler/handle-oppskrifter) describes
personalized suggestions, replacements and pantry exclusions. It does not
establish an adapter contract or household approval. Native hints must acquire
their own truthful observation scope and current product facts before use;
they cannot masquerade as generic search observations. Until proven, purchasing
uses existing explicit product plans and their cart reconciliation. No bulk
recipe addition, retry or alternate checkout path is introduced here.

The actual Application detail-to-private-snapshot, exact scaling and approved
product-plan path has passed with synthetic observed-contract responses.
A synthetic seven-day Application flow now covers all three selected providers:
35 ingredient occurrences aggregate into five searches per preparation. Seven
rice quantities total 700 g; one explicit 250 g pantry amount leaves 450 g and
one 500 g pack. The four other shared ingredients require eight packs together.
Unavailable candidates are excluded; exact approved candidates produce nine
packs while preserving two existing manual cart items. Restart/replay does not
dispatch another cart mutation. Existing cart tests cover intervening manual
changes, partial writes and reconciliation. These are controlled provider
fixtures, not authenticated purchases or a measured native-hint speedup.

The authenticated MENY detail gate is demonstrated. Intermittent MENY
rendering and Oda service failures are not proof that recipe sources are
exhausted; existing bounded failure handling remains in place. Oda/Mathem synthetic acceptance may replace an
unavailable service only where an actual contract is known; it cannot invent
missing detail or native operation schemas. No speedup has been measured.
