# Finalized recipe delivery

An explicit request to receive a saved menu uses `meal_concierge_recipe_delivery`.
It does not require a grocery purchase. Reads, saves, edits and state migration
do not send anything. “Plan and give me next week's recipes” supplies delivery
intent; an explicit resend is a new occurrence.

New state defaults to chat enabled, email disabled, PDF and managed images
enabled within both channels. Older state retains its existing email setting,
queued order emails and receipts; chat is not retroactively enabled. `status`,
`configure` and `request` show the separate order-delivery-day email so an
immediate delivery and the configured later occurrence remain intentional.

## Email connection setup

The managed `meal_concierge_email_sender` tool provides status, one-time
configure, send and reconciliation. It uses the host's existing email integration;
the grocery service never receives credentials. CLI clients use the same runner
with `operation=email_sender`. Agent brands do not select a transport.

Start with status. If the connection exists, configure its selected sender,
recipient and timing (`on_request`, `delivery_day`, `both`) once. This stores a
non-secret connection/account reference in this household. It sends nothing,
creates no schedule and does not release held work or rewrite pending recipients.
For multiple accounts, select connection_id explicitly. No default-account switch
is performed later. Enabling email does not disable chat; managed send selects
email only. Low-level request also accepts an explicit enabled `channel` subset.

For a host without a connection, an installing agent can run the release's
`email_sender.py` with the existing integration's details. Substitute verified
absolute paths, never credentials in command arguments. Gmail example:

```sh
python3 /absolute/meal-concierge/email_sender.py --runtime-id my-household \
  --gmail-python /absolute/hermes/.venv/bin/python \
  --gmail-credentials /absolute/existing/google_token.json --unattended
```

This requires the existing Google Python environment (`google-auth` and
`google-api-python-client`) and a recorded grant with Gmail read and send access.
It neither copies nor writes the credential file; access-token refresh stays in
memory. Login and revoked-grant repair belong to the existing integration.
**If that integration has its own write policy, use its guarded JSON command
instead** (`--command /absolute/helper ...`); the standalone example is not a
way to bypass policy. Bob uses his guarded Workspace helper this way.

SMTP example, using a password already supplied by the host's secret environment:

```sh
python3 /absolute/meal-concierge/email_sender.py --runtime-id my-household \
  --smtp-host smtp.example.test --smtp-sender sender@example.test \
  --smtp-username sender@example.test --smtp-password-env EXISTING_SMTP_PASSWORD \
  --unattended
```

SMTP defaults to STARTTLS/587; implicit TLS supports `--smtp-tls implicit
--smtp-port 465`. Sender identity is explicitly configured, not independently
proven by SMTP. Endpoint/username changes invalidate the original account binding.
Inspection connects/authenticates but sends no MAIL/RCPT/DATA. Set unattended
only when the same runtime's scheduled jobs have the connection/secret access.
This flag does not establish a native scheduler or make an asleep host available.

Setup writes one new owner-private `$XDG_CONFIG_HOME/meal-concierge/email-sender.json`
(default `~/.config/...`) and never overwrites an existing connection. A custom
`MEAL_CONCIERGE_EMAIL_CONFIG` must be supplied to both MCP and CLI/scheduled
processes. The file contains runtime_id, receipt_dir and connections with stable
id, type (`command` or `smtp`) and unattended support. Commands are fixed argument
lists, not shell strings. Native command protocol is one JSON request on stdin
and one bounded JSON result on stdout: inspect returns actual account/sender and
capabilities; check(binding) returns ready; send/reconcile receive binding and
message_base64 without passing bytes through model text. Outcomes are accepted
with a receipt, affirmative not_sent with evidence, or unknown. Native guards
must check the actual MIME, not separate address arguments. Unsupported native
connectors remain unsupported; no public file links or invented PDF support.

Managed send requires exact menu_ref, delivery_requested=true and stable
request_id. The executor serializes attempts per household and occurrence,
exports exact MIME, begins through the service, sends once, persists its receipt,
then acknowledges. It freezes Date/Message-ID with the content. `reconcile`
reuses the original account and message; lost acknowledgment never resends.
Explicit `retry` requires recorded not_sent and retains the original occurrence.
Unknown Gmail results can be positively matched in sent mail when the existing
grant permits it. A missing match proves nothing; Message-ID is not Gmail
deduplication. SMTP cannot automatically reconcile a lost acceptance response.
Gmail may replace Message-ID. New messages also carry a frozen random delivery
marker: fallback inspects headers of up to 50 matching sent messages, then checks
the marker, Date, addresses, subject and complete decoded MIME content. This is
not an exhaustive mailbox scan; missing/rewritten markers or a message outside
that bounded result remain unknown. Older messages without a marker retain only
the original Message-ID check.
Unknown and required-action outcomes must be surfaced, not silently retried.

