# Stock, menu changes and batches

Keep the model's chosen menu in `selection_mode="agent"`; numeric saved goals
remain advisory unless explicitly strict. Adjusting stock or batch layout does
not require switching to ranked planning or rewriting preferences.

## Reported stock

Use only current user-reported stock. `planner_input.available_ingredients`
accepts up to 32 distinct item names, for example:

```json
[{"item":"brokkoli","use_first":true},{"item":"ris","quantity":500,"unit":"g"}]
```

Unknown quantities, incompatible units and distinct ingredient identities do not
subtract purchases. Exact quantified stock is subtracted once from aggregated
needs before package rounding. Two 400 g needs and 500 g stock leave 300 g to buy.
This is no persistent inventory, freshness check or dietary assurance.

For a later stock/omit/include change, use products `record_ingredients` with the
exact menu reference and ingredient source positions returned in requirements.
These decisions replace request stock for that whole ingredient; do not allocate
the same total separately to each dish. `include` means buy. In a replan, supply
the current stock assertion for the whole remaining menu; omission does not
reuse old stock after possible cooking.

## One pantry check for the menu

Read the whole menu and aggregate needs before asking about staples, rather than
asking about or buying butter separately for each meal. Include only relevant
items with unknown stock: for example butter, oil, flour, sugar, spices, vinegar
and sauces. Ask whether there is enough for the total, or how much is available.
A useful question is: "This menu needs 90 g butter and 2 tbsp oil. Do you have
enough of either, or should I buy them?" Reuse an explicit answer or household
standing instruction; salt and pepper are not automatically stocked by the
shopping engine. Do not turn default profile suggestions into user assertions.
An explicit request to buy everything needed can resolve the check as `include`.

For a saved menu, `products record_ingredients` stores the reply against exact
source positions. `have_all` covers each named source need; use it for every
position covered by the user's enough-for-the-whole-menu answer. `include`
records that the item needs buying. `omit` is only for optional ingredients.
For two 45 g butter requirements and 60 g reported stock, allocate 45 g to the
first position and 15 g to the second with `have_quantity`: buy only the remaining
30 g. Never subtract 60 g from both. Product review rows retain their source-bound `ingredient_decisions`, including
`include`, so recovering the plan does not lose an answered stock question.
Existing current answers resolve subsequent
preparations; do not repeat the question on retries. A new menu revision needs
new source bindings and a recheck of changed demand, not a blind replay.
After recording changed stock for a saved menu, start a fresh `prepare` with
its exact `menu_ref` and `continuation_mode="reset"`. Do not reuse the earlier
`product_plan_ref` or apply arguments: covered requirements may have disappeared.
For an unsaved preview, start a fresh `prepare` with the original `planner_ref`
and `ingredient_decisions`, without `product_plan_ref`. A product-plan
continuation retains its frozen stock decisions; it cannot accept new stock.
After saving, record the answers against the saved menu's returned source
positions, then prepare the saved menu before cart apply.

When reported stock uses incompatible units, distinguish "enough for the whole
need" from an exact partial quantity. Do not pass grams as millilitres or invent
exact stock conversions. Ask a practical sufficiency question when necessary.
Once stock is resolved, use the existing `package_count` and `quantity_basis`
agent estimate for source volumes versus gram-labelled packs, pieces versus
weight, or bunches versus retail packs. Explain ingredient-specific size/yield
assumptions and choose observed packs for the combined remaining need. This is
not exact coverage. Preserve raw/cooked, drained/gross and edible/bone-in forms;
a substitution that changes preparation needs a coherent recipe adaptation.
Use `shared_package` when one selected pack serves distinct requirements.

## Remaining-week changes

