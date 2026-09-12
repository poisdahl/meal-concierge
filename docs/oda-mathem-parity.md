# Oda, Mathem and MENY: ordinary flow evidence

As of 12 September 2026 (Europe/Oslo), the remaining #50 recovery and delivery
criteria are complete within the separately observed provider capabilities.
Oda and MENY passed ordinary same/lower/higher-total delivery changes. Mathem
passed unchanged-total delivery; its bounded slot reviews exposed no priced
alternative, so lower/higher live outcomes remain unavailable rather than
claimed. Owned-order cleanup is verified. The final MENY cart restoration needed
separate operator reconciliation before ordinary per-item restoration, as
recorded below. Earlier assisted results retain their original scope. Bank
reconciliation is outside this work and is not a completion gate.

## Ordinary Mathem failed-addition recovery — 2026-09-10

**The ordinary failed-addition recovery criterion is now demonstrated.** The
installed public `61ca1cb5` release completed the same captured merchant change
through actual Hermes, its maintained skill, MCP and Application. The owner
performed the required Bank Norwegian app approval; no helper paid or wrote
protected checkout history on the product's behalf.

After the owner returned, an authorized recovery still failed. Its retained
native payment GET positively matched the original order/change, and independent
MCP reads confirmed the unchanged one-package/SEK 124.50 paid base and unpaid
addition. Ordinary Hermes reconciliation recorded that current attempt's own
failure. A fresh ordinary preparation archived it intact and preserved the
original addition, earlier failed history, account/address/card, delivery and
dietary permissions. The new review and required local notice disclosed the
same SEK 18.50 payable and separate SEK 18.51 overview; absent fee rows remained
absent. No goods were restaged.

Following the owner's explicit request, Hermes acknowledged the exact delivered
notice, confirmed one payment and selected Appen once on that new confirmation.
Bob then sent the requested Signal notification to the existing owner contact.
After the owner approved in the bank app, ordinary Hermes reconciliation returned
`confirmed=true`, `changed_existing_order=true` and `retry_allowed=false`.
Independent merchant reads confirmed two packages/SEK 143.00 and
`paid_and_modifiable`, with an empty cart and cleared checkout/change state.
The original and successful recovery confirmations resolve to the same confirmed
result; failed confirmations retain their separate failed history.

The exact local result notice was delivered and acknowledged. That notice's
CLI process exited 134 after its successful tool result and final reply were
persisted; it was not resent. Hermes then prepared and confirmed cancellation
of only this test order once. Independent merchant reads confirmed `cancelled`,
an empty cart, both notices acknowledged and no pending checkout, cart change,
order change or cancellation. The reads preserved the full product state.
No bank settlement, refund or authorization release is inferred from the
merchant result. At that checkpoint, the separate changed-delivery-price criterion
remained open; earlier ordinary Oda recovery and #60 acceptance are unchanged.

## Paired payment investigation and bank approval — 2026-09-10

Further owner-authorized, instrumented Application trials paid one Oda addition
at NOK 16.70, producing two packages/NOK 263.10, and two Mathem additions at
SEK 18.50 each, producing five packages/SEK 198.50. The first Mathem payment
completed native 3D Secure device identification without a visible challenge;
Oda returned payment success directly. A separately reviewed experiment failed
exactly one transaction-bound hidden Mathem fingerprint request. That payment
also succeeded, so fingerprint failure alone does not explain the earlier
decline. These are real merchant payments through Application, not additional
Hermes-conversation acceptance. The original decline's cause remains unknown.

The shared implementation now retains the original payment tab and verification
context while an issuer challenge is pending. A visible challenge requests user
approval in the matching bank app or bank page; a hidden fingerprint frame does
not claim that user action or an app request is required. Reconciliation checks
the same attempt without another payment. Missing observation, restart or a
lost response before the first observation retains an unknown outcome and
blocks recovery dispatch. Original Oda additions correctly report saved-card
payment even when the household's new-order preference is Vipps.

A fresh Mathem base payment at SEK 124.50 displayed the issuer's actual choice
between Bank Norwegian Appen and BankID. No method was selected before that
challenge expired. Its original native payment GET subsequently reported a
terminal retry for the same new unpaid order. Ordinary candidate reconciliation
matched the original goods, amount and delivery, recorded only that terminal
failure and retained the original authentication context. Missing observation
alone still cannot enable another payment.

Checkout `authenticate` now selects the supported Bank Norwegian Appen method
once, bound to the current original or recovery confirmation. A durable marker
precedes the click. An isolated real Chromium cross-origin fixture verified one
app-method click, no BankID click and no password-field/text read; forms, changed
frames and extra controls were rejected. Native DOM and persisted Application
tests also cover hidden challenges, route changes, lost responses, recovery IDs
and restart. Access to the exact dedicated bank page is separate from the
general browser viewer.
An optional national ID belongs in the verified bank UI, entered directly by
the user. BankID passwords must never be accepted.

The investigation also reproduced two ordinary navigation failures: selecting
a new order used a different continuation label, and opening addition checkout
populated the original delivery in an initially slotless cart. The fixes select
the destination before its continuation control and freeze that delivery
materialization only when goods, total, address and original window agree.

With the old paid Mathem order still active, a new one-package checkout showed
SEK 18.50 in both MCP and the native overview but SEK 124.50 on the settled
payment control. Final validation stopped before any payment POST. Preparation
now rejects that disagreement as well; absent fee rows are never invented.
After ordinary cancellation of the old test order, the unchanged staged cart
returned SEK 124.50 and a complete native breakdown: SEK 18.50 goods, SEK 99
small-order fee, SEK 7 packaging, SEK 79 delivery and SEK -79 delivery discount.
The exact selected delivery quote remained zero. This demonstrates an effect
of the old-order context on the displayed prices, not a general merchant rule.

The same failed order's retry review omitted both the SEK 79 delivery row and
its SEK -79 delivery credit. Goods, packaging, small-order fee and SEK 124.50
total remained identical. Recovery now accepts only this specific omission of
a full cancelling delivery/credit pair, with no product discount and an
original exact zero delivery quote. It still rejects any other amount change.
The recovery summary shows the native retry rows as absent and preserves the
original review and selected quote. Signed discount conversion is retained;
a negative credit must not become an unknown amount.

That same-order retry dispatched once and entered a new issuer challenge. It
exposed a capture race: the old retry failure article remained briefly visible
before the new payment redirect. Recovery now ignores that stale article and
retains unresolved saved-card state until context or a confirmed outcome is
available. The live issuer chooser also includes two invisible checkboxes and
an `Avbryt` link. The helper now permits those untouched elements while still
rejecting credential inputs, visible checkboxes, extra actions and changed
frames. A real Chromium fixture verified this markup with zero BankID, cancel,
checkbox or password-read events. The earlier live chooser stopped before any
method click, and the challenge expired during diagnosis.

After terminal failure of that attempt was independently verified, a separately
reviewed diagnostic native retry of the same SEK 124.50 order selected Bank
Norwegian Appen once. Ordinary Application reconciliation then confirmed one
package and SEK 124.50 paid on the same order. This demonstrates live method
handoff followed by merchant-confirmed payment. The helper performed that retry;
it does not establish ordinary product-driven failed-addition recovery.

A subsequent actual Hermes conversation used the installed MCP tools to prepare
one SEK 18.50 addition to that paid base. Its first confirmation stopped for the
required notice. After acknowledging independent delivery to the local test
inbox, Hermes confirmed again, dispatching one payment, and called `authenticate`
once on that same confirmation. The tool result reported
`bank_app_method_chosen=true` and a retained challenge.
Independent merchant reads still showed the original one-package/SEK 124.50 base
and `unpaid_order_change`; this stage alone is not a successful addition or
recovery result.