The private receipt directory contains original message bytes and evidence.
Preserve it across upgrades and include it in the host's private backup policy.
Do not delete it to fix an uncertain send. The service's existing outcome journal
remains authoritative; local receipts bridge the network-send/ack crash window.

Synthetic acceptance exercises setup/restart, PDF/MIME, duplicate calls, account
changes, unknown/accepted reconciliation, concurrent executors, explicit retry,
order-day scheduler gates and a real loopback SMTP sink. This is not evidence of
a live Gmail recipient accepting email; that requires a separately authorized send.

## Normal native host path

Use this lower-level path for chat or an explicitly supported external sender
without the managed executor. Do not also run it for an email already managed
by email_sender.

1. Read the saved menu's exact `menu_ref={menu_id,revision,digest}` and delivery
   status. Inspect the actual host's native output tools and their limits.
   Capability evidence is an assertion by the trusted native host adapter, not
   a recipe field or a claim the MCP service independently verifies. Do not
   send a real probe email to inspect support.
2. For email, obtain explicit opt-in and a selected recipient. `configure`
   enabling email requires the native sender's exact address and inspected
   capability evidence. Each request explicitly supplies its destinations;
   saved profile email alone never proves sender support.
3. Call `request` with `delivery_requested=true`, one stable `request_id`, the
   exact menu reference, and exactly the enabled `destinations` and
   `capabilities`. Chat destination is `{platform,conversation}`; email is
   `{recipient,sender}`. Capabilities include `transport`, `verified=true`,
   `evidence`, `pdf`, `images`, and byte limits: chat `text_limit`, email
   `message_limit`, and `attachment_limit`. Use the actual supported limit or
   a conservative lower operational bound. Email capability also binds `sender`.
4. The service freezes the selected menu and every outbound part. Read subsequent
   part pages with `get` and `next_offset`; exact `part_id` selects one part for
   recovery. Status pages jobs and reports the complete held-work count/digest.
5. Transfer each frozen file through the ordinary private RPC connection. A
   local native host passes an exact read request on stdin to
   `cli.py --delivery-output /chosen/new/file.pdf`. It creates a private file
   exclusively, transfers 128 KiB chunks and checks the complete SHA-256. It
   never opens an arbitrary source URL or sends anything. Remote hosts require
   an authorized byte-transfer/resolver; a service-local path is not a file
   delivery. Do not paste base64 into model text or create public asset URLs.
6. Immediately before each actual native send, call `begin` for its original
   job/part. Only `dispatch=true` permits this attempt. Send the returned frozen
   text or transferred file to the frozen destination once. A PDF upload and
   each image preview have independent outcomes; a MIME email containing
   text/HTML/PDF/inline covers is one send. Images are previews/inline content,
   not separate image-file attachments by default.
7. `ack` records the original token, `accepted`, `not_sent` or `unknown`, and
   actual native evidence for affirmative outcomes. `accepted` means accepted
   by the transport, never read by the recipient. `delivery_transport.send_smtp`
   supports an already connected/authenticated SMTP sender with the exact frozen
   MIME bytes; it changes no account configuration and performs no retries.

`request` replay returns the original occurrence even after the active menu,
preferences or source images change. Different destinations/content with the
same request ID are rejected. A lost begin response is inspected with exact
`get`; it does not authorize another send. Unknown sends are reconciled against
the original destination/content. Only affirmative no-send evidence permits
an explicit `retry`. Successful parts remain accepted when another upload or
channel fails.

## Formats and failure behavior

Text, PDF and email are rendered from the saved recipe snapshots, including
scaled ingredient amounts, portions, available dates, steps and separate text
and image credits. Source attribution names the recorded publisher/host. Missing
ingredients/steps and link-only rights remain explicit. PDFs use bundled Unicode
fonts and only managed JPEG bytes; recipe HTML is converted to inert plain text
blocks before the PDF renderer sees it. No URL fetch or execution occurs.

