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
| Explicit Oda Vipps-to-card switch | `checkout switch_payment` on the existing confirmation with the requested saved-card override |

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
proof of settled payment. Report the service's separate order/payment outcome;
only a matched submit/reconcile with `confirmed=true` establishes success for
that intent. `manual_checkout_required` is a handoff, not success.

For Oda Vipps, preserve the bound pending attempt while awaiting user payment or
when its outcome is unknown. A missing phone notification or merchant unpaid
label does not establish expiry. An explicit payment switch first reconciles
that original attempt; only its verified terminal state can unlock the returned
new review. Do not submit another order to solve a missing notification.

For bank/device approval, follow the returned handoff. Never collect BankID
passwords or repeat payment because the chooser is unavailable. Owner-reported
approval may support the documented resume, but the service must still verify
the exact outcome. Follow `retry_allowed` and the returned recovery action;
unknown cannot be converted into permission by making a new idempotency key.

If the response explicitly establishes no dispatch and gives a safe fresh
prepare path, follow that supported path within the existing mandate. Otherwise
keep the original journal and reconcile. Do not restore older state over a
possibly completed payment, cancellation or send.