A pending purchase or order change protects its own slots, cart and payment,
but a menu-only change to a separate menu can proceed when the service proves
the menus are independent. For a requested distinct whole menu, read the
current menu with `get`, choose it with `plan`, and `save` the unchanged
`save_ref` as `planner_ref` together with the exact current `menu_ref`. For a
exact slot change to an already independent menu, use `edit_slots`, even if
the older order has a prepared recovery review or an
active provider handler. Neither menu action resolves, retries or changes the
older payment; do not prepare or apply new cart goods until it resolves. If
the service reports linked or unidentified protected work, reconcile that
operation before changing its slots. Do not confirm payment merely to unlock
planning, or replace a whole menu to bypass a cooked, locked or batch-dependent
slot.

Read the current `menu_ref={menu_id,revision,digest}` and stable slot IDs. Use
`edit_slots` with a stable idempotency key to add, replace, remove or move exact
future occurrences. Repeating a recipe creates another preparation; linked
later servings use `source_slot_id` or an earlier add's `source_edit_index` and
shop the combined prepared portions once. Record per-date portions explicitly.
For a new selection of remaining dinners, use `replan_prepare` with
`remaining_dates`, current `planner_input`, and any actual
`locked_slot_ids`/`as_of_date`; then pass returned `apply_arguments` unchanged to
`replan_apply`. Keep past, cooked, locked and batch-dependent slots. Do not replace
a whole menu just to evade a blocked slot. A partial replan retains the original
whole-menu dietary policy; a full replacement can introduce agent-mode policy.

An expressly requested extra meal/course uses menu `edit_slots` with
an `add` entry containing date, meal_type, portions and exact recipe/discovery
reference, menu reference and stable idempotency key, rather than displacing
an ordinary dinner. Regenerate product preparation for the resulting menu and
use its apply path to reconcile existing menu goods while preserving extras.

## Batches and reported cooking

Recurring batch settings belong in one accepted `profile` update under `meals`:
`meal_mode`, `dinner_days`, `dishes`, `batch_dishes`, `portions`,
`prepared_portion_range`, `cook_days`, `eat_days`, `recurring_batch_accepted`.
Record acceptance only when the household chose that arrangement; reuse an
accepted arrangement in subsequent weeks. Setup does not rewrite one silently.
Consumed portions per meal differ from prepared batch portions. Shopping counts
each source preparation once; dependent leftover meals add no duplicate purchase.

For an explicit one-menu arrangement, use `batch_prepare` with the exact menu
reference and a `batch_spec` containing:

- `source_slot_id` and its exact `source_snapshot_digest`;
- `prepared_portions` and `consumed_at_source` (the source meal's portions);
- `suitability={source:"agent"|"current_user",value:"suitable"}` and
  `storage` with the same source, `method:"refrigerated"|"frozen"`, and an
  explicit `max_interval_days` or ISO `use_by_date`;
- for an `agent` assessment, a short recipe-specific `basis` and `reheating`
  instruction in `storage`, assessed against the last leftover date;
- `leftovers=[{slot_id,portions},...]` for one to six later dinner slots within
  that storage interval and the available portion remainder.

Model judgment is `agent`; only actual user-reported suitability or storage is
`current_user`. The exact source snapshot, interval, portions and leftover dates
are checked during preparation. Pass its unchanged `batch_plan` to `batch_apply`.
Only when the user has authorized that exact arrangement, copy the plan's
`batch_digest` and `confirmation_statement` into
`batch_confirmation={batch_digest,statement}`. Do not invent a digest or claim
confirmation that was not given; this path does not return `apply_arguments`.

Use `cooking mark_cooked`/`mark_not_cooked` only from the user's actual report,
with the returned menu/slot identity and expected revision. Batch source cooking
needs actual `prepared_portions` and `consumed_at_source` in `actual_batch`.
Dependent consumption requires a matching cooked source and enough remaining
portions. Do not infer cooking from a purchase or silence. Preserve actual
recipe-specific storage/reheating guidance and its source; unknown guidance or a
freezer preference cannot establish safe storage life. Use feedback `experience`
for reported time, portion fit and leftover experience, not invented ratings.
