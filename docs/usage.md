# Using Meal Concierge

Talk to your agent in your preferred language. Start with a request such as:

> Plan next week's seven dinners for two.

Meal Concierge can use recipes from your selected, connected store without
an imported recipe collection. It can also use recipes you save in its local
bank. See [adding recipes](recipe-import.md) for personal recipes and the
optional offline collection.

## Set up your household

On first use, review the household, portions, dietary preferences, weekly plan
and recipe sources. Keep the suggested settings or describe what you want to
change. Ask to review your setup again at any time.

Examples:

- “We are two adults. Plan five dinners and leave Friday free.”
- “Cook twice a week, with leftovers for the other days.”
- “Include vegetarian lunches as well as dinners.”
- “Show which recipe sources you are using.”

One installation belongs to one store and household. To shop at another store,
use a separate installation; do not change the provider on an existing bank of
orders and shopping settings. Multiple trusted agents can share one installation.

## Plan, save and adjust meals

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

## Receive recipes and automate a weekly plan

> Give me the recipes for the saved menu as a PDF.

Chat text is the default for new installations; PDF and available images depend
on your agent's actual attachment support. Email is optional. See
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
