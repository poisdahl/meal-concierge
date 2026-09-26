# Recurring batches and dietary findings

The ordinary `menu plan` path uses accepted structured household settings. For
seven dinners for two, two cooking sessions can supply six portions on Monday
and eight on Thursday. Set these through the existing profile tool only after
the household accepts them:

```json
{"meals":{"meal_mode":"batch","dinner_days":7,"dishes":2,"batch_dishes":2,"portions":2,"prepared_portion_range":[6,8],"cook_days":["Monday","Thursday"],"eat_days":["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],"recurring_batch_accepted":true}}
```

`portions` retains its original meaning: consumption at each meal. Prepared
quantity is separate. `fresh` preserves the existing different-dinner default;
`mixed` assigns batches to cooking sessions with dependent eating days. Cooking
days must match the explicit dish count. A conflicting request-level portion
count is rejected, never silently applied to only part of the calculation.
Existing free text, exclusions and numbers are preserved on upgrade. Recurring
settings do not grant shopping or payment authority.

The proposal shows every eating day and its source before saving. Save uses the
unchanged handoff/reference. Each source is stored and purchased once at its
prepared quantity; leftovers add no shopping or recipe-use events. The same
accepted settings work in subsequent weeks without another batch confirmation.
A preferred three/four-portion preparation range does not cap seven two-portion
meals: the plan prepares six/eight portions with no shortfall warning or extra
approval. Consumption and dates remain unchanged, as does the saved preference.
The actual cooking amounts remain visible in the plan, recipes and shopping.

Optional candidate `facts.batch_guidance` carries `basis`, `suitability`
(`suitable`, `unsuitable`, `unknown`), `storage` and `reheating`. The host model
assesses the exact recipe and serving dates and authors practical cooling,
storage and reheating guidance. This is an agent assessment, not a source fact
or a user report. Missing guidance
remains visibly unknown. A household freezer preference establishes no safe
storage life. Planned leftovers remain separate from reported cooking and
remaining food. Marking a source cooked requires actual prepared/consumed
portions. Dependent consumption needs a matching cooked source and sufficient
remaining portions. Existing exact-plan `batch_prepare`/`batch_apply` remains
available for explicit manual arrangements, including multiple disjoint sources.

## Dietary rules and retail evidence

New `diet.rules` entries specify an ingredient/allergen `term` and a `kind`:
`allergy`, `sensitivity`, `preference`, `never_buy`, or ambiguous
`allergy_or_sensitivity`. Do not infer diagnoses. Existing
`allergies_or_sensitivities` strings stay ambiguous; existing `avoid` strings
remain ordinary preferences. Do not delete or soften a rule merely to proceed.

Missing generic safety metadata no longer rejects every recipe. Recipes expose
item-specific unknowns and deviations and can proceed to product selection.
Known allergy/never-buy conflicts require alternatives. Product selection prefers
compatible approved candidates before comparing their costs; it preserves
unresolved requirements when no compatible approved product exists. Existing
candidate authority, pantry decisions and partial-order scope still apply.
Required goods are never silently omitted.

An explicitly plant-qualified cream/sour-cream phrase, such as "plantebasert
fløte", is not itself positive evidence of dairy cream. The original retail
text remains intact; unqualified dairy ingredients, allergens and traces still
count, and a negated plant claim grants no exception. Plant wording establishes
neither allergen absence nor nutritional superiority.

Oda's and Mathem's exact numeric product routes resolve only to the same product's canonical
public URL. The anonymous reader retains the visible ingredient/allergen rows,
source URL and missing-information limits.
No account, cart or payment effect is involved. Search-provided literal fields
are retained too. Missing/unavailable detail is unknown; a product name or an
absent term is not proof of allergen absence. Negated/ambiguous statements do
not become positive conflicts. Explicit retailer free-from labels are reported
as compatible labels, not universal safety certification.

MENY product detail beyond its supported search fields remains unknown. Oda
and Mathem use the same final-product assessment and exact substitution
permission rules.

## The existing final confirmation

Checkout prepare assesses the actual final product IDs, including supplemental
cart goods. The ordinary final summary includes dietary findings and their
stable `finding_id`s. Show those findings before asking for the existing final
confirmation. For unknown allergy/exclusion information, offer an alternative or
review that affected item in that same confirmation. Pass only the IDs actually
reviewed as `dietary_review` on `confirm`; this is not another approval round.
Explicit manual item review also works under an otherwise standing purchase
policy. Documented allergy/never-buy conflicts cannot be overridden.

A different product, changed retail evidence, changed rule or an older pending
summary without an assessment requires an updated summary. Scheduled context,
delivery guards, any configured optional amount ceiling, cart reconciliation and protected payment journal remain
bound to the same operation. No warning authorizes an unrelated purchase.

## Standing uncertainty permission and native notices

Automatic handling needs an explicitly accepted entry in
`diet.uncertainty_permissions` for each material finding, for example:

```json
{"kind":"sensitivity","term":"onion","product_ref":"10","condition":"unknown","accepted":true,"notify":true}
```

Supported conditions are `unknown`, `preference_deviation` and
`sensitivity_conflict`. The kind, term, exact product and condition must match.
Known allergy/never-buy conflicts are never coverable. Generic automatic-order
authority is insufficient. These entries cover uncertainty only; the existing
purchase policy, schedule ceiling, delivery constraints and platform payment
approval remain required.

Explicitly keeping an incomplete cart preserves its missing required packages
in the final checkout summary, reconciled result and any result notice. Counts
describe packages; they do not infer how many meals the remaining goods cover.
Automatic occurrences stop while required menu packages are missing. Review
the incomplete cart for an interactive checkout or restore the missing goods.

