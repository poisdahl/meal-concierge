# Proposal: incremental checkout transition extraction

Status: design draft. This PR changes no runtime, locking, state format or tool
contract. Function length motivates investigation; it does not prove a defect.

## Existing structure and reason to investigate

`service.Application` combines RecipeOperations, PlanningOperations,
OrderOperations, EmailOperations and DeliveryOperations on one household instance.
The code deliberately shares state, locks, provider clients and frozen references.
At review commit 69c7a5c, `_products_operation` is 802 lines and
`_checkout_reconcile_unlocked` is 695 lines. Those sizes identify review hotspots,
not a measured source of production failures.

Keep the single household process and current public contracts. No microservices,
event bus, replacement persistence layer, new dependency, or generic workflow
framework is proposed. The aim is to make one transition easier to reason about
without changing which side effects can occur.

## Establish an invariant map before extracting code

For each candidate transition, document its entry points, lock owner/order,
state snapshot, stale-context guards, journal writes, external calls, and terminal
or uncertain outcomes. Read these facts from the code and exercised traces; do
not infer that a method name or file boundary establishes ownership.

The first map should cover `_checkout_confirm`, its provider callbacks and the
matching reconciliation path. In particular, record where it compares the
prepared context, persists `clicking` and payment evidence, invokes the provider,
and records protected results. The browser lock and the state file lock have
different purposes and lifetimes; extraction must preserve both.

This document does not claim to be a complete lock-order map. Producing and
reviewing that map is an acceptance criterion for the first implementation PR.

## Pilot change

Choose one existing pure decision, such as checking prepared-review expiry and
payment-preference consistency, after mapping its actual callers. Extract it
without moving a lock, network call, journal commit or exception handler. Supply
the existing inputs explicitly and retain current result/error semantics.

Use small typed inputs only where they clarify a real boundary. Do not introduce
new rejection of historical optional fields, normalize away evidence, or change
canonical JSON representations and digests as an incidental typing improvement.
Do not merge distinct pre-dispatch, uncertain, and confirmed states into a generic
failure enum. Legacy journals remain readable through their existing paths.

Only after a successful pilot should a second PR consider a larger transition.
Product preparation is a separate follow-up with its own source binding, pantry
allocation and continuation invariants; do not refactor both domains together.

## Behavioral invariants

- The current authorization policy and exact current confirmation remain required.
- A complete relevant journal commit precedes external dispatch.
- Changes in goods, account/address, delivery, payment or bound preferences cannot
  be concealed by copying only part of a context into a new type.
- Unknown provider outcomes retain the exact original attempt and reconcile;
  timeout is not a definite pre-dispatch rejection.
- A request replay returns its existing disposition and cannot dispatch twice.
- An unrelated menu-only change stays permitted where the current service proves
  independence; extraction must not add blanket global serialization.
- Reconciliation remains possible for old pending journals and old client shapes.

## Verification and acceptance

Use representative synthetic fixtures to compare before/after public results,
persisted journal evidence and provider call traces. Canonicalize only known
nondeterministic clocks/IDs; do not discard changed amounts, references, outcomes
or ordering from the comparison. Include expired reviews, changed context, lost
responses, interrupted dispatch, callback exceptions, restart, and replay.

Add explicit concurrency cases for two confirmations and a racing profile/cart
change. Verify a single permitted dispatch, preserved lock order and bounded
completion. A passing sequential fixture is insufficient evidence for a lock
change. Prefer making no lock change in the pilot.

Run focused regressions, the required complete synthetic suite and the separate
real MCP transport probe on the final implementation commit. These establish
synthetic behavior only, not live payment success. Record any unexercised provider
path explicitly. A docs-only design PR needs source review, not artificial tests.

Accept a pilot only if reviewers can locate the invariant more easily, existing
public behavior is preserved, and the new boundary removes genuine duplication
or coupling. If the extraction adds forwarding layers or more hidden shared
state, abandon or narrow it rather than expanding the refactor to justify itself.

## Rollout and rollback

Each extraction is an independently reviewable PR. Keep feature changes and state
migrations separate. Source rollback must never restore an older household state
or discard a dispatch journal. A new typed helper must remain compatible with the
same persisted data as the code it replaces.

Merge of this proposal records the invariant-first approach only. The pilot and
any follow-up require separate implementation review and validation.
