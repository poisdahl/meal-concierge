# Acceptance matrix

This matrix accounts for all 27 currently served MCP tools and the documented
installer/CLI paths. Its historical conversation baseline is public source
**82e64b8** on **2026-09-07**. A shared
fixture test, a real model conversation, an authenticated read and a purchase
are separate forms of evidence. That historical public checkout passed 1,181 tests with
nine optional skips; those skips are not acceptance of the corresponding
external environments.

The native conversation environment was a task-specific Hermes profile in the
maintainer-approved Bob container, using its real Codex model, maintained skill,
MCP bridge and Application. The model remained `gpt-6-astra` with low effort.
Grocery responses in these conversations were controlled synthetic fixtures;
the service rejected external network connections. The installed recipe pack
was the actual pinned public release. Separate authenticated Mathem acceptance
later added and removed one real cart item through Application, as described
below. Those menu conversations performed no live delivery selection, order,
payment or cancellation. Later provider probes have their separate scope below.

## Served features

Tool names below omit the common `meal_concierge_` prefix. The
[reference](reference.md), [runtime](runtime.md) and
[import guide](recipe-import.md) define the exact supported actions and limits.
The listed test modules are in the public `tests/` directory. All were included
in the successful public suite; coverage here does not claim every possible
provider outcome.

| Feature | Served tools or normal path | Current evidence | Remaining boundary |
|---|---|---|---|
| Setup, status and continuity | `status`, `setup`, `profile`; installer, service/bridge restart | `test_installer.py`, `test_meal_concierge.py`, `test_meal_concierge_acceptance.py`; 15 native Bob prerequisite conversations; normal Mathem external-manager install/upgrade and installed SDK checks below | Install/upgrade, dedicated authenticated profile and connected native Mathem setup-to-order are verified below; published-source installation and reconnect verified below |
| Recipe search and details | `recipes`, `recipe_discovery` | `test_meal_concierge_recipes.py`, `test_meal_concierge_retailer_recipes.py`, `test_meal_concierge_recipe_selection.py`; native seven-day store scenarios below; authenticated MENY adapter search/detail and Oda/Mathem MCP search plus exact public details | Later Oda calls returned MCP internal error -32603; a repeat MENY search timed out rendering; no exact native bulk-cart preview exists |
| Import and migration | `recipe_import`, `migration`; `import_recipes.py` | `test_recipe_import.py`, `test_meal_concierge_migration.py`; authenticated real Mealie 3.24.0 import and independent bank readback; prior scoped MC-07 extraction acceptance | New imports target builtin. RecipeSage source-account and further client/input acceptance remain unverified/deferred; fixture formats are documented separately |
| Recipe writes, favorites and labels | `recipe_write`, `recipe_favorite`, `recipe_labels`, `recipe_lifecycle` | `test_meal_concierge_recipes.py`, `test_meal_concierge_private_recipes.py`, `test_meal_concierge_acceptance.py`; new builtin saves and exact original-operation recovery covered | External labels/lifecycle writes exist only for exact retained recovery, not new primary-library use |
| Covers, attachments and archives | `recipe_cover`, `recipe_image`; private export/restore and consistent installation backup | `test_recipe_import.py`, `test_meal_concierge_recipe_assets.py`, `test_meal_concierge_recipe_contract.py`; prior scoped MC-07 attachment/image/local-sender acceptance | Original attachment extraction is client-specific. New Mealie source-account test covered text fields, not native image upload; remaining MC-wide client matrix is #52 |
| Dinner/week planning and alternatives | `menu`: plan, resolve_handoff, save/get, assess | `test_meal_concierge_planner.py`, `test_meal_concierge_recipe_selection.py`, `test_recipe_selection_runtime.py`; native store, mixed-bank and installed-pack menus below | Ranking is bounded; unknown safety facts and unsupported preferences are not satisfied by model assertions |
| Replanning, batches and leftovers | `menu`: lock, replan, batch actions | `test_meal_concierge_replanning.py`, `test_meal_concierge_batch.py` | Shared synthetic Application coverage plus native Mathem two-source menu and once-per-source shopping allocation below; cooking/storage safety remains unknown |
| Cooking and learning | `cooking`, `feedback` | `test_meal_concierge_replanning.py`, `test_meal_concierge_feedback.py`, `test_meal_concierge_batch.py` | Cooking/history and preference evidence do not establish food safety or authorize order changes |
| Pantry and exact quantities | `menu` available_ingredients; `products` decisions | `test_meal_concierge_pantry_selection.py`, `test_meal_concierge_products.py`; native Thai/spicy/stock ranking below | Request-scoped exact stock, not an inventory ledger; unknown measures remain actionable unresolved needs |
| Catalog, product planning and substitutions | `catalog`, `products` | `test_meal_concierge_products.py`, `test_meal_concierge_product_capacity.py`; whole-week and offline-pack preparation below | No measured native-hint speedup; current prices/availability and exact candidate approval remain required |
| Favorites and recurring goods | `product_favorites`, `recurring` | `test_meal_concierge.py`, `test_meal_concierge_acceptance.py`, `test_meal_concierge_products.py` | Shared persistence/interval behavior verified; real account cart effects remain separately pending |
| Cart and delivery | `cart`, `delivery` | `test_meal_concierge.py`, `test_meal_concierge_products.py`, `test_meal_concierge_mathem.py`; seven-day manual-quantity/replay fixtures; authenticated Mathem readiness and exact Application add/remove | Native Mathem whole-week cart17 packages and exact Sep13 slot selection/readback passed; earlier Sep9 reservation release remains unverified |
| Checkout, orders and recovery | `checkout`, `orders` | `test_meal_concierge.py`, `test_meal_concierge_mathem.py`, `test_meal_concierge_acceptance.py`; existing provider journal and drift/uncertainty fixtures | Native Mathem original order accepted after one dispatch and reconciliation; bank authorization/charge unknown. Bound addition accepted after one failed native payment and one operator-assisted recovery; Free delivery change accepted through native prepare/confirm/reconcile after scoped UI preparation, with unchanged18 packages/582.51 SEK and zero payable. Native cancellation accepted after two pre-dispatch stops and a persisted-review comparison fix; independent receipt/tracking confirmed the result with unrelated order preserved. Oda/MENY guards and external phone approval retained |
| Scheduling and email | `schedule`, `email` | `test_meal_concierge.py`, `test_weekly_scheduler.py`, `test_email_scheduler.py`; recorded local-sender/occurrence/recovery checks | One owned systemd occurrence ran ordinary Hermes with standing Mathem checkout and verified local pre/result notices; no real recipients or Hermes-native-cron claim |
| Finalized menu presentation | `recipe_delivery`; maintained CLI byte export | `test_recipe_delivery.py`; current shared frozen text/PDF/image/email, begin/ack and unknown-send recovery contracts | Existing shared evidence is retained; #50 does not depend on #53's remaining native PDF/transport acceptance or permit real recipients |

