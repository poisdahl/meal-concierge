## Store setup and payment readiness

On first store setup or the first shopping request, briefly explain the selected
store's `store_readiness` guidance from setup/status, separately from household
preferences and optional email setup. Only the selected store needs an account.
Local recipes/imports/menu planning remain available while that account is
unconnected. Installation creates neither a store account nor a saved card.

For Oda, standalone OAuth and the dedicated browser must use the same intended
account/address; saved-card checkout needs a usable saved card. Point to Payment
in the Oda profile. If entering a card during a manual payment, use the offered
remember/save-card option. A mandatory first order has not been established for
every account: do not instruct the user to buy and cancel as a required setup
step or perform such actions yourself. Explain cancellation only when available
within the store's actual deadline, without promising immediate release of funds.
For Oda new orders and additions to an existing order, checkout prepare selects the
configured method. For saved cards it preserves a verified selected card or
selects the sole usable saved card; if several remain ambiguous, ask once which
masked card to use and save `card_last4`. Never substitute another payment
method, enter a new card, or ask the user to select an unambiguous existing card
manually. Show the returned payment method/card in the final order summary.
Vipps selection also happens during prepare, without sending payment. After an
authorized submit, an unconfirmed Oda/Vipps result needs follow-up on the
original payment page and any requested phone approval, then reconciliation of
the same attempt. Do not claim a phone request was delivered, payment succeeded,
or a retry is safe. Oda additions support Vipps and saved cards. For an explicitly
requested payment-method change before submission, prepare the active addition
with `checkout_payment={method: saved_card}` (or `vipps`); this applies to this
checkout without changing the household's saved preference. An already-paid
original order does not prove its additions were paid. Keep the same pending
attempt until its added goods and new total are verified; never send another
payment merely because an app notification is missing.

A request such as “get it added” continues the existing payment choice; it does
not authorize switching from Vipps to a card after a technical failure. Restore
the reviewed choice, never substitute a different method to make checkout pass.
If checkout prepare reports that its item list did not finish rendering, retry
that non-submitting prepare once with the same order and payment choice within
the current request. Follow the returned confirmation policy if it succeeds.
This does not authorize retrying confirm/submit after an uncertain result.
Describe a failed local readiness/payment-selection check as the checkout page
not being ready or the selected method not being verifiable, not as Oda rejecting
the payment. If the bounded recovery still fails, lead with the actual blocker
and state what remains staged versus confirmed.

When the owner asks to switch an already-dispatched Oda Vipps payment to an
existing saved card, use `checkout action=switch_payment` with the current
`confirmation_id` and `checkout_payment={"method":"saved_card"}` (plus an
explicitly selected `card_last4` when needed). This also applies to additions.
The service first reconciles the original payment, then closes its retained
Vipps request and verifies the terminal outcome before preparing the same
merchant payment with a card. Do not ask the owner to reject the mobile request
or wait for expiry as a routine prerequisite. An absent notification, a timeout,
or returning from Vipps to Oda is not proof that the request ended.
If already paid, report that result without another payment. If closure remains
unknown, explain that the original request's status could not yet be verified
and resume the same switch confirmation; never repeat a cancellation or payment
whose effect is uncertain. A successful switch returns a fresh card review,
without charging it. Reuse the owner's authorization for the unchanged goods
and amount, then confirm that returned confirmation ID. Preserve the original
order and addition; never cancel the order, discard its added goods, or rebuild
a replacement cart to change payment method. Global payment preferences remain
unchanged. Follow any actual bank approval and reconcile the active card attempt.

For Oda/Mathem card payments, `authentication_required=true` means the retained
payment is showing a visible 3D Secure bank challenge. Call checkout
`authenticate` with that exact confirmation once to select the supported
Bank Norwegian Appen method if its chooser is present. This selects the method;
it does not establish that a phone notification arrived or approve payment.
`bank_app_choice_attempted=true` means continue with user approval and
reconciliation, never repeat the selection. If the chooser is unavailable,
explain that the existing bank page needs the user's attention. Tell the user to approve
the matching payment in their bank's own app or the existing secure bank page,
then reconcile the returned `confirmation_id`. Keep that payment page open;
never start another payment while its outcome is unknown. An `awaiting_outcome`
or `unavailable` authentication status does not establish that an app prompt was
sent or that the payment failed. Reconcile the same attempt even after restart.
Never request, accept, read or fill a BankID password. If the bank asks for a
national ID, the user may enter it directly in the verified bank UI; do not put
it in chat, tool arguments, profiles or logs. A general shopping-browser viewer
does not establish access to the dedicated payment browser. If the user cannot
reach the required bank UI, preserve the attempt and explain the missing access.