The service groups complete recipes within the text limit and splits oversized
recipes at section/line/word boundaries, preserving available content. UTF-8
byte limits conservatively also bound codepoint and UTF-16-unit limits. Frozen
files are at most 32 MiB; actual lower native limits apply. PDF failures,
missing/corrupt images and unavailable attachment support retain recipe text and
return named omissions. An oversized MIME email first omits attachments; if the
complete text still exceeds the native limit, it is not sent. Readable
`text_fallback` parts remain available for inspection, with `unavailable` status;
they do not authorize rerouting email-only delivery into chat.

## Pause, disable and resume

`pause` fences undispatched work on both channels, including pending/claimed
order emails. `disable` fences the selected channel and changes its future
preference. Both report affected and uncertain work. The old order-email send
gate checks the fence under its existing state lock. Dispatched messages cannot
be recalled, and unknown attempts retain their original tokens and destinations.

Ordinary PDF/image preference changes affect future occurrences. Re-enabling a
channel does not release held work. `resume` requires the exact current
`held_work` list or `held_work_digest` from status; it leaves the backlog held.
Explicit `release_hold` or `discard` addresses each original new-delivery part.
Use `release_order_hold` for a specifically selected old order-email occurrence,
or its existing scoped native cancellation controls to discard it. Releasing
an order hold permits its original native timer to run; it does not send itself.
Reconcile uncertain sends before releasing them. Grocery schedules, checkout
notifications and existing native timer ownership controls remain separate.

This implementation creates no recurring chat timer. Automatic chat is
unavailable without separately verified native timer/destination acceptance;
an automatic delivery request with no enabled channel is explicitly rejected.
Existing configured order-day email remains supported.

## Transport acceptance

The following labels concern this new finalized-menu delivery path. Previous
import/image, scheduler or checkout acceptance does not establish it. See the
[existing client acceptance](../clients/README.md),
[email scheduler contract](email-scheduler.md), and
[managed asset contract](recipe-assets.md) for those separate results.

| Host/path | Text | PDF/image outbound | Email | Scheduled chat |
| --- | --- | --- | --- | --- |
| Shared Application + actual MCP 2.1.1 + Unix RPC/CLI | Verified local synthetic transfer | Verified exact PDF/image byte transfer; native recipient not implied | Verified single MIME send to loopback-only SMTP sink | Unavailable in this implementation |
| Hermes native host | Read-only current Bob client/MCP status verified; no delivery occurrence | Unverified | Sender inventory unavailable; no probe send | Unavailable here |
| OpenClaw native host | Unverified for this new occurrence | Unverified | Unverified | Unavailable here |
| Codex desktop | Verified emitted synthetic seven-day recipe text in the bound task | Verified for the original synthetic occurrence: recipient confirmed readable PDF and native image preview; historical transport acknowledgements remain unknown | Unverified | Unavailable here |
| Codex CLI | Unverified native reply for this occurrence | Local file transfer verified; visual preview unavailable in CLI itself | Unverified | Unavailable here |
| Claude Desktop 1.46388.4 / embedded Code 2.1.260 | Verified 19,172-byte complete frozen saved-week text in the bound native conversation | Verified 276,833-byte readable ten-page PDF and two exact managed-image native previews; all four parts accepted | Unverified; email absent from the occurrence | Unavailable here |
| Grok Bot 0.44.0 | Verified once for current-source bank-only saved week: 15,719-byte structured text accepted by `SendToUser`; exact final bubble styling/read status unobserved | Definitively `not_sent` for the 585,065-byte PDF and three managed images because Bot group rooms drop attachments | Unverified | Unavailable here |
| NanoClaw native host | Unverified for this new occurrence | Unverified | Unverified | Unavailable here |

