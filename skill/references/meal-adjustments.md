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
