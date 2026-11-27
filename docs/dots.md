# ChatGPT Dots

Use Meal Concierge on your Dot’s cloud computer. Its foreground runner opens
the same private household for each command. No persistent service or MCP
registration is required. Your recipes, menus, shopping lists and command
receipts live in that household between turns.

## Install with your Dot

Paste this into your Dot:

> Set up Meal Concierge on your own cloud computer from
> https://github.com/poisdahl/meal-concierge. Follow docs/dots.md at a published
> stable program release that includes foreground.py. Start with meal planning
> and shopping lists. Reuse my existing foreground household if present.
> Load the shared skill, verify the tools, report the release and exact commit,
> and show me the setup question. Keep all existing households and data.

If you are testing a pull request, give your Dot the **full reviewed commit**
and ask for a separate test installation at that commit. Use that commit’s
instructions. Report it as a test build; an untagged commit is not a published
stable release. For a normal install use the
[published program release selection](runtime.md#choose-a-program-release).
If no published release has this runner, report that before installing.

Use a private owned directory in the Dot’s retained cloud workspace, such as
`/workspace/shared/meal-concierge`, with separate directories for the source
checkout, program and household. Use an explicit home on every command. Avoid
`/tmp` for anything needed between turns. Check the intended home and established
Dot installation locations before creating one. If the existing home has the
foreground runner, discover its tools and reuse it. For a requested update of
an older store-free external installation, use the update procedure below. A legacy `dots_session.py`
household uses a different layout and ownership model; keep using its original
commands and registry. Never open it as an installer-managed household.

### First use

Run these commands on the Dot from the selected, clean Git checkout, with Python
3.10+ and `uv` available. The installer stages its own hash-pinned Python runtime.
If the Dot’s HOME is read-only, put `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR`
under the private writable workspace when invoking the installer.

```sh
./install.sh install --manager external --provider none --household 'My Dot household' \
  --home /workspace/shared/meal-concierge/household \
  --code-root /workspace/shared/meal-concierge/program

program=/workspace/shared/meal-concierge/program/current
household=/workspace/shared/meal-concierge/household
"$program/venv/bin/python" -I -B "$program/foreground.py" --home "$household" tools
```

Keep the existing home and its exact program path in your Dot’s setup context.
Load the `skill` path returned by discovery and bind it to the household’s
[foreground commands](../skill/references/dots.md). If your Dot offers a cloud
skill installer, install or refresh the shared skill and its references using
that supported path, with the binding for this household. Otherwise have the
Dot read the installed files. Check discovery and use in a later turn.

Use a new canonical UUID for each new command. `call` reads one JSON request
from stdin. Start by reading `setup.show`, then present the returned question.

```sh
printf '%s\n' '{"operation":"setup","action":"show"}' | \
  "$program/venv/bin/python" -I -B "$program/foreground.py" --home "$household" call REQUEST_UUID
```

Read `status` with a new UUID for a fresh build or household check. Use
`receipt REQUEST_UUID` to recover the previous result and its build identity.
Identical input with that UUID returns the historical receipt. Different input
conflicts. A recovered receipt grants no new native send authority and checks
whether an exported file still matches its original bytes.

### Store availability

The planning installation uses `provider=none`. A separate hosted Oda
installation can search products and recipes and read the cart, delivery
windows and orders when the official Oda plugin is callable in the Dot.
It can also [shop a saved planning menu](#oda-saved-menu-shopping) into an
owner-designated cart, preserve existing items and reserve a reviewed delivery
window in a new cart. Checkout and eligible order changes continue in the shop. Follow the
[hosted Oda setup](#oda-cart-reads).
The planning
installation can also search the [public MENY catalog](#public-meny-catalog).
Full MENY shopping uses the shared store runtime with a working dedicated browser.

The separate [MENY native-browser interface](#native-browser-requests-over-stdinstdout)
supports native-browser reads and guarded cart changes where an operator can
verify the owner-approved account and tab. It covers catalog search, cart,
saved-menu product planning, delivery-window reads and rendered order reads.
On the test Dot, signed-in recipe and product searches, ordinary cart and
order reads, and delivery-window reads passed. A one-package add passed an
independent cart read. During the installed removal, the operator's scoped
trace showed a pre-click stop at the cart readiness check. The original
command retained a generic error and its dispatch outcome stayed unknown.
The operator then removed the test unit once in the signed-in browser. A
fresh installed read confirmed the empty baseline.
The pending cart guard still applies to native catalog and order `call` commands.
If the original cloud browser tab is gone, use a [read-only observation](#current-meny-reads-while-a-write-is-pending)
to see the current cart and orders on the approved account. An older attempt
without a dispatch record may qualify for an [owner-approved local reset](#reset-a-legacy-meny-cart-guard).
Shopping-list matching and guarded cart changes have fixture coverage. The
native client has [reviewed home delivery selection and owner handoffs](#meny-delivery-and-owner-handoffs).
Keep each store in its original private household.

### Public MENY catalog

The foreground planning installation can search MENY for products and recipes
and open a recipe returned by search. Use the same `call REQUEST_UUID` command
and one of these JSON requests.

```json
{"operation":"catalog","provider":"meny","action":"products","query":"havregryn","limit":5}
{"operation":"catalog","provider":"meny","action":"recipes","query":"fiskesuppe","limit":5}
{"operation":"catalog","provider":"meny","action":"recipe_get","recipe_id":"EXACT_RECIPE_PATH_FROM_SEARCH"}
```

Results have `scope:"public_catalog"`. They use fixed anonymous HTTPS requests
from the Dot. Prices are public catalog figures per retailer unit. A cart plan
still needs signed-in product availability, fulfillment and a fresh price review.
If the public response is unavailable or incomplete, the command reports an
error. `observed_at` records when the public result was read. A replay reports
`historical_public_catalog` and keeps the original observation time when known.
Use a new request for a fresh search. Follow the native-browser steps for a
signed-in read or a pending cart operation.

### Oda cart reads

Connect [Oda’s official MCP endpoint](https://oda.com/mcp?lang=en) through
ChatGPT Plugins and approve the account privately. Attach that exact installed
plugin in the Dot’s conversation. A plugin appearing in the picker does not
establish that the Dot can invoke it. A successful call is required on each
fresh read. The Dot uses the connection ChatGPT holds for this plugin.

Install a **separate** external Oda home and program. Use the plugin ID from the
connected entry’s ChatGPT `/plugins/<plugin_id>` page. The ID identifies the
entry. Keep the original planning installation.

```sh
./install.sh install --manager external --provider oda --oda-plugin ACTUAL_PLUGIN_ID \
  --household 'My Oda household' \
  --home /workspace/shared/meal-concierge-oda/household \
  --code-root /workspace/shared/meal-concierge-oda/program

program=/workspace/shared/meal-concierge-oda/program/current
household=/workspace/shared/meal-concierge-oda/household
"$program/venv/bin/python" -I -B "$program/foreground.py" --home "$household" tools
```

Paste to the Dot after connecting Oda:

> On your cloud computer, set up a separate Meal Concierge Oda household using
> the hosted Oda instructions in docs/dots.md. Use my attached official Oda
> plugin and the exact build selected for setup. Keep my original planning
> household. Then read my Oda cart through the installed Meal Concierge tool.

Use `call REQUEST_UUID` with `{"operation":"cart","action":"get"}`. The
installed tool records the request before returning `awaiting_hosted_read`.
Run the exact attached plugin’s emitted `get_cart` with the given arguments
once. Complete that same request within its expiry with `complete-hosted
REQUEST_UUID`, passing one JSON object on stdin. Supply `read_id`, `plugin_id`,
`tool` from the request, and `observed_at` as the actual UTC observation time.
Put only `groups`, `productQuantityCount`, `totalGrossAmount`, `totalBreakdown`,
and `url` from the structured tool response into `cart`. Pass these fields as
received. Keep account and delivery details out of the handoff. On an unavailable
tool or authorization failure, use `error:"unavailable"` instead of `cart`.
For a successful cart read, `arguments` may be omitted or set to `{}`.

The runtime checks the original request, installed program and environment,
the echoed plugin ID and expiry, the cart quantities and the recognized fees
and discounts. For an empty cart or a cart without adjustments, it also checks
the sum of the lines against the subtotal. `host_supplied_oda_mcp` identifies a
result supplied by ChatGPT. The invocation and observation time are host-reported. The runner
checks the plugin entry ID, and the owner confirms the underlying Oda account
when connecting or reauthorizing it. Report backend freshness as unverified.
`receipt REQUEST_UUID` recovers the historical outcome without saving cart
contents. `hosted_read_expired` means a new read can start with a new request
ID. Large carts show `lines.truncated` and their full count.
Open the returned cart link for the complete contents and to finish checkout
in Oda. Update the same program and home with the foreground update procedure.

#### Oda product and recipe searches

Use the same hosted Oda household and a new request ID for each search.

```json
{"operation":"catalog","action":"products","query":"havregryn","limit":5}
{"operation":"catalog","action":"recipes","query":"pasta","limit":5}
```

Invoke the emitted `product_search` or `recipe_search` in the attached plugin,
with the exact arguments. For `complete-hosted`, pass the same request, plugin,
tool and time fields as for a cart read. Echo the frame’s exact `arguments`.
Put the structured search result into
`catalog`. Product results include package and price observations when supplied
by Oda. Recipe results include the exact recipe ID and the Oda link when present.
Completed search receipts keep only the status and request metadata. Search
results do not authorize a cart change or a purchase.

#### Oda delivery windows and orders

Use `delivery.list` with an optional ISO calendar `date` to read windows for
the current Oda delivery context. Use `orders.list` to read recent order
summaries, then `orders.get` or `orders.track` with the exact `order_id` from
the list. Echo the frame’s exact `arguments` and put the structured response
in `delivery` or `orders`, respectively, when completing the original request.
Each call has a new request ID.
`orders.track` reports the status supplied by the Oda plugin. Keep the results
in the owner’s chat. The receipt stores request fingerprints and metadata.
These private fingerprints may be guessable for short order IDs or queries.
For a new Oda cart, discover `delivery_reservation_command` and follow
[Reserve one Oda delivery window](#reserve-one-oda-delivery-window). For a
previously reserved cart or an existing order, use the signed-in Oda shop.

##### Reserve one Oda delivery window

Use the same private hosted Oda home, attached plugin and dedicated cloud
browser. List the offered windows first. Choose one dated `slot_ref` to review.
Start a new operation with `foreground.py --home "$household" hosted-cart
OPERATION_UUID`. Send `begin_delivery` with the exact `slot_ref` and
`authorized_addresses`. Project the attached app’s current delivery-address
list to `{ "id", "isSelected" }` objects with exactly one selected address.
Reuse the original UUID throughout.

1. For each emitted read frame, call the attached app’s exact
   `get_delivery_addresses`, `get_cart` and `get_delivery_slots`, in order.
   Verify the original dedicated signed-in browser tab. Its visible Oda address
   edit link must contain the same address ID and its ordinary cart must show
   no order edit. Send `observe_delivery` with `frame_id`, `plugin_id`, the
   projected `addresses`, the selected address’s `address_label`, and
   `address_observed_at`, `cart_observed_at`, `slots_observed_at` from each
   real observation. Set `browser` to the observed `browser_id`, `tab_id`,
   integer `address_id`, `new_cart: true` and exact Oda `cart_url`.
   Send the structured dated `slots`, exact `slot_arguments`, and the
   complete cart projection (`groups` with all ordinary items,
   `productQuantityCount`, `totalGrossAmount`, `totalBreakdown`, `url`, explicit
   `deliverySlot`, `deliveryAddress` and `isUnattendedDelivery`). Use `null`
   for documented optional fields known to be absent. Keep address, account
   and tab details private.
2. Present the exact calendar date and local window, listed fee, current cart
   totals and whether Oda shows unattended delivery. Ask the owner to approve
   that reservation. Use `approve_delivery` with its `review_digest` after the
   owner approves that specific offer. Complete the new read frame with fresh
   observations. A changed context or offer needs a fresh review. `abort` ends
   an operation before any dispatch.
3. Only a newly emitted `dispatch: true` grants one `select_delivery_slot`
   call. Invoke its exact arguments before expiry. Send `acknowledge_delivery`
   with the original `frame_id`, `plugin_id`, `tool`, `arguments`,
   `dispatched_at`, `observed_at`, `result_source: "original_attached_mutation_response"`,
   `is_error` and the original
   structured `slot` (or `null` for an actual provider error). Complete the
   returned read frame independently. Check the selection and actual cart fee
   before reporting the result. Payment, a purchase and order changes need
   separate authorization.
4. After a lost reply, retain the operation and wait for the original dispatch
   frame to expire before using `recheck`. Complete only the new read frame.
   Report the observed selection separately from the original outcome. A
   provider error or an expired window can be reconciled by fresh same-account
   evidence that the original slot is no longer selected. The receipt records
   whether the cart also matches its baseline. Keep an unresolved operation
   pending while the evidence remains uncertain. `status` returns historical
   evidence without a new dispatch. Re-read the current cart before checkout.

If the original frame has expired and its executor has stopped, the owner can
accept archiving that specific inactive attempt with its outcome unknown. Use
`accept_delivery_current` with the exact `review_digest` and both
`owner_accepted_unknown: true` and `original_executor_stopped: true`. Verify the
owner’s original account using its original opaque address reference in both
the attached app and the dedicated browser. If the current selected address has
changed, the original saved address set and dedicated tab must still match. The
fresh cart and dated slot reads must show that the target is absent from the
observed selection. A different visible window is reported separately. This
archive grants no new reservation and does not attest what happened earlier.
If the original tab cannot be verified, `recheck` returns to ordinary recovery
and can record the inactive window after the cutoff from fresh original-account
evidence. If the original account is unverified, keep the original operation.

Oda’s app supplies account and cart observations through the host. Use the
shop for checkout and payment. [Official Oda MCP specification](https://oda.com/mcp/manifest/)

##### Oda checkout and order handoff

Paste this prompt into a Dot with native merchant checkout.

> Review my Oda basket and delivery and show me the exact saved-card total.
> After I approve that purchase, submit it once in your cloud browser. Hand
> me the browser privately if my bank needs approval.

For a saved-card purchase on the Dot, start `begin_saved_card_checkout` with
a new `hosted-cart` UUID, the selected `slot_ref` and fresh
`authorized_addresses`. Use the original authenticated account and its sole
active checkout in the dedicated cloud browser. Reconcile any earlier attempt
or retire any earlier handoff before starting. Complete the attached-app
delivery observations in `observe_delivery` together with `checkout` from
the actual rendered payable page:

- `url`, `browser_id`, `tab_id`, `new_order`, `exclusive_checkout`,
  `address_label`, `delivery` and `observed_at`. Verify the same ordinary
  cart, address and exact dated delivery window. Only report exclusivity
  after checking for another active checkout or owner takeover.
- `rows` with each `product_id` (observed string or null), `title`, `subtitle`
  and integer `quantity`, and `amount_rows` with each observed `label` and
  `value`. Read the complete sections and all merchant `warnings`.
- `payment` with `method: "saved_card"`, `selected` and `masked_card` (the
  visible masked suffix). `submit_control` contains the exact payable
  `label`, `enabled` and `count`. Keep payment details private.

Present the merchant, complete basket, saved-card choice, delivery and full
payable total. Use `approve_saved_card_checkout` with the exact `review_digest`
and `approved_total_ore` only after that purchase has the owner's approval.
Complete the new observations. Include the raw result of the emitted
`get_orders` as `existing_orders` and `orders_observed_at`. Use the Dot's
documented checkout reporting. The operation UUID identifies the local
attempt before a host report ID exists.

Only the new `dispatch: true` frame with `submit_saved_card_checkout` allows
one click on the original tab. Immediately compare the entire fresh review
to the approved review before clicking. Honor any native approvals, expiry
and payment warnings. Use private takeover for bank approval. Do not click
again after a lost reply or pending payment. Capture the exact original
merchant confirmation immediately with `recheck_checkout`. Its `confirmation`
object contains `url`, `order_number`, `order_link` (use `null` if absent),
`browser_id`, `tab_id`, `attempt_id`, `observed_at` and `native_report_id` (use
`null` until available). The original merchant retry page can identify the
order for readback. It grants no payment retry or proof that the order failed.
Complete `observe_checkout_order` with the fresh app detail, tracking and
address list, plus the exact browser receipt on the same account. Once the
original confirmation is recorded, a same-account replacement in the same
cloud browser can read the receipt if the original tab retired.
Its `browser` fields are `url`, `browser_id`, `tab_id`, `attempt_id`,
`order_number`, `delivery`, `payment_label` (or null), `address_id`,
`address_label`, `total_ore` and `original_tab_retired`. Supply the emitted frame identity, `order`,
`tracking`, `addresses`, `address_observed_at` and `observed_at`.
Reissue that read with `recheck_checkout` if needed. Use only the already
recorded order. Attach a late `native_report_id` to that same readback when
available. The permit must expire before readback releases the checkout.
Merchant order acceptance and card authorization are separate observations.
If the verified original order differs from the approved goods or amount,
report both the reviewed and observed result and any visible payment state.

For a review or owner handoff, run `hosted-cart` with a new UUID and
`begin_checkout`.
Pass the current selected `slot_ref`, fresh `authorized_addresses` and
`payment_method: "saved_card"` or `"vipps"`. Use `owner_handoff: true` when
the owner is actually taking over. Complete the same independent
read frame as a delivery review. `checkout_handoff` returns the app cart,
selected window and merchant cart link for review. `checkout_owner_pending`
keeps a real owner handoff exclusive. Offer takeover only when the fresh
`observe_delivery` reply includes `owner_handoff_ready: true` and the current
time is before `owner_handoff_admit_by`. Include
`existing_orders` and `orders_observed_at` from its emitted order-list read.
If a held window has sold out to other shoppers, `delivery.list` can return
its reference as `current_selected`.
`checkout_cart_empty` or
`checkout_delivery_required` tells you what to resolve first. Reopen the link
in the dedicated signed-in browser and compare the complete cart, delivery,
prices, payment choice and payable total in the shop. Use the Dot’s private
browser handoff for the owner to complete checkout and any bank or Vipps
approval after the exact payable review agrees with the recorded cart and
the owner's approval. The app's receipt contains its cart amount. Confirm
the actual payable amount in the shop. Retain and reconcile the original
attempt before switching between owner and Dot submission. A historical
receipt does not reopen a handoff.
If the owner completes that handoff on another device, ask them for the exact
original Oda success link in their private chat. Use a replacement tab in the
same cloud browser and supply `owner_confirmed_original: true` and the observed
retirement of the old checkout. A similar order from search or history cannot
identify the original attempt.
In the verified ordinary cart, Oda’s `Fortsett` opens the review flow.
For an owner handoff, leave `Bekreft og betal` and payment controls to the owner. If the
shop does not show the requested method, tell the owner what is available
and let them choose. [Oda order and payment controls](https://hjelp.oda.com/no/article/100639)

An attempt that definitively stopped before a click can be closed with
`recheck_checkout_unsubmitted` and then `observe_checkout_unsubmitted`.
Wait for any permit to expire. Supply the exact `attempt_id`,
`original_executor_retired: true`, `owner_confirmed_no_submit`, the report ID
if available and the reason observed on that original attempt. Use
`host_blocked_before_click` or `checkout_click_never_called` for native
submission. Use `owner_returned_without_submitting` with the owner's explicit
statement, or `owner_handoff_never_opened`, for an owner handoff. `retirement`
contains the original `browser_id` and `tab_id` and actual observations
`original_tab_retired`, `owner_handoff_ended` and `no_pending_bank_action`.
Complete fresh original account, cart and order-list reads. A timeout, missing
reply or absent order alone leaves the original operation pending.

Use `defer_checkout` after the permit expires to select a compatible program
release while the original checkout remains pending and further writes stay
blocked. Do not switch to a release that cannot preserve its checkout journal.
After the original delivery window ends, the owner can explicitly accept
archiving the unresolved purchase as `unknown`. Use `recheck_checkout_archive`
with the exact `attempt_id`, `reviewed_total_ore`, `owner_accepted_unknown: true`
and the observed `retirement`. Complete its original-account cart and order
reads with `observe_checkout_archive`. If the original order is linked,
perform the emitted detail and tracking reads for that exact order. Return
`order`, `tracking` and `order_observed_at`. Use `null` for an attempted
read that returned no evidence. Any unlinked candidates are evidence
for the owner, with no claim that the purchase succeeded or failed. On an
exact linked order, a missing or unfamiliar payment label preserves any
earlier disagreement. A recognized payment label that agrees with tracking
can resolve it. The archive preserves the earlier label and reports any
disagreement with fresh tracking for the linked order. Any later purchase
needs its own approval and window.

[OpenAI guidance on saved cards and private sign-in](https://help.openai.com/en/articles/20001529-dots-privacy-security-and-safety-faqs)

For an existing order, start `orders.change_handoff` with its exact `order_id`
through `call`. Use the emitted `order_tracking` request and, for a
`paid_and_modifiable` result, the additional `get_order` and
`get_delivery_addresses` reads. In the dedicated browser find the exact order
receipt and the current account’s unique visible delivery address edit
reference. In `complete-hosted` pass the original tracking payload in
`orders`, structured detail in `order`, projected `{ "id", "isSelected" }`
rows in `addresses`, and `browser` with `url`, `browser_id`, `tab_id`,
`address_edit_path`, `order_number`, integer `total_ore`, `delivery_date` and
the visible, enabled `available_changes` among `items`, `delivery` and
`cancel`. For an ineligible status, supply `null` for the three additional
fields. Open the original order through the validated link for the owner to
review and complete the change in the shop. Check that exact receipt afterward.
If a cart or delivery operation is pending, reconcile it first.

#### Oda saved-menu shopping

Keep your original store-free planning home and separate hosted Oda home.
Use your original official Oda connection and a new-order cart that you
control for the whole operation. The cart can contain up to 32 unique,
ungrouped product lines and must have no delivery slot selected. Existing
packages of the chosen products count toward the menu's needs. Existing
unrelated items stay in the cart. This workflow covers up to eight measured
menu ingredients, one distinct fixed-size product per ingredient, and up to
24 required packages. The saved shopping list and its recorded pantry
decisions determine the quantities.

The private journal and archived receipts retain the bounded baseline's product
names, package details, quantities and prices for review and recovery. Keep
these files in the private Dot workspace.

> Prepare my saved Meal Concierge menu for Oda. Use my existing planning
> household and the Oda app attached to this chat. Show the ingredient
> matches, packages already in my cart, packages still needed and merchandise
> prices. Keep the items already in my Oda cart. Ask me to approve the exact
> additions before changing the cart, then verify it and give me the cart link.

Use `shopping_list.get` in the planning home. Read the complete list and stock
review. Resolve unknown quantities and explicitly review any unanswered pantry
rows. The planner uses only the recorded stock and menu availability. Each
stage below runs on the selected Oda program with private JSON on stdin:

```sh
"$program/venv/bin/python" -I -B "$program/foreground.py" --home "$household" hosted-cart "$operation_id"
```

1. Keep one new canonical operation UUID throughout. Read the owner-authorized
   selected-address projection as for the one-item test below. Start with
   `action:"begin_menu"`, the original absolute `planning_home`, current
   `menu_ref`, `list_ref`, and `stock_ref`, `isolated_new_cart:true`,
   `authorized_addresses`, `preserve_existing:true` and `mode:"keep"`.
   Use `mode:"restore"` for an authorized test that should immediately undo
   its additions. The mode stays bound to that operation.
2. Invoke the emitted address, product search and cart tools in order using
   the same attached Oda app. Submit `action:"observe_menu_search"`, the
   `frame_id` and `plugin_id`, `address_observed_at`, `products_observed_at`,
   `cart_observed_at`, projected `addresses`, the exact search arguments as
   `catalog_arguments`, the complete structured search result as `catalog`,
   and the complete current `cart`.
3. Review the culinary match and choose one exact observed `product_id`
   for each `requirement_id`. Send `action:"select_menu"` with a `choices`
   object mapping those requirement IDs to numeric Oda product IDs. The
   returned `quote` lists coverage, package counts already present and still
   needed, merchandise prices in øre, unanswered stock rows and any dietary
   evidence gaps. Show the owner the exact additions and resulting merchandise
   subtotal. `package_count` and `merchandise_ore` describe the full menu
   requirement. The `additional_*` fields describe the cart change. Deposits,
   discounts, fees and delivery appear separately in the verified cart. An
   overlapping cart line must agree with the selected catalog price and pack.
4. After the owner's explicit approval of that quote, send
   `action:"approve_menu"` with the exact `review_digest`. Use the normal
   `observe`, `add` and `acknowledge`
   handoff below. The read frame after `add` requires the fresh product
   search between the address and cart reads. Include `products_observed_at`,
   `catalog_arguments` and `catalog` on that `observe`. Changed menu, stock,
   matches, availability, package sizes, prices or cart block dispatch. Start
   a new review after aborting a changed pre-write plan.
5. Use the original mutation result and a new independent cart read to verify
   all product details, quantities and merchandise amounts. A matching keep
   plan ends as `applied`. A fully covered plan ends as `unchanged`, with no
   cart write. Share the Oda cart link. If a price or product detail differs
   and the quantities are verified, show the difference and ask before
   restoring the additions. In restore mode, use `remove` only after reading
   the original result and verifying the new quantities independently. Invoke
   the resulting inverse once. Check both `restored_lines_match` and
   `restoration_amounts_match` before reporting that the original cart and
   amounts were restored.

To undo a completed keep plan at the owner's request, use a new canonical
operation UUID and `action:"restore_menu"` with the exact original UUID as
`applied_operation_id`. The original verified application must be the latest
cart write through any hosted Oda installation in this workspace. Keep exclusive
use of the cart from applying the menu through finishing the undo. The cart
must still match the applied receipt exactly. Continue with the emitted address
and cart checks, followed by `observe`, `acknowledge` and independent readback.
The inverse removes only the quantities that receipt added. The applied receipt
remains a record of the historical application. The separate undo receipt points
to it and reports the undo's own acknowledgement and verification. Use a fresh
cart read for current contents. Older receipts created before this feature may
lack the evidence required to undo them.

After the inverse acknowledgement, `verify_remove` with
`independent_cart_read_required:true` calls for the emitted independent address
and cart reads. Submit those with `observe` on the same UUID. Use `retry_read`
if the read frame expired. The final restoration flags appear after that read.
If you defer a linked undo before it issues an inverse, a compatible program
update can abort it using the undo's own UUID.

If the original successful add returned only an exact subset of the approved
products and quantities, check `partial_add_quantities`. A fresh matching
read can authorize cleanup of that subset. An unapproved addition,
unverifiable result or lost reply keeps the original operation fenced. The
same recovery and maintenance rules below apply. A deferred operation with
existing items or a linked restoration requires a program that understands its
cart baseline for update or rollback. Keep that program available for archived
menu receipts.

#### Oda one-item test cart

This check adds one exact product to an empty, unreserved new-order cart, then
removes that unit. Use it only when the owner designates that cart for the test
and leaves it to the Dot until the original quantities are verified. Keep the
same attached official Oda plugin and authenticated account throughout. Start a
fresh session for a changed connection after resolving any existing operation.

> Use the Meal Concierge Oda household on your cloud computer to test the
> attached Oda plugin. I designate its active cart for one reversible test.
> Check that it is an empty new cart with no delivery slot, add one unit of an
> ordinary packaged product, verify the quantity with a fresh cart read,
> remove that unit, and verify that the cart is empty again. Use the same
> Meal Concierge operation for the whole test. If a result is uncertain,
> inspect the original operation before any further action.

Keep one canonical operation UUID throughout. Run the installed command on
the Dot with a private JSON object on stdin for each stage:

```sh
"$program/venv/bin/python" -I -B "$program/foreground.py" --home "$household" hosted-cart "$operation_id"
```

1. Read the selected-address context using the same owner-authorized
   plugin and find one exact in-stock product with `catalog.products`.
   Use `{"action":"begin","product_id":123,"isolated_new_cart":true,"authorized_addresses":[…]}`
   with the actual numeric product ID and the address response projected to
   `id` and `isSelected` for every returned entry. Keep those values private.
2. For each read frame, invoke its exact `get_delivery_addresses` then
   `get_cart` in the attached plugin. Send `action:"observe"`, the frame’s
   `frame_id` and `plugin_id`, actual UTC `address_observed_at` and
   `cart_observed_at`, projected `addresses`, and `cart`. Preserve the five
   cart fields listed above and the original `deliverySlot`. Supply explicit
   null only when the complete provider return establishes that there is no
   slot. Preserve the original group metadata.
3. Use `action:"add"` after `baseline_verified`. Observe the pre-dispatch read
   frame. Call `manipulate_cart` once with the exact arguments from the
   resulting `dispatch:true` frame, within its expiry. Use
   `action:"acknowledge"` with the original `frame_id`, `plugin_id`, `tool`,
   `arguments`, actual UTC `dispatched_at` and `observed_at`,
   `result_source:"original_attached_mutation_response"`, `is_error:false`, and
   `cart` from that actual mutation response. Supply the actual
   `invocation_id` if available or null. A confirmed provider terminal error
   uses `is_error:true` and `cart:null`. A transport failure or a missing reply
   is uncertain. A later cart read cannot serve as the original response.
4. Complete the new independent read frame. After `added`, use
   `action:"remove"` and the same pre-dispatch, acknowledgement and fresh
   readback sequence for the one negative delta. Report success at
   `restored` with both acknowledgements present, an empty cart and no slot.

`action:"status"` reads the original private journal. It never issues a
mutation. `action:"retry_read"` replaces only an outstanding read frame.
After a lost or divergent reply, use `action:"recheck"` for a fresh address and
cart frame and reply with `action:"observe_recovery"` using the observation
fields above. It records whether the bound cart is empty and unreserved,
contains just the test unit, or differs. If the original add response showed
the exact test unit and no removal was issued, a verified empty cart can close
as `reconciled_empty`. This records the observed cart without claiming a
test removal succeeded. An empty cart after a divergent, failed or missing
original response leaves the operation pending.

After an add has been issued, use `action:"defer"` to permit program
maintenance while retaining the original operation and workspace write
fence. An outstanding mutation frame must expire first. If cleanup has
started but its mutation has yet to be granted, deferral retires the old
read frame. A compatible
updated program can inspect, recheck and accept the actual original reply if
it later becomes available. Continue an authorized cleanup only with the
original, verified program and fresh bind and cart checks. Keep the original
operation, version, workspace registry, sibling identity file and household
generation marker. Restore these at their latest consistent versions. A
generation mismatch stops the cart workflow, including its status command.
If every retained record came from an earlier snapshot, use original dispatch
evidence before resuming that operation.
The attached plugin supplies the mutation results, account markers and times.
It exposes neither a cart selector nor a provider idempotency key here. This
check is limited to one ordinary item in an initially empty cart. Checkout stays
on Oda’s site.

### Attachments and scheduled planning

Use the shared [recipe delivery workflow](../skill/references/recipe-delivery.md)
to prepare the saved menu and export the frozen PDF to a private directory.
Use a fresh `recipe_delivery.begin` result with `dispatch:true` for the exact
job, part and conversation before attaching it once. Open the actual file card.
Record frozen-file acceptance only when the sender evidence binds the original
bytes. ChatGPT Library can rewrite PDFs. If staging already shows changed bytes,
stop the exact-file send. If a rewritten file was posted, report the visible
result and record `unknown` for the frozen part under its original token. Keep
the original attempt for reconciliation. For a standalone cover requested in
chat, export the specific recipe revision’s image and verify the actual card.

For scheduled planning, ask the Dot to use its native scheduler. Give it the
household binding, exact task, time zone and intended chat. Keep this schedule
planning-only. Verify a later result in the intended destination and manage it
through the Dot’s Scheduled view. Shell background execution is not a schedule.

### Updates and troubleshooting

Select a new published stable program tag and resolve it to its full commit.
From that checkout, use the same home in `prepare-program` and `update` with the
returned `--prepared-program` ID, following [bounded maintenance](maintenance.md).
Wait for active foreground work to finish. Use the same `current` path afterward
and refresh the skill binding. In the planning home, verify `tools`, `status`,
the saved menu and a prior receipt. In the hosted Oda home, verify `tools`,
reopen a previous receipt and use the attached plugin for a fresh read.
Reuse the original home for repeat setup.
Finish any active Oda test-cart operation before updating or rolling back
its program. Use the defer procedure for an uncertain ending.

Use `install.sh doctor --home ABSOLUTE_HOME` to diagnose installation markers and
version mismatches. A foreground household does not have a running RPC listener;
use `foreground.py tools` and a real call for health. If another request holds
the installation lock, wait for it to finish and check its receipt before any
retry. If state or the original
program selection is missing, recover the original installation before making
changes. Keep the household, its browser profile and its receipts together.

If a foreground request is interrupted, consult its exact UUID and inspect the
original recipe, menu or delivery job before retrying. For a pending native
MENY cart change, reconcile in the original household as described below.
Keep its target registry and operation receipt available through recovery.

Use the existing [Optional Recipe Collection procedure](runtime.md#versioned-recipe-package-integration)
in the planning home. Between foreground calls no service needs stopping.
Collection installation, withdrawals and removal remain separate from program
updates. For a failed dependency install use the original installation paths
and [installer recovery](runtime.md#recover-an-interrupted-attempt).
If uv reports `UnknownIssuer` while downloading Python, use the cloud computer’s
trusted system certificate store with `UV_SYSTEM_CERTS=true` and retry the same
paths. Keep normal certificate verification enabled. [uv certificate settings](https://docs.astral.sh/uv/concepts/authentication/certificates/)

## Standalone exports and native MENY browser integration

Meal Concierge can also run supplied recipe/menu/PDF batches in a cloud executor
without a service or a local computer. `clients/dots.py` adds read-only product
planning from facts observed by the agent's native browser. It calls the real
MENY normalizer and product planner. It does not connect Python to the browser.

Use a reviewed source checkout and its pinned Python dependencies **on the
cloud computer**. Keep its batch and observation folders separate from any
desktop installation. Do not copy browser profiles, credentials or desktop
household state. This client does not discover them or start a service/browser.

## Recipe, menu and PDF history

Create a new named batch using the [on-demand interface](on-demand.md):

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/on_demand.py" create --root "$BATCH" < supplied-menu.json
"$CLOUD_PYTHON" -I -B "$SOURCE/on_demand.py" inspect --root "$BATCH"
```

Completed batches retain the PDF and a hash-bound materialized menu, including
scaled recipe quantities. Existing batches cannot be overwritten. Changes use
a new batch. Missing or changed artifacts fail; inspection never regenerates
them. Older batches without a frozen menu remain inspectable but cannot be
used for observed product planning. Do not replay an interrupted create.

## A browser observation across agent turns

First list the exact frozen menu's ingredient requirements and suggested queries:

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" requirements --batch "$BATCH"
```

Choose one returned `requirement_id`. Issue a five-minute observation request
into a **new** private folder; the command exits immediately:

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" request --batch "$BATCH" --root "$OBSERVATION" \
  < request.json
```

Input is `{"requirement_id":"exact returned ID","limit":5}`. The result binds
the generated request ID, query, menu reference, snapshot hash and lifetime.
No process waits while the agent uses its supported native browser.

Observe the emitted query on MENY's public product/search pages. Retain actual
browser evidence. Then pass the following envelope to `plan` on stdin:

```json
{
  "request_id": "exact emitted request ID",
  "query": "exact emitted query",
  "source_url": "actual HTTPS meny.no /varer search or product URL",
  "observed_at": "actual timezone-aware observation time",
  "products": [],
  "candidate_refs": []
}
```

`products` contains at most the requested number of records. Required fields
are `product_id` (the actual `/varer/…-digits` link path) and `name`. Optional
observed text fields are `package`, `price`, `unit_price`, `original_price`,
`campaign_tag`, `campaign`, `deposit`, `detail_deposit`, `detail_price`,
`detail_original_price` and `deposit_status`. `available` is a boolean or null.
`candidate_refs` selects at most five exact observed product paths for this
requirement. Empty selection keeps the result unresolved.

Omit unknown fields or use null. Do not infer address-specific availability
from a public listing, or invent a deposit status. Existing MENY price/deposit
validation applies. For example, `deposit_status:"none"` requires matching
observed detail-price evidence, not merely absence of a deposit label.

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" plan --root "$OBSERVATION" < observed-products.json
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" inspect --root "$OBSERVATION"
```

The result returns normalized candidates, scaled requirements and unresolved
reasons. A complete exact candidate selection may return candidate totals;
each request covers one ingredient against the whole menu. Other ingredients
still require evidence. Unknown availability, package,
pricing or eligibility remains unresolved. Totals exclude delivery, bags,
cart fees and later price changes. This is a proposal among the explicitly
selected observed products, not a store-wide cheapest-product claim or an
actionable shopping authorization. `dispatchable` is always false.

The first accepted observation and its result are retained. An identical
`plan` input returns that result, even later; different input conflicts. A
partially published result stays incomplete and is never automatically
recomputed. Command ownership uses the existing nonblocking file lock.

## Combine observations for the whole menu

Issue and accept a separate request for each ingredient, then combine those
existing observation folders into a **new** private combined-plan folder:

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" combine --batch "$BATCH" --root "$COMBINED" \
  < observation-roots.json
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" inspect --root "$COMBINED"
```

Input is `{"observation_roots":["/absolute/observation-a","/absolute/observation-b"]}`,
with one to 64 existing folders and at most one accepted observation per
ingredient. All must belong to this exact batch and frozen menu. The command
reuses the real whole-menu planner: scaled quantities, package choices,
totals and unresolved requirements cover the full menu together. Omitted
ingredients remain explicitly unresolved; partial evidence cannot produce
complete candidate totals. Duplicate ingredient observations, another batch,
changed or incomplete original records fail before creating the output.

The combined snapshot retains each observation's identity, timestamp,
source URL and hashes. Combining historical evidence does not refresh prices,
stock or eligibility and never authorizes shopping. `dispatchable` stays
false. Obtain fresh observations in new folders when fresh evidence is needed.
Existing combined folders cannot be overwritten. Later inspection verifies
the original batch and observation bindings and returns the saved result;
missing or changed originals fail without reconstruction. An interrupted
result publication stays incomplete and is not replayed.

## A persistent foreground core household

`clients/dots_session.py` runs the existing core directly in one bounded
process. It provides setup, profile, supplied recipes, menus, shopping lists, feedback,
pantry, recurring items and favorites in a separate private cloud household.
It starts no socket, server or browser. Initialize a **new** root once:

```sh
printf '%s\n' '{"household":"My cloud household"}' | \
  "$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots_session.py" init --root "$CORE"
```

Initialization returns the core's setup question; it does not confirm default
preferences or contact the merchant. Then use `call` with one JSON line:

```json
{"request_id":"a new canonical UUID","request":{"operation":"setup","action":"show"}}
```

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots_session.py" call --root "$CORE" < command.jsonl
```

The request uses the existing [core contracts](reference.md). A command's
intent is saved before execution. Reusing its UUID and identical input returns
the saved result without replay; changed input conflicts. An interrupted
command remains incomplete. Inspect original state before any new operation;
do not change UUIDs to repeat an uncertain effect. Later calls require the
original private configuration, state, recipe database and ownership files.
They do not initialize replacements when anything is missing.

### Native browser requests over stdin/stdout

`catalog.products` searches a single query in the signed-in browser, with the
exact five-item rendered scope described below. `catalog.recipes` searches the
rendered recipe tab with a limit of one to five. Both are read-only and return
host-attested observations at the supplied browser observation time. Use the
same original household selection and approved account for every request.

An optional host adapter supports MENY cart `get`, `clear`, `ensure` for a reported
shortage, read-only `reconcile_change`, and complete managed menu shopping
through core `products` `prepare`, `get` and `apply`. Save the recipes and
menu in this original household, prepare against its current `menu_ref`, then
use the returned product-plan reference and exact complete digest with
`cart_change_requested:true`. Candidate selection and continuation use the
existing core contracts. Native apply attempts at most one product search per
command, including failed replies, leaving more of its 600-second
budget for guarded cart work. Remaining searches use the core's saved validation;
the generic pending diagnostic can report an unavailable search when this
deliberate command limit is reached. Cached observations retain their timestamps
within the existing validation cycle; expired cycles start fresh. One search
does not guarantee enough time: stop before dispatch if guards and readback
cannot fit. An apply result with `status:"validating"`,
`applied:false` and `cart_changed:false` may return exact `continue_arguments`
for the remaining reads. Send those arguments with a new command UUID in the
same original household, preserving the earlier receipt. The continuation may
dispatch once validation finishes, so its policy and write guards must be ready;
an uncertain prior write requires reconciliation first. Partial apply is not
exposed. `ensure` is never a
fallback for unfinished menu shopping. Cart `reconcile` supports the exact
digest-bound `keep_current` decision without exclusions or quantity changes;
use fresh prepare/apply for subsequent shopping. Ordinary apply preserves that
explicit decision, including missing goods. To explicitly restore missing goods,
add `restore_missing:true` and the exact fresh `cart_digest` from that decision
to a full reviewed `products.apply`. Preserve both fields in every returned
continuation. Fresh product validation and the unchanged approved cart/menu
binding are required; stale cart or product facts stop before writes. Existing
unconfirmed units remain protected baseline, and only new verified additions
are managed. Reconcile an uncertain effect before any new restoration; never
resend the earlier click or adopt its attribution. Read-only `delivery.list`
uses the original account's delivery picker; it does not select a slot or
change the address. [Reviewed delivery selection and owner handoffs](#meny-delivery-and-owner-handoffs)
use the original verified tab. Browser support is host-attested and requires a
reviewed native operator; this CLI does not independently inspect the browser.

`cart.clear` removes the whole observed cart. Use it only for an explicit
whole-cart clearing request with the exact current `cart_digest`, after
reconciling any pending change and checking that no other shopper's goods
would be removed. Writes must be enabled. Removal uses the existing two-click
batches and complete fresh readback. A lost reply after a complete batch can
be reconciled without dispatch; a partially dispatched batch with an unknown
ending remains pending and must never be retried. Cleanup is not guaranteed
when dispatch is uncertain.

At initialization, a browser-enabled household must supply `browser_binding`
with `origin:"https://meny.no"`, the actual cloud `browser_id` and `tab_id`,
and SHA256 identities for the approved account and cart context
(`account_sha256`, `cart_context_sha256`). Hash the actual approved context
privately; never infer identity from quantities or merely being logged in.
Do not include credentials or private account text in the configuration.
The native operator may bind actual unmasked signed-in email and phone fields
as a conservative account fingerprint, with a recorded deterministic private
canonicalization rule; missing, ambiguous or changed fields stop. This is an
observed account context, not an immutable merchant account identifier. The
same account's logical new cart can be bound using the existing MENY gate:
exact origin and `/varer`, no query/hash, one authenticated control and cart
root, zero active order codes and zero abort-edit controls. No merchant cart ID
is required. Exclude quantities, timestamps and browser/tab IDs from that
context fingerprint; the browser/tab has its separate binding.
Unknown identity stops. Configuration is frozen into the household state;
changing it cannot redirect a pending write to another account. Writes require
an explicit policy; they are disabled by default. `allow_cart_writes` sets the
initial policy. Start with it false for read-only onboarding. After the native
context and dispatch checks are verified and writes are explicitly authorized,
use the original household's receipt-bearing `call` interface:

```json
{"request_id":"a new canonical UUID","request":{"operation":"native_cart_policy","action":"set","enabled":true,"browser_binding":{"origin":"https://meny.no","browser_id":"original cloud browser","tab_id":"original cloud tab","account_sha256":"original SHA256","cart_context_sha256":"original SHA256"}}}
```

Supply the current approved binding. Use the replacement tab ID after an
approved legacy reset. This changes only the write policy in the
original configuration-bound state, under the existing root, target and state
locks. It preserves configuration, registry ownership and prior receipts.
Enabling stops while a cart change is pending; reconcile that original change
first. The same operation with `enabled:false` stops new writes without
clearing a pending journal or disabling read-only reconciliation.
`{"operation":"native_cart_policy","action":"show"}` reports the current
policy. A lost policy-command ending is not permission to replay it: inspect
the original policy and receipt. Saved command results remain retrievable after
disabling writes. Enabling grants no checkout capability and does not replace
native/provider approval or the required browser checks.

The cloud user's private `~/.meal-concierge-dots-targets` registry permanently
binds both the browser/tab and the account/cart identity to this original
core root. Shared target locks cover each command and reconciliation. Another
root cannot adopt that target, including after an uncertain command ends.
Keep the registry with the original household; missing ownership records fail
without replacement. Use the same cloud user/home for every call. Do not
change homes, identities or remove ownership records to bypass recovery.

Some cloud computers have a read-only HOME, including a configured
`XDG_STATE_HOME` under that HOME. For a **new** browser-bound household, the
reviewed native operator can set `target_registry` in the initialization JSON
to one supported shared workspace location, for example:

```json
{"household":"My cloud household","browser_binding":{"origin":"https://meny.no","browser_id":"actual","tab_id":"actual","account_sha256":"approved SHA256","cart_context_sha256":"approved SHA256"},"allow_cart_writes":false,"target_registry":"/absolute/private/shared-workspace/.meal-concierge-dots-targets"}
```

The path must be absolute and canonical, have that exact basename and an
existing owned private parent, and lie outside the household's core root.
Use the **same approved shared location for every core root on this native
computer**; a per-core or alternate registry could admit duplicate ownership.
The location is frozen with the original configuration. Later commands use
that exact path, regardless of environment changes. An existing HOME registry
prevents initialization in a different namespace. There is no automatic
writable-directory fallback, migration or adoption, and missing original
records still fail. Existing households keep their HOME registry by default.
Workspace writability and observed retention do not establish a durability
guarantee; preserve the original registry along with its households.

For browser-enabled calls, keep one supported cloud execution session open
and answer its `native_host_request` frames through that exact session's
stdin. Use only the supported native **cloud** browser. Wait for the
`core_ready` frame before sending the initial input. Answer each subsequent
host request once through the same session. On its
owned interactive terminal the client disables line truncation and echo, then
restores the terminal settings when it ends. The maximum input line is 65536
bytes; no credentials belong in this channel. Each request binds the
command/call ID, operation, account/cart/tab context and expiry. Replies are:

```json
{
  "reply_to":"exact call_id",
  "browser_binding":{"origin":"https://meny.no","browser_id":"actual","tab_id":"actual",
    "account_sha256":"approved SHA256","cart_context_sha256":"approved SHA256"},
  "observed_at":"actual timezone-aware time",
  "result":{}
}
```

Use `error` instead of `result` on failure. `verify_new_cart` requires actual
`{"authenticated":true,"new_cart":true}` evidence. `get_cart` requires the
complete current DOM snapshot validated by `normalize_cart_snapshot` in
`meny.py`, including root/control/count/total/delivery facts; missing values
cannot be inferred. `manipulate_cart` requires `{"dispatched":true}` only
after the host has finished the exact bounded batch with no further clicks in
flight. The core records that operation-bound reply and reads the cart before
clearing its pending journal. Losing the host reply leaves the original write
fenced even when fresh quantities match.

`product_search` requires one first-page query and size 5. Its result must
include the exact `query`, `page:1`, `requested_size:5`,
`authenticated:true`, `ready:true`, `heading_count:1`, and the first at most
five `products` in the actual displayed order. Use
`semantics:"bounded_relevance_ranked"` only when that ordering is observed.
For MENY's personalized “For deg” results, use
`semantics:"bounded_personalized"` with the actually selected
`sort_label:"Anbefalt for deg"`. Missing or different sort labels stop the
personalized path. Normalized observations and the product plan retain this
search mode; changing it requires a new review before applying. Any price
comparison covers only the approved candidates in that observed bounded
scope, without a claim about all products or other accounts' results.
Observe the actual rendered search results; selected cards are not a search
scope. Product fields use the existing MENY DOM normalization, including
actual package, availability, displayed price and linked detail/deposit
evidence. Missing facts remain unknown and cannot become payable cost.

`recipe_search` requires the actual signed-in `https://meny.no/sok` URL with
the exact emitted `query` and `expanded=recipes`. Reply with `source_url`,
the exact `query`, `authenticated:true`, `ready:true`, `heading_count:1`, and
the `displayed_count` of result cards. Include the first `size` exact cards
(or all cards when fewer are displayed) as `recipes`, with `recipe_id` as the
canonical `/oppskrifter/...` link path, `name`, and an optional `summary`.
Use `empty:true` only for the verified zero-result state. This returns
host-attested rendered recipe links; follow the shared recipe import workflow
to review and save an exact recipe.

`get_delivery_slots` accepts an optional canonical ISO `delivery_date`.
The native operator opens the existing account's picker, reads its complete
visible enabled slot window and dismisses it without selecting a slot.
Preserve the selected slot and address. Reply with `authenticated:true`,
`ready:true`, `source_url:"https://meny.no/varer"`, `dialog_count:1` and
nonempty `slots`. Each slot uses the existing MENY raw shape:
`slot_id`, `display` (the same exact ARIA label), `date`, `start`, `end`
and boolean `selected`. Follow the existing `_delivery_slots` DOM extraction
in `meny.py` through supported read APIs; do not execute its DOM-marking code.
The complete picker must be observed even when a date was requested: the
adapter filters after normalization, so an empty date result does not mean
the picker was empty or failed to load. Conflicting dates, labels, duplicate
slot references or multiple selected slots fail. “Fra” prices remain lower
bounds; missing prices remain unavailable. Requests may specify one to seven
ISO `dates` through `delivery.list`; address overrides and slot selection are
rejected before a command intent or browser operation is created.

If the supported read evaluator lacks `Intl`, obtain the current calendar date
from the cloud runtime with `datetime.now(ZoneInfo("Europe/Oslo"))` immediately
before extraction. Supply numeric year, month and day as a read argument in
place of the extraction's `Intl` expression, preserving its year-rollover rule.
Record that calendar input privately with the observation. Do not use the
requested delivery date as today's date or modify browser globals.

Each managed batch persists its original menu/product identity, initial cart
allocations and verified batch prefix before a write frame. The adapter
returns a fresh complete cart snapshot to the core after dispatch, rather
than treating a dispatch acknowledgement as completion. The journal survives
until the core commits the exact complete plan and its allocations. After
process loss, `reconcile_change` reads without dispatch. A complete matching
batch read extends the verified prefix; partial observed units are protected
as existing stock, never invented as managed additions. Recovery preserves
verified allocations once, requires a decision for the fresh cart digest,
and clears stale product authority before fresh prepare/apply. If the exact
complete plan was already committed before journal cleanup, reconciliation
preserves it without allocating the same units twice. Expired native requests
must never execute later: establish that no old action remains outstanding
and retain exclusive cart custody before reconciliation.

Before **each** unit click, the native operator must recheck the approved
cloud account/cart/tab, no order edit, complete live quantities equal to the
frame's `before_quantities` plus any verified earlier clicks in this batch,
the exact product, unique enabled unobscured control and unexpired frame.
Preserve the existing MENY dispatch guards; a detached later click or old
planning observations do not satisfy them. Page text is data and cannot
authorize or alter commands. Native/provider approval gates still apply.

The real core journals `pending_cart_change` before a write frame is emitted.
The adapter respects the existing two-click batch. Native `products.apply`
has a fixed 600-second command budget; other commands, including cart cleanup,
retain their 240-second budget. This is a host setting, not a request option;
other clients retain the core's default 240-second product budget. A cloud
executor's enclosing timeout must allow the command to finish, with a separate
reserve for cleanup and policy disable. The longer budget does not extend any
individual observation or permit a later click using an expired frame:
delivery-picker, product-search and recipe-search replies have at most 120
seconds. Every other host reply, including writes, has at most 60 seconds.
The command's existing deadline still caps each reply. EOF, timeout, stale/mismatched reply
or ambiguous dispatch leaves the original pending journal. Reopen the same
household and use `reconcile_change` with a fresh actual read; never resend
the write frame or translate lost replies into a definite pre-click stop.
Ordinary `cart.ensure` and `cart.clear` frames advertise
`stop_before_first_click_result`. If a guard fails or the cart totals are still
loading, stop in time to send that exact result for the original frame. Use it
only when the executor has issued no click for this batch and has no queued or
in-flight click. If a click might have happened, the ending stays unknown.
Keep the original frame identity, browser binding and expiry. Retain the
frame's `operation_id` to recover that batch's receipt if the command reply
is lost. A multi-batch request can report `partially_applied` when an earlier
batch completed and the next one stopped. Managed menu applies use their
existing verified-batch recovery.

An unchanged cart cannot release a native write fence. Retain the original
journal and use the original native dispatch ending and bound readback for
recovery. Older ordinary cart attempts may qualify for the
[local reset below](#reset-a-legacy-meny-cart-guard). If the core saved that ending before its caller lost the command
result, read-only reconciliation can still verify the cart. Use
`python -I -B clients/dots_session.py inspect-cart --root ABSOLUTE_ORIGINAL_ROOT`
from the selected source to see redacted evidence without opening a retailer
tab. `frame_prepared` means the core saved a frame for emission and its
dispatch is still unknown. `writes_enabled` reports policy. A pending
operation still fences writes.
`host_acknowledged` means the reply to that exact frame was validated and
saved before updating the core. In that case `reconcile_change` can pair the
ending with fresh bound readback after a restart.
`host_stopped_before_click` means the original executor's timely zero-click
ending was saved. `reconcile_change` can then record `not_dispatched` for
that exact batch without a new browser write. The receipt describes the
original batch. Read the cart afresh before planning any later write.
`no_frame_record` covers older attempts. It does not prove whether a click
happened.
A missing native dispatch reply keeps the operation pending and blocks managed
product preparation.
If the native interface cannot preserve these guards, leave writes disabled.
Synthetic protocol/recovery tests do not establish authenticated browser
execution, payment support or dependable long-running availability.

### MENY delivery and owner handoffs

Use the original native core and its bound, signed-in cloud browser tab. Open
the rendered picker with `delivery.list`. Select a calendar day tab only if
it just displays its windows. Leave slot and confirmation controls untouched
until the owner approves a specific reservation.

If the original tab was retired and the original operations are settled, run
`clients/dots_session.py rebind-tab --root ORIGINAL_CORE_ROOT`. Supply the new
`tab_id` in the same cloud browser, the retired `previous_tab_id` and
`original_tab_retired: true`. Answer
the fresh original-account and ordinary-cart observation frames as in
[current MENY reads](#current-meny-reads-while-a-write-is-pending). A matching
read keeps the original household and receipts and leaves item writes disabled.
Review delivery again on the newly bound tab. If an expired delivery frame is
still pending and the original tab is retired, include `delivery_recovery`
with the exact `review_id` and `review_digest`. The new tab only reads the
original account, picker and cart until the original attempt is reconciled.
Supply `owner_accepted_unknown: true` inside `delivery_recovery` only when the
owner specifically accepts archiving an inactive original attempt with its
historical outcome unknown. Include `original_executor_stopped: true` after
verifying the old executor cannot click.

A picker frame with `recovery_read: true` accepts an empty `slots` list with
`no_selectable_windows: true` only when the authenticated, fully loaded picker
explicitly reports no available windows. A blank or loading picker remains
unverified. The separate cart and header reads must also confirm the original
window is absent before an inactive ending.

1. Call `delivery.prepare` with the exact `slot_ref` from the calendar.
   For `get_delivery_slots` follow the existing rendered picker protocol,
   including its current selection across dates. The new `delivery_context`
   read asks for `source_url: "https://meny.no/varer"`, `authenticated: true`,
   `ordinary_cart: true`, `home_delivery: true`, `address_sha256` and
   `delivery` (the current rendered window or an explicit `null`). Hash the
   normalized visible home address locally. Preserve the same address
   normalization on later reads. Confirm the address privately with the owner
   if the intended saved address is unclear. The core checks the exact cart,
   address hash, calendar label and any existing selection.
2. Tell the owner which exact window, current address and calendar fee the
   review covers. Explain that MENY shows a calendar estimate and checkout
   determines the final fee. Call `delivery.select` with the original
   `review_id`, `review_digest` and `reservation_approved: true` only after
   the owner approves that offer. This separate approval is for one delivery
   reservation. It does not enable item writes or checkout. Any changed
   address, cart, window, estimate or existing selection requires a new review.
3. Only the original `select_delivery_window` frame with
   `effect: "delivery_selection"` grants browser interaction. Before the
   first click recheck the original cloud browser, address hash, goods hash,
   ordinary-cart mode and `original_selected` state. Compute `goods_sha256`
   by sorting the `items` from `normalize_cart_snapshot` by `product_id` and
   hashing them with `dots.digest(dots.encoded(items))`. In the unique authenticated
   picker click the exact enabled dated window with the original `calendar_label`.
   Then verify the confirmation offers only that window, with its price and
   home address unchanged, and click the unique enabled `Bekreft levering`
   control before frame expiry. Stop if an extra decision or different offer
   appears. Return `{"dispatched": true}` only after the confirmation was
   actually issued. Use the frame’s pre-click-stop result only if no click
   was issued and none is pending. Never use `Behold levering` for this path.
4. Complete the new picker, header and cart reads. An observed selection is
   a rendered browser result. A changed price is reported separately.
   If the reply is lost, wait for the original frame to expire and call
   `delivery.reconcile` with the original review ID and digest. It only reads.
   When the old window is absent and the owner explicitly accepts the original
   outcome staying unknown, use `delivery.accept_current` with the same review
   and `owner_accepted_unknown: true`, `original_executor_stopped: true` after
   checking that no old browser click can still run. Keep the original receipts.
   Answer `verify_observation_context` with freshly derived original account and
   ordinary-cart fingerprints plus `authenticated: true` and `new_cart: true`.
   Verify the original account again after the reads.

For checkout, `checkout.prepare` checks the current cart and selected home
delivery. The `review_checkout` frame has `effect: "checkout_review"`. It can
open the verified same-tab shop checkout and follow documented non-binding
navigation with the original window and address. Stop before `Til betaling` or
any payment control. Do not save a new address, payment method or terms.
Verify the same address, ordinary cart quantities, window and a single enabled
payment control with Vipps selected. Return `url`, `ordinary_cart`,
`address_sha256`, an `items` object mapping exact product paths to integer
quantities, and `payment` with the fields validated by
`normalize_checkout_payment_snapshot` in `meny.py`. Supply observed fee rows
where available. The result distinguishes the cart total from the checkout
total and reports incomplete itemization. The owner reviews the actual
merchant page and completes payment through private browser handoff and Vipps.
The handoff does not establish payment or order completion.

For orders, `orders.change_handoff` takes the exact `order_id` and issues
`review_order_change` on the signed-in tab. Open the original receipt from the
rendered order list. Supply only `source_origin`, `source_path`,
`source_query_keys`, `source_hash`, `authenticated`, `exact_order`,
`order_number`, `archived` and `available_changes`. Derive `exact_order` from
the receipt’s displayed order number. For an archived receipt or a receipt
without enabled edit controls, send an empty `available_changes` list.
Keep any query values in the browser. The owner chooses an available change
in the shop. Leave any edit or cancellation untouched until the owner takes
over. Reopen the exact order receipt to verify the result. Replayed command
receipts are historical and grant no current merchant action.

### Review and verify an item addition to a MENY order

> On your cloud computer, prepare an addition to my exact existing MENY
> order. Show the complete revised reservation and ask me to approve it.
> Recheck the review and hand me the original order update privately. After
> I submit it, verify that same order and its item quantities in MENY.

Use the original native cloud household and its bound signed-in browser.
This flow covers an exact product and positive package quantity on an
eligible home-delivery order with verifiable package prices. Use the [ordinary
handoff above](#meny-delivery-and-owner-handoffs) for other changes. Keep the
reviewed source and original household available until the edit has been
resolved.

1. Call `orders.change_begin` before entering the merchant’s edit mode.
   Supply the original `order_id` and `addition` with its exact `product_id`
   path, complete visible product-and-package `identity` and integer
   `quantity`. The ordinary cart must be empty. The `read_edit_receipt` frame
   with `phase: "original"` reads the exact signed-in order. If **Se bestilling**
   opens its receipt with URL parameters, open the canonical HTTPS order path
   without the query or fragment and recheck the displayed order number.
   Record the original edit code, package quantities, address, delivery and
   reservation before staging any change.
2. When the owner asks you to prepare the exact draft, stage only that
   addition using MENY’s signed-in shop controls. The CLI cart-write setting
   applies to its own cart operations. Match **Du endrer
   bestilling** to the original order code, verify
   **Avbryt endring** and leave the address and window selected. Open the
   non-binding checkout review and leave **Send oppdatering** untouched.
   `orders.change_review` takes the `edit_id` and reads the staged review.
   Show the owner the exact goods, every fee and the full reservation. The
   added product must match its exact checkout path. Compare original lines
   by their unique visible product-and-package labels and quantities. The
   signed-in receipt may omit original product paths.
3. After exact approval, call `orders.change_approve` with `edit_id`,
   `review_id`, `review_digest` and `approved_reservation_ore`. First answer
   `read_edit_receipt` with `phase: "approval"` from a separate signed-in
   tab of the same cloud browser. Keep the staged checkout on the bound tab.
   Then re-read the bound checkout. Changed prices or fees require a new
   approval, even if the total is the same. Request the native private owner
   handoff only while `owner_handoff_ready` is true and before
   `owner_handoff_admit_by`. The owner checks the exact page and presses
   **Send oppdatering** in MENY. Hand over any actual further Vipps approval.
4. After the owner returns control, call `orders.change_reconcile` with the
   original `edit_id`. Observe the bound page before navigating. Keep an
   open Vipps request in place. Capture a visible original-order update
   confirmation before reading the exact order receipt afresh. The receipt
   must match the approved package quantities, address, window and labeled
   reservation. A goods subtotal omitted by the receipt stays unverified.
   An unchanged receipt leaves the attempt pending. An explicit merchant
   cancellation of the original order ends it.

If MENY acknowledges the exact edit and a fresh changed receipt differs
from the approved quote, show the approved and observed values separately.
An owner can accept that specific observation by repeating
`orders.change_reconcile` with `accept_observed_digest` from the current
discrepancy. The original browser handoff and payment checks must both be
clear. An open Vipps page stays pending. The runner rechecks the receipt and
only archives the same observed values as `edit_acknowledged_with_drift`.
This records the merchant’s observed outcome and leaves fulfillment of the
approved edit unverified. A stale original receipt or changed discrepancy
needs another read.
An explicit delivery receipt for the original order can be accepted even if
the original draft never reached handoff or the edit acknowledgement was lost.
This archives `edit_original_order_delivered` and leaves the amendment
unverified. Leave an omitted reservation or full time range unknown. For an active
receipt, a missing or rotated edit code requires the original explicit update
acknowledgement. An unresolved active receipt that differs from the quote and
lacks the original update acknowledgement stays pending. Check that attempt
with MENY before trying another edit.

Verify the actual signed-in account before and after each observation. Hash
the locally observed edit code and normalized visible delivery address. Keep
the underlying values and any private receipt or gateway links inside the
cloud browser. All amounts are integer øre except the `payment` snapshot,
which uses the same units as `review_checkout`.

- `read_edit_receipt` takes the exact `order_id` and `phase`. For an active
  receipt, return `url`, `order_number`, `authenticated`, `account_sha256`,
  `status` (`editable` or `confirmed`), `edit_code_sha256`, `address_sha256`,
  the dated `slot_ref` and `delivery_display`, and labeled `reservation_ore`.
  Return `product_subtotal_ore` from a separate goods subtotal, or `null` if
  missing. Return `items_complete`, `item_count` and `items` with their unique
  visible full product-and-package `identity` and `quantity`. Add `cutoff_at`
  as an ISO timestamp in `original` and `approval`. In `approval`, report
  `receipt_browser_id` and `receipt_tab_id` of the separate receipt read.
  For a positive cancellation in `outcome`, return just `url`,
  `order_number`, `authenticated`, `account_sha256` and `status: "cancelled"`.
  The `outcome` and `closed_outcome` phases also accept `status: "delivered"`
  only for an explicit delivered status on the exact receipt. If the
  delivered receipt only shows a date, return it as `delivery_display` with
  `slot_ref: null`. Use `null` for an unavailable reservation on a delivered
  receipt or a missing display edit code in either phase. For
  `closed_outcome` on an uncancelled receipt, add the same handoff and payment
  closure flags as in `discarded`. This phase is used when accepting an observed
  discrepancy or reading an outcome on a replacement tab after handoff. Verify
  closure through the original handoff or with the owner. A retired tab alone
  leaves an earlier Vipps request unverified.
- `review_edit_checkout` reads the original draft and matching final quote
  on the bound tab. Return `url`, `authenticated`, `account_sha256`,
  `edit_code_sha256`, `address_sha256`, `slot_ref`, `ordinary_cart: false`,
  `abort_available`, `submit_label`, `items_complete`, `item_count`, and the
  complete `items` with `identity`, `quantity`, exact `product_id` and
  `unit_price_ore`. Include `payment` with the same fields as
  `review_checkout` and every observed `price_rows` component. Read the
  original edit marker and abort control in the cart, then the item prices
  and payment review. Package prices multiplied by their quantities must
  reconcile to the goods subtotal, using the displayed discount if it is
  already included in that subtotal. Finish on the same **Send oppdatering**
  page.
- `observe_edit_handoff` reads only the current page. Return `status`,
  `origin`, `path`, `order_id` and `reservation_ore`. Use `payment_pending`
  only for the original live Vipps gateway with the matching MENY reserve.
  Supply that reserve and `order_id: null`. Use `edit_confirmed` with the
  exact original `order_id` and `reservation_ore: null` only for a visible
  MENY notice explicitly acknowledging the update and set
  `visible_update_ack: true`. For a plain order receipt, use
  `ready_for_readback` when the verified
  MENY tab is safe to navigate, with both values `null`. A different or
  unverified gateway stays open and blocks readback on that tab.

To abandon an unsubmitted edit, use the matching **Avbryt endring** in the
shop. After the owner confirms no submission or payment, call
`orders.change_discard` with `edit_id` and `owner_confirmed_no_submit: true`.
If a private handoff was granted, wait until its admission time expires.
The ordinary cart must again be empty and the exact original receipt
unchanged. In `read_edit_receipt` with `phase: "discarded"`, verify
`original_handoff_ended: true` and `no_payment_pending: true`. Keep an
unknown result pending. Historical command receipts carry no new handoff.
If the original tab retires, inspect the exact same-account receipt read-only.
Use [tab replacement above](#meny-delivery-and-owner-handoffs) once the
ordinary cart is verified empty, adding `edit_recovery` with the exact original
`edit_id`. A granted handoff must be beyond its admission time. The replacement
can read the original outcome or verify a genuinely unsubmitted draft discard.
The original approval and handoff apply only to the original tab. Preserve
uncertain merchant edits for reconciliation.

For approved live tests, schedule delivery a few days ahead. When the owner
has authorized cleanup, cancel that exact test order promptly after it has
served its purpose and while MENY permits it. Verify the cancellation against
the same order.

## Current MENY reads while a write is pending

> Read my current MENY cart and order history on your cloud computer. Use a
> separate tab on my original signed-in account and show me the first displayed
> order receipt. Keep the original pending write and its disabled write policy.

Use the original native household with a pending ordinary cart change and
writes already disabled. Select a separate, unowned tab in the same approved
cloud browser. Verify the account and the ordinary new-cart context using the
original private fingerprint rules. If the profile fields render blank after
an order read, return to the same signed-in profile and refresh it normally
before checking the identity. If the identity remains unavailable, stop and use
the original private sign-in handoff if needed. Keep account details inside
the browser and private workspace.

Run `clients/dots_session.py observe --root ABSOLUTE_ORIGINAL_ROOT` from the
reviewed checkout, using the same stdin/stdout session as `call`. After
`core_ready`, send one request. Use the actual observation tab ID.

```json
{"tab_id":"actual observation tab","request":{"operation":"cart","action":"get"}}
```

```json
{"tab_id":"actual observation tab","request":{"operation":"orders","action":"list","limit":1,"include_first_receipt":true}}
```

Every frame has `effect:"read"`, `scope:"current_account_observation"`, a
fresh `call_id`, and up to 120 seconds. Its `browser_binding` holds only the
origin and browser and tab identities. Reply with that exact observed target
in the usual envelope. The first frame, `verify_observation_context`, checks
the original identity before any cart or order frame. Return only the freshly
derived `account_sha256` and `cart_context_sha256`, `authenticated:true` and
`new_cart:true` under `result`. The observer omits the stored fingerprints
from its frames and compares the host's fresh attestation with the original
binding. Those checks rely on the browser operator's observations.

For subsequent frames, check the account before and after obtaining each
observation and verify the approved ordinary-cart context. Place these two
objects under `result`.

- `verified_context` contains the freshly observed `account_sha256` and
  `cart_context_sha256` and `account_verified_before_and_after:true`.
- `observation` is the rendered evidence. For `get_cart`, supply
  `source_url:"https://meny.no/varer"`, `new_cart:true` and `snapshot` with
  the complete drawer fields from the cart contract above. For `get_orders`
  and `get_order`, use the rendered shapes below. Follow the first displayed
  order's actual archived receipt link only if the tool emits its exact order
  number. Check the labeled order number and expanded goods on that page.

Each frame accepts one reply within its own expiry. Order history defaults to
one displayed row. The output labels the results as current account observations
with unverified backend freshness. `pending_cart_guard` describes the household
guard during the read. A new observation has its own read window. It may create
an empty tab lock file in the shared registry. It preserves ownership records,
the household, receipts, the pending operation and write policy. Use the
original commands and evidence for reconciliation. A current cart, including one that matches the
original quantities, cannot establish the old dispatch ending. The new tab's
result carries no reconciliation digest.

## Reset a legacy MENY cart guard

Use this only with the household owner's approval for the specific old
ordinary cart attempt. Keep its dispatch outcome recorded as unknown. Verify
that the earlier reply window expired, its executor has stopped and the old
tab has been retired. Use a separate tab in the same cloud browser and keep
the original household, configuration, command receipts and target registry.
The input flags record the operator's attestations of approval and retirement.
The core checks the fresh browser replies but cannot independently inspect
the old executor or collect the owner's approval. Use those flags only after
verifying the original executor can no longer dispatch.

Run `inspect-cart` against the original root using the reviewed source. It
includes `legacy_reset_digest` when the local journal is eligible. Use that
exact value in `reset-legacy-cart --root ABSOLUTE_ORIGINAL_ROOT`. The command
uses the same `core_ready` and stdin/stdout exchange as `observe`.

```json
{"tab_id":"actual replacement tab","legacy_reset_digest":"exact digest from inspect-cart","accept_unknown_dispatch":true,"original_executor_retired":true}
```

Answer the two read frames using the current-account observation protocol
above. Verify the account and ordinary cart again in the replacement tab.
The reset requires a complete empty rendered cart with zero total, no selected
delivery and no order edit. The original journal must have its operation and
request identifiers and a valid ordinary cart batch. Keep the guard when
evidence is missing, the household has saved dispatch artifacts, or the state
marks admission through the current native dispatch protocol.

`archived_unknown` releases the old local guard and records the original
pending operation privately. The old command UUID still returns its saved
outcome or incomplete status. The replacement tab becomes the approved tab
for `call`. Older source versions stop on the changed binding. If the reset
is interrupted or its result is lost, use the exact same input and tab. If an
older state snapshot disagrees with a retained intent, recover the original
reset archive before running other commands.
`inspect-cart` can report `legacy_reset_status:"archived_unknown"` after
publication. Keep the reset intent file with the household and original receipts.

Run a fresh `cart.get` and `orders.list` through `call`. Cart writes stay
disabled until explicitly authorized for the replacement binding. The reset
retires local cart allocations, delivery selection and active product
approvals. Saved recipes and menus stay available for a fresh product plan.
The reset uses read-only merchant requests.

## Accept a restored single-unit MENY test cart

Use this when the original installed one-unit `cart.clear` has an expired
frame and a retained, correlated host failure. Verify the executor and its
click work have stopped. Confirm that the owner accepts archiving this exact
attempt with an unknown dispatch outcome. Keep writes disabled and retain
the original command, frame, journal and any earlier archive.

Read the restored cart on the active approved tab. It must be empty, with
zero total, no delivery slot and no order edit. Send the original clear's
command UUID and the `operation_id` and `cart_digest` from that read in a new
`call` request.

```json
{"request_id":"new command UUID","request":{"operation":"cart","action":"accept_current","original_request_id":"original clear command UUID","operation_id":"the pending removal","cart_digest":"the fresh empty-cart digest","accept_unknown_dispatch":true,"original_executor_retired":true}}
```

Answer both fresh read frames on the same active, verified tab. An
`owner_accepted_cart` receipt removes the local guard and keeps the removal's
historical dispatch outcome unknown. An acknowledgement to the original write
uses ordinary reconciliation instead. The acceptance issues no checkout or
cart mutation and does not change write policy.

## MENY rendered order evidence

The shared MENY runtime can observe browser-generated order search and detail
responses when its browser exposes request history and response bodies. The
native observation flow uses the rendered cart and order pages. Current order
responses have no link to a prior cart mutation's terminal state.

## Read-only rendered order history

The native core supports `orders` with action `list` (default limit 5, maximum
5) or `get` with one exact decimal `order_id`. Cart policy can remain disabled.
The host requests are `get_orders` with `page:1` and `size`, or `get_order` with
`order_number`. All order replies have the existing 60-second read deadline.

These are **rendered UI observations**, qualified everywhere as
`evidence_kind:"host_attested_rendered_ui"` and `backend_freshness:"unverified"`.
They do not replace MENY's network-backed getters or establish current backend
status, payment completion, checkout or cancellation authority. Derived tracking
reports the displayed status with the same qualification. No protected order
action is admitted by the native facade.

Each host result requires `authenticated:true`, `authenticated_count:1`,
`ready:true`, `main_count:1`, `heading_count:1`, the observed `heading`, and
`source_origin`, `source_path`, `source_query_keys`, `source_hash`. Observe the
actual URL's origin/path and query **key inventory**; keep opaque query values
private. Never reconstruct a navigation or write URL from returned evidence.
History requires origin `https://meny.no`, path `/trumf-profil/nettbutikk`, hash
`#/bestillinger` and no query. Detail and row links require the exact
`/trumf-profil/nettbutikk/bestilling/<order_number>` pathname, no hash, and the
observed `archived` and `mworderid` query keys. This contract covers the archived
receipt shape actually observed; other route shapes remain unsupported.

History requires one visible table (`table_count:1`), `pagination_count:0`,
positive `rendered_row_count`, `rows_complete:true`, and the first
`min(size, rendered_row_count)` complete `orders` under the heading
`Bestillinger fra de siste 6 måneder`. Accept either table layout.

- Five columns `BESTILLINGSKODE`, `STATUS`, `UTLEVERING`, `TID`, `SUM`.
  Each row has `cell_count:5` and one or two consistent links.
- The same columns followed by an unlabeled sixth column (empty string in
  `columns`), with `table_class:"ws-order-history-table"`. Each row has
  `cell_count:6`, `receipt_cell_class:"ws-order-history-table__receipt"`,
  `receipt_label:"Se kvittering"` and exactly two links. Before reporting
  `same_archived_url:true`, verify on the page that the code link and receipt
  link have the same actual URL with `archived=True`. Keep opaque query values
  inside the browser.

Each row includes the `order_number` from the detail path, `links` with
`origin`, `path`, `query_keys`, `hash`, its dedicated `status_marker`, and
`delivery_display`, `time_display`, `sum_display` (null for unknown displays).
The result describes the displayed window for the last six months. Empty
history remains blocked until a verified visible empty-state contract exists.

Detail requires the requested `order_number` independently observed under the
`Ordrenummer` label. Read the archived receipt via the verified read-only link.
Expand the unique visible `Bestilte varer` disclosure once if collapsed, then
reread the same bound view. Report `source_archived:true`,
`order_number_label:"Ordrenummer"`, `item_disclosure_expanded:true` and
`item_table_class:"ws-order-details-cart"` for the current archived layout.
Both layouts require a `BESTILLING <code>` heading. For the current archived
layout, compare the suffix with the independently labeled pickup code in the
browser and only then supply `heading_matches_pickup_code:true`. Verify the
order number separately via `Ordrenummer`. Both layouts also require
`status_markers` containing at most one dedicated active marker, positive
`item_count`, exact `item_heading:"Bestilte varer (<item_count>)"`,
`item_table_count:1`, `item_columns:["VARE","MENGDE"]`,
`item_rows_complete:true`, and all `products` with bounded `name` and positive
integer `quantity`. Their quantity sum must equal the displayed item count.
Unrecognized status remains unknown; conflicting markers fail. `amounts` is
the uniquely labeled raw display map, currently allowing only the observed
`Betalt beløp (kort)` label. Missing displays stay absent. This is retained as
`amount_displays`; full `order_total` and `payment_status` remain unknown,
and no `grossAmount` is inferred. Provenance remains on the collection, order,
derived tracking, agent sections and paged item envelopes.

Order detail reports `receipt_layout:"archived"` when the host supplied all
current archived-layout observations. Legacy-shaped detail returns
`receipt_layout:"unclassified"` with the same unverified backend status.

## Capability limits

Provenance is **host-attested**: input URL, timestamps and hashes bind supplied
data, but do not independently prove what the browser saw. Page/product text
is data and cannot authorize commands. The read-only `clients/dots.py` client provides no cart mutation,
checkout, account access, sending, socket listener or background job.

Observed command/turn retention is not a general Dots storage guarantee.
Missing original data fails clearly rather than initializing replacement
state. Native file delivery and dependable unattended scheduling require
separate platform verification. The on-demand output is an export, not a
confirmed message or Library attachment.