Codex desktop documents a PDF preview panel in its
[official app changelog](https://learn.chatgpt.com/docs/changelog#codex-2026-02-05-app).
That documented feature is not by itself acceptance of this delivery path.

On 2026-09-07 the original Codex desktop probe froze seven dated synthetic
recipe snapshots (recipe revision 1), scaled from two to six portions, with
600 g carrots and 3 l water per recipe and distinct recipe/image credits.
The actual RPC/CLI exported a 67,971-byte PDF and a 15,175-byte managed synthetic
JPEG; the exact 4,010-byte recipe text was emitted once in the bound task.
The PDF SHA-256 is
`67892fb792b39cebd8254c8612ada3be0eb65636cb408165212a19c7c26556a8`;
the image SHA-256 is
`01359d0f5a975cbbfe00ce0c7fb862b38584b6aaed012130e1f3a749eff1ea18`.

The file panel initially returned `queued` and automated Codex UI inspection
was denied. During subsequent reconciliation, the recipient explicitly answered
“ja og ja” when asked whether the PDF in the original task could be opened and
read and whether the image actually appeared as a preview. This affirmative
recipient observation satisfies the remaining native presentation gate for that
original occurrence; it is not a new send or an automated transport receipt.
The immutable original journal still records text `accepted`, PDF/image
`unknown`, with the original attempt tokens and destination. It was not edited,
reseeded, retargeted or resent, and the refused inspection route was not retried.

These observed payload sizes establish one successful native presentation,
not a measured maximum Codex message/attachment limit. The probe used conservative
operational bounds of 16,000 text bytes and 1,000,000 attachment bytes. Shared
integration tests separately exercise bounded splitting, oversized attachments,
missing/disabled assets, text fallback and partial/unknown outcome recovery.
Existing six-page native and thirteen-page long-fixture visual reviews apply to
unchanged PDF bytes; no renderer or layout changed. Grok's earlier introspected
`SendToUser` schema was not a performed transfer; the current result below is
separate. Other host qualifications remain as listed above. See [issue 53](https://github.com/poisdahl/meal-concierge/issues/53)
for the acceptance result reusable by issue 52.

On 13 September, Grok Bot 0.44.0 exercised this delivery path from a current
public-source retained installation. Request
`issue52-grok-final-20260913-a` froze bank-only menu
`menu_0162193d6ce0eb841f317d45` revision 1, digest
`5657fde91b2d4a108fae3ddcef9ddd3f9370eae01c2c541aabc5e75260836f87`.
The complete 15,719-byte text part was accepted once. The maintained plain-text
renderer separates section titles with line breaks, prefixes list rows with
bullets and preserves source URLs in parentheses. The final delivered
group-room bubble was not exposed in the available Bot transcripts, so exact
visual styling and recipient-read status are not claimed. Its 585,065-byte PDF
and three managed images were positively `not_sent`, rather than unknown,
because the selected Bot group-room transport drops attachments. The sender did
not retry them. This is accepted evidence for the structured current-client
text payload and attachment limit, not a successful Grok PDF/image presentation.

The same date's owner-confirmed Claude Desktop Code occurrence used retained
menu `menu_dfc3b9a6641e31009991a612` revision 1, digest
`82dea9d29f815f13c4877e90c298798f3a234d5d271163469ccaceefaaf3741a`.
Request `issue52-claude-final-20260913-a` presented the complete 19,172-byte
text, a 276,833-byte ten-page PDF and two managed JPEG previews of 32,546 and
140,434 bytes in the same native conversation. The operator opened the PDF in
Claude's built-in viewer, observed readable pages 1 and 10 and the full 1–10
page structure; both image previews were visible. Exported hashes matched the
frozen metadata. Each part was begun, presented and acknowledged once with its
original token and actual native file/message receipt. Final `get` reports all
four parts `accepted` and `all_accepted=true`; `recipient_read` remains unknown
because the API does not record the separate operator observation. A first
read-only binary export used the absent default Linux socket, returned zero
bytes and `dispatched:false`, then succeeded against the actual Desktop socket
before any `begin`; no send was replayed.

## Validation

Run `python -m unittest discover -s tests -p test_recipe_delivery.py -q` in the
standalone pinned runtime. The suite uses saved synthetic recipes through the
ordinary menu path, actual MCP/CLI file transfer, a loopback SMTP receiver,
frozen content recovery, source/asset changes, scaled quantities, large text,
partial/unknown outcomes, explicit holds, legacy send fencing and migration.
Its SMTP test never forwards externally and uses `.test` recipients. Inspect
rendered PDF pages as well as text extraction; automated tests cannot establish
native attachment presentation or visual layout on every client.

The twelve focused tests passed with the pinned dependencies and Python
3.12.12. The private repository's required `fleet` validation passed. All 322
existing-plus-new product tests were exercised: 319 passed under Python 3.12.13;
the three Grok runtime tests rejected that interpreter because their fixture
requires 3.12.12, then all three passed under 3.12.12. The two subsequent
attachment-limit/section-splitting regressions are included in the twelve-test
focused run. PDF visual review covered all 13 pages of a long Norwegian fixture
and all six pages of the frozen seven-day native candidate, with readable
quantities, page breaks and distinct credits.
