## Use ingredients the user already has

For a current request such as “use my broccoli and chicken”, pass
`planner_input.available_ingredients` with exact `item` names, optional exact
`quantity`/`unit`, and `use_first: true` only where the user requests priority.
At most 32 distinct items are supported. Do not infer stock from a basket,
order, recipe pantry flag or previous shopping. This input belongs to this
planning request; it is not a maintained inventory or a freshness/allergen fact.

Use the returned loaded-ingredient match reasons to explain the selection.
Matching names alone never establish that a meal is covered. Unknown quantities
and incompatible units leave quantified purchases unchanged. Preserve distinct
foods and substitutes rather than silently treating their names as equivalent.

The exact saved menu carries the stock assertion. Product preparation aggregates
the whole menu, subtracts compatible stock once, then rounds packages. For
example, two 400 g rice meals minus 500 g on hand need 300 g before rounding.
Do not repeat that 500 g as another pantry deduction. Later exact
`ingredient_decisions` replace the request stock for that entire ingredient;
allocate the user's total once across the returned positions. An `include`
decision explicitly buys the ingredient. Replanning uses the newly supplied
stock assertion; if omitted, old stock is not assumed still available. It does
not consume or update any persistent stock ledger. Cart changes retain their
separate explicit request.
