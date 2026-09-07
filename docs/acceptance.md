# Acceptance matrix

This matrix describes public source **82e64b8** on **2026-09-07**. It accounts
for all 26 served MCP tools and the documented installer/CLI paths. A shared
fixture test, a real model conversation, an authenticated read and a purchase
are separate forms of evidence. The public checkout passed 1,181 tests with
nine optional skips; those skips are not acceptance of the corresponding
external environments.

The native conversation environment was a task-specific Hermes profile in the
maintainer-approved Bob container, using its real Codex model, maintained skill,
MCP bridge and Application. The model remained `gpt-6-astra` with low effort.
Grocery responses in these conversations were controlled synthetic fixtures;
the service rejected external network connections. The installed recipe pack
was the actual pinned public release. No real cart, order, payment or
cancellation effects have been demonstrated by this work.

## Served features

Tool names below omit the common `meal_concierge_` prefix. The
[reference](reference.md), [runtime](runtime.md) and
[import guide](recipe-import.md) define the exact supported actions and limits.
The listed test modules are in the public `tests/` directory. All were included
in the successful public suite; coverage here does not claim every possible
provider outcome.

| Feature | Served tools or normal path | Current evidence | Remaining boundary |
|---|---|---|---|
| Setup, status and continuity | `status`, `setup`, `profile`; installer, service/bridge restart | `test_installer.py`, `test_meal_concierge.py`, `test_meal_concierge_acceptance.py`; 15 native Bob prerequisite conversations and native profile/menu flows | Mathem protected-action installation/upgrade is pending its implementation; bank use works independently |
| Recipe search and details | `recipes`, `recipe_discovery` | `test_meal_concierge_recipes.py`, `test_meal_concierge_retailer_recipes.py`, `test_meal_concierge_recipe_selection.py`; native seven-day store scenarios below; authenticated Mathem search plus exact public details | Authenticated MENY detail remains pending; Oda authenticated recipe search was unavailable; no exact native bulk-cart preview exists |
| Import and migration | `recipe_import`, `migration`; `import_recipes.py` | `test_recipe_import.py`, `test_meal_concierge_migration.py`; authenticated real Mealie 3.24.0 import and independent bank readback; prior scoped MC-07 extraction acceptance | New imports target builtin. RecipeSage source-account and further client/input acceptance remain unverified/deferred; fixture formats are documented separately |
| Recipe writes, favorites and labels | `recipe_write`, `recipe_favorite`, `recipe_labels`, `recipe_lifecycle` | `test_meal_concierge_recipes.py`, `test_meal_concierge_private_recipes.py`, `test_meal_concierge_acceptance.py`; new builtin saves and exact original-operation recovery covered | External labels/lifecycle writes exist only for exact retained recovery, not new primary-library use |
| Covers, attachments and archives | `recipe_cover`, `recipe_image`; private export/restore and consistent installation backup | `test_recipe_import.py`, `test_meal_concierge_recipe_assets.py`, `test_meal_concierge_recipe_contract.py`; prior scoped MC-07 attachment/image/local-sender acceptance | Original attachment extraction is client-specific. New Mealie source-account test covered text fields, not native image upload; remaining MC-wide client matrix is #52 |
| Dinner/week planning and alternatives | `menu`: plan, resolve_handoff, save/get, assess | `test_meal_concierge_planner.py`, `test_meal_concierge_recipe_selection.py`, `test_recipe_selection_runtime.py`; native store, mixed-bank and installed-pack menus below | Ranking is bounded; unknown safety facts and unsupported preferences are not satisfied by model assertions |
| Replanning, batches and leftovers | `menu`: lock, replan, batch actions | `test_meal_concierge_replanning.py`, `test_meal_concierge_batch.py` | Shared synthetic Application coverage; no new authenticated provider-order change is certified by a menu replan |
| Cooking and learning | `cooking`, `feedback` | `test_meal_concierge_replanning.py`, `test_meal_concierge_feedback.py`, `test_meal_concierge_batch.py` | Cooking/history and preference evidence do not establish food safety or authorize order changes |
| Pantry and exact quantities | `menu` available_ingredients; `products` decisions | `test_meal_concierge_pantry_selection.py`, `test_meal_concierge_products.py`; native Thai/spicy/stock ranking below | Request-scoped exact stock, not an inventory ledger; unknown measures remain actionable unresolved needs |
| Catalog, product planning and substitutions | `catalog`, `products` | `test_meal_concierge_products.py`, `test_meal_concierge_product_capacity.py`; whole-week and offline-pack preparation below | No measured native-hint speedup; current prices/availability and exact candidate approval remain required |
| Favorites and recurring goods | `product_favorites`, `recurring` | `test_meal_concierge.py`, `test_meal_concierge_acceptance.py`, `test_meal_concierge_products.py` | Shared persistence/interval behavior verified; real account cart effects remain separately pending |
| Cart and delivery | `cart`, `delivery` | `test_meal_concierge.py`, `test_meal_concierge_products.py`, `test_meal_concierge_mathem.py`; seven-day manual-quantity/replay fixtures; authenticated Mathem read-only readiness | Live cart mutation and delivery selection have not been exercised in this backlog run |
| Checkout, orders and recovery | `checkout`, `orders` | `test_meal_concierge.py`, `test_meal_concierge_mathem.py`, `test_meal_concierge_acceptance.py`; existing provider journal and drift/uncertainty fixtures | Oda/MENY guarded paths remain; MENY phone approval is external. Mathem still returns a manual checkout handoff. Automated Mathem submit/change/cancel and real payment-result verification remain #50 |
| Scheduling and email | `schedule`, `email` | `test_meal_concierge.py`, `test_weekly_scheduler.py`, `test_email_scheduler.py`; recorded local-sender/occurrence/recovery checks | No real recipients used; synthetic scheduling does not certify unattended live Mathem checkout |

