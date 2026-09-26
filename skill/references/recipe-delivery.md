# Deliver the saved recipes

A request to receive recipes authorizes only its requested destinations and
formats. Reading/saving a menu, a saved recipient or purchasing groceries alone
does not authorize sending. Use the exact saved `menu_ref` and current native
capabilities. Optional sender failure does not block meal planning or permit
rerouting content. Never send a probe to discover support.

## Managed email

Start with `meal_concierge_email_sender status`. Reuse the host's existing
connection. If setup is actually needed, `configure` records the selected
connection, sender, recipient and timing (`on_request`, `delivery_day`, `both`)
once; it sends nothing and creates no timer. Do not change accounts merely to
work around an unavailable connection. Follow the returned setup guidance if
capability is missing; do not invent credentials or bypass a guarded sender.

Call email_sender `send` with the exact `menu_ref`, `delivery_requested=true`
for the actual request, and one stable `request_id`. The executor freezes and
exports the message/PDF, begins the attempt, sends once, stores the receipt and
acknowledges it. **Do not also call native send or manual begin/ack.** Email-only
delivery does not change chat preferences or send a chat copy.

After a lost response, reuse the same request ID or use `reconcile`; never
create a replacement occurrence. Only an affirmative recorded `not_sent` allows
explicit `retry`. Unknown is not failed. A receipt proves sender acceptance,
not that the recipient read the message.

## Native chat or an unmanaged supported sender

Use `meal_concierge_recipe_delivery` for this separate path. Inspect the actual
destination and output capabilities: chat `{platform,conversation}`, or email
`{recipient,sender}` only for an explicitly supported unmanaged integration.
Capabilities contain `verified`, `evidence`, `transport`, supported `pdf`/`images`
and byte limits (`text_limit` for chat, `message_limit` for email, plus
`attachment_limit`). Email also binds the inspected sender. Never claim file
support merely because a tool can print a path.

Enable an unmanaged email channel only after explicit opt-in, using `configure`
with the actual destination and verified capability if it is not already enabled.

1. Call `request` with one stable `request_id`, the exact menu, actual
   `destinations` and `capabilities`, and `delivery_requested=true`. Use
   `channel="chat"` or `channel="email"` for a requested single-channel send;
   omit it only when all enabled destinations are requested. It freezes the parts.
2. Read `get` pages using `next_offset`; retain exact `job_id`/`part_id`.
   Transfer frozen files through the installed native delivery integration.
   A local host can use its installed `cli.py --delivery-output /new/file.pdf`
   with the exact read request on stdin; it verifies the complete digest.
   A remote host needs a supported private byte transfer. Do not paste base64
   into model text or replace attachments with public URLs.
3. Immediately before each native send call `begin` for that part. Send its
   exact frozen text/file and destination only when `dispatch=true`, once.
   Call `ack` with the original token and actual `accepted`/`not_sent`/`unknown`
   outcome/evidence. Lost begin/send/ack results require exact `get`/`reconcile`.
   Retry only after affirmative no-send evidence; preserve successful parts.

Use the host's supported formats. Grok group rooms support recipe text but not
PDF/image attachments through that transport. Missing attachment support must
be reported, not hidden as a successful delivery.

## Scheduled order email

Use the existing scheduler and the order's original provider, recipient, menu
snapshot and occurrence. Sender setup creates no scheduled jobs; interactive
inspection does not adopt or move scheduler ownership.

A managed order-day job invokes email_sender `send_order` with its exact
`provider`, `order_id` and returned scheduler invocation. The executor owns the
send lifecycle; never send manually afterward. `reconcile_order` only reads the
original attempt; `retry_order` requires recorded no-send evidence. An old job
without a sender needs explicit `adopt_order` preserving its original recipient,
then the returned scheduler update. On-request email setup is not permission to
create delivery-day jobs.

For a requested schedule change, inspect the actual native job before using
email `scheduler_plan`. Its binding is `{platform,scope,job_id}`, not a label.
Apply the returned plan/prompt to that exact job, verify the native state and
any previous job's removal, then `ack_scheduler` with the returned digest,
generation and verified binding. Reuse a job after lost creation acknowledgment;
do not create a duplicate. Reschedule after an order's delivery date changes and
remove the associated follow-up after confirmed cancellation. Keep unrelated jobs.

Pause/disable fences undispatched work but cannot recall sent messages. Resume
uses the exact returned held-work list/digest and does not silently send its
backlog; release a hold only for that original requested occurrence. Preserve
pending sends, receipts and original identifiers throughout recovery.

For English output, pass `language="en"` when saving the menu or requesting
explicit recipe delivery. Recipe/menu reads accept the same optional language.
Use the returned presentation text and report a `fallback` honestly; available
variants may be incomplete across a collection. Never claim automatic translation.
Recipe reads expose `available_languages` and `source_text_digest`, with translated
ingredients/steps in bounded presentation pages. Keep canonical ingredient IDs,
quantities, provenance and retailer matching unchanged; use host-chosen localized
search queries. English variants must reflect the reviewed adapted recipe.
