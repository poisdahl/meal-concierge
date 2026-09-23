## Delivery, checkout and email

A checkout with `menu_attribution=cart_only` does not order the saved menu.
`menu_coverage=not_assessed` means there are no quantified menu requirements;
an empty `menu_shortfall` is not evidence that the menu is covered. Report the
actual grocery purchase separately and preserve the saved menu and recipe usage.
Quantified menu checkout retains its existing shortfall review and notice rules.


Dietary checkout uses the actual final product IDs and the exact public Oda or
Mathem product information reader where available. Missing detail remains unknown.
Show affected items, source information, allergy/sensitivity unknowns and material
preference deviations in the ordinary final summary before its existing
confirmation. Offer alternatives for exclusions. Unknown allergy/exclusion
information needs affected-item review in that same confirmation: pass only
the reviewed summary’s `dietary_assessment.assessment_digest` as
`dietary_review_digest` on confirm or submit. Do not copy lists of finding IDs.
Unknown ordinary preferences are advisory and require no acknowledgment. Never fabricate
review, and never override a documented allergy/never-buy conflict. A substitution
or changed finding requires a revised summary.

Automatic uncertainty requires accepted diet.uncertainty_permissions entries
with exact kind, term, product_ref, condition=unknown|preference_deviation|
sensitivity_conflict, accepted=true and notify=true. Generic auto-order authority
does not cover uncertainty. Reuse existing expressly covering permissions; no
weekly approval is needed. Purchase amount/delivery/scope and native payment
approvals still apply. No incomplete order or omitted ingredient may be hidden.

When checkout returns notice.dispatch=true, use the existing authorized native
household messaging route to send the frozen payload.message once; it retains
all affected items and findings. Do not wait for a user reply. Call checkout
notice_result with notice_token, actual send_outcome and sender_receipt only after
the native sender result. Unknown/failed sending is not delivered; reconcile
uncertainty and report failure if a required notice cannot be established.
Continue the same confirmation_id, submit idempotency key or auto occurrence.
Only confirmed reconciliation establishes purchase success. Send and acknowledge
its returned result notice too; failed result messaging must never repeat payment.
Recovery returns dispatch=false for already claimed notices. Keep the actual
supported correction options and verified deadline; unknown deadlines/edits stay
unknown. Oda and Mathem additions require a currently modifiable order; MENY
editing can require new checkout/Vipps. Mathem cancellation requires a fresh
review. Moving delivery uses the shared final-total authorization rule below
with unchanged goods and the exact requested window. A provider-reported textual
deadline is retained verbatim; do not invent an ISO date or year. Never promise
that every item can be removed, replaced or refunded. Preserve an unconfirmed
Mathem attempt and its payment page; neither an empty cart nor an unchanged
original order authorizes restaging or another payment. A merchant-reported
failure is distinct from unknown effect and from an explicit platform approval.
Use the supported recovery review above only when the product verifies its
binding; retain the original attempt and do not use an external helper as a substitute.

If an exact Oda/Vipps recovery has been reconciled as `not_sent`, the owner again
reports no request, and its exact order page is stuck at `Betaling påbegynt`
without an actionable retry,
`checkout(action="abandon_unpaid", confirmation_id=..., order_id=...,
vipps_request_not_received=true)` may release only that local interactive
checkout journal. The provider API must still return the exact order as
`unpaid_order` or the known conflicting `paid_and_modifiable` /
`paid_and_not_modifiable` state; retain that API status and the page status in
the result. The original attempt must also have no dispatch/request timestamp;
a pre-dispatch `verifying` context is accepted only for this same order. Report
that the old merchant entry remains payment-started and is durably fenced from
future recovery. Ignore that fenced ID when resolving a later checkout's new
order candidates, while preserving ambiguity among every non-abandoned order.
Then review the current cart before preparing at most one fresh checkout. Never
use this for a scheduled attempt, another provider, fulfillment status, or any
recorded dispatch/request context.


Use exact returned delivery slot refs. Display exact/from/unavailable prices as
returned; “fra 0” is not free. Preserve explicit or provider-external selections.
Cheapest delivery requires exact prices for every eligible candidate. Checkout
revalidates the selected slot and provider totals before final dispatch.

Begin a delivery-only request with `orders change_begin delivery_only=true`;
MENY full-order additions retain their existing checkout policy.
For an existing-order delivery change at any provider, the user's concrete
requested window authorizes unchanged goods at a verified unchanged or lower
full order total, even with `confirmation_policy=fresh`. Display
`summary.delivery_change`: original and new totals, signed difference and
signed payable amount, in its currency. A negative amount is the merchant's
reported order adjustment, not proof of a bank refund. These full totals include fees and discounts;
the slot quote and payment/reservation amount are separate facts. Unknown or
from-prices never establish an unchanged/lower total. Never infer a refund from
a decrease or cancellation.