## Native menu conversations

Every row used actual Hermes model/tool execution, not scripted model replies.
Store test prompts explicitly identified the synthetic data. Model replies
retained this distinction and named unknown nutrition/time/safety evidence.

| Source revision and case | Observed Application result | Effects and limits |
|---|---|---|
| 294c9e1, empty bank, MENY | Seven detail reads; seven distinct source recipe keys, two portions, exact save/get digest | Bank remains empty, AI fallback false, no cart/checkout calls. The model corrected one invalid ISO-week input before saving |
| 294c9e1, empty bank, Oda | Seven public-detail fixture reads; seven distinct source recipe keys, two portions, exact save/get digest | Native integer recipe IDs and public detail flow exercised; bank remains empty, AI fallback false, no cart/checkout calls |
| 294c9e1, empty bank, Mathem | Seven public-detail fixture reads; seven distinct source recipe keys, two portions, exact save/get digest | Same bounded workflow and no personal saves; synthetic provider access is not authenticated checkout evidence |
| ee7ba9b, three bank recipes plus MENY | Seven saved dinners: three bank and four store recipes; exact save/get digest | Bank remains at three entries; AI fallback false; no cart/checkout calls |
| ee7ba9b, installed pack only | Twenty local detail reads; seven distinct saved dinners at two portions from the actual 4,599-recipe pack | External recipe sources disabled; bank count unchanged; AI fallback false; model assessed the saved menu and reported unmet/unknown soft goals |
| ee7ba9b, preferences and available stock | Stored Thai/spicy preferences, selected two tagged recipes including the exact available ingredient, saved/get the exact menu | Ten seeded bank entries remain; stock stays on that menu request; unsupported diet/nutrition/quality requirements were explicitly named |

The ee7ba9b rows reuse unchanged bank/preference/MENY planning behavior. The
294c9e1 rows exercise the changed Oda/Mathem reader. The subsequent 82e64b8
change affects only Mealie pagination and its fixtures/docs; it does not change
these conversation paths. Earlier failed harness attempts are not counted as
passes: the first fixture omitted a probe deadline argument and used a wrong
MENY JSON-LD context; an initial prompt also failed to identify test-only food.

## Integrated recipe-to-product evidence

The seven-day Application fixture in `test_meal_concierge_products.py` runs
against all three provider shapes. Thirty-five ingredient occurrences become
five product searches. Rice totals 700 g; one explicit 250 g stock amount leaves
450 g and one 500 g pack. The other shared needs require eight packs. An
unavailable candidate is excluded, the exact approved plan adds nine packs,
and two existing manual units remain. Restart/replay dispatches no second cart
write. This is synthetic mutation/reconciliation evidence.

The release-pinned offline pack was installed in the isolated Linux MC runtime:
4,599 ready recipes, no installation errors/conflicts. A selected seven-dinner
pack menu produced 46 complete ingredient requirements and 58 packs with each
of the three synthetic provider catalogs. A separate automatic seven-day
selection required explicit decisions for nine optional/unresolved ingredients
before product preparation. Those decisions are not assumed pantry ownership.
See [pack coverage and limits](recipe-pack-build.md).

The actual authenticated Mathem recipe search returned two source URLs; both
public pages resolved through Application with exact source binding, scaling
from four portions to two, cache replay and zero personal saves. Oda's known
public page passed the same detail path; its authenticated search did not.
See the [retailer capability matrix](retailer-recipes.md).

## Open provider acceptance

#43 still requires the authenticated MENY adapter detail read. #41 and the #45
integration tracker retain that provider gate rather than describing the
synthetic rows as authenticated success.

#50 remains open: the Mathem website in Bob still needs its own login after
successful MCP OAuth. The connected seven-dinner purchase conversation,
automated protected-action implementation, normal installation/upgrade,
account-mismatch checks and observed order/payment/change/cancellation outcomes
are not complete. Missing login/card/challenge and drift/uncertain-effect
fixtures must accompany the eventual verified implementation. Owner test
purchase authorization is available; authorization alone is not provider
capability evidence. No order or payment should be inferred from the reads above.

MC-08 remains complete in its recorded scope. The user-deferred cross-client
MC follow-up is [#52](https://github.com/poisdahl/meal-concierge/issues/52);
these results neither reopen that program nor certify its untested clients.
