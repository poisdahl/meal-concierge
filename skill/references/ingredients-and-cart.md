## Ingredients, packages and cart

Products `prepare` is read-only and requires the exact menu reference or complete
planner handoff. Select observed exact interchangeable `candidate_refs` using the
user's meal and grocery request; routine equivalent package choices do not need
separate user approval. Show the useful product/quantity/cost overview before
ordering. A search hit is not proof of ingredient equivalence. When an Oda
recipe exposes a direct ingredient-product association, treat that exact product
ID as the strongest search evidence, but still verify its current availability,
package, quantity and price; the source link alone proves none of those facts.
Oda and MENY search the complete Norwegian item identity. Mathem uses only reviewed exact
Swedish whole-identity mappings; never translate isolated words or erase
meaningful properties. A missing or uncertain localization remains a product-plan
review, not a recipe SKU or guessed rewrite. If returned hits are irrelevant,
pass a concise localized `search_query` with that requirement's candidate selection and prepare again;
only references returned by that exact search can be selected. Exclude pet food
and other nonfood hits. Canned/cooked versus dry ingredients require compatible
quantities and cooking instructions; never replace dry beans with canned beans
while retaining a pressure-cooking method. Raw quantities, incompatible units,
unknown availability and eligibility remain unresolved. Use returned
`candidate_diagnostics` to explain the actual blocker: unreadable package size,
incompatible units, unknown pant or an observed package limit. Estimate pricing
does not convert ml to g or pieces to weight. A conversion needs an observed
basis; a product's declared piece count is such a basis, a guessed piece weight
is not. Never mark ingredients as already at home to hide unresolved coverage. Plain
cooking water stays in the recipe but is excluded from shopping by default;
explicit `include` can request it, and named bottled/mineral water is distinct.
When the selected title merely omits a compatible source qualifier such as
fresh, frozen, canned, dried or preparation wording, the exact candidate may be
used without a separate question only when the remaining base identity matches
and there is no contradiction. If the current user explicitly accepts a nearby
dairy-fat variant or wants to bind such an omission, include that exact ref in
`semantic_authorization` with `authorized_by: current_user` and their concise
reason. Never use either path for another identity or species, an explicit form/state
contradiction, extra title ingredients or flavors, or any allergy, sensitivity
or never-buy conflict. Only ordinary package/organic metadata may remain around
the exact base identity. A literal title allergen is positive evidence and must
block. The returned digest binds the authority; use the unchanged compact apply
arguments.

When one observed physical package covers several requirements, repeat one
identical `shared_package` object in every member's candidate approval. It must
list all member requirement IDs, one package count, a concise quantity basis and
`authorized_by: current_user`; every member selects the same sole ref. Review the
combined need. Treat the group atomically and use the returned apply arguments,
which count that SKU, quantity and cost exactly once even when an offer exists.
For a normal culinary package decision where a source tsp/count requirement
cannot be converted exactly to the retailer's grams, or drained content differs
from the package's net weight, keep the source amount and
select one observed candidate with `package_count` and a concise `quantity_basis`
in its `candidate_approvals` entry. This follows the existing grocery request;
do not add another approval just for a sensible spice jar or produce pack.
Use the complete required amount when estimating enough packages. The same
products prepare/apply path records `coverage_status=practical_estimate`,
rechecks observed price/availability and includes recurring goods. Explain the
estimate briefly; never invent gram/ml equality, stock, or dry/cooked equivalence.
Missing numeric package metadata does not block a deliberate count of observed
retailer units; keep its size and exact coverage unknown.
For Oda/Mathem products explicitly labelled with an expected or minimum variable
weight, `price_mode=estimate` may use the declared weight to calculate a package
count. Keep `coverage_status` and merchandise price visibly estimated, leave the
payable total unknown, and let checkout remain the final price authority.
Observed package limits bound this selection; they do not establish remaining customer eligibility
after prior purchases or account for separate cart extras.

Unconfirmed pantry goods remain on the shopping list; pantry flags do not justify
claiming the user owns them. Avoid stopping the flow for each spice or optional
garnish. Follow a clear request to omit optional ingredients; source text such as
"(optional)" is preserved. Ask one combined stock question only when useful to
the requested shop, and continue independent planning. Pass `ingredient_decisions`
with the returned source position `{collection,recipe_index,ingredient_index}`:
`include`, `omit` for optional ingredients only, `have_all`, or `have_quantity`
with exact quantity/unit. Pantry flags never prove stock. Quantities describe
stock allocated to that specific recipe requirement; do not allocate the same
stock twice. The plan exposes gross need, confirmed allocation, net need,
package count and surplus. Existing provider-cart goods are not pantry stock.

