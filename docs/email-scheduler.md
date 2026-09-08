# Order recipe email scheduler contract

The existing Application email operation owns the order-bound email journal.
A native scheduler owns its timer and invokes the existing service; it does
not start another household daemon. Every email remains bound to its original
provider, order, recipient and menu snapshot after a provider or menu change.

See the [native acceptance matrix](scheduler-acceptance.md) for exact tested
client versions, persistence/pause/timing evidence and sender limits.

This is the Application, JSON CLI and native MCP contract. Native platform
persistence, actual timer invocation and sender availability must be verified
on the selected platform separately. Existing jobs without managed ownership
remain `unowned_legacy`; their compatibility path does not establish verified
scheduler ownership. Interactive access never adopts or moves a scheduler.

## Installation owner and weekly job

The installation owner is independent of weekly `enabled`; an email-only
installation has no weekly/control timer. These actions use `operation=schedule`
or the equivalent native MCP tool:

1. Call `owner_plan` with `scheduler.owner={platform,scope}`. Initial adoption
   additionally requires `scheduler.inventory={platform,scope,verified:true}`
   from an authoritative inventory of the known old scope. Existing owner
   transfer requires its current `scheduler.generation`. Pending same-target
   plan replay returns the same generation; a different unresolved target is
   rejected. Finish any already pending per-job handover first.
2. The returned `owner` contains target `owner`, `generation`, `state=handover`
   and `original_owner`, plus the retained first-adoption `adoption_scope`. All managed and legacy scheduler dispatch is fenced
   during this hold. Adopt every current weekly/email job individually; use
   exact old identities and verified native removal, never unscoped labels.
3. For a weekly job call `scheduler_plan` with target `scheduler.binding`, plus
   the saved weekly generation for a managed job. Initial weekly adoption needs
   inventory `{platform,scope,verified:true,matching_jobs:0|1}`; if one old job
   exists, include its exact `previous_binding`. A legacy `cron_job_id` must be
   accounted for by that previous binding. Disabled old weekly jobs can plan
   their existing native identity solely to acknowledge removal, without
   creating a replacement timer.
4. Apply the returned `cron` weekday/time/timezone and exact `cron_prompt` to
   the paused native job, verify any previous job removed, then acknowledge
   with `automation_digest` and `scheduler={binding,generation,
   previous_binding,previous_job_removed,verified:true,state:active|paused}`.
   The acknowledgment records native adapter evidence; the core does not query
   the external platform itself. A lost response reuses the same plan/ID.
5. Call `ack_owner` with `scheduler.owner`, the returned owner `generation`,
   and `inventory={platform,scope,verified:true,bindings:[...]}`. `bindings`
   contains exactly the remaining household job bindings in the target scope,
   excluding unrelated native system jobs. Under one lock the service checks
   all current rows, including emails added during handover, terminal jobs,
   previous bindings and unresolved effects. Each continuing job needs a
   verified target registration; sent/cancelled emails and disabled weekly jobs
   need verified removal. No active job is inferred from an unavailable lookup.
6. A native weekly invocation calls `schedule due` with the exact returned
   `invocation` as `scheduler`, then carries the returned `occurrence` and
   `scheduler` unchanged into checkout `auto`. The existing local-week
   `YYYY-Www` identity and 30-minute admission window remain authoritative.

`pause_scheduler` requires the exact weekly invocation and returns a new
revoked generation. Changes to effective weekly settings also pause it.
Replan and verify native state before resuming; disable/enable cannot revive
old invocations or acknowledgments. `disable` preserves installation ownership
and legitimate order emails. For native removal of a disabled weekly job,
acknowledge its returned plan with `state=removed` and verified previous removal.

Automatic delivery selection persists an original-attempt dispatch marker
before provider I/O. Pause before that point prevents selection; pause after it
cannot recall the action. A timeout remains unresolved, blocking selection,
preparation and owner activation until `schedule reconcile` with the original
`occurrence` obtains positive selected-slot evidence. Reconciliation waits for
the existing provider-operation lock and does not read through an unresolved
protected checkout/payment. This action only reads;
a mismatch or unavailable read never permits a resend. Late results may resolve
their original effect, but cannot publish stale provenance or overwrite another
attempt. Pre-dispatch checkout is also fenced at prepare and final click.
Dispatched checkout/payment remains recoverable with its original confirmation
and idempotency references, independently of current ownership. Manual
cart_ready continuation remains manual and carries the original occurrence.

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

Terminal native bindings remain reserved until explicit removal acknowledgment,
including sent jobs. `automation_plan` returns outstanding terminal removals.
Remove and verify exact current and unremoved previous identities; never delete
by an unscoped automation label. Call email `ack_cleanup` with exact provider,
order_id and `scheduler={binding,generation,previous_binding,
previous_job_removed:true,verified:true,state:removed}` from the original job.
The same acknowledgment replays idempotently; stale generations cannot free a
new job's reused ID. Cleanup of old-scope terminal jobs remains available during
global handover. Previously removed identities are omitted from removal plans;
retain the original scheduler record from status for the acknowledgment.

A terminal legacy email with no scheduler record uses `ack_cleanup` without
inventing a native timer: supply the current owner-plan generation,
`verified:true,state:removed` and
`inventory={platform,scope,verified:true,matching_jobs:0}` after authoritative
absence verification in its retained original scope. A new email created during
handover records the selected target scope instead. Empty inventory from another
scope cannot establish removal. This receipt accounts for the
terminal legacy row during first global adoption. None of these operations
cancels or changes the provider order.

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

Run the focused contract tests with
`python integrations/meal-concierge/tests/test_email_scheduler.py` in the
private repository, or `python tests/test_email_scheduler.py` in the exported
product. The shared-code validation profile is `scripts/validate.py fleet`.
