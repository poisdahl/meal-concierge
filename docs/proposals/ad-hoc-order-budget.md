# Proposal: an explicit budget for one ad-hoc new order

Status: design draft. No tool parameter, spending guarantee, state migration or
checkout behavior is implemented by this document. The field names below are
illustrative and must not be passed to installed tools.

## Problem and existing behavior

`product_planner.build_product_plan` compares the product budget with merchandise
and known deposits. Its `excluded_costs` explicitly includes delivery, bags,
cart-level fees and checkout price drift. This is a documented product estimate,
not an existing all-in budget bug.

`OrderOperations._scheduled_checkout_problem` already enforces the configured
maximum for automatic scheduled checkout. Delivery-only order changes also have
their own exact-order price authority. Neither should be replaced by this proposal.

For an ad-hoc request such as "keep this shop under 800 NOK", the improvement is
to carry that explicit ceiling to the last verified merchant total before
dispatch. Fresh confirmation and standing authorization retain their existing
meaning. A ceiling never authorizes an order, substitution, cancellation, or
increase in spending.

## Proposed scope

The initial implementation should cover one new order, including all goods
actually purchased in that cart. It should not cover existing-order additions,
reductions, delivery-only changes, split payments, or refunds. Those flows have
different full-total and payable-now semantics and require separate designs.

Record an optional ceiling in integer minor units with explicit provider
currency (NOK or SEK), a server-owned purchase intent identity, and the existing
version/digest binding. Do not accept booleans as amounts or silently convert
currencies. The binding must survive preparation, fresh review, restart and
idempotent retry of the same intent. A plan preview is not a purchase intent.

The cap is request-scoped. It must not change the household's scheduled budget
or its authorization policy. An explicit changed ceiling requires a new review
of this exact intent; no stale confirmation may retain approval of a different
ceiling. Removing a ceiling is also an explicit change, not a fallback on error.

## Amount authority and decision rule

1. Show the product estimate with its existing exclusions and uncertainty.
2. At checkout preparation, use the authenticated provider's full order total,
   including delivery, bags, deposits, applicable discounts and cart fees.
3. Bind that total, currency, goods, account/address, delivery, payment method and
   ceiling to the prepared review using the existing exact-review mechanisms.
4. Revalidate immediately before the existing controlled dispatch. A changed
   total or changed bound context follows the existing stale-review path.
5. Reject dispatch when the verified applicable amount exceeds the cap. If the
   merchant amount is unavailable or cannot establish compliance, return a
   specific budget blocker; do not convert unknown to zero or infer compliance.

The implementation must distinguish a merchant's full order total from a bank
reservation or a payable-now amount. Never add them together by inference.
Variable-weight or later-adjusted merchant quotes need an explicit amount basis:
an estimate is not a verified upper bound. A hard cap cannot be certified from
such an estimate. Either the provider establishes a bound or the hard-cap request
remains blocked. The feature cannot promise control over later independent bank
fees or settlement adjustments; report the exact scope of its verified evidence.

## Recovery and compatibility

- Read-only reconciliation of an already dispatched purchase must remain
  available even if the budget is now lower, missing or exceeded.
- Never create a second order to recover a budget or transport error. Preserve
  the original confirmation, intent and dispatch evidence.
- Do not retrofit newly required budget fields into older uncertain journals.
  Absence in a legacy or uncapped request means no new request-scoped cap, not
  zero and not proof of approval. Existing authorization rules still apply.
- Any new persisted representation needs a migration/compatibility decision and
  tests before implementation. Keep new fields optional for old client contracts.
- Recurring extras and existing cart items count toward the new order's total;
  do not silently remove them to make the budget pass.

## Required validation before implementation can be accepted

| Scenario | Required observation |
|---|---|
| Products below cap, delivery pushes full total above it | No submit call; explicit full-total blocker |
| Full total exactly equals cap | Budget check passes; independent purchase authority still required |
| Missing total, currency mismatch, estimated variable-weight amount | No invented compliance; no automatic checkout |
| Price/delivery/cart/cap changes after review | Old review cannot dispatch |
| Restart before and after dispatch | Same intent retained; at most one provider dispatch |
| Lost response or an already confirmed order above the current cap | Original attempt remains reconcilable; no second purchase |
| Legacy journal, uncapped request, existing scheduled maximum | Existing behavior preserved |
| Order addition or delivery-only edit | Existing flow and price authority preserved; new cap not misapplied |

Tests should use the current synthetic Oda, Mathem and MENY fixtures. No live
purchase or payment is needed to validate these invariants. Provider evidence
that fixtures cannot establish must be reported separately.

## Decisions and rollout

Before coding, agree on the user-facing distinction between a merchandise target
and a strict order ceiling, how unsupported variable-weight quotes are explained,
the exact intent lifetime, and the additive tool/state schema. Link those decisions
from the implementation issue. A later implementation PR should begin with one
new-order path and an explicit provider support matrix, then expand only when
the same amount semantics are established.

Merge of this proposal accepts a design discussion artifact only. It does not
complete or advertise the feature, authorize deployment, or close an eventual
implementation issue.