When the user names a menu ingredient they already have, acknowledge that it
will be used from home first. Persist that explicit assertion immediately with
products `record_ingredients`, the current menu_ref and have_all (or their stated
have_quantity) for its exact ingredient sources, even if no product is in the
cart yet. Do not claim it was recorded after merely reading the cart. Subsequent
product preparation reuses it for that exact menu revision. Change a recorded
assertion through record_ingredients; old preparation arguments cannot override it. Rebind an unchanged
one-shop stock assertion to the new exact sources if the menu is revised; never
turn it into permanent unlimited inventory. For an authorized shop, reprepare
and apply to remove now-unneeded menu purchases. Usually answer “Da bruker vi
fullkornsspaghettien du har hjemme og kjøper ikke mer denne gangen.” Do not lead
with “nothing to remove” or an unchanged total package count; those details do
not explain the user's result.

Product preparation defaults to `price_mode=estimate` for practical planning.
Use `price_mode=exact` when a known payable product total is required. `estimate` can use one
explicitly approved available regular-price package despite unknown pant; show
its merchandise estimate and unknown total separately. Never claim it is the
cheapest or a confirmed total. `budget_ore` limits known product costs; unknown
pant keeps budget verification incomplete, and delivery/cart fees are excluded.
The provider's checkout summary is the final price authority. Never derive an
absent fee from totals or turn a from-price, member/coupon uncertainty or variable
weight into an exact price. Preserve every returned fee label.

Products `lowest_cost` compares at most three exact alternatives within returned
search scopes, only when all totals and approved matches are complete. Preserve
the selected save handoff and original non-price reasons. It never claims global
cheapest or locks prices. Later prepare may take `previous_product_plan` to show
observation drift. Comparison and candidate approval do not authorize cart edits.

Apply only for an authorized cart update: send the returned compact
`apply_arguments` unchanged and add `cart_change_requested=true`. The complete
unchanged product plan/digest also remains supported. The compact route
regenerates the exact plan and requires the reviewed digest. Drift requires a new review;
never silently substitute another plan. All-at-home completion retains explicit extras and removes earlier menu purchases.
Unattributed existing cart contents still require reconciliation.

When preparation is incomplete but has reviewed selected lines, it can also
return `partial_apply_arguments`. For the same authorized cart update, send those
arguments unchanged with `cart_change_requested=true` to sync only the selected
lines. This path rereads those selections and every earlier accumulated partial
selection, is idempotent, does not add
recurring goods, and records no complete product-plan digest. Continue preparing
the remaining lines and finish with one full apply. Checkout stays blocked while
the partial marker exists; never use raw cart changes to bypass it.

For a large incomplete plan, an MCP `issues_only` projection preserves every
requirement and blocker while omitting verbose sources, product observations and
selections. Use its bounded exact candidate refs and diagnostic codes to correct
or accumulate `candidate_approvals`, choose `price_mode=estimate` only where the
returned package/price facts support it, and then prepare the entire same menu
again with the unchanged binding. Do not reduce the menu scope: products prepare
has no requirement-subset control. Full and partial apply arguments, when
returned, remain unchanged. A partial continuation includes every unresolved
issue in `remaining_issues` beside the exact arguments under the
`partial_apply_arguments_with_issues` projection; do not discard either part.
When a compact observation reports `omitted_products`, more provider-ranked
options exist outside that projection. Use another returned candidate or rerun
prepare with an exact localized `search_query`; never infer that the first shown
candidate was the only or best option.

If products apply stops for cart or menu drift, reconcile that exact state and
then rerun products prepare/apply. Never work around the stop with raw cart
ensure/change, a scratch script, or a hand-copied product list. In particular,
the selection's package count is the menu requirement, not an increment to add
to the package already in the cart. Raw additions made for menu ingredients do
not carry menu ownership and can survive a later dish replacement as apparent
household extras, causing duplicate or obsolete products.

Raw cart sync/reconcile always requires the exact current
`menu_ref={menu_id,revision,digest}`. Supply complete product requirements, not
raw deltas. Same-SKU starting quantities count toward need; only exact goods the
owner explicitly marks extra use starting+required quantities. Different brands
and packages remain different IDs. MENY shares one household browser: perform
provider-facing calls sequentially, including recipe discovery.

Genuine outside cart drift returns one digest-bound question with extras, shortages and starting
goods; a verified menu replacement is synchronized automatically. Suggest keep_current but require an explicit answer; silence is not one.
Reconcile with the exact returned digest and current menu ref. Exclude only
named product IDs, restore missing quantities, or explicitly accept named
shortfalls. Reread after changed state. Scheduled work stops for unresolved cart
questions. It cannot infer the suggested answer.

Use `meal_concierge_product_favorites` for product favorites; top-level
product_id/product_name come unchanged from search. Recurring adds use the same
product fields and exact weeks/months interval; the service persists its anchor.
Never route “favorite this recipe” to the product tool.
