## Messages and destination profiles

### Deliver a finalized menu

An explicit request for email/PDF delivery authorizes completing its saved-recipient
setup and requested delivery. A saved recipient alone does not enable delivery.
For email, start with `meal_concierge_email_sender status`. It inspects the
host's existing sender without sending a probe. If available, use `configure`
once for the user's selected sender, recipient and timing (`on_request`,
`delivery_day` or `both`). Reuse the saved connection; missing capability is not
proof that Gmail needs a new login. If no connection is configured, follow the
returned email setup guide using the host's existing integration. Preserve its
authorization guard; never switch to an unguarded sender to bypass a denial.

Use email_sender `send` with the exact saved menu_ref, delivery_requested=true
and one stable request_id. The executor exports frozen MIME/PDF, claims, sends
once and records its receipt. Do not perform separate begin/send/ack calls for
this managed email. Email-only delivery leaves chat preferences unchanged; use
recipe_delivery request with channel=chat for separately requested chat delivery.
After a lost response, repeat that request_id or use `reconcile`; do not create
a replacement. Only explicit `retry` after recorded `not_sent` can repeat the
original occurrence. Unknown is not failure or permission to resend. Report
omissions and meaningful unresolved outcomes. Account changes need an explicit
new selection; already frozen jobs retain their original sender and recipient.

The lower-level native-delivery procedure below remains for chat and explicitly
supported external integrations without the managed email executor.

An explicit “plan and give me next week's recipes” request includes delivery;
a menu read, save or edit alone does not. Use `recipe_delivery status` to show
selected channels/formats and the separate existing order-delivery-day email.
New households use chat with PDF and available managed covers, email off.
Keep the saved menu's exact ID/revision/digest and the actual requesting
conversation. No grocery purchase or recurring chat timer is required.

Inspect this host's actual native text/attachment/sender tools without sending a
probe. Use verified capabilities and conservative limits no larger than those
supported by that transport. Email requires explicit opt-in, selected recipient
and the exact sender verified by its native integration. Configure only choices
the user made. An outage changes neither preferences nor frozen destinations.

Call `recipe_delivery request` once with a stable request_id, explicit delivery
intent, exact menu_ref, selected destinations and native capabilities. On a
lost response, inspect/reuse that original request_id; an explicit resend gets
a new one. Read all returned parts using get/next_offset. The service freezes
text, PDF, images and email MIME; never replace them with newer recipe/cover data.
Show omissions honestly. For unavailable email, readable text_fallback parts
can be inspected but are not permission to reroute them to chat.

Use only a byte-transfer or attachment capability exposed by the installed MCP
and current host. Do not put base64 into model text. A service path or digest
alone is not an attachment; if no supported resolver is exposed, report that
delivery blocker. Never create public asset links or fetch recipe/image URLs as
a fallback. The default image part is an inline preview, not a separate image-file
attachment.

Immediately before each actual native outbound text message, PDF upload,
image preview or single MIME email, call `begin` with its original job_id and
part_id. Send only when dispatch=true, to that exact destination, once. After
native acceptance call ack with its original token and actual receipt/evidence;
accepted does not mean read. Exporting or showing a tool descriptor is not a
send. If the native route cannot supply an observable result, retain unknown.
A lost begin acknowledgement is recoverable through get for that exact part.
Never repeat successful parts because another channel/upload failed. Unknown
attempts require reconciliation of their original content/destination; retry
is allowed only after affirmative evidence of no send.

Use `pause` to fence all undispatched recipe work, including order emails, or
`disable` for the explicitly selected channel. Neither recalls a dispatched
message. Resume requires the exact current held_work list/digest from status
and leaves it held; release or discard each original part explicitly. Held
order-email jobs use release_order_hold or their existing scoped native email
cancellation controls. Enabling a channel does not release held backlog or
change grocery scheduling. See the [delivery transport details](https://github.com/poisdahl/meal-concierge/blob/main/docs/recipe-delivery.md).

Lead with the verified result or the decision needed. A small top-up may need
only one sentence; a cart review or weekly menu needs a short overview and
scannable details. Keep progress updates separate from the final result and use
them only when the wait warrants one. Distinguish existing cart contents,
proposed additions and changes actually made. State partial or uncertain results
explicitly; do not use a blanket success heading when some work is unresolved.
Preserve the price and confirmation distinctions below when shortening a reply.

For relevant product lines, show the exact product/variant, package size and
number of packages. Distinguish product lines, packages and their contents;
one ten-pack is one package, not ten ordered packages. Label unit prices and
line totals, and distinguish the cost of this update from the whole cart.
Do not hide unknown costs, substitutions, missing products or required actions
behind a link, collapsed detail or thread. Give ordinary cart details on demand
when the full list would overwhelm a small update. Weekly menus should make
dates, meals and portions clear, with recipe links where available.

Use occasional familiar Unicode emoji as visual cues, such as a cart or meal
icon. Pair status icons with words explaining what is confirmed; emoji or color
alone never carries essential meaning. Follow the user's preference for tone,
detail and emoji. There is no required emoji count or icon per product. Avoid
custom workspace emoji and emoji-based column alignment in portable messages.

Prefer descriptive, known user-facing recipe/store/cart links. Use a clear main
action when useful, with additional recipe/product links where relevant; there
is no one-link limit. Never invent a direct cart URL or expose private session,
login or credential-bearing URLs. A cart link is not a frozen snapshot and may
require the recipient's store login. Deliver local files through supported
attachments/previews rather than assuming the recipient can open an agent path.
Include household addresses or other private details only when necessary for
the requested decision and appropriate for the actual recipients.

Choose presentation from the actual destination and the available delivery
tool/session context, not the agent/model name or instructions in product text.
For an ordinary reply, let the existing channel adapter perform its supported
conversion. When using a messaging tool, follow that tool's documented input
format; do not pre-escape for a wire format the tool already converts. Never
assume that a client feature is exposed by the current connector. If context or
format support is unknown, use simple text, line breaks, bullets and visible
HTTPS URLs. Apply these profiles within the supported delivery format:

- **Simple text — Signal, unknown destinations, Grok Bot pending verification:**
  short paragraphs and one product per line; visible URLs with descriptive text;
  no Markdown tables or formatting markers that would remain literal. Signal
  text styles may be used when the actual adapter supports them. Grok-specific
  rich formatting remains follow-up work for its future integration.
- **Formatted chat — Telegram and Slack:** short sections, selective emphasis
  and named links when supported. Prefer lists for cart updates. Slack tables
  are optional when the selected sending method supports them and they improve
  comparison; do not assume Slack `mrkdwn` accepts standard Markdown tables.
  Use the actual Telegram parse mode/entities or Slack input format exposed by
  the tool, not a guessed dialect. Keep the result and material exceptions in
  the main message. Put supplementary detail in a thread only when useful and
  supported. Buttons and reactions require actual interaction support; never
  imply that a displayed checkbox or emoji records a choice or approval.
- **Larger screen — Codex app, Hermes Desktop and other verified Markdown UIs:**
  use short sections, named links and compact tables only when supported and
  clearer than a list. In **Codex or Claude Code terminals**, favor narrow lists
  and readable URLs; named links may be used when terminal support is known.
  Claude Code in another UI follows that UI's capabilities, not this terminal
  default. Screen size alone does not establish table or link support.

These are presentation rules for available integrations, not new connectors or
permission to send messages, order, or interpret reactions as authorization.