## Mathem installation and continuity — 2026-09-07

Public source `e36815fd01b9100d903e6e8c0ba68a1db9ab6384` was deployed to the
maintainer-approved Bob source mount and Meal Concierge sidecar. All 143 source
files matched the public archive. Bob's household/provider configuration stayed
Oda; 1,576 database/assets/metadata files and the existing household state were
preserved. Two independent installed SDK connections discovered all 27 tools
and completed status calls. Bob's existing browser and Signal containers were
not restarted. This establishes deployment and connectivity, not Mathem payment
readiness.

A separate unauthenticated Mathem fixture exercised the real `install.sh` with
the external manager: fresh browserless install, native foreground execution
under its own background owner, installed SDK connection, graceful stop, normal
browser-enabled update, another SDK connection and graceful stop. The pinned
3.12.12/MCP 2.1.1 runtime and released 4,599-recipe pack were used. Config,
household state, database and assets were unchanged, including every recipe ID
and version. Of 1,576 compared files, 1,574 were byte-identical; two import reports
correctly changed from 4,599 created recipes to 4,599 unchanged recipes. Their
original bytes remain in the installer's pre-update backup. The initial overly
strict report-byte assertion failed; only the unrun second SDK stage was resumed
after these exact differences were reconciled. No installation or import was
repeated for that recovery. This fixture supplied no retailer credentials and
does not certify systemd/launchd or authenticated browser continuity.

## Dedicated Mathem login and native attachment — 2026-09-08

The owner completed login in a new dedicated profile. Normal same-version
browser recovery and window closure released native profile ownership before
Chromium 151 acquired it; no cookies or credentials were copied and no locks
were removed manually. The installed `e36815fd` service read an empty cart and
one cancelled order, and its dedicated browser matched the selected OAuth
account/address reference. The earlier delivery slot remained selected; this
is not evidence of its reservation being released. These reads establish
account binding, not saved-card or payment readiness.