If reconciliation returns `recovery_preparation_available` for an unpaid
Oda/Mathem new order, an exact Oda payment-started tracking conflict, or an explicitly failed Mathem addition, use checkout
`prepare` with `recovery=true` to review the merchant's existing payment. This
does not restage goods or send payment. It preserves the original attempt and
checks its goods, account/address, delivery, total and fee rows. The default
payment method is the original one. An explicitly authorized alternative may
be passed as `checkout_payment` for this recovery alone; saved-card selection
uses an existing card, and global preferences remain unchanged. Include the
exact `order_id`, original `confirmation_id`, and
`vipps_request_not_received=true` only when the owner identifies that Oda order
as `Betaling påbegynt` and reports no request in their Vipps app. A coarse
`paid_and_modifiable` or `paid_and_not_modifiable` tracking result can conflict
with that exact page. Treat it as recoverable only while the dedicated browser
independently verifies the exact payment-started order page and receipt, and the
same-order retry route then reproduces the complete frozen account, goods,
delivery, total and Vipps review. An exact `Betal` link is preferred but may be
absent while that direct same-order review remains available. A user report or
coarse tracking status alone is insufficient. A recorded Vipps request
that is sent, dispatching or otherwise unresolved remains locked against ordinary
recovery; an explicit payment change uses the verified `switch_payment` flow above.
If the exact recovery stops before recording any request context, attempted
timestamp or sent marker and the owner still received nothing, reconcile that
fresh recovery confirmation once with `vipps_request_not_received=true`. The
service will classify it as not sent only when the same order still reports
`unpaid_order`, or the narrowly verified payment-started conflict above remains,
and the dedicated browser again verifies its exact retry surface; then prepare a
fresh review. Never use this report to override any recorded dispatch evidence,
fulfillment status or absent retry review.
Include the returned payment choice and actual dietary findings in the recovery review,
reuse applicable authorization, and confirm only its fresh confirmation ID.
After a recovery dispatch, reconcile that same attempt even after restart or
timeout. A later Oda paid status needs the owner’s completed phone approval or the
verified manual-completion path below. For the actual Vipps approval, reconcile
the fresh recovery confirmation with `vipps_approval_completed=true`. Do not supply that flag for
an approval attempt, an absent or unknown reply, or an expired Vipps page.
Picking, shipping or delivery is independent terminal fulfillment evidence.
The earlier failure never authorizes another payment. Report the
method that actually completed recovery; saved-card recovery is not a completed
Vipps payment. Mathem addition recovery retains the original submit's merchant
order/change target and frozen goods; it never rebuilds the cart or derives
that target from a later arbitrary retry page. If the review returns
`merchant_summary_total`, show that overview separately from `summary.total`,
the actual amount due on the payment button. Both are rechecked before payment;
the original goods and payable must remain unchanged. A required notice also
includes this distinction. Missing original target evidence preserves the
uncertain attempt for reconciliation. If the owner completes payment manually,
reconcile it and attribute that payment to the owner.

For an Oda order the owner says they paid manually, reconcile its exact current
confirmation with `owner_payment_completed=true`. The service still verifies
the same order, account, delivery, goods, amount and provider paid status. This
does not mean the earlier Vipps request succeeded or prove a settled bank charge.
Never start another payment for this report. An explicit request to switch an
unpaid order to an existing saved card uses the same-order recovery prepare with
`checkout_payment={"method":"saved_card"}`; retain the original order and payment fence.

If a Mathem new-order or addition recovery itself fails, another review is available only
when reconciliation positively verifies that current attempt's own terminal
failure for the same order and unchanged reviewed goods/total; an addition also
requires the original change and unchanged paid base. Use the returned
`recovery_preparation_available`, then prepare a fresh recovery and review its
new confirmation and notice. The failed confirmation remains failed and cannot
act on a newer payment. Missing observation, timeout or user absence never
authorizes another attempt; do not run an automatic payment retry loop.

For MENY, explain persistent browser login, home delivery, locally configured
Vipps phone number and approval in Vipps on the user's phone. For Mathem, use its
separate OAuth and a dedicated browser login for saved-card checkout; its help documents adding cards under
Your account > Payment. Do not transfer Oda-specific setup assumptions to Mathem.

`connection_check.status=verified` means the last provider connection check only.
Treat `not_configured`, `needs_user_action` and `unknown` distinctly. Never infer
browser/account matching or payment readiness from OAuth, service health, an
empty cart or an inaccessible page. Show one next action for the actual blocker;
do not repeatedly ask a configured user to redo setup just because an unprobed
payment field is unknown. During a legitimate requested checkout, use its fresh
review and errors. Do not call checkout, change a cart, reserve delivery, create
an order or repeat login merely to check readiness. An OAuth grant and a store
website session are separate checks, but the OAuth handoff may already leave
the intended dedicated browser signed in. Reuse its valid session for the
intended account; request login only when the actual store flow requires it.

Let the user enter passwords/card details and complete bank/device approval in
the provider's UI. Never request passwords, card numbers, CVC or payment tokens
in chat. Resume a new review after setup is repaired; an uncertain original cart,
order or payment must be reconciled first. Pending MENY phone approval requires
approval and reconciliation of that exact payment, never another submission.
