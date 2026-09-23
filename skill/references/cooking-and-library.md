## Cooking, adjustments and library copy

`meal_concierge_cooking` records only reported cooked/not-cooked outcomes.
Structured menus require exact menu ID, expected revision and slot ID; legacy
history requires the exact week/recipe identity. Ordering and silence are not
cooking. Feedback experience takes a menu-provided feedback_target plus reported
actual_active_minutes, portion_fit and/or leftover_portions. Never infer those
values. Inspect/undo/reset remain explicit; experience does not silently alter
recipes, preferences or planner weights. Accept/reject/swap feedback uses the
exact returned handoff/slot references; favorites remain separate native state.

Menu lock takes exact menu ref/slot and desired boolean. Replan_prepare takes
explicit remaining dates and candidates; pass its exact apply_arguments unchanged
to replan_apply. The opaque replan_ref remains available when large plan details
are omitted, and the service rejects missing, changed or stale references. Apply
preserves past, cooked and locked slots plus predecessor snapshots. Product/cart changes remain
separate. For recurring meals use profile meals.meal_mode=fresh|batch|mixed,
batch_dishes, dishes, prepared_portion_range, existing portions consumed per
meal, and exact cook_days/eat_days. Set recurring_batch_accepted=true only after
acceptance of those settings; reuse them in later ordinary menu plan calls with
no repeated confirmation. Show every proposed eating slot, source, preparation,
shortfall and recipe-specific guidance. Never silently change quantities.
Candidate facts.batch_guidance can retain basis, suitability, storage and
reheating from actual guidance; missing guidance stays unknown, never a household
storage-life guarantee. Reported food and plans remain distinct.
Batch_prepare is opt-in and needs explicit source, portions,
suitability, storage/interval and exact leftover targets. Show the unchanged
batch plan and get its explicit confirmation before batch_apply. Actual batch
cooking needs reported prepared/consumed portions; dependent leftovers require a
confirmed source and sufficient remaining portions. No inferred storage safety,
stock or consent. Invalid dependents require replanning together.

`meal_concierge_migration` explicitly copies exact recipes between different
libraries. Prepare is read-only: review exact identities and each preserve/omit/
stop metadata choice, then execute the unchanged preview with its explicit
confirmation. Resume the same plan after partial/uncertain results. Never create
a replacement import, infer label mappings from names, change sources or primary
routing, or delete to roll back metadata failure. Primary-library changes remain
separate local configuration after the final report has no uncertainty.
