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

## Normal native host path

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
| Hermes native host | Unverified for this new occurrence | Unverified | Unverified for this new occurrence | Unavailable here |
| OpenClaw native host | Unverified for this new occurrence | Unverified | Unverified | Unavailable here |
| Codex desktop | Verified emitted synthetic seven-day recipe text in the bound task | Unverified native presentation: frozen PDF/image output emitted, file panel queued; UI inspection was denied | Unverified | Unavailable here |
| Codex CLI | Unverified native reply for this occurrence | Local file transfer verified; visual preview unavailable in CLI itself | Unverified | Unavailable here |
| Claude Code/Desktop | Unverified for this new occurrence | Unverified; earlier PDF import/managed-image results are separate | Unverified | Unavailable here |
| Grok Bot | Unverified for this new occurrence | Unverified; MC09 owns its concrete UI/attachment test | Unverified | Unavailable here |
| NanoClaw native host | Unverified for this new occurrence | Unverified | Unverified | Unavailable here |

Codex desktop documents a PDF preview panel in its
[official app changelog](https://learn.chatgpt.com/docs/changelog#codex-2026-02-05-app).
That documented feature is not by itself acceptance of this delivery path.

On 2026-09-07 the current-task Codex probe froze seven dated synthetic recipe
snapshots scaled from two to six portions. The actual RPC/CLI exported a
67,971-byte PDF and a 15,175-byte managed synthetic JPEG; the exact 4,010-byte
recipe text was emitted once in the bound task. The PDF artifact citation and
image embed were emitted, but `open_in_codex` returned `queued` and Computer Use
refused Codex application inspection. PDF/image outcomes remain `unknown`,
with no repeat send. This does not yet satisfy the capable-client attachment
acceptance gate. Grok's separately inspected native `SendToUser` attachment
schema is not a performed transfer and is not counted as acceptance.

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