An ordinary Hermes conversation initially found the normal Oda household and
stopped without writes. After an explicit MCP socket environment reference was
added to Bob's actual trusted configuration, the same conversation read
Mathem status, setup and preferences. Its native session contains the three
successful read results and a final assistant message; the CLI subsequently
exited 134, an unresolved process/cleanup failure. The normal gateway retained
its Oda socket. Only idle Bob was stopped and started for this config correction;
both retailer services, the existing browser and Signal kept their container
identities and start times. This private runtime attachment correction is
separate from the published product source and does not establish the remaining
menu-to-purchase or existing-order gates.

The subsequent ordinary conversation saved seven dinners for 14–20 September:
two six-portion batch sources, each allocating two portions on three dates,
and one fresh two-portion dinner. Two own recipe imports and one authenticated
Mathem recipe supplied the menu. The planner's initial recipe/date assignment
differed from the requested assignment; that attempt failed. The next explicit
test instruction accepted the actual assignment before the exact menu was saved.
Storage suitability and actual cooking/freezing outcomes remain unknown.

A later preview used the unpublished candidate source digest
`9a253771438f4117941fc6d2bffdaa425da8d3af3193d63e817ad4c88c1db36d`:
public `2e5f11f` plus seven reviewed runtime-file candidates, installed only in
the dedicated Mathem testservice. All 143 source files and both state/database
files were verified after update; the installed Hermes MCP client reconnected.
The ordinary model's single `products.prepare` returned 12 approved products,
17 packages and a 458.01 SEK merchandise estimate, with no unresolved needs.
Nine explicitly synthetic pantry ingredient groups covered the unsupported
count/mass, drained-weight and density bridges; this does not prove a purchased
whole week from an empty physical pantry. Each batch source contributed its
shopping quantities once. The changed package parser recognized the observed
Swedish origin-and-mass labels, and the oil search retained both Norwegian
ingredient identity and actual Swedish provider query. Deposit and payable
amounts remained null and the 700 SEK product budget remained unverified.
The native final reply matched the structured plan and exited zero. No cart,
delivery, order, payment or send occurred in that preview.

The next ordinary turn stopped before its first cart write: the original
product-plan object exceeded the native file reader's usable response. Its
three provider calls only rechecked the empty cart and historical cancelled
order. The saved reply reported that stop accurately; CLI exit 134 remains an
unresolved process result. The compact apply route now uses the exact prepare
inputs and reviewed digest, retaining fresh and final prewrite checks. A real
27-tool MCP/stdio/Unix/Application probe with a synthetic Mathem provider
accepted 3,003 characters of compact inputs and repeated apply with one cart
write. This is transport and synthetic recovery evidence, not native shopping
acceptance. A second isolated candidate, source digest
`94b29cb9f2bd32c6d1a540bdb521143d8d1d12cca086f651c0ebf1afd09c6135`,
passed fleet validation and verified all 143 deployed source files, unchanged
state/database bytes and installed-client reconnect. Purchase acceptance was still
open at that preview stage; the later observed results are recorded below.

The Mathem incomplete-order integration test uses seven dinners requiring
14 eggs, rounded to three six-egg packages. After synthetic removal of one
package and explicit keep-current, automatic checkout stops. Browserless
handoff and protected manual review expose the missing package; the local
notice, reconciled result and frozen result notice preserve it across a lost
payment response and restart, with one synthetic payment dispatch.

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
from four portions to two, cache replay and zero personal saves. A fresh Oda
authenticated search subsequently returned recipes 3330 and 4122; both exact
public details passed the same Application checks. Later Oda initialization
and search calls returned MCP internal error -32603, so this is a successful
read with intermittent service availability, not proof of stable access.

After owner login, the existing MENY adapter passed authenticated probe, search
and detail reads in Bob's reserved browser. The reference lasagne yielded four
portions, 16 ingredients and six steps. A detail from the successful search
(lasagne with salsiccia) also passed Application source binding, scaling to two,
cache replay and zero personal saves: four base portions, 17 ingredients and
nine steps. The adapter's command transport used Bob's existing browser wrapper;
its login checks, extraction, lock and deadline logic were unchanged. A repeat
search timed out waiting for rendering; subsequent authenticated detail checks
passed using the previously observed search result. Neither run changed a cart.
See the [retailer capability matrix](retailer-recipes.md).