The original addition subsequently reached a visible failed retry page. Its
retained payment's native GET returned terminal `checkout-payment-retry` with
the same order number and numeric change ID as that exact retry URL. Independent
MCP reads still showed `unpaid_order_change` and the unchanged one-package/
SEK 124.50 paid base. No user action or specific issuer-decline cause is inferred
from this result. The late-failure resolver now also supports this original
addition contract, preserving the original context and history and recording
only the bound terminal failure after the paid-base checks.

With that original failure recorded, actual Hermes/MCP prepared its supported
recovery without restaging goods. A new required local notice disclosed SEK
18.50 payable and the separate SEK 18.51 merchant overview. Hermes acknowledged
the delivered notice, confirmed one recovery payment and selected Appen once
on that recovery confirmation. This attempt also later returned terminal
`checkout-payment-retry`: its own retained native payment GET matched the same
original order/change, while independent MCP reads still showed the unchanged
SEK 124.50 paid base and unpaid addition. No owner approval or specific decline
cause is inferred. Ordinary reconciliation retained the attempt; no second
payment was sent from its failed confirmation.

That observed second failure exposed a missing continuation. The product now
resolves a current Mathem addition recovery's own positive terminal failure,
with exact original order/change and unchanged paid-base checks. After a fresh
review succeeds, it archives the complete failed attempt privately and gives
the replacement a new confirmation and required notice. Old confirmations
permanently replay failure and cannot operate on a later payment. This does not
enable a new-order retry cycle or an automatic payment loop. Merchant-confirmed
ordinary failed-addition recovery remained unaccepted at that point; the later
ordinary result above completes that criterion.

The scoped follow-up preserved the full household state through deployment.
An actual installed Hermes/MCP preparation then archived the failed recovery
and returned a fresh review for the same one-package addition: SEK 18.50
payable, SEK 18.51 overview, original account/address/card and unchanged
dietary unknowns. It stopped before notice delivery, confirmation, bank-method
selection or payment. At that checkpoint the paid base remained active and
the addition unconfirmed while awaiting owner availability. The later ordinary
recovery above records the completed payment.

Final source checks passed 43 focused recovery tests, 1,362 public tests with
nine optional platform skips, and the canonical fleet profile including 543
static checks. Both installed Oda and Mathem RPC health/status calls passed;
all four deployed runtime sources matched the reviewed files in each of the
three scoped services. These source checks alone did not establish the later
merchant-confirmed ordinary recovery.

The final review also identified a delayed-confirmation case: reaching the
merchant success route can precede its paid tracking status. Original saved-card
attempts now retain uncertainty until a bound context, a verified failure or a
complete merchant confirmation resolves them. Restart tests reject another
payment during that gap. Free delivery changes retain ordinary reconciliation
without being presented as bank authentication.

Both earlier paid test targets were cancelled once through ordinary Application
cancellation and independently read as cancelled. No refund or authorization
release is inferred from cancellation. The Oda household preference was restored
to Vipps; dietary permissions and unrelated orders were preserved.

## Ordinary Oda payment recovery and retained Mathem failure — 2026-09-09

The later owner authorization covered the necessary bounded live payments.
The installed candidate now exposes `checkout prepare recovery=true` through
Application, MCP and the skill for an identified unpaid **new order**. It keeps
the original journal and merchant order, checks the original products,
quantities, account/address, delivery, total and fee rows, and prepares a fresh
confirmation without restaging goods. An explicitly authorized
`checkout_payment` override applies only to this recovery. The final payment
rechecks the merchant target and review under the existing operation lock;
a dispatched recovery, including a lost response or restart, is reconciled
through the retained attempt and cannot authorize a second recovery dispatch.

A real Hermes conversation used that installed path to recover the same Oda
order from the hosted Vipps timeout below. The owner-authorized existing saved
card paid the unchanged one-package, NOK 246.40 review for 12 September
07:00–13:00. The ordinary confirm returned `confirmed=true` and
`paid_and_modifiable`; independent merchant reads agreed. The pending checkout
cleared, both original and recovery confirmation IDs resolve to the same
protected result, and all 17 earlier orders remained unchanged. Global Vipps
settings and dietary permissions were preserved. This demonstrates saved-card
recovery of an unpaid Oda new order, not successful Vipps approval or Mathem
addition recovery. One earlier confirmation stopped before payment on a review
comparison error; that error and a separate prepared-summary inconsistency
were corrected before the fresh confirmation. Helpers read evidence but did not perform recovery or write
protected journals.

The separately authorized Mathem test produced an actual failed addition:
one extra pasta package, SEK 18.50, to a paid one-package base order of
SEK 124.50 for 13 September 14:00–16:00. After one payment dispatch the native
page explicitly reported that payment failed, and merchant tracking changed to
`unpaid_order_change`. The original paid goods remained unchanged. An earlier
confirmation had stopped before dispatch because checkout navigation populated
the existing delivery in the cart; a fresh unchanged commercial review was used
for the sole payment attempt.

The supported unpaid endpoint, both with and without the order number, returned
`checkout-payment-retry` with an order/change ID, delivery and financial rows,
but no product IDs or quantities. The receipt showed only the original paid
goods. The original payment response body was no longer retained. The available
React Query entry was written by the later retry-page confirmation request,
so it cannot prove that the original dispatch assigned this change ID to the
frozen goods. The earlier report incorrectly called SEK 18.51 the payable:
the native payment button actually showed SEK 18.50, while SEK 18.51 was the
separate merchant overview total. The payment button receives the merchant's
remaining amount; the overview total is not a substitute for it.

On 10 September the owner pressed the native retry button and immediately saw
“Tack för din beställning”. Fresh merchant reads confirmed the same order now
contained two packages and totaled SEK 143.00. Ordinary Hermes checkout
reconciliation then confirmed the owner-completed addition and cleared the
pending checkout/change. Its exact required result notice was delivered to the
existing local test inbox and acknowledged through the ordinary tool. No helper
payment or protected-journal edit was used. The original decline's cause remains
unknown; the successful owner retry does not demonstrate product-driven recovery.

The follow-up implementation captures the original failed dispatch's exact
order/change target in its active tab and persists it with the reviewed goods.
It allows one fresh `checkout prepare recovery=true` for that Mathem addition
only while the paid base remains unchanged and merchant tracking reports
`unpaid_order_change`. Original goods and actual payment-button amount must
match; the separate overview total is disclosed and frozen, and added fees
stop recovery.
Original/recovery aliases reconcile the same attempt after response loss or
restart, and an uncertain recovery never enables another payment. Missing
original capture remains reconciliation-only. Automated tests exercise the
original guarded submit through persisted state and restarted Application,
review drift, owner-payment races, required notices and one recovery dispatch.
A bounded 10 September ordinary Hermes trial added one more package to that
same paid order with the installed fix. Its first confirmation stopped before
notification or payment because checkout navigation populated the original
order's delivery slot in the cart. A fresh prepare reviewed identical commercial
terms. After its exact required local notice was delivered and acknowledged,
one payment dispatch confirmed three packages and SEK 161.50, with the same
account/address/card and 13 September 14:00–16:00 delivery. The process exited
134 after the confirmed tool result and final reply were saved; it was not
repeated. The exact result notice was delivered to the local test inbox and
acknowledged through the ordinary tool. The initial payment succeeded, so no
recovery or further test purchase was attempted. This demonstrates the deployed
ordinary addition path, not a live failed-payment recovery.

**At that checkpoint, live product-driven failed-addition recovery remained open.**
The completed ordinary recovery is recorded above.

The later paired investigation cancelled both paid test orders, as recorded above.
The owner completed the originally failed Mathem addition. Isolated tests cover changed
goods/fees/target, final DOM drift, method selection, lost response, reopening
the journal and refusal of a second recovery dispatch. These tests and the
Oda live result do not satisfy the missing Mathem acceptance.

## Owner-authorized Oda Vipps timeout — 2026-09-09

