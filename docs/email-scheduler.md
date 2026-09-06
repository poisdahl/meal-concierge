# Order recipe email scheduler contract

The existing Application email operation owns the order-bound email journal.
A native scheduler owns its timer and invokes the existing service; it does
not start another household daemon. Every email remains bound to its original
provider, order, recipient and menu snapshot after a provider or menu change.

This is the Application/JSON CLI contract. Native MCP schema and shared skill
integration, installation-wide weekly scheduler ownership, and actual native
platform handover remain pending. Existing protocol-4 jobs without a
`scheduler` record are reported as `unowned_legacy`; their compatibility path
does not establish verified scheduler ownership. Ordinary interactive access
does not adopt or move a scheduler.

## Register or hand over one email job

Use `operation=email` with the exact `provider` and `order_id` for all calls.
`scheduler.binding` has exactly three nonempty text fields: `platform`,
`scope` (the private installation/account/conversation scope used by the
native scheduler), and `job_id` (its exact native identifier). A label or the
service's automation key alone is insufficient to remove a native job.

1. Inspect the authoritative native job inventory in the known old private
   scope. An inaccessible scheduler, unknown scope or missing lookup response
   cannot establish absence. Preserve unrelated jobs. Create any replacement
   in its native **paused** state and retain its exact identity; after lost
   creation acknowledgment inspect and reuse that job, never create another.
2. Call `scheduler_plan` with `scheduler.binding` set to that target. On first
   adoption, include `scheduler.inventory` with `platform`, `scope`,
   `verified=true` and `matching_jobs` (zero or one). If one matching old job
   exists, include its exact `scheduler.previous_binding`. Multiple matching
   old jobs must be reconciled first. For an already managed job, include its
   current `scheduler.generation`; the service retains the saved old binding.
3. The service pauses dispatch, invalidates any pre-dispatch claim and returns
   the intended binding, previous binding, generation, stable invocation,
   `cron_prompt` and `automation_digest`. It refuses handover during an
   unresolved send. Repeating the same pending plan returns the same identity.
4. Remove and verify absence of the exact returned old job before enabling a
   replacement. If the binding is unchanged, update the existing job in place.
   Install the exact returned prompt. Verify the native job's intended state.
5. Call `ack_scheduler` with the returned `automation_digest` and a scheduler
   object containing `binding`, `generation`, `previous_binding`,
   `verified=true`, and `state=active` or `paused`. When a previous binding is
   present, also include `previous_job_removed=true`. These are assertions of
   completed native adapter checks, not checks performed by the core itself.
   Exact acknowledgment replay is idempotent; a stale generation, changed
   target, old delivery date or different acknowledgment is rejected.

The record exists from the first plan, so unfinished adoption cannot fall back
to legacy sending. `ack_automation` cannot remove that fence. A delivery change
requires a fresh plan/prompt and verified native update. The same provider order
retains its email occurrence identity across retries, pause and rescheduling.

`pause_scheduler` accepts the exact current returned invocation as `scheduler`.
It rotates the generation and revokes pre-dispatch claims. It cannot recall an
already dispatched message: that token and uncertainty remain intact. Inspect
email `status` after a lost pause response to recover the current generation.
Apply and verify the native pause too. Resume by acknowledging the new exact
plan after verifying native state; old acknowledgments cannot resume it.

## Dispatch and reconciliation

Use `due -> begin_send -> sender -> mark_sent`. Both `due` and `begin_send`
must carry the exact returned `invocation` as `scheduler`, including the
binding, generation and `occurrence_id`. The service verifies the fresh bound
provider order and delivery date. Only `begin_send` returns a dispatchable
payload. Send its exact recipient, subject and HTML once, then call `mark_sent`
with the original token after confirmed sender success. The same successful
token can repeat `mark_sent` after a lost acknowledgment; a different token
cannot.

After a lost dispatch/sender response, inspect the original sender attempt.
Call `reconcile_send` with its exact `claim_token` and `send_outcome=unknown`
while unresolved. This leaves `sending` locked indefinitely, across restart,
pause and lease expiry. Never infer no-send from a timeout or missing log.
Affirmative sender evidence can resolve with `send_outcome=sent` or `not_sent`
plus a bounded `sender_receipt` reference identifying that evidence. A managed
dispatched job also requires these explicit no-send fields for `release`.
Only definite no-send releases it for another attempt of the same occurrence.
Recovery uses the original sending token even if the scheduler was paused.

Cancellation cleanup includes the managed scheduler record. Apply native
removals to its exact scoped identities, including a still-pending previous
binding; never delete by an unscoped automation label. These operations do not
cancel or change the provider order. Another follow-up cannot adopt an owned
target or claim it as an old job to remove. Cancelled jobs retain their binding
reservation while native cleanup lacks a reconciled completion; this slice
does not offer automatic reuse of those identifiers. Previously verified
removed bindings are omitted from later cancellation cleanup.

## Frozen payload and test boundary

The menu and recipient are captured when the email is scheduled. Subsequent
menu/profile changes cannot replace that content. `test` and `begin_send` accept
`images_supported=true` only when the sender can resolve local managed assets.
They return exact digest/CID descriptors, never image bytes or filesystem paths.
The `recipe_email.build_message` helper on the data host resolves those frozen
references and builds MIME without sending or reading a newer bank version.
Frozen recipe and independent image attribution remain readable when the
destination has no inline-image support or an asset is missing/corrupt at
preparation or sender time. A queued email from before image-credit rendering
uses its original menu snapshot to render both alternatives consistently.

Optional image descriptors, duplicate fallback HTML and warnings are dropped
as needed before either preparation result exceeds the existing RPC limit;
required text that still cannot fit fails before dispatch. `test` never consumes
the scheduled job. The focused tests exercise actual Application/snapshot and
socket preparation, MIME construction and a loopback SMTP receiver, including
exact CID bytes and both credits. They also reopen the journal after a lost
send acknowledgment and reconcile the one accepted message. Synthetic provider
reads and loopback SMTP are not proof of real delivery or native scheduler
persistence. A sender on another host/container needs supported narrow asset
transfer before it can claim inline-image support; client-local paths alone do
not provide that access.

Run `python -I -B tests/test_email_scheduler.py` in the pinned Python environment
from [the runtime instructions](runtime.md). These tests use synthetic provider
reads and a local SMTP receiver.
