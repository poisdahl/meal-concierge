# Existing orders and uncertain outcomes

Start with the exact returned order/confirmation and the actual reason. Keep
the original intent and identifiers. A timeout or missing notification is not
proof that a write failed. Preparation, cart updates and generic order listings
are not confirmation of a new purchase or order change.

## Choose the operation

| Situation | Existing tool path |
|---|---|
| Lost cart-write response | `cart reconcile_change`; never resend the delta |
| Add goods to an existing order | `orders change_begin` with its exact `order_id`, then the supported cart update and checkout for that edit |
| Change only delivery | `orders change_begin` with `delivery_only=true`; preserve the original goods, account and address |
| Reduce an order | `orders remove_prepare` with desired remaining quantities, then unchanged review through `remove_confirm`; uncertain result uses `remove_reconcile` |
| Cancel an order | `orders cancel_prepare`, then its exact confirmation/submission; uncertain result uses `cancel_reconcile` |
| Uncertain checkout/payment | `checkout reconcile` with the original confirmation/idempotency identity |
| Explicit Oda payment-method switch | `checkout switch_payment` on the current confirmation with the requested Vipps or saved-card override |
| Explicitly stop an active Oda payment | `checkout abort_payment` with the current dispatched confirmation; observe an uncertain closure without clicking again |

Follow returned arguments and the discovered tool's provider-specific guidance.
Do not clear unrelated cart goods to start an order edit. Existing-order changes
keep the receipt address and delivery except for the requested change. Report a
reduction separately from any unverified bank refund. Removing all items is not
an implicit order cancellation. An `ensure` top-up adds only the missing quantity;
do not also add it as an extra. A recurring-product substitute records the exact
observed replacement for that occurrence before normal weekly/cart application.

## Delivery price and authorization

A requested window covers an unchanged/lower verified total. An explicitly
bound `max_total_ore` also covers an increase within that ceiling. Follow the
returned `confirmation_required`: when true, show the exact full old/new totals
and obtain approval for that same review; never approve only a fee delta or set
`delivery_price_approved=true` without it. Generic standing authorization is not
an increase budget. Unknown amounts remain unknown. Oda and MENY use NOK;
Mathem uses SEK. Reuse valid authorization within its actual scope.

## Payments and recovery

Do not change payment methods automatically. An accepted merchant order is not
proof of settled payment. Distinguish a Meal Concierge verification failure from
a documented retailer/payment-provider rejection; name the actual source.
Report the service's separate order/payment outcome;
only a matched submit/reconcile with `confirmed=true` establishes success for
that intent. `manual_checkout_required` is a handoff, not success.

Oda supports saved cards and Vipps, including an explicit switch in either
direction. A prepared review can change method before dispatch without changing
the household default. Once a payment request has been sent or may have been
sent, preserve the current confirmation and use `switch_payment`; it reconciles
that attempt before returning any replacement review. Card replacement requires the exact native terminal failure. Vipps
replacement requires verified native closure (and may cancel that exact request
once when the user asks to switch). Never describe an unsubmitted hosted form as
a sent notification.

For Oda Vipps, preserve the bound pending attempt while awaiting user payment or
when its outcome is unknown. A missing phone notification, expired hosted page or merchant unpaid
label does not establish expiry. An explicit payment switch first reconciles
that original attempt; only its verified terminal state can unlock the returned
new review. Do not submit another order to solve a missing notification.

Read `workflow.next_action` against the active payment attempt. A prepared
recovery child uses its fresh `confirmation_id` and the existing confirmation
policy; a dispatched or uncertain child must be reconciled under that same ID.
Tell the user to approve a Vipps request only when that attempt positively
records a sent request. MENY's acknowledged phone request counts as positive
evidence; a saved-card attempt or a legacy Oda/Vipps attempt with no request
evidence needs reconciliation, not a phone-approval instruction.

For an exact Oda order left payment-started by a legacy Vipps attempt with no
request context or dispatch timestamp, an owner report that no Vipps request or
manual payment occurred can accompany a read-only same-order recovery review.
Use `checkout prepare`
with `recovery=true`, the original or exact current `confirmation_id`, the exact `order_id`,
`vipps_request_not_received=true`, and the requested existing
`checkout_payment` (`saved_card` or `vipps`). The service must independently
verify the same order, account, goods, delivery and payable amount before it
returns a fresh recovery confirmation. The report alone never authorizes a
retry or a new order. Preserve the original journal and confirm only that fresh
review if its existing authorization policy permits; reconcile uncertainty.
`retry_allowed=false` still forbids another payment attempt from the old
confirmation; it does not forbid this non-submitting review.

When reconciliation establishes `payment_request_state=not_sent` and
`payment_dispatched=false`, prepare recovery with the requested payment method
for that same order. A new order or a method-switch cancellation is unnecessary
for a request that never reached Next. Finding its hosted page later does not
itself mean the request was sent; renew the exact no-request report only when
it is still true, then follow the service's fresh review.

For bank/device approval, follow the returned handoff. Never collect BankID
passwords or repeat payment because the chooser is unavailable. Owner-reported
approval may support the documented resume, but the service must still verify
the exact outcome. Follow `retry_allowed` and the returned recovery action;
unknown cannot be converted into permission by making a new idempotency key.

When the owner explicitly asks to cancel an Oda order with an unresolved card
or Vipps payment, `orders cancel_prepare` identifies the current attempt. Use
`checkout abort_payment` with that exact confirmation only when it returns the
abort route. It durably fences a single native cancellation and retains the
checkout journal. An unknown closure remains pending; resume the same abort to
observe, never click again. A lost card tab may be resolved by read-only native
status for its retained exact payment ID. If native evidence says paid, reconcile the purchase
before ordinary cancellation. Only a positively closed payment permits a fresh
`orders cancel_prepare` for the same order. Confirm that exact cancellation under
the existing policy; its result is terminal only after verified merchant
cancellation. Report refund and authorization release as unknown unless separately
verified. For a requested payment-method change after closure, use the existing
`switch_payment` review path.

If the response explicitly establishes no dispatch and gives a safe fresh
prepare path, follow that supported path within the existing mandate. Otherwise
keep the original journal and reconcile. Do not restore older state over a
possibly completed payment, cancellation or send.

## Checkout product display differences

A manual new-checkout `prepare` can return `line_difference` with a digest and
indexed raw cart/checkout rows. Inspect the complete product name, description,
brand, package and quantity. Exact native product identities take precedence.
If every remaining pair is demonstrably the same product with a cosmetic
presentation difference, repeat `prepare` with `identity_review` containing that
digest and `decisions` (`expected_index`, `actual_index`, `reason`) for the
unresolved pairs. Explain the actual evidence in each reason; do not merely say
“same product”. This is model judgment within the authorized purchase, not a new
user approval step. If the evidence is ambiguous, obtain the missing product
information instead of guessing.

The service retains every raw field and binds these decisions to this exact
checkout, account, delivery, amount and quantities. A changed checkout requires
a new review. Decisions cannot override conflicting IDs, missing/extra goods,
quantity changes or indistinguishable variants, and never become global aliases.
Existing-order edits and payment recovery retain their original goods binding.