A separate Mathem cart probe on the same implementation used the real provider
client and its lock through a task-only SSH command transport. After a fresh
empty baseline and product/price verification, Application `cart ensure` added
one 500 g Fusilli package at 15.95 SEK. MCP readback and the already authenticated
website showed that exact quantity. Website navigation reached `/se/cart/` and
`/se/checkout/delivery/`; it did not reach or submit payment. Application
`cart change` then removed only the test-added quantity. Final readback was an
empty cart with no pending cart journal. Native delivery-slot reads for two
future dates also normalized successfully, but no slot was selected. This was
a direct Application/provider test, not the required connected model purchase
conversation or proof of full browser/MCP account-identity binding.

## Provider acceptance and retained boundaries

The authenticated recipe-read gate for #43 is now demonstrated and completes
the remaining provider input to #41/#45. The native menu conversations and
seven-day cart fixtures above retain their synthetic provider scope; the new
reads and the one-item cart probe do not turn them into real purchase acceptance. Provider availability
failures remain explicit failures, not exhausted recipe sources.

The #50 Mathem implementation, observed order follow-ups and published-source
installation are verified within the recorded scope below. The first addition
payment failed; one separately scoped operator-assisted recovery produced the
exact accepted addition, with both attempts retained. Free delivery change and
cancellation are accepted with their stated preparation and payment limits.
The owner explicitly excluded README and client-install guides from this task;
those files were preserved, and this matrix, reference and maintained skill
record the current provider behavior. Earlier cart/checkout reads and the unverified old reservation release are
retained as historical evidence; they are not repeated or upgraded into success.

