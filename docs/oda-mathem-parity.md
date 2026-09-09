# Oda and Mathem: current evidence and open ordinary flows

As of 2026-09-09, #50 is **incomplete for ordinary end-to-end parity**. Its earlier
closure accepted a recovery-assisted addition and a UI-prepared free delivery
change. Those results remain valid within that scope; neither demonstrates the
whole product sequence requested in the resumed acceptance. Bank reconciliation
is outside this work and is not a completion gate.

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

The bounded #60 review criterion is complete. #50 remains open for supported
failed-payment recovery and the provider-independent same/lower/higher-final-total
delivery-change criteria. This test adds no payment or delivery-change authority.

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
| Addition/payment | Earlier addition required helper recovery. New ordinary candidate flow paid one extra package once and later reconciled two packages/143 SEK | Ordinary paid addition demonstrated; failed-payment recovery remains unverified |
| Delivery change | Earlier acceptance followed UI preparation. New ordinary candidate flow began with a closed browser, prepared 10 September14–16, submitted once and reconciled unchanged two packages/143 SEK | Ordinary free change demonstrated; paid review remains unverified and the zero limit remains local |

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

Required and still open: any supported recovery
through the product with original-change/goods binding; supported price-changing
review/authorization; affected Oda native flow checks after the now-verified browser/account/receipt reads; verification of each subsequent fix on exactly authorized runtime/client targets. No new general order, payment, deployment
or recipient authorization follows from historical one-shot tests.

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
