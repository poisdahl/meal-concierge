# Using Meal Concierge

Talk to your agent in your preferred language. Start with a request such as:

> Plan four dinners from Thursday for two, using the carrots we already have.

Meal Concierge can use recipes from your selected, connected store without
an imported recipe collection. It can also use recipes you save in its local
bank. See [adding recipes](recipe-import.md) for personal recipes and the
optional offline collection.

## Set up your household

On first use, review the household, portions, dietary preferences, meal plan
and recipe sources. Keep the suggested settings or describe what you want to
change. Ask to review your setup again at any time.

The agent gives a short guide as you make the relevant choices. Ask at any time:
"Show everything Meal Concierge can do and all my settings and preferences."
That overview includes meal and purchasing preferences, recurring groceries,
pantry assumptions, recipe sources, delivery options and optional automation.
The default cooking-time preference is 0–45 minutes of active work, with 60 as a
soft upper limit. Elapsed oven or resting time is separate; missing times remain
unknown. Organic and local labels carry no default price premium.

Examples:

- “We are two adults. Plan five dinners and leave Friday free.”
- “Cook twice a week, with leftovers for the other days.”
- “Include vegetarian lunches as well as dinners.”
- “Show which recipe sources you are using.”

One installation belongs to one store and household. To shop at another store,
use a separate installation; do not change the provider on an existing bank of
orders and shopping settings. Multiple trusted agents can share one installation.

## Dietary goals and substitutions

New installations start with an explicit, editable Norwegian dietary-guideline
preset, including adult daily and weekly reference quantities. Setup explains
this starting choice and offers to keep, change or remove it; the retailer or
conversation language does not determine the household's dietary goals. The
preset favors plant-rich meals, fish and legumes and minimizes processed red
and white meat. Its amounts describe the whole diet, not dinner-only quotas or
raw shopping weights. The agent applies the actual saved goals to ingredients
and methods; service readiness and ranking do not certify nutritional compliance.