The owner subsequently authorized an intentionally unapproved Vipps test and
explicitly reviewed the six existing dietary unknowns for one ZAFFIRI
Fullkornsspaghetti 500 g package. The installed product at `b8152c6` and a real
Hermes conversation prepared and dispatched the unchanged NOK 246.40 checkout
through normal MCP/Application calls. An earlier confirm had stopped at the
dietary gate before dispatch; after the owner's review, the expired review was
replaced once and its actual finding IDs were supplied to one confirm. No
permanent dietary permission was added.

The original `pay.vipps.no` page showed Oda, NOK 246.40 and a prefilled phone
field matching the household's configured number. Neither **Next** nor app
approval was used. At 19:05:59 UTC that same retained page displayed **“Oh no,
your payment timed out”**, with no phone inputs. Two independent reviewers
confirmed the page and order evidence. This demonstrates the hosted timeout
screen before phone submission, not delivery of an app request or an
independently verified terminal merchant payment status. The current
[Vipps frontend](https://pay.vipps.no/dwo-api-application/v1/deeplink/vippsgateway/assets/index-BAdhHFQL.js)
can display timeout for a `TIMEOUT` status or HTTP 401; its **Go back** control
calls its cancellation routine before returning, and remained untouched.

At the timeout checkpoint, the merchant reported exactly one new `unpaid_order`, with the reviewed
product/quantity, NOK 246.40 and 12 September 07:00–13:00 delivery. All 17 earlier
order records remain unchanged. The cart was empty and the original product
attempt remained `uncertain`. One ordinary same-attempt reconcile returned
`confirmed=false`, `expired=false`, `payment_followup_required=true` and
`retry_allowed=false`; the product does not reflect this hosted timeout as a
recoverable failure. Completed native tool results and final replies were
retained despite CLI exit 134; none of those processes was blindly repeated.

This is a new-order test. Its merchant order details directly identify the
goods, but do not establish an existing-order change ID or the required change-to-goods
binding. A bounded supported unpaid-endpoint GET for this new order returned
the delivery-slot state rather than an unpaid-change payload; that is not a
general claim about the merchant's capabilities. No recovery payment, helper write to protected product journals, cancellation,
runtime change or restart was performed. At that checkpoint the unpaid
order and original timeout page were preserved; the later recovery is recorded
above. **Failed-addition recovery remained open at that checkpoint; its later
ordinary completion is recorded above.**

## Ordinary Oda review — 2026-09-09

The bounded ordinary Oda prepare-only test passed on 2026-09-09 with public
`b8152c6e24e1da51f9cc8b79fcc5c75b99e1c0f1`. The same real Hermes conversation
used the installed skill, normal Oda MCP socket and Application. One prepare
returned a frozen review; a subsequent status read confirmed
`awaiting_confirmation`, and the actual model reply agreed (CLI exit 0).
No helper staged goods or prepared the checkout UI.

The review contains one ZAFFIRI Fullkornsspaghetti 500 g package at NOK 16.70,
NOK 199 small-order fee, NOK 11.70 packaging and NOK 19 delivery: NOK 246.40
total. Delivery remains 12 September 07:00–13:00 Europe/Oslo, CEST
(05:00–11:00 UTC), at the exact selected price. Discounts and deposits remain
null, not proven zero. Independent provider and browser reads match the frozen
cart, selected delivery, account/address, amounts and the actually selected
saved card. The product itself binds that selected card; this is not inferred
from page-wide masked text. Original dietary unknowns remain reported and no
permission or suitability claim was added.

`menu_attribution=cart_only` is now an observed review result. No menu,
quantified plan or frozen menu reference exists; an empty shortfall list does
not establish menu coverage. Existing menu/plan/usage, profile, policy,
configuration and all ten observed existing orders are preserved. The expected
unpaid order remains untouched. One test package, selected delivery and the
unsubmitted review remain for manual owner cleanup. No confirm, submit, auto,
order/payment dispatch, cancellation, real message, timer or Mathem/#54 action
was performed. Cleanup, reservation release and expiry were not claimed.

Earlier native failures and CLI134 outcomes remain retained. PRs #64–66 fixed
observed navigation and selected-card binding. The owner then authorized
automatic saved-card selection and configurable Vipps: PR #68 added those
settings, and ordinary turn08 demonstrably selected the saved card before a
separate amount-parser failure. Oda rendered `1 vare`, while the parser expected
`1 varer`; PR #69 corrects that singular label without relaxing count or fee
checks. The final ordinary turn passed after that fix. No helper performed the
payment-method selection. Live Vipps payment acceptance remains unverified.

Both source changes passed independent correctness/adversarial review and
required GitHub CI. The final candidate passed 1,313 public tests and actual MCP
transport; all 154 published files matched both Bob installations, with normal
27-tool MCP status and healthy Bob. The first update added only the saved-card
default to household state; the singular-label update preserved state bytes.
The earlier 18 CI errors were fixed by using the household timezone in test
fixtures, with all 18 reproduced as passing under UTC while Oslo's date differed.
Production scheduler behavior and CI configuration were unchanged.

The bounded #60 review criterion is complete. At that checkpoint, #50 still
awaited provider-independent same/lower/higher-final-total delivery acceptance,
which is recorded in the later provider-specific trials below. This earlier review adds no payment or delivery-change authority.

## Failed-payment recovery audit — 2026-09-09

The original private journals were inspected on their original host, without
copying credentials, cookies or authentication logs. They establish an explicit
Mathem failure, followed by one helper-assisted payment and eventual acceptance
of the extra package. They do **not** establish the required merchant
change-to-goods binding before that recovery payment:

| Evidence | What it establishes | Missing binding |
|---|---|---|
| Original frozen addition review | Original 17 packages/564.01 SEK, added one/18.50 SEK, combined 18/582.51 SEK; reviewed account, delivery and card | No merchant change ID in the original review or order-change journal |
| Merchant failure notice and recovery link | Explicit failed latest addition, same order number and a merchant change ID | No product IDs or quantities associated with that change ID |
| Recovery payment surface | Same retry URL, delivery, selected card and 18.50 SEK | No product rows; the helper associated the newly observed change ID with the older local review |
| Later accepted receipt | Exactly the intended extra package and 582.51 SEK | An outcome after payment cannot supply the missing pre-dispatch proof |

The cause of the original failure remains unknown. Neither a required device
approval nor its absence was established by that failure. An unpaid tracking
status alone also occurs during delayed successful payment and must remain
distinct from explicit failure.

Current public merchant code supplies a concrete further inspection route.
Oda and Mathem serve identical retry/payment components in build
`e058f12f2909fdc24119655b00ed36caff12210d`. Their native retry carries
`orderNumber`, `orderChangeId` and the selected payment in `retry_modification`
mode. Its review renders financial groups and delivery, without product rows;
unrendered response fields remain unknown. Separately, the supported
`/no/checkout/unpaid/` and `/se/checkout/unpaid/` pages read an unpaid change and
render `payload.orderChange.itemGroups`, including product identities and
quantities. The public component does not consume a change ID from that object.
Its cancel/retry buttons are separate mutations, not inspection controls;
their backend effects were not verified. See the merchant's
[retry component](https://www.mathem.se/_next/static/chunks/18e-h93sthr5a.js)
and [unpaid-change component](https://www.mathem.se/_next/static/chunks/0m0ld4kq-w21j.js),
also served [by Oda](https://oda.com/_next/static/chunks/0m0ld4kq-w21j.js).
This is static client evidence, not authenticated response or payment acceptance.

The original artifacts contain no capture of that unpaid-change page or its
goods payload. Thus its exact change-ID-to-goods association remains unverified;
the sparse retry page does not prove that the shop lacks a richer supported
route. A future implementation must first observe that association for an
existing explicitly failed attempt, then retain provider/account/order/items,
amount/payment, fresh review and authorization, one dispatch and reconciliation
across lost responses/restart. Do not invoke the unpaid page's retry mutation to
discover what it does, reconstruct an old journal or induce a new failure.

Fresh read-only Mathem calls now show the original order cancelled, an empty
cart and no selected delivery. The installation has no pending checkout, cart
change, cancellation or order edit. Its checkout/browser/skill bytes match its
recorded `a109de99` release; state bytes were unchanged by inspection. There is
no remaining original payment to recover. No new purchase/payment authority was
granted, and no recovery, protected-journal write, install or restart occurred.

Oda/Mathem already share protected checkout journals and exact accepted-order
reconciliation. MENY's proven no-dispatch/expiry retry rules do not establish an
Oda/Mathem failed-change contract. Ten existing isolated tests passed for retained
uncertainty, lost responses, restart/expiry, binding rejection, notice replay,
pre-click failure and Oda payment follow-up. No recovery code or new ordinary
Hermes recovery conversation was claimed by that audit. Its recovery criterion
remained **open** until the later bound ordinary run recorded above; static code
and synthetic tests alone could not complete it. The already accepted order,
addition, free delivery, cancellation and #60 review remain accepted.

## Observations and source binding

Separate authenticated calls on 2026-09-08 at 08:28 UTC used Oda's
`https://oda.com/mcp` and Mathem's `https://www.mathem.se/mcp`, each through its own
installed OAuth client and identity. Both running services used public source
`b8cc15f3cbaa54fc86d14d1c921e69da6c423503`. Each server independently negotiated
protocol `2025-11-25`, reported server version `1.1.0`, and returned 25 upstream
tools. These differ from the product's 27 served tools. All 25 input schemas,
output schemas and annotations compared equal; this does not establish equal
operation semantics.

Original responses, receipts and journals stay on their original private host.
The task evidence set `issue50-ordinary-20260908` contains sanitized contract and
semantic summaries, source hashes, the isolated candidate and retained failures.
The independent raw contract digests are Oda
`3f3416c233bf8181f8414cf393b14e1665c4031ee21052653692228eba56d2a4`
and Mathem `1bfae89b0306a8fc61cd2ba6e271b5682de4d5361c8c370691ec0e6b59840c7e`.
No credentials, cookies or authentication logs were exported.

| Interface | Separate actual observations | Conclusion for this scope |
|---|---|---|
| `initialize`, `tools/list` | Both report checkout/payment in the shop, not MCP; matching 25-tool contracts | Shared transport; protected browser operations still required |
| `get_cart`, `get_delivery_addresses` | Both authenticate separately; both carts empty, total string `0.00`, no cart delivery | Common read shape; separate account IDs and browser binding required |
| `product_search`, malformed input | Oda returned 500 g Sopps pasta at 26.50 NOK; Mathem returned 500 g Barilla pasta at 15.95 SEK. Both reject integer `queries` with `isError`, a list-type validation error and no structured result | Shared parsing/error handling; product IDs, prices and currencies are provider-specific |
| `get_orders`, `get_order`, `order_tracking` | Separate cancelled receipts returned currency, ISO delivery date, display, gross amount and product quantities. Both omit `productQuantityCount` and order address. Oda: 24 packages/948.05 NOK; owned Mathem test order: 18/582.51 SEK | Sum product quantities for both. Bind full date and currency. Cancellation status is observed; no new cancellation was submitted |
| `get_delivery_slots` | For 13 September, Oda returned 20 available slots with exact prices 19–79 NOK; Mathem returned 19 at zero SEK. Both returned dated timestamps with offsets | Shared slot normalization; Oslo/Stockholm provider binding. One date's free Mathem slots do not establish a store-wide restriction |
| Browser account/receipt | After owner Oda login, both exact cancelled receipts and their own MCP address references matched independently on 8 September. Oda uses `Total inkl. MVA`; Mathem uses `Totalt inkl. moms` | Shared reader with provider origins/labels verified separately; frozen-reference mismatch rejected for both. This is read acceptance, not payment acceptance |
| Cart writes and slot selection | Ordinary Mathem turns staged the original and added package under their own authority. The later bounded Oda #60 conversation staged one 500 g package and selected 12 September 07–13 Oslo at exact NOK 19 | Separate live write/readback evidence for both; Oda remains prepare-only, with no purchase |
| Addition/payment | Earlier addition required helper recovery. New ordinary candidate flow paid one extra package once and later reconciled two packages/143 SEK | Ordinary paid addition demonstrated at this checkpoint; the later ordinary failed-payment recovery is recorded above |
| Delivery change | Earlier acceptance followed UI preparation. New ordinary candidate flow began with a closed browser, prepared 10 September14–16, submitted once and reconciled unchanged two packages/143 SEK | Ordinary free change demonstrated; paid review remains unverified and the zero limit remains local |

## Established Oda candidate and retained differences

This section records the 8 September candidate checkpoint. Later ordinary Oda
and Mathem review, payment and recovery results are recorded above.

Before changing browser control flow, an isolated, network-free Oda navigation
candidate rebound the origin, locale, routes, currency and provider defaults to
Mathem. Its actual navigation JavaScript, exercised against the separately
preserved Mathem modify-page shape, rejected `/se/checkout/modify/`. This is a
bounded navigation result, not a successful trial of the whole current website.
No Mathem identity was sent to Oda and no production route guard was removed.
The later isolated candidate at browser SHA256
`ba3ffb43c72c75c5c27d064f75c49db49c509ae4113dc19f29514f0ea1925ffc`
rebound the complete Oda class before receiving Mathem data and used only the
Mathem installation's own client/profile. Original receipt/account binding
passed; the normal cart surface returned `wait`, and checkout review stopped
at `cart is empty`. No slot or destination was selected and no submit occurred.
An earlier redundant read failed with a browser operation error; its closed
browser and unchanged-state result were preserved. The focused continuation
completed, also with unchanged state and normal browser close.

At 12:41 UTC on 8 September, the complete established Oda initial-review flow,
with static Mathem origin/provider/locale binding applied before customer data,
passed against the authorized one-package Mathem cart. Source browser SHA256
was `2d9dc062599df874dd037e120149491aec883621eec302a7410ecf39b0abc91b`;
the isolated rebound source was
`1fcf89bf1e7959e55cb32fe76f90bcf57aba47e4eea554eb1bb7548f260cf725`.
Account, cart continuation, recommendations, expanded items and total review
passed at 124.50 SEK. Cart and household state remained byte-identical and the
owned browser closed. Submit methods were disabled. This proves the bounded
initial control-flow comparison, not ordinary product/payment acceptance,
selected-card semantics or the active-order modify route.

Shared transport, Application order journals, protected confirmations and
reconciliation remain in place. Product quantity counting now uses the shared
product-line implementation because both current services omit the old Oda
aggregate. Checkout/addition reconciliation requires each provider's currency;
delivery-change reconciliation also binds the complete requested ISO date,
including year. Synthetic wrong-currency/year outcomes reproduce the former Oda
false-positive and now retain the pending attempt without allowing retry.

Oda delivery confirmation now rereads the prepared review instead of repeating
slot navigation. Its delivery/card snapshot and existing amount checks execute
in the final browser turn after the fresh provider/expiry callback. Missing or
changed review stops before dispatch; a lost final response remains uncertain.
This is isolated control-flow acceptance, not authenticated Oda payment acceptance.
The shared account/receipt binding now also supplies Oda's omitted order address.
The original address reference is frozen at `change_begin` and checked again
for review, submit and accepted-state reconciliation. Addition delivery comes
from that original order, independently of the current cart. An explicit
contradictory MCP address is never overwritten by receipt fallback. Legacy
uncertain operations without an original binding remain pending; today's
selected address cannot repair missing historical evidence.

Oda new-order review now checks the browser account against the selected MCP
reference, with a second reference check before the final callback. New-order
and addition confirmation compare the complete reviewed surface—items,
availability, login, delivery/address, masked card and submit control—in the
same JavaScript turn as the amount check and only click. Cancellation retains
its original browser launch arguments from the shared binding's first open.
The initial Oda address check is restricted to one delivery section, matching
Mathem's existing rule; an address elsewhere in the body cannot satisfy it.
These changes have isolated regression evidence; native paid acceptance is
still required. In particular, Oda's masked-card text extraction has not yet
been verified against the current selected-payment layout. Mathem already uses
its observed scoped payment parser. Receipt/account reads do not prove Oda's
selected-card semantics, and no unobserved replacement selectors are assumed.

Mathem's Swedish navigation, original/added/combined amount rows and independently
verified receipt/account binding remain narrow overrides. The prior retry page
omitted goods. Neither the original native journal nor a fresh failed-change URL
proves that a later sparse retry page belongs to those frozen goods. A proposed
recovery implementation was therefore rejected before publication; no new retry
action is exposed. The original payment failure's cause remains undetermined:
merchant failure text was observed, but a required card/device challenge was not
established. Missing cause alone does not authorize another payment.

At that checkpoint, Mathem's zero-payable guard was **an implementation limit**,
not a proven shop rule.
[Mathem describes variable delivery fees](https://support.mathem.se/sv/article/384bf5)
and [delivery discounts](https://www.mathem.se/se/about/gratis-leverans/).
[Oda describes delivery edits with payment of a difference](https://hjelp.oda.com/no/article/89b454).
The shared final-total implementation described below replaces that limit with
verified original/final/payable amounts and applicable price authorization.
Its changed-price merchant acceptance still requires a supported live review.
Free-slot observations cannot establish that paid edits are unavailable everywhere.

## Shared binding verification after Oda login

On 2026-09-08, separate isolated candidates used each running installation's
own provider client, browser profile and browser UID. Installed source remained
`b8cc15f3`; shared-reader candidate browser SHA256 was
`3134935b6dfc66f6eb714a68d77ab8f33c0edbc58b0ef464174ecc44754722cb`.
Both read the exact original cancelled receipt, matched its account reference,
reread the frozen binding and rejected a deliberately wrong reference. Each
owned browser closed normally; household state bytes remained identical. No
checkout click, payment, cancellation or protected journal write occurred.
This independently verifies the shared primitive, not an installed Application
or model flow. Later final-click/launch fixes have separate regression tests.

A subsequent read at 11:25 UTC found Mathem's cart empty and its two orders
cancelled. Oda had unrelated nonempty cart work, which was preserved. Across
10–16 September, Mathem returned 10–19 available slots per date, all at zero SEK;
Oda returned 20 per date with prices spanning 0–89 NOK. These reads use the
installed adapter's validated minor-unit prices, not guessed raw response keys.
They do not establish a paid Mathem edit contract or authorize new orders.

At 13:55 UTC, after the new ordinary addition had reconciled, a separate read-only
browser observation opened the owned order's actual delivery calendar through
its merchant-provided action link. The installed browser source SHA256 was
`a10895f5c31ddf3f8ea70e7a1127ca6cec7308927243689acb7cf20d2bf0392e`.
The initially visible calendar covered 9–11 September: 38 parsed available
windows on 10–11 September displayed `0 kr`; the only button price text anywhere
in that table was `0 kr`. Empty cells were not counted as available windows.
No slot was selected and no submit occurred. Original account/receipt binding,
cart, order/tracking and household-state comparisons passed; the observation
browser closed. The first subsequent ordinary turn stopped after fresh reads because the model
interpreted the preserved local cart plan's missing quantity as an order
discrepancy. No change began. The plan compares with the now-empty cart; the
merchant order independently contained both paid packages. That distinction was
clarified without editing the plan. Subsequent ordinary delivery review must
navigate independently from a cold browser. This establishes the offered current calendar, not paid
edit behavior or a permanent store restriction.

## Remaining acceptance

The supported price-changing delivery review/authorization criterion remains
open. Ordinary failed-addition recovery and the affected Oda #60 review are
complete, with their separate evidence above. Any future fix still requires
verification on its exactly authorized runtime/client targets. No new general
order, payment, deployment or recipient authorization follows from historical
one-shot tests.

### Shared final-total delivery rule — 2026-09-10

The candidate uses one authorization rule for Oda, Mathem and MENY: an exact
requested window covers the same or lower verified full order total; an increase
requires an explicitly scoped maximum or one approval of the fresh review.
Original and new totals include merchant fees/discounts. Payable is recorded
separately and is never used to derive the new full total. Ordinary additions,
payment recovery and scheduler policy retain their existing behavior. See the
[tool contract](reference.md#delivery-only-changes-and-price-authority).

Application tests exercise all three providers under fresh and standing policy,
including same/lower/higher totals, limits, fresh approval, goods drift and lost
dispatch/restart replay. Oda/Mathem DOM tests independently parse original,
final and payable values and recheck them at the final control; selected-card
drift also stops Oda. These are synthetic checks, not changed-price acceptance.

On 10 September, the existing Bob Oda conversation used his installed skill and
MCP to read status, orders, products and delivery slots without opening an edit.
It found no modifiable future order and a 500 g pasta package at NOK 16.70;
12–13 September slot quotes varied, including NOK 9/19/29. These are slot
quotes, not verified changed-order totals. Private household state and Bob's
global Oda route remained unchanged.

The separate Bob Mathem conversation used the existing dedicated socket and
installed skill/MCP to read status, orders, products and slots, including exact
order reads. Its persisted final reply reported no modifiable order, a 500 g
pasta package at SEK 18.50 and zero-price slots. The CLI later exited 134;
retain that failure rather than describing a clean process pass. Household
state and global routing remained unchanged. Separate adapter reads confirmed
both providers' order statuses and date-specific slot prices. Neither
conversation prepared or submitted checkout.

At that read-only checkpoint Bob exposed Oda and isolated Mathem sockets,
without an active MENY service/socket. Earlier authenticated MENY adapter reads do not establish
an installed Bob/MENY checkout path. A lack of test installation or nonzero slot
quotes is not evidence that a merchant cannot support the requested change.

The owner subsequently granted explicit full live-test authority, including
new test orders, payments, delivery changes, cleanup and the retained MENY
setup. The following results supersede the earlier authorization blocker.

#### Ordinary Oda delivery outcomes

The same-total outcome used installed `issue50-live-completion-20260910`, based
on public `112e939d` plus the delivery navigation fix. The lower/higher outcomes
used the subsequent `issue50-live-completion-20260910-signed` candidate, which
also preserves signed delivery adjustments. Bob used his ordinary installed
skill/MCP/Application conversation throughout.
One 500 g fullkornsspaghetti package remained unchanged on the same owned order.

| Requested 12 September window (Oslo) | Original full total | Reviewed final total | Signed payable | Observed result |
|---|---:|---:|---:|---|
| 13–18 | 246.40 NOK | 246.40 NOK | 0.00 NOK | No cap or additional approval; one confirmation and same-attempt reconciliation; independent merchant read matched |
| 16–21 | 246.40 NOK | 236.40 NOK | −10.00 NOK | No cap or additional approval; one confirmation and same-attempt reconciliation; independent merchant read matched |
| 04–09 | 236.40 NOK | 256.40 NOK | 20.00 NOK | Prepare required approval and left the merchant order unchanged; one explicit approval of that frozen confirmation, one submission and reconciliation; independent merchant read matched |

The final higher-total proof SHA256 is
`eb90a7e1667458f5e116c9e63167de35fb6690a2f4fa58623eb81dfa8256ac6e`.
All four price-test conversations exited normally without an abort or watchdog
exit. The lower adjustment originally stopped safely because its sign was lost
in browser parsing; the corrected review and final-control checks preserve
negative delivery payable while original/final totals remain nonnegative.
Initial navigation failures also remain retained: the correction shares the
verified homepage order-card/calendar sequence and active browser context.

Bob then cancelled only the owned order once, reconciled that cancellation and
restored the original Vipps preference through ordinary setup. An independent
merchant read confirmed cancellation and no protected pending operation remained
(proof SHA256 `3c4dde3d4845bc30b754f052679369c723055be41b65c6c63f620207aa75ce87`).
Merchant adjustments and cancellation do not establish bank settlement, refund
or reservation release. Separate Mathem and MENY results follow below.

#### Mathem and MENY delivery trials

A new ordinary Bob/Mathem test prepared one 500 g pasta package at SEK 124.50
for 12 September 06–11 and dispatched its original payment once. The provider
presented a Bank Norwegian challenge; Bob selected the app method once.
Same-attempt reconciliation later positively identified payment failure and the
same unpaid order, with supported recovery preparation available. Owner-authorized
ordinary recoveries then each reached an actual app challenge and later positively
failed on that same order. The cause of each bank failure is not established.
A defect excluded new-order recovery children from late-failure observation;
the repair now binds each child's own terminal failure to its exact unpaid order,
unchanged goods, full total, account and delivery before permitting a fresh review.
The focused 43-test recovery suite passes, including unknown status, wrong-order
failure, restart and historical replay. One isolated Application reconciliation
preserved the native failure page during deployment; that bridge is technical
evidence, not an ordinary acceptance result. Subsequent installed Bob
reconciliation demonstrated the repaired failure path. The latest owner-ready
fresh preparation stopped before dispatch because the merchant changed its date
label to “Imorgon” after Stockholm midnight. A separate pure read proved that
URL, account/address, saved card, SEK 124.50 total and final control all matched;
only the numeric-date parser rejected the relative label. The repaired shared
matcher resolves one observed today/tomorrow label using the provider's local
date, rejecting mixed/duplicate dates and changed times. All 86 Mathem checks
pass, including midnight, year and leap-day transitions. Existing numeric-date
and year-matching semantics are unchanged. The failed history remains preserved. Following the repaired fresh review,
ordinary Bob confirmed one new recovery and selected the app method once. The
owner-requested Signal alert was sent once through Bob's existing exact sender,
with a returned message timestamp. The owner then approved, and an independent
installed order read reported paid-and-modifiable, one package and SEK 124.50
(proof SHA256 `56ef0cab36e9b3ff3d01e8bfe5cd337b7f2ea17439a23f57b9b0229cc0d262ad`).
Ordinary reconciliation then confirmed that same order. A requested delivery
selection still stored the relative merchant label; deriving its stable display
from the already verified, offset-aware slot corrected this second boundary.
Ordinary Bob reselected 12 September 07–09, reviewed original/new totals of
SEK 124.50 and zero payable, and confirmed once without a price limit or extra
price approval. Same-attempt reconciliation and an independent merchant read
confirmed the changed window, unchanged goods and paid/modifiable status
(proof SHA256 `0c0c712c364023d4a7ae77adc213c5ec0dcbf031639af2ae31e4491d73b5dad9`).
A second ordinary full review for 09–11 also returned SEK 124.50 and zero payable;
it was not submitted. Across 11–24 September, all 231 returned selectable slots
quoted an exact zero delivery fee; no distinct priced alternative was returned.
Mathem therefore demonstrates an unchanged full total. Lower/higher full-total
outcomes were not demonstrated; the quote observations are not 231 full-order
reviews or a claim that other prices are globally unavailable.
The first cancellation review then incorrectly reported unavailable because the
receipt used “Imorgon”; a read-only check showed the matching order/total and an
enabled cancellation button within its reported deadline. Cancellation now uses
the same provider-local date reader, retaining its required bound delivery and
all total, order and final-control checks. The repaired ordinary conversation
prepared cancellation, confirmed once and read the same cancelled order. A
separate installed read independently confirmed cancellation with no pending
operation (proof SHA256 `65ba66e31a3bdd157f6b3f922ba4b6f92531475f5ca43e0b525858d447cb427e`).
Other provider journals remained unchanged. A further read-only audit found an
empty cart, no pending operations, all 29 notices sent, and five pre-existing
order entries unchanged. Address and unattended-delivery settings also matched
the initial empty cart. Its unused 12 September 09–11 cart selection remained;
the initial cart had no selected window. Payment release/refund was not established.

The retained MENY setup was temporarily selected after Oda cleanup. Direct
browser egress returned a blocked page. Using the existing approved Bob proxy
with the same dedicated profile restored access; no cookies or credentials were
copied. A stale Chromium lock from the replaced container was removed only after
exclusive stopped-profile ownership was verified. Ordinary installed Bob status,
cart, order, delivery and catalog reads then succeeded with native CLI exit 0,
and all three provider state files remained unchanged. The readiness proof
SHA256 is `7edabf020e251ae3b979a492c375aedcd4dc8e47baa44175b7d00f1806977907`.

After that probe restored Oda, the owner explicitly authorized the existing
five-line MENY cart for the full trial. The retained MENY route was selected
again with the same dedicated profile and approved proxy. Ordinary Bob selected
13 September 08–10 once and verified it, then used the digest-bound `keep_current`
path for the five unchanged quantities. The saved menu and recipe usage were
preserved. The selected slot's “from 0 kr” label does not establish the final fee.
Checkout preparation first stopped with `MENY checkout control changed`; one
further preparation stopped with `MENY payment page did not finish rendering`.
A separate current-page read found the payment-information heading but no Vipps
control. The loaded public checkout code confirms that Aera card-storage terms
indicate Card selection and that “Til betaling” submits the order before opening
the payment page; it is not a safe navigation step for finding Vipps. A later
ordinary preparation succeeded: NOK 599.90, five unchanged quantities, the
selected delivery and Vipps. Passive observation saw the merchant's native
payment-options responses succeed with Card, Invoice and Vipps; the earlier
missing control's cause remains unproven. Bob declined confirmation on the
known lamb exclusion, with no tool call or payment in either confirmation turn.
The test was therefore reduced to the four remaining existing lines. Ordinary
cart get/change/get verified only the lamb removal. The owner separately accepted
the four products' reported ingredient/dietary uncertainty. Fresh preparation
returned NOK 545.90 and Vipps. A changed control and an expired delivery
reservation stopped before dispatch; a later actual Vipps handoff expired and
was reconciled before the owner requested one new attempt. The owner approved
that new handoff. Its receipt read failed after navigation, losing the transient
confirmed order ID. The repair persists only the exact authenticated receipt
identity before navigation and reads the latest completed, matching order
response; malformed responses remain errors. For this base order only, a
separate diagnostic restored its original authenticated receipt URL from the
dedicated browser history, without repeating payment or editing the journal.
Ordinary same-attempt reconciliation then confirmed the exact four-item order.
This assisted base-order restoration is separate from delivery acceptance.

The receipt DOM showed a reservation but no full-total label. The exact provider
order response supplied `totals.totalGrossAmount`; the reader now binds that
amount to the same `ngOrderId`, retaining reservation reporting separately and
rejecting a conflicting visible full total. An independent installed order read
verified NOK 545.90, the four unchanged quantities and 13 September 08–10
(proof SHA256 `f9f666ac5d5574be7ab60c5510919331200c6d4d7a3ec013746868b738640596`).
Ordinary delivery listing returned 68 unique selectable windows for
13–19 September, all with **from** zero quotes. Those minimum prices do not
establish zero fees or exclude lower/higher full totals. Two ordinary preparations for 13 September 10–12 stopped before payment.
Although selection had verified that window, the cart omitted its time and
preparation redundantly tried a temporary alternative reservation. Passive DOM
observation showed the selected window and enabled keep control, followed by a
closed picker before the expected confirmation. Delivery-only review now compares
the fresh normalized selection against the frozen requested slot and reuses it;
window drift and actual reservation expiry still stop checkout. The next
preparation passed that step but gave up before a valid exact-order response
arrived. The existing two-phase read now waits up to 40 polls per phase, retaining
one reload and all exact-response, latest-body, identity, status and amount checks.
The subsequent prepare verified the selected window and NOK 545.90, but stopped
before submission because an existing-order checkout uses **Send oppdatering**.
Passive authenticated DOM inspection and the public frontend establish that this
control submits the bound existing order; its response may open payment or return
an order directly. The control caption alone does not prove that mobile approval
is unnecessary (private evidence SHA256
`9df748326a819a9ae362d1536b5e9d306680b1706408b02e18abd1ad4f8f1f29`).
The installed repair binds all four final-control/recovery selectors to the
existing order and accepts an actual authenticated same-order receipt or actual
Vipps gateway, preserving the existing dispatch fence and reconciliation. It does
not treat the shared order POST as proof of a payment redirect.

On 11 September, ordinary Bob preparation for 13 September 10–12 showed original
and new full totals of NOK 545.90, difference zero and no new price approval.
One confirmation, same-attempt reconciliation and exact-order read confirmed the
change (native CLI 0, stdout SHA256
`018ef418bab52e8da42444e7c7e712394d4ca52be1c67e1045841e38a14c00c1`).
A separate installed merchant read verified that window, full total and unchanged
products/all other order fields (SHA256
`af86d3f26a2597ac5b6d31cfe9b1c70e0725f3a5e459fa84c9a381e38ec16431`).
The review's payable field was the full NOK 545.90; it is not evidence of a new
charge. This update returned no new Vipps handoff. A bounded passive observer
ended before confirmation and captured no order POST/receipt; that missing
capture is not used as proof.

The next ordinary change selected 14 September 08–12. Full review showed
NOK 545.90 → 525.90 (−20.00), with no maximum or new approval required. Bob
confirmed once, reconciled the same attempt and read the exact order again
(native CLI 0, stdout SHA256
`a8617f871aff5c0e1bddc2b22993b747b7eb801f722a8e38e5b1ad97f61b2683`).
The separate merchant read matched the native frozen review and confirmed
NOK 525.90, the requested window and every unchanged product/quantity (SHA256
`546b51c29c0c36231cfb7fd15defe8e378f931f62c9df9bfe0c17f2489bb7a1d`).
A separate passive observer captured the actual successful order POST with
that exact owned order/full total, no redirect, and then its authenticated
receipt; it stopped itself after capture (SHA256
`7c98e8c63e4859b7fbbd4ae938d933fa32de7707333e457da86e0e2ca4cd97c4`).
This demonstrates a merchant order-price decrease, without establishing refund
or bank settlement.

The first narrow-window review for 14 September 07–08 returned NOK 525.90 →
545.90 (+20.00), with no maximum and an explicit price approval required. After
one approval under the owner's full test authorization, one ordinary confirmation
sent the update. The successful same-order response contained NOK 545.90 and
no redirect, but the receipt remained an unauthenticated empty shell. Ordinary
same-attempt reconciliation correctly stopped on its login requirement. One
separately reviewed diagnostic reload of that exact receipt restored its
authenticated confirmation without login, payment or journal edits. Ordinary
reconciliation and a separate merchant read then confirmed the requested window,
NOK 545.90 and unchanged products (proof SHA256
`9d786df1b25dd2818e1f12d7170a5ee82e6687ea36a64218063b2d16d4707ec0`).
This first higher result is assisted evidence and does not establish ordinary
higher-price acceptance.

The native receipt reader now permits one bounded reload of the same exact
MENY receipt URL during reconciliation. It still requires authentication and
confirmation text, rejects changed or malformed receipt identities, and preserves
the uncertain attempt and receipt when rendering remains incomplete. It never
reloads checkout or a payment callback. Five focused tests include execution of
the actual URL extractor; all 377 core checks pass. A fresh ordinary lower-price
trial initially failed during preparation before any confirmation existed. One
fresh review of the retained selection then returned NOK 545.90 → 525.90 without
new price approval. Ordinary confirmation, reconciliation and independent
merchant readback verified 14 September 08–12 and every unchanged product
(proof SHA256 `7eac0ff0752f138628bdc2597d7a21aa8c73231bc5171cc32c2513a8bd95ae46`).
A passive observer separately captured its successful same-order response and
authenticated receipt (SHA256
`77289b12e1798caa82c43373c8a09a23c4b682a6b7b8530042eb5decc33db75a`).
The fresh higher-price review returned NOK 525.90 → 545.90 (+20.00), with no
stored maximum and `confirmation_required=true`. An earlier stale-selection
check stopped before dispatch. A later uncertain selection was resolved through
an ordinary list read, without repeating it. An unrelated host reboot then
interrupted the fresh review before any confirmation call; the durable journal
remained `awaiting_confirmation`. Restoring the same reviewed service preserved
all journals, profiles and configuration and involved no browser assistance.
Bob confirmed that still-valid review once under the exact +20.00 approval,
reconciled the same attempt and read the owned order (native CLI 0, stdout SHA256
`ea1d75497005c04f639aa8854c7f4123171b324360deec4596cd19c63a0785af`).
The independent merchant read matched the review, NOK 545.90, 14 September
07–08 and every unchanged product/quantity (SHA256
`b1ce69f549dc95be970bf0260813507d898e41f68e28bc2ad199e181878819de`).
A passive observer captured exactly one successful same-order POST and its
authenticated receipt, with an empty redirect and no Vipps gateway (SHA256
`f17c9b20d11e239d47315b0ab489c7b98a6a3de6b152f3a0e2b079e85bdca9cf`).
This completes ordinary MENY same/lower/higher-total delivery acceptance; it
does not establish a new bank charge or refund.

Final cleanup on 12 September (Europe/Oslo) first exposed an already-active
merchant edit after checkout had cleared the local edit journal. The bounded
repair adopts that edit only after verifying the exact authenticated order and
cart code. Ordinary `change_begin` then `change_abort` ended it without changing
the confirmed order. Ordinary cancellation used one fresh prepare, one confirm
and same-attempt reconciliation; an independent order read verified cancellation.

Cart restoration stopped after a partially applied two-item batch: lamb was
added, salmon was absent, and the subsequent carrot addition was not dispatched.
The unresolved batch was not replayed. Separate operator reconciliation verified
completed, uncached HTTP 200 cart-sync and calculator responses, three stable
cart reads and the same exact three variants. It archived the original pending
journal, recorded only the observed addition, marked the local cart plan for
review and cleared only that matched pending operation. This is assisted cleanup,
not ordinary cart-recovery acceptance. Two subsequent ordinary `ensure` calls
restored salmon and carrot separately. Independent reads verified all five
original variants at quantity one, no extra variants and no pending operation
(final-cart proof SHA256
`76f8270a67f950a8c8c0ce81d699f885e84f44b6b85878366a13e6280ea6bd46`).

The original Oda route was restored with all current provider journals preserved.
Its first verification stopped on environment-list ordering; a separate read-only
check confirmed identical environment mappings, healthy Oda RPC, all 155 original
source files and unchanged other containers. The later owner-selected Bob model
was preserved. Both independent code reviews approved the final implementation;
1,390 standalone tests passed with nine optional macOS skips, and the real MCP
SDK/stdio/socket, reconnect and interrupted-dispatch checks passed.

Repeated ordinary Bob MCP startups also exposed actual cancellations at the
configured ten-second connection timeout. Increasing only that connection timeout
to thirty seconds restored native tool discovery without a Hermes code change or
restart. Completed failed-addition recovery and the Oda delivery matrix are
retained.

### Earlier ordinary acceptance — 2026-09-08

On 8 September the owner approved one bounded Mathem order, its addition,
delivery change and cancellation, plus the exact Bob/Oda and dedicated Mathem
rollout. Published source `058c43f` was verified in all three installations and
through both installed MCP connections. Two source-directory permission defects
in the deployment procedure were preserved and corrected; product state and
configuration bytes were unchanged through rollout.

The first ordinary Hermes prepare staged one package and selected delivery,
then reviewed 124.50 SEK including fees. It did not submit. Inspection caught a
shared attribution bug: a supplemental-only cart would mark the unrelated saved
seven-slot menu ordered. Checkout now distinguishes cart-only purchase from
quantified menu shopping and preserves that unassessed menu. That initial review was replaced through ordinary prepare after the fix, before
payment. The first prepare alone was not completed-order acceptance.

The next ordinary Hermes prepare failed with `Mathem checkout navigation did
not finish`, before confirmation or payment. A read-only observation found the
complete Mathem cart page with exactly one enabled `Fortsätt` button. The shared
cart startup now reuses the established Oda open/reload/settle/click sequence
with separately observed provider labels and origins. Mathem retains its exact
storefront full-cart link, destination choices and scoped card parser. Delayed
navigation is polled without repeating a dispatched control; lost click replies
stop. Login, wrong origins and ambiguous controls stop before continuation.
The installed candidate's fresh ordinary Hermes prepare then passed, followed
by the required local notice and one payment confirmation. The first two
reconciliations reported `unpaid_order`; the original attempt stayed uncertain
with retry disabled. A later ordinary reconciliation returned
`confirmed=true`, `paid_and_modifiable` and the exact 124.50 SEK order, without
another payment. Its result notice was delivered locally. Initial checkout and
delayed reconciliation are now demonstrated; delivery-change acceptance remains
a separate criterion. The cart-only menu and usage stayed outside
ordered-menu attribution.

The ordinary addition then staged exactly one extra package. Its first prepare
failed during navigation without payment; the CLI exited 134 after recording its
result. A bounded diagnostic now records only fixed route/action names. The
intermittent redirect cause remains unproven. A later ordinary prepare succeeded,
but confirmation correctly stopped because Mathem had populated the addition
cart's delivery with the original order's delivery during browser navigation.
Fresh MCP comparisons found only that delivery field changed; goods and 18.50 SEK
were unchanged. The guard was retained and a fresh ordinary review was required.
That review passed and the local before-notice was delivered and acknowledged.
After one payment confirmation, the immediate ordinary reconciliation reported
`unpaid_order_change`; retry stayed disabled. A later ordinary reconciliation
returned `confirmed=true`, `changed_existing_order=true` and
`paid_and_modifiable`: two packages, combined 143 SEK. No helper submitted or
recovered payment. The after-notice was delivered to the local receiver. Natural
CLI 134 exits are retained separately from the persisted tool results and do not
justify repeating an action. Tool-call IDs distinguish new intents from history
rows reinserted by conversation compaction. This completes the ordinary paid
addition criterion; it does not demonstrate recovery of an explicitly failed
payment.

The next ordinary delivery sequence began with the browser closed. Hermes called
`orders change_begin`, MCP delivery list/select for 10 September14–16 Stockholm,
and checkout prepare; the product performed all calendar/review navigation.
Review bound the same original account/address, two goods, original and combined
143 SEK and zero payable. One confirmation initially remained unconfirmed with
retry disabled despite merchant status `paid_and_modifiable`. One reconciliation
of that same attempt then confirmed the exact new delivery and unchanged goods
and amount. No second submit, slot choice, helper preparation or result notice
was needed. This demonstrates the formerly UI-prepared criterion through the
ordinary installed skill/MCP/Application path, within its free-change limit.

The same ordinary conversation then prepared cancellation of only the new test
order, confirmed once and received `cancelled=true`. Fresh product reads and an
independent provider verification both confirmed the same cancelled order, two
packages/143 SEK and the changed delivery. The cart was empty with no selected
cart delivery. No protected operation or order edit remained pending. All four
new local notices were acknowledged, as were the four retained historical
notices. Both pre-existing first-page order entries remained byte-identical and
no unexpected order appeared. Original menu, menu planning, usage/history,
configuration, standing policy and disabled scheduler were preserved; the own
cart plan was unchanged from its pre-addition snapshot. No timer was created.
The verifier only read original protected results and current provider state;
it did not perform any product action.

| Ordinary scenario | Expected | Actual installed candidate outcome |
|---|---|---|
| Mathem initial order | One package, at most250 SEK; one payment; retained saved menu | 124.50 SEK, one dispatch, delayed ordinary reconciliation confirmed; cart-only/not-assessed |
| Mathem addition | Exactly one extra package, at most25 SEK | 18.50 SEK extra; one payment dispatch, later ordinary reconciliation confirmed two packages/143 SEK |
| Mathem delivery | Normal start, valid review, one submit, exact new date and unchanged goods | Cold browser to 10 September14–16; zero payable; one submit and one later reconciliation confirmed |
| Mathem cancellation | Cancel only the new order and preserve unrelated activity | One cancellation confirmation; merchant cancelled; cart empty, no selected cart delivery/pending edits; original two entries unchanged |
| Oda affected review | Own account, ordinary review and verified selected-payment field | Account/receipt reads and paired isolated guards passed; current cart empty. Separate no-purchase review scope awaits owner authorization |

The required fleet profile passed on current private main plus the eleven owned
files, including the separately committed Grok changes for regression only.
Public feature `1797d13840ea082a02d42373f412bd9f448348a5` was merged through
PR58 as `d5084d2e9add484a55f105b43a72464d4da8d998`, with identical trees.
Required GitHub CI passed all 1,284 tests with three optional Linux skips.
The first local public-suite run exposed five module-import errors from one
privately rooted exported test; the corrected export passed all 305 tests in
those five modules before the full CI pass. The failure log is retained.

All 151 files in the actual published archive matched the reviewed manifest.
The exact immutable feature release, excluding the disjoint unmerged Grok
changes, is now installed in the three approved Bob/Oda/dedicated Mathem
services. All 151 source files per service and both native SDK connections with
27 tools passed; Bob was healthy. Original household state/configuration,
empty carts, cancelled own Mathem order and unrelated services were unchanged.
Deployment retained an initial macOS archive-metadata rejection and a strict
receipt-page close rejection, both before any restart. Explicit continuation
verified the actual frozen-account delivery page before closing only the own
browser; no lock or journal was removed or restored. Both image and Bob bind
passed complete unprivileged runtime preflight before the three idle restarts.

The retained unauthenticated installation completed a normal update to the
same 52 runtime/skill files and reconnected through its installed SDK with
27 tools. All 1,581 state files and configuration remained byte-identical;
the fixture stopped normally. Documentation follow-up revisions do not change
the pinned functional runtime or the remaining acceptance criteria.
The final real Hermes continuation on the published runtime exited normally
and made exactly three new tool calls: status, cart get and exact own cancelled
order get. It reported Mathem ready/standing, cancelled order, empty cart and no
selected cart delivery. Pending checkout/cancellation and order edit were null;
pending cart was not exposed in those tool replies, so the model correctly
marked it unshown. The separate read-only state verifier confirmed no pending
cart operation. Household state/configuration and service identities were
unchanged, and no provider browser was opened.

Existing unrelated state,
original standing policy, private journals and the explicit documentation/client
exclusions are preserved.

Relevant isolated regressions are in `test_meal_concierge_mathem.py` and
`test_meal_concierge.py`: separate provider quantity/currency/date examples,
prepared-review drift and one-click JavaScript, plus existing expiry/auth,
lost-response/restart, cancellation replay, dietary/substitution/shortfall and
notice contracts. These tests do not substitute for the remaining native flows.
