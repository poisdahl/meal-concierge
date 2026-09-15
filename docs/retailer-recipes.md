# Retailer recipe details

Recipe discovery uses the selected store's search and exact recipe pages.
The shared private-recipe boundary preserves source attribution, scales supported
quantities and enforces store eligibility. Discovery creates a frozen technical
snapshot; saving a personal recipe or favorite is a separate action.

## Search and detail support

| Store | Search | Recipe details | Shopping |
|---|---|---|---|
| Oda | Authenticated MCP, bounded pages | Exact public recipe page | Shared scaling and ordinary product matching |
| Mathem | Authenticated MCP, bounded pages | Exact public recipe page | Shared scaling and ordinary product matching |
| MENY | Logged-in browser, bounded visible results | Exact recipe page in that browser | Shared scaling and ordinary product matching |

The integrations do not use native bulk recipe-cart additions. Oda and Mathem's
`manipulate_cart` schema accepts `recipeId` and `fromRecipePortions`, but it does
not expose an exact product-expansion preview. A native write capability cannot
establish approved product identities and quantities before dispatch. Shopping
therefore uses explicit product plans and their normal cart reconciliation.

## Oda and Mathem

Compact discovery consumes `page` (1–50), `size` (1–20), and boolean `hasMore`.
Continuations bind provider, query and page size. Only explicit `hasMore: false`
establishes query exhaustion. Missing/malformed metadata, nullable recipe URLs,
oversized pages and the page-50 bound remain incomplete results. They cannot
authorize automatic AI generation. Automatic selection retains its six-page,
80-detail and 30-second source-search budgets.

Details use the bounded unauthenticated HTTPS reader with public DNS checks,
TLS verification and no redirects. No credentials, embedded links, images or
contexts are fetched. The exact provider host, locale, numeric recipe ID and
searched title must agree, and the page must contain one structured Recipe.
Numeric `recipeYield` supplies base portions; other shapes fail explicitly.

Swedish `st`, `tsk`, `msk` and `krm` map to count, 5 ml, 15 ml and 1 ml, while
preserving original wording. Other ingredient text uses the shared source
parser. Cloves, handfuls and unsupported amounts stay unresolved rather than
becoming invented product counts.

The two stores retain separate credentials, identities and locks. MCP login
is separate from website login. An unavailable or failed store read remains a
source limitation; it does not establish that no suitable recipes exist.

## MENY

`recipe_search` accepts `query` and `size` (1–20). It returns
`{provider: "meny", query, recipes: [...]}` from a bounded prefix of currently
visible, identity-checked cards. It has no `page`, total, next cursor or
exhaustion flag. A short result cannot establish source exhaustion, and an empty
query does not establish that other useful queries have no results.

`recipe_detail` takes only the exact `recipe_id` path returned by search. It
uses the dedicated browser, login checks, operation lock and deadline. It never
presses a recipe shopping control. The requested page, its sole canonical URL
and its sole JSON-LD Recipe URL must agree. Embedded context, author and image
URLs are not fetched, and structured text is never executed.

The result contains `provider: "meny"`, the base culinary `recipe` and separate
`capabilities`. `recipeIngredient` and `recipeInstructions` are string arrays;
only the explicit `Antall personer: N` yield format supplies person servings.
Other yield text stays preserved with unresolved servings. Supported ingredient
measures become exact quantities; residual text stays unresolved. The shared
scaling path adjusts base quantities once; it does not use website portion
controls. `totalTime` is not promoted to exact cooking-time evidence because
the displayed time can be a range.

## Shared limits

Native shopping hints are not household approval or product evidence. No reader
infers dietary compliance, pantry stock or package selection from a recipe page.
Missing details retain their source link and explicit limitations. Private
storage preserves original store content without granting redistribution rights
or permitting its use with another selected store.