The maintained summary cites [Helsenorge](https://www.helsenorge.no/kosthold-og-ernaring/kostradene/)
and [Helsedirektoratet](https://www.helsedirektoratet.no/faglige-rad/kostradene-og-naeringsstoffer/kostrad-for-befolkningen).
It lives entirely in `diet.patterns`; replacing or clearing that list removes the
preset. Existing profiles, including empty/custom goals, remain unchanged. For an
older generic “national guidelines” goal, clarify the intended country once rather
than silently replacing it. Separate saved nutritional targets still apply.

Optional numeric targets start disabled: zero minima, an empty fish-gram range
and all-zero plate fractions mean no additional target. They are not an
instruction to eat zero of those foods. An explicit fish range `[0, 0]` is a
zero-fish target, distinct from an empty range. Existing profiles keep all their
saved values during upgrades; changing a dietary pattern does not erase separate
saved targets. Ask to change those too if they no longer fit. An explicit profile
reset restores the installation's configured defaults for the selected fields.

Avoided ingredients should be left out or replaced with something that works in
the dish. Any nutritional reason for choosing one replacement over another must
come from your saved goals and available product information. A plant-based label
alone establishes neither nutritional fit nor allergy safety. The agent explains
material substitutions; it does not silently relax avoid preferences or turn them
into allergies.

## Plan, save and adjust meals

Plan for dates, a period or a number of dinners, including across calendar weeks.
Weekly planning and automation remain optional. The first saved-plan message
names each recipe’s actual source with its link, marks adaptations, and offers
full ingredients and steps, recipe swaps and using ingredients you already have. A short plan is assessed for the days it covers, not as a whole week's diet.

Review the proposed dishes, dates, portions and any missing information, then
ask to save the menu. Saved menus keep the recipes and amounts used for that
plan, even if the original recipes later change.

- “Replace Wednesday's dinner with a quicker dish.”
- “Add lunch for Saturday.”
- “Make four portions on Monday and use two on Tuesday.”
- “We didn't cook Tuesday's meal. Replan the rest of the week.”
- “We liked this dish. Remember that for future menus.”

Tell the agent which meals share a batch of food so ingredients are counted
for the cooking batch. Changing portions is different from adding another
independent meal. Cooking feedback and recipe favorites are separate: you can
mark a dish as a favorite without claiming you cooked it.

## Use ingredients you already have

> We have 500 g rice and six carrots. Prefer recipes that use them.

Meal Concierge can use that information when choosing recipes and calculating
what to buy for the current plan. It does not maintain an automatic inventory
of your kitchen. Specify quantities when you know them, and tell the agent
which existing cart items are extra household shopping.

During setup, choose whether salt, pepper and cooking oil can normally be assumed
available. An accepted list avoids repeated questions for ordinary amounts. The
agent still asks about unusual quantities or a different oil type, and checks
roughly every eight weeks during your next normal shop whether anything needs
restocking. It never buys merely because a check is due. Other recipe staples,
such as butter, sugar and flour, get one combined stock question for the plan.

## Recipes and favorites

Save a recipe from a link, text, supported attachment or connected recipe library.
Review extracted portions, ingredients and steps before saving; uncertain
quantities stay visible. Ask to edit, favorite or archive a saved recipe.

- “Save this recipe and mark it as a favorite.”
- “Show my favorite vegetarian recipes.”
- “Use only my saved recipes for this menu.”

Store recipes may have store-specific usage restrictions and may not be suitable
for shopping at another retailer. Source links and credits remain attached.
See [recipe import](recipe-import.md) for formats, Mealie/RecipeSage and
[updating the optional collection](runtime.md#versioned-recipe-package-integration).

## Groceries and prices

Ask to review the groceries for your saved menu, then add what is missing.
Meal Concierge matches ingredients to products and package sizes, checks the
live cart and accounts for verified goods already there. It may need your choice
where a quantity, substitution or package size is unclear.

- “Show the groceries and estimated cost before changing the cart.”
- “Make sure we have two cartons of milk in the cart.”
- “Add our usual household items as well.”
- “Compare a cheaper version of this menu.”
- “Find suitable offers on dinner ingredients, then suggest meals around them.”
- “Just order these groceries; I don't need a meal plan.”

Product selection considers dietary and cooking suitability, actual need and
the lowest payable total for quantities you can use. The agent should explain
a material premium for a meaningful benefit, and avoid excess packs bought only
for a lower unit price. It can use observed offers when choosing recipes, checking
membership and multibuy conditions. Search coverage differs by store; this is
not a guarantee that every campaign or the globally cheapest basket was found.

Recurring groceries can follow elapsed time, such as one pack every 21 days from
the last confirmed purchase. An overdue item stays due for one normal quantity
at the next regular shop, without catch-up packs. Existing calendar schedules
remain available. Incidental top-up orders include only the requested goods
unless you ask for recurring items too. Fruit quantities belong in these items,
rather than duplicated in general meal preferences.

A menu estimate is not the final order price. Delivery, discounts, deposits,
other fees and changing product prices can affect checkout. Product selection
does not guarantee the cheapest possible basket.

Dietary preferences guide planning and product selection. Check ingredient labels
and allergens yourself; missing information is not proof a product is suitable.

## Checkout and existing orders

Oda and Mathem follow the same workflow: review the cart, choose delivery,
prepare checkout, then confirm the final summary. Both support saved-card
checkout in their dedicated browser. Oda also supports Vipps; MENY uses Vipps.
Complete any store, bank or phone approval yourself.

Preparing checkout does not place an order. The default is to ask for your
confirmation before submitting. You can separately arrange standing authorization,
which lets a clear request to order or cancel proceed after the current summary
has been verified. It does not remove payment-provider approvals.

- “Show my cart and available delivery windows.”
- “Prepare checkout and show the final total.”
- “Add milk to my existing order.”
- “Remove one item from that order.”
- “Move the delivery to Friday.”
- “Cancel this order.”

Changes depend on the store's current cutoff and available controls. Review
any price change. An accepted merchant change does not prove a bank refund has
settled. If payment or an order change is uncertain, ask the agent to inspect
the original attempt before making another one.

## Receive recipes and optionally automate planning

> Give me the recipes for the saved menu as a PDF.

Chat text is the default for new installations; PDF and available images depend
on your agent's actual attachment support. Email is optional and contains the
complete recipes without a duplicate PDF by default. Choose delivery after
confirmed purchase to receive that order's saved recipes promptly. Grocery-only
orders produce no recipe message. A PDF remains useful through a supported
message destination when email is unavailable or when explicitly requested. See
[recipe delivery](recipe-delivery.md) for setup, timing and limitations.

For repetition, ask your agent to schedule the weekly plan or shopping preparation.
The agent needs a persistent scheduler and an available host. Automatic ordering
is a separate opt-in with delivery rules and any spending limit you choose; ordinary planning does
not enable it. Vipps still requires approval on your phone.

Ask to show, change or pause the schedule. Pausing recipe messages is separate
from pausing grocery planning or ordering. Confirm which activity you want to stop.

## Updates and help

[Update the program](runtime.md#updates-failures-and-recovery) and
[update the optional collection](runtime.md#versioned-recipe-package-integration)
separately. Neither requires starting a new household.

If tools disappear, open a new conversation and ask the agent to inspect its
connection to the existing service. If a store session expires, complete login
again in the intended account. Preserve your installation and data while
troubleshooting; do not reset them to resolve a missing tool or an uncertain order.

## Who chooses the food

The host assistant chooses recipes, meal order and suitable observed products.
The ordinary tool path uses `selection_mode="agent"`; the service checks exact
references, amounts, configured restrictions and external effects. Saved numeric
food goals remain visible without blocking a meal because a name classifier
cannot recognize it; an explicitly strict target remains required. Ordinary
brands and shared packages do not require another user approval.

Recipe adaptations keep their source and are separate from originals. New
quantities are labeled estimates with assumptions. Ask to save an adaptation
when it should become a lasting bank entry. Oda, Mathem and MENY recipes retain
their store binding and use the existing schema-2 format.

An explicit request to empty the cart uses its freshly observed digest. The menu
is retained, earlier product completion is invalidated, and an uncertain removal
is reconciled before another write. An empty cart is not a cancelled order.