An increase needs an expressly covering price/budget limit or one new approval.
When selecting the requested window, pass `max_total_ore` only for an existing
user-authorized maximum full total in that provider's currency. The limit is
bound to this exact order/window; never invent it from generic standing policy.
If prepare returns `confirmation_required=true`, show the exact new window,
difference and new total and ask once. After that approval, confirm its unchanged
`confirmation_id` with `delivery_price_approved=true`. Without an increase or
within the bound limit, confirm the fresh review without another question.
Changed goods/account/order or stale review stop; reprepare changed amounts.
MENY retains its full-order review. An actual Vipps request requires phone
approval; an existing-order update may instead return an authenticated receipt
directly. Use the actual result, not the submit caption. Keep every uncertain
submission under its original confirmation/key and reconcile without another
dispatch. Do not reselect an uncertain window.

For other protected operations, follow `confirmation_policy` and explain it in ordinary language: fresh means
"show the final order or cancellation summary and ask before submitting";
standing permits submit/cancel_submit for an explicit current order/pay/cancel
request without another agent question. Keep the configured policy unless the
user changes it. It governs the final protected action, not intermediate reads
or searches. Preview/prepare never submits. One stable idempotency key represents
one intent; reuse it only to recover that attempt. A later intent needs a new key. Begin exact existing-order
changes before modifying their cart/delivery. No uncertain action is repeated.
Only bound checkout submit/reconcile `confirmed=true` establishes success.
Oda and Mathem preserve the original account/address binding across order edits
and reconciliation. If an older uncertain operation lacks this evidence, retain
it and report the missing binding; never rewrite the journal or substitute the
currently selected account. A new review is appropriate only before dispatch.

An actual MENY payment request through Vipps requires approval on the user's
phone.
Keep that attempt for reconciliation. Only an explicit no-dispatch result with
safe fresh-prepare instructions permits one new standing-authorized submit.
A confirmed expired delivery reservation can be renewed once with the same exact
slot before that pre-dispatch retry. Never infer non-dispatch from a timeout.

Select one installation scheduler owner explicitly with `schedule owner_plan`
and `ack_owner`; interactive access never transfers ownership. The owner may
serve email-only installations without a weekly timer. Inspect authoritative
native inventory, create replacements paused, retain exact platform/scope/job
IDs, apply the returned prompt and verify exact old-job removal. Unknown or
unavailable inventory is not absence. Preserve unrelated native system jobs.
Use weekly/email `scheduler_plan` and `ack_scheduler`; carry the returned
invocation unchanged. Finish the global owner acknowledgment only after every
current weekly/email job is verified in the target scope and terminal jobs are
removed. New emails during handover remain fenced until included. Never use
legacy set_cron_job or ack_automation to bypass managed adoption.

For a due managed schedule, call schedule due with its scheduler invocation,
then checkout auto with its returned occurrence and scheduler. Cart_ready never
pays. Carry its occurrence into later manual prepare or submit; this remains
manual continuation. Auto checkout additionally requires complete menu/product
preparation, a known exact total and configured delivery guards. A configured
`maximum_total` remains a hard budget ceiling, but may be absent. Updating settings or pausing
invalidates old workers; replan and verify before resuming. Disable affects only
the weekly run, preserving order emails. An uncertain delivery selection stays
in its original occurrence; use schedule reconcile, which only reads selected
provider state. Do not retry selection while unresolved. Preserve checkout
confirmation/idempotency references and reconcile dispatched payment separately.

After a confirmed order, schedule its recipe email for the verified delivery
date when delivery-day email is selected (or a legacy recipient is configured).
For a bound sender, use email_sender `send_order` with the exact provider,
order_id and scheduler invocation. It owns due/begin/send/ack; do not also send
through Gmail manually. `reconcile_order` recovers its original attempt;
`retry_order` requires an explicit retry request and affirmative no-send evidence.
Old pending emails need explicit `adopt_order` without changing their recipient,
then scheduler_plan and native verification to activate the updated prompt.
Use the selected native scheduler and
recover unfinished jobs with automation_plan. Due claims a job; begin_send with
the exact invocation and token must return dispatch=true before the sender is
called. Send that frozen payload once, then mark_sent only after confirmed
success. Reconcile uncertain sends with the original token and actual sender
evidence; not_sent requires affirmative evidence, never timeout inference.
Requested test email never consumes the scheduled job. After sent/cancelled
jobs are removed and their exact native absence verified, call email ack_cleanup.
Bindings stay reserved until this exact acknowledgment; preserve unrelated jobs.
See docs/email-scheduler.md for request fields and legacy cleanup.

External cancellation uses email reconcile for the exact provider/order.
Missing orders, auth errors and timeouts are not cancellation evidence.
Cancel_followup requires explicit owner confirmation of that exact external
cancellation. Apply returned automation_cleanup/removals to exact native jobs,
verify absence and preserve unrelated jobs. Never re-cancel a cancelled order.
Live acceptance must preserve existing account/cart work, reconcile uncertainty
and complete cleanup of its exact authorized artifacts. Ordinary tests are
synthetic and never create real orders, payments, emails or cron jobs.
