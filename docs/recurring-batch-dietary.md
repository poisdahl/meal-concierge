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
Two four-portion batches for seven two-portion meals produce a visible six-
portion shortfall and propose six/eight prepared portions. The planner does not
save a complete-looking menu or silently increase the accepted range.

Optional candidate `facts.batch_guidance` carries `basis`, `suitability`
(`suitable`, `unsuitable`, `unknown`), `storage` and `reheating`. Copy actual
recipe-specific guidance and its attribution; never invent it. Missing guidance
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
remain exclusions. Do not delete or soften a rule merely to proceed.

Missing generic safety metadata no longer rejects every recipe. Recipes expose
item-specific unknowns and deviations and can proceed to product selection.
Known allergy/never-buy conflicts require alternatives. Product selection prefers
compatible approved candidates before comparing their costs; it preserves
unresolved requirements when no compatible approved product exists. Existing
candidate authority, pantry decisions and partial-order scope still apply.
Required goods are never silently omitted.

Oda's exact numeric product route resolves only to the same product's canonical
public URL. The anonymous reader retains the visible ingredient/allergen rows,
source URL and missing-information limits. This path was checked against the
public [Oda product detail](https://oda.com/no/products/40887-r-gulrotsuppe/).
No account, cart or payment effect is involved. Search-provided literal fields
are retained too. Missing/unavailable detail is unknown; a product name or an
absent term is not proof of allergen absence. Negated/ambiguous statements do
not become positive conflicts. Explicit retailer free-from labels are reported
as compatible labels, not universal safety certification.

MENY and Mathem detail collection beyond their current search fields remains
unverified and therefore unknown. Mathem's protected checkout and full dietary
integration remain [issue #50](https://github.com/poisdahl/meal-concierge/issues/50).
This change does not enable automatic Mathem payment or editing.

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
amount/delivery guards, cart reconciliation and protected payment journal remain
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
changes remain manual. A provider `modificationDeadline` is shown only when it
is an explicit timezone-aware timestamp; otherwise the deadline is unknown.
No message promises universal removal, replacement or refund.

## Validation scope

`test_meal_concierge_recurring_dietary.py` exercises ordinary Application tool
requests with synthetic retailer/cart/order state and a task-local sender inbox:
recurring discovery/save, balanced shopping, subsequent-week reuse, shortfalls,
mixed meals, real-format detail parsing, typed/ambiguous findings, manual product
changes, legacy pending summaries, scheduled and direct standing checkout,
failed/uncertain notices, result recovery and exactly one payment dispatch.
Existing batch/replan, product, scheduler, payment uncertainty and provider tests
remain part of the fleet profile. These tests and the anonymous Oda detail read
do not claim authenticated dietary checkout, live purchases or native external
recipient acceptance. Those require separate authorization.
