# Oda and Mathem: current evidence and open ordinary flows

As of 2026-09-08, #50 is **incomplete for ordinary end-to-end parity**. Its earlier
closure accepted a recovery-assisted addition and a UI-prepared free delivery
change. Those results remain valid within that scope; neither demonstrates the
whole product sequence requested in the resumed acceptance. Bank reconciliation
is outside this work and is not a completion gate.

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
| Cart writes and slot selection | Existing native Mathem acceptance retained; no new writes in this inspection | Matching schemas alone do not verify new mutation semantics |
| Addition/payment | Earlier original native order succeeded. Its native addition payment failed; a separate helper later paid the retry page | Ordinary paid addition and product-owned recovery remain open |
| Delivery change | Earlier native review/confirm/reconcile followed UI preparation. Current code limits Mathem to zero payable/unchanged total | Ordinary navigation from a normal starting page and price-changing review remain open; the zero limit is local |

## Established Oda candidate and retained differences

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
completed, also with unchanged state and normal browser close. Full review of
an authorized nonempty Mathem order flow remains outstanding.

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

Mathem's zero-payable guard is **an implementation limit**, not a proven shop rule.
[Mathem describes variable delivery fees](https://support.mathem.se/sv/article/384bf5)
and [delivery discounts](https://www.mathem.se/se/about/gratis-leverans/).
[Oda describes delivery edits with payment of a difference](https://hjelp.oda.com/no/article/89b454).
Removing Mathem's guard requires an actual supported changed-price review,
original/combined/payable reconciliation and applicable payment authorization.
The current single-date free-slot observation cannot justify deleting it or
claiming paid edits are unavailable everywhere.

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

## Remaining acceptance

Required and still open: ordinary Mathem addition payment; any supported recovery
through the product with original-change/goods binding; delivery change starting
without UI preparation; supported price-changing review/authorization; affected Oda native flow checks after the now-verified browser/account/receipt reads; candidate
publication, normal installation/upgrade and verification on exactly authorized
runtime/client targets. No new general order, payment, deployment
or recipient authorization follows from historical one-shot tests.

New single-order testing awaits exact authorization. Existing unrelated state,
original standing policy, private journals and the explicit documentation/client
exclusions are preserved.

Relevant isolated regressions are in `test_meal_concierge_mathem.py` and
`test_meal_concierge.py`: separate provider quantity/currency/date examples,
prepared-review drift and one-click JavaScript, plus existing expiry/auth,
lost-response/restart, cancellation replay, dietary/substitution/shortfall and
notice contracts. These tests do not substitute for the remaining native flows.