For a covered automatic attempt, `submit`, `auto` or the bound `confirm` returns
one frozen notice with `dispatch=true` before any payment dispatch. Use the
existing authorized native household messaging route to send its frozen concise
`payload.message`, retaining affected products and uncertainties without waiting
for a reply. Call `notice_result` with its `notice_token`, actual native
`send_outcome=sent|not_sent|unknown` and `sender_receipt`. As with native recipe
email, the trusted host reports the actual sender result; invented receipts or
an agent saying it sent a message are not evidence.

Resume the same `confirm`, `submit` key or `auto` occurrence after verified send.
Failed/uncertain notices do not pass the gate. Repeated calls never authorize a
second notice dispatch; reconcile an uncertain native send before changing its
outcome. If the required route is unavailable, use the existing failure path.
A definite failed send may be retried only through a newly prepared, still
undispatched checkout; never discard a possibly dispatched payment journal.

Only reconciliation establishes purchase success. It returns one result notice
with actual order status and remaining findings. Verify its native send too.
An uncertain result notice keeps the purchase result and cannot cause another
payment. Replay retrieves the same result/notice. Oda correction messages offer
additions only for a currently modifiable order; MENY editing requires current
provider review and can require another checkout and Vipps approval. Mathem
additions use the bound original order and protected checkout. Cancellation
requires a fresh available review; moving delivery requires an available free
window, unchanged goods/order total and zero additional payment. A provider
`modificationDeadline` is shown only when it is an explicit timezone-aware
timestamp. Mathem's observed textual deadline is reported separately without
inventing a year or ISO timestamp; absent evidence remains unknown.
No message promises universal removal, replacement or refund.

## Weekly shop continuity

`products apply` includes due recurring goods once; a shared product adds the
menu quantity and the recurring quantity. `cart weekly` refreshes that scope
with the exact current menu reference. Checkout `weekly=true` requires applied
menu product evidence. Completed orders fulfill only quantities actually bought;
cancellation releases that fulfillment. Original idempotency keys always recover
existing results before evaluating a new shop.

Review material dietary findings with the current confirmation and
`dietary_review_digest=summary.dietary_assessment.assessment_digest`. Legacy
finding lists remain supported. Missing information about an ordinary preference
is advisory. Known allergy and explicit never-buy conflicts remain blocking.
Legacy `avoid` values are preferences; explicit exclusions use `diet.rules`.

`meals.equipment` records available specialist appliances. Ordinary pots, pans,
oven and basic utensils are defaults; recognized required specialist equipment
must be available or have an explicit usable alternative in the recipe. Planning,
materialization, saved-menu assessment and bound checkout honor this constraint.

Explicit dates preserve recurring batch planning. A one-plan
`planner_input.prepared_portion_range` adjustment is retained in the handoff and
does not mutate the permanent profile. Optional ingredient markers in source
text are retained. The agent asks one combined stock question for relevant pantry
staples before purchase, reusing current answers and explicit household standing
instructions. Unconfirmed stock is not deducted by the engine; this is not an
instruction to buy staples before the user answers. See [the pantry check](../skill/references/meal-adjustments.md#one-pantry-check-for-the-menu). Exact candidate selection accepts an optional localized `search_query`
to recover irrelevant search results while retaining provider evidence binding.

Partial replans preserve complete frozen batch components and their original eating
portions even after household defaults change. Explicit `meal_mode=fresh` requests
fresh replacements without changing that default. Fractional or unequal batch
projections need a specific adjustment; they are never rounded silently.

## Agent-selected menus

`selection_mode="agent"` accepts one exact ordered recipe per cooking/source date
in the accepted batch layout. Leftover slots and quantities still derive from
that layout, and real cooking outcomes remain separate. Numeric saved dietary
minima are measured and displayed; only explicit `strict_targets` make them hard
in agent mode. Saved legacy/ranked menus keep their existing policy. A partial
replan retains the whole-menu policy in `planning_scope`; a full replacement can
adopt a new one. This distinction continues through product preparation and
checkout and does not relax allergies, never-buy rules or retail uncertainty.

Ordinary `diet.avoid` and `preference` rules remain advisory to the deterministic
planner, but are selection instructions for the host model. It must prefer
recipes that already fit, or proactively adapt a suitable dish before saving.
The existing recipe adaptation operation records measured replacement ingredients
and a coherent method, then product preparation checks observed store candidates.
No separate "allow plant substitutions" preference is required. If neither a
fitting recipe nor a good adaptation is available, the model explains the conflict
and asks about an alternative or an exception for that meal. It must not silently
use an avoided ingredient. An explicit meal-specific exception can apply to an
ordinary preference; it cannot override an allergy or `never_buy` rule.

Nutritional priorities come from saved household patterns, goals and preferences,
including when comparing replacement products. The host interprets named patterns
using relevant authoritative guidance and observed product information; the
service does not embed a preferred nutrient balance or food-group bonus. Avoiding
an ingredient alone does not imply a nutrient goal or a plant-based diet. A
replacement must fit the dish's heat, acidity, texture and flavor. Marketing
labels establish neither nutritional fit nor allergy safety, and missing facts
remain unknown. The menu briefly explains substitutions and the saved goals
behind nutritional choices. Narrow cream/sour-cream preferences and `never_buy`
rules do not imply a milk allergy or a ban on all dairy;
only explicit user intent changes stored rules. Numeric goals with missing
evidence stay visibly unverified rather than being silently certified.