MC-08 remains complete in its recorded scope. The remaining cross-client
MC follow-up is [#52](https://github.com/poisdahl/meal-concierge/issues/52);
these results neither reopen that program nor certify its untested clients.


## Recurring batches and advisory dietary checkout (#55)

The shared [recurring-batch/dietary path](recurring-batch-dietary.md) has focused
Application tool-flow coverage using synthetic retailer/state and a verified
local sender inbox. It includes two batch sources across seven meals, exact
shopping totals, subsequent-week reuse, explicit shortages, mixed schedules,
retail findings through manual and standing/scheduled checkout, substitution,
legacy-pending protection, failed/uncertain notices and payment/result replay.
Anonymous exact Oda and Mathem product-detail reads additionally verify their
visible ingredient/allergen extraction paths. Mathem final-cart fixtures cover
documented exclusions, missing detail and a substitution that is not covered by
the original product's standing permission. Delayed receipt/restart/expiry
recovery retains one payment dispatch, and shared cancellation/result replay
keeps bank authorization, charge, release and refund unknown without separate
evidence. These checks are separate from the historical model/provider
acceptance below and source deployment. The original Mathem purchase and local
notices are now demonstrated; no real notification recipient was used. MENY
detail availability remains unknown where not supplied. Remaining Mathem gates
stay open under #50.


## Native Mathem order and recovery-assisted addition — 2026-09-08

The same ordinary Bob Hermes conversation continued through the installed skill,
MCP, Application and dedicated Mathem browser. The original automatic purchase
used candidate06, source digest
`b1772a935cb3919af92db439630d1ffb28869d1a6b32102ff5b20167ffac34f5`.
Addition reconciliation used candidate09: public `a677466e` plus nine task-owned
runtime overlays, with 143-file source digest
`824175836e0977ab03b9ed283ae3dd6528b17f9e4cd225dfe667d5f29b567cc6`.
These were isolated validation candidates at the time of the conversations;
the final published-source verification is recorded below.
Bob retained its normal Oda gateway and the scoped CLI used the Mathem socket.

The model applied the complete approved product plan once: 12 products and 17
packages for two batch sources and one fresh dinner. Nine explicitly synthetic
pantry groups supplied the declared shortfalls; physical stock and safe cooking,
freezing and storage remain unverified. The approved tomato substitution was
carried into the final product assessment. The native conversation selected
13 September, 14–16 Europe/Stockholm, and reviewed 564.01 SEK: 482.52 merchandise,
24.51 product discount, 99 small-cart fee, 7 bags and 79 delivery offset by 79
free-delivery credit. Deposits remained unknown. The final review bound the
original OAuth/browser account, receipt address, delivery and selected saved card.

A synthetic sesame-allergy rule exercised the final-product unknowns. Eight
products supplied retailer fields and four lacked usable detail; no absence of
sesame was inferred. Twelve exact standing uncertainty permissions covered these
final products. One owned native systemd timer invoked the ordinary Hermes
conversation at 03:57 Stockholm for occurrence2026-W37, attempt1. This is a
systemd-owned worker, not a Hermes-native-cron claim. It paused for the required
local notice. The 2,052-byte pre-dispatch payload was delivered once, read back
and acknowledged before the same occurrence continued.

There was one purchase dispatch. Its first return was unconfirmed; one explicit
reconciliation and independent provider reads established the exact original
order, goods, amount, delivery and `paid_and_modifiable` status. That initial
unconfirmed response is not evidence of a lost HTTP response. Bank authorization
and charge remain unknown. The 2,226-byte result notice was delivered and
acknowledged once through the same local inbox. No network recipient was used.
The saved native final reply agreed with the structured result.

The same conversation then exercised bound change begin, empty abort, begin
again, and an already-satisfied minimum quantity without a write. One explicit
additional package of the already ordered pasta was staged once. Native prepare
reviewed original17/564.01 SEK, added1/18.50 SEK and combined18/582.51 SEK with
the same account/address/card/delivery. Its exact dietary finding retained the
applicable standing permission. A new 280-byte local pre-dispatch notice was
verified and acknowledged. One addition-payment submit and one reconciliation
returned unconfirmed with retry disallowed. Fresh provider reads showed the
unchanged original order, empty cart and `unpaid_order_change`. The exact order
page explicitly reported that payment for the latest added goods failed.

At that point the failed attempt remained unresolved in its application journal;
there was no restaging or invented successful result notice.
The merchant's unique recovery link was bound on the original host to the same
order and failed change, then opened once without a final payment click. Its
review showed the same18.50 SEK and saved card. No bank/device challenge was
observed, and no cause of the failed payment has been established. The local
reconciliation fix now defers receipt/account navigation until a potentially
accepted provider result, preserving any unpaid checkout or bank approval page.
Focused tests cover this behavior for original orders and additions, as well as
lost-response/restart/expiry/drift with one dispatch.

One separately scoped operator-assisted recovery then completed the merchant's
exact failed change through its existing payment page. An independent fresh
account/receipt check and final UI guard verified the same saved card, address,
delivery and18.50 SEK. The recovery page omitted product rows; its goods binding
was the preserved original one-package review and exact merchant change
reference. This limit is explicit. The guard was first exercised without a click,
then one recovery payment was dispatched. No new cart, order or change was
created. The immediate read remained unconfirmed; a later independent provider
read showed all original goods plus exactly one extra pasta package,18packages,
582.51 SEK and `paid_and_modifiable`, with the same delivery. Bank authorization
and charge remain unknown. This establishes a recovery-assisted accepted
addition, not an unassisted native addition-payment success. The original failed
native attempt, its notice and the separate recovery dispatch are preserved.


## Native free delivery change — 2026-09-08

The same conversation began an edit of the reconciled 18-package order and
selected the freshly available 10 September, 14–16 Stockholm window through
MCP. Authenticated inspection and one scoped UI selection verified that exact
free window in the original order's delivery dialog. The first native prepare
stopped before confirmation because a collapsed summary hid the original-order
row. Candidate11 requires all three review rows before proceeding; its source
digest is `c020eb76f9e3a686c87d3c2610d4c6ce63b9aef531137c4e9c544128c4b31f3f`.

A new ordinary native prepare bound the original account/address/card, unchanged
18 packages, original and combined totals of 582.51 SEK and additional payable
0 SEK. One native confirmation returned unconfirmed; one reconciliation verified
the changed delivery with the same goods and total. The cart was empty, with no
cart delivery or pending change. This result includes operator-assisted UI
preparation; it is not evidence of an entirely unassisted preparation sequence.
Paid or refund-dependent changes and dates outside the available UI remain
unsupported. The old 9 September reservation's release and all bank outcomes
remain unknown.


## Native cancellation and retained failures — 2026-09-08

The same ordinary conversation prepared the exact test order for cancellation.
The first confirmation omitted `order_id` and stopped at input validation. The
second supplied both IDs but stopped before the final click: JSON object key
ordering changed when the review passed through the state journal. Independent
provider and browser reads verified identical receipt values, consequence and
an active unchanged order. Neither attempt dispatched cancellation.

Candidate12 compares the same receipt keys and exact field values independently
of JSON object key order. Its 143-file source digest is
`55ee8a0523b475ec1939b94a90caad69ceb17969cd1ce4e055161cff24605fb4`.
A focused real-StateStore/final-JavaScript test accepts persisted key ordering
and rejects changed amount, delivery, deadline or extra fields before clicking.
All 55 focused Mathem tests pass in both private and public layouts, and the
private fleet profile passes. The native tool result and maintained guidance
now specify both order and confirmation IDs.

A fresh native preparation succeeded; its subsequent CLI exit134 remains an
unresolved process outcome. The exact persisted review was retained and checked
before continuation. One native `cancel_confirm` then returned `cancelled=true`,
with retry disallowed, and the normal model reply agreed (CLI exit0). A separate
provider read confirmed the cancelled test order with its original 18 packages,
582.51 SEK and 10 September delivery record retained. The unrelated historical
order was unchanged; the cart was empty with no delivery, and no checkout, cart,
order-change or cancellation operation remained pending. All four local
purchase/addition notices remained delivered and acknowledged. A cancelled
order does not establish bank authorization, charge, refund or release; those
remain unknown. The earlier negative cancellation verification is preserved.


The completed occurrence was subsequently disabled through the ordinary SDK
schedule path. Its exact native systemd links were stopped/removed and their
absence acknowledged to the same scheduler binding. Unit sources, occurrence,
notices and all other household state were preserved. An initial cleanup check
stopped before mutation because systemd reported the symlink path instead of
its resolved source; that failed observation remains recorded.


## Published source and runtime verification — 2026-09-08

Feature revision `b8cc15f3cbaa54fc86d14d1c921e69da6c423503` was published after
independent correctness and adversarial FINAL APPROVE of the exact change and
bounded candidate evidence. All 143 files downloaded from GitHub's revision
archive matched the approved public checkout; source-manifest digest:
`b54c51135578b112e799c51294093d0e89f42516ec26d491c84fd204f0e8a8b8`.
The final image and dedicated Mathem service verified these same 143 files,
both original state/database files, unchanged configuration and installed-client
reconnect. The first upgrade precheck stopped before build or service mutation
because successful cancellation had normally removed the menu's active order
reference. Its continuation checked the retained cancellation confirmation and
draft menu; no order effect was replayed.

Bob's normal MCP/skill mount and Oda sidecar were then upgraded to this exact
published release. Bob retained its Hermes image, normal Oda socket and trusted
configuration. All 143 source files in both containers matched, both installed
Oda and Mathem SDK connections discovered 27 tools and completed status, and Bob
was healthy. All 1,577 existing Oda state/database/assets/metadata files were
byte-identical after update. The dedicated Mathem service, shared browser and
Signal container identities/start times were unchanged during this Bob upgrade.

Bob's authorized stop returned exit1 without OOM; the deployment stopped before
configuration replacement. The stopped identities, absence of pending work and
database recovery journals, unchanged original Compose and exported source
were independently checked before completing only the unrun replacement. The
first deployment journal and its stop result remain preserved. This continuation
did not replay a stop, order or payment and did not restore older household state.


The retained unauthenticated Mathem installation completed a normal stopped
`install.sh update` from the published image. Its actual 52 staged runtime/skill
files matched the public archive. Configuration, household state and all 1,576
recipe/database/assets/metadata files were unchanged, retaining the 4,599 recipe
IDs and versions. The normal service ran and the installed SDK discovered all
27 tools and completed status. The immediate post-stop ownership check failed;
a later ordinary acquisition of the same locks verified natural release and
unchanged data. No locks were removed and neither update nor service execution
was repeated. Original failed-cleanup journals remain, alongside the separate
verified completion. The task-only fixture container is stopped and retained.

Finally, the same ordinary Hermes conversation used the newly installed public
skill and MCP for four read-only calls: status, schedule, cart and the exact
cancelled test order. The persisted results verified Mathem guarded checkout,
disabled/removed scheduling, an empty cart with no delivery and the cancelled
582.51 SEK order. The final model reply retained unknown bank outcomes and
matched these results (CLI exit0; reply SHA256
`4f7d38f988d52d8d0cb754a433c455f66214db60cdf152bb048f3b267a0c7156`).
The owned temporary noVNC SSH forward was stopped; browser, proxy and VNC
services were unchanged.
