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
| MENY | Public page JSON-LD observed 2026-09-06; authenticated adapter acceptance pending | No integration contract established; adapter reports unsupported | Base ingredients, shared exact scaling and ordinary product matching through the integrated private boundary |
| Oda | Existing MCP recipe search returns links; public page JSON-LD is separate website evidence | No observed MCP detail or native recipe-operation contract | Link handoff; generic matching for an independently available eligible recipe |
| Mathem | Existing synthetic search integration; authenticated detail schema remains unknown | No observed Mathem detail or native recipe-operation contract | Link handoff; generic matching for an independently available eligible recipe |

The exact [MENY reference page](https://meny.no/oppskrifter/pasta/hjemmelaget-lasagne)
returned one `application/ld+json` Recipe object with `recipeIngredient` and
`recipeInstructions` as arrays of strings, an exact `url`, `inLanguage`, author
metadata, `dateModified`, and `recipeYield: "Antall personer: 4"`. Tests use
invented recipe text in these observed shapes. No store recipe collection or
private account/basket response is included. This public HTTP observation is
not evidence of an authenticated browser read.

Only the observed explicit person-count format supplies person servings.
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
Whole-week integration remains pending, so issue #43 stays open.
Authenticated MENY reads remain a separate gate requiring the coordinated
existing browser target. Oda/Mathem synthetic acceptance may replace an
unavailable service only where an actual contract is known; it cannot invent
missing detail or native operation schemas. No speedup has been measured.
