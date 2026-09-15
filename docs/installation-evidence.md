# Historical installation and client evidence

Developer reference. These records describe bounded tests in September 2026,
using the versions stated below. They are not installation instructions or a
claim that every current client, retailer or delivery path is verified.

Older installations automatically imported recipe pack `2026-09-06.5`.
That historical behavior no longer applies: installation and code updates now
leave the local recipe bank unchanged. The optional collection is imported
separately. See the [current user guide](../README.md),
[platform matrix](platform-acceptance.md) and [runtime reference](runtime-reference.md).

## Codex and Claude installation lifecycle

On 2026-09-07–08, the README prompt was exercised through real agents on Apple
Silicon macOS 26.6.2 with user launchd. These were fresh, disjoint household homes
on a preprovisioned host whose known earlier installations were inspected, not
a claim that the whole host was empty.

| Actual client surface | Version | Fresh installation source | Second client on that same service |
|---|---|---|---|
| Codex CLI | 0.153.4 | `3982f62ad5fa63dddb2bb9ee57b89023da001bad` | Claude Code CLI |
| Claude Code CLI | 2.1.241 | `3982f62ad5fa63dddb2bb9ee57b89023da001bad` | Claude Desktop Code |
| Claude Desktop → Code → Local | Desktop 1.46388.4, embedded Code 2.1.260 | `e36815fd01b9100d903e6e8c0ba68a1db9ab6384` | Codex CLI |

Each agent used the existing installer, persistent unit and native plugin
registration. Installed source/dependency/skill bytes matched its recorded
checkout. Each household received pack `wikibooks-themealdb-en` version
`2026-09-06.5`: 4,599 recipes and 1,570 managed JPEG assets. Actual native skill,
identity/setup, stored recipe revision and image reads succeeded. SDK probes
made during initial registration were kept separate from native acceptance.

Sending the same README prompt again reused each household, release, service
and registration without another installation or silent upgrade. New CLI
processes and a normal full Desktop quit/relaunch restored MCP and skill.
Separate controlled restarts of each household service preserved its data and
registration; both clients read the same saved recipe and managed image again.
For Desktop, the app restart left the service PID unchanged; only the later
service restart changed it. The compared JPEG was 122,076 bytes with SHA-256
`c5ecbe93fe85873762990f8585ea23b982da1623505ac0f2d2d6f66b2b4afda2`.
Recipe IDs differed between fresh households; cross-client comparisons used
the correct identity within each household.

These were guided runs: the operator selected host, store, household, paths and
current setup defaults, completed native approvals and opened the required new
conversations. Desktop's repeat initially checked only the skill file/hash;
one explicit reminder caused the registered Skill invocation to succeed without
repeating business reads. Desktop's quota interruption resumed after the normal
quota reset and manual Mac unlock, without changing limits or approvals.

Incomplete paths were retained and reconciled:

- Codex and Claude CLI reported the missing pinned Oda browser prerequisite
  during read-only preflight. Mathem core installation did not require login.
- A native Desktop Code discovery action was explicitly denied. The agent
  stopped; only later explicit authorization of the same action and native
  **Allow once** resumed it. No substitute command bypassed the refusal.
- Claude CLI's initial background installation ended after core publication,
  before recipe-pack completion. The agent inspected the original process and
  markers, then used the same source's stopped update recovery and verified the
  terminal result. Data and the old release remained; recovery created a new
  release UUID at the same source revision. Subsequent repeats kept it unchanged.
- An initially supplied recipe ID from another household returned not found in
  Desktop. Those failures were preserved; the correct household's recipe and
  image then matched. This was not treated as missing data or a reinstall trigger.

The [issue #54 acceptance record](https://github.com/poisdahl/meal-concierge/issues/54)
identifies the retained evidence and final review. Earlier
[client component tests](#codex-and-claude-component-and-attachment-checks) retain
their own narrower bounds and were not repeated for this lifecycle work.

Linux/user-systemd remains an installer target, but this trial does not establish
its fresh native-client lifecycle. Codex Desktop/IDE, Windows/WSL, remote/cloud
execution and Claude Desktop Chat were not tested here. A user login/reboot was
not exercised; persistent launchd configuration and controlled service restarts
were verified. Store authentication remained `awaiting_login`; delivery/payment
readiness remained unknown. No purchase, real-recipient send or scheduler was
activated. This acceptance does not extend to shopping, menu or delivery flows.

## Codex and Claude component and attachment checks

The package schema targets Codex CLI **0.153.4** and Claude Code **2.1.241**.
Codex's `tool_timeout_sec` is 700; Claude's per-server `timeout` is 700000 ms.
These leave time for the production bridge's maximum 660-second RPC wait.
Timeouts bound the client wait; they do not prove an external operation stopped.

The [OpenAI plugin specification](https://developers.openai.com/plugins/build/plugins)
defines the manifest and local marketplace layout. The [Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp)
defines stdio attachment and native tool permissions/timeouts. The [Claude Code plugin reference](https://code.claude.com/docs/en/plugins-reference)
defines its package and session loading. The tested Claude binary's stdio schema
also accepts the per-server millisecond `timeout` field.

Run the package/service checks in the [pinned standalone Python environment](../runtime-requirements.txt):

```sh
python -I tests/test_client_packages.py -q
```

These exercise both generated MCP commands through the real SDK, bridge and
Application, with synthetic provider responses. They cover shared state between
two clients, bridge teardown, service restart, owner collision, unavailable
attachment and preservation of an existing output directory. They do not use
provider credentials or native service managers.

The separate `client_package_probe.py` exercises installed authenticated native
clients in an explicit fresh scratch directory. Its test-only provider rejects
external networking. The Codex run demonstrated native permission denial before
service dispatch, reconnection within the same conversation after a service
restart, a 645-second synthetic provider read, and recovery after losing a cart
write response with exactly one dispatch. These are subprocess-service checks;
native service-manager installation remains a separate runtime check.
The native Claude Code run also passed status/setup calls, profile updates,
client permission denial before service dispatch, and persisted state after a
service restart. These checks used the authenticated CLI and generated plugin; the bounded
Desktop result is recorded separately below. Model-client acceptance must be
recorded independently of SDK checks or login status.

On 2026-09-07, the isolated native Claude Code workflow also loaded the complete
packaged skill, read original text, a photo and all three PDF pages, and returned
schema-2 import previews. Text and PDF recipes were explicitly saved to builtin;
the photo recipe was saved as a draft after attaching its cover. An automatic
menu selected and saved seven distinct dated dinners for two without a
client-supplied candidate list. A separate native save denial caused no service
save dispatch. Unknown quantities and unrelated favorites/preferences/cart state
were preserved.

Cover bytes crossed the host-file boundary through the existing CLI's stdin;
recipe saves and image reads used native MCP. ImageContent responses before and
after saving matched the managed JPEG and included separate image credits. Fresh
CLI conversations after service restarts retrieved the same recipe revisions,
menu and image bytes. These are synthetic-provider subprocess-service results,
not desktop rendering, native service-manager or scheduler acceptance.

The photo/PDF runs first encountered a test permission mismatch between `/tmp`
and its `/private/tmp` alias; same-file Read retries passed after correcting the
exact path allowance. The final photo read recovered from two nonexistent tool
names before using the discovered tools. Those failures remain recorded. Some
model narration misstated cooking-time, readable-note or source-yield details;
the actual recipe fields remained correct. Flawless presentation is not claimed.
A subsequent bounded Claude Desktop 1.46388.4 test with embedded Claude Code
2.1.260 observed the native packaged skill, MCP status and setup
show/keep_current. Original PNG, TXT and three-page PDF files were attached
through the native chooser, produced three separate import previews and were
explicitly saved as three non-favorite drafts. Source amounts, four steps and
an unknown tomato unit were preserved; a hostile source instruction had no
observed effects. Profile and cart state stayed unchanged, with synthetic
provider reads.

Native recipe_image calls for the preview and saved recipe returned the same
managed JPEG. Opening View screenshot on the saved tool result displayed the
actual image in Desktop. Cover bytes entered through one host CLI stdin
transfer; this does not establish native MCP byte upload.

The project MCP and skill required archive/unarchive of the exact test
conversation to restart its engine; /reload-plugins alone was insufficient.
The PDF pages=1-3 read initially failed because pdftoppm was absent, then native
Read of the complete original PDF succeeded with all three pages. TXT had a
native attachment and exact original transcript, but no separately observed
Read tool call.

This Desktop result does not establish full menu/lifecycle acceptance,
service-restart persistence or native service-manager/scheduler behavior.
Earlier Codex attachment/preparation attempts timed out before model events;
the later bounded Codex workflow is recorded below. These packages do not
implement a second importer, normalizer, scheduler, sender or credential owner.

On 2026-09-07, native Codex CLI 0.153.4 also passed original text/photo import, the bundled three-page PDF fallback, explicit draft saves, internal-only planning and saving of seven dated dinners for two, and fresh-client readback after a synthetic service restart. Saved recipe revisions, quantities, unknown measures, menu contents and managed image bytes were preserved. Cover upload used the host CLI in normal native approval mode; save and image retrieval used MCP. Recipe and image attribution remained separate, and the source's hostile instructions caused no observed provider writes or favorite changes.

The photo's initial sandbox denial and the planner's initial six-eligible-recipe shortfall were retained and reconciled before the reviewed follow-ups. Codex JSONL does not expose individual builtin image-view or approval-decision events; native helper execution, transcript, business-tool results and managed image bytes are observed. Import prompts also repeated source and quantity safeguards, so these runs do not isolate the canonical skill’s prompt-injection defenses. These checks used synthetic fixtures and subprocess services, and do not establish external-provider recommendation quality, complete nutrition, native service managers, scheduler persistence or full Desktop lifecycle acceptance.


## Desktop menu and preserved-state upgrade — 8 September 2026

The [MC10 platform matrix](platform-acceptance.md#new-desktop-menu-and-upgrade-acceptance)
records the subsequent seven-dinner menu, actual local image presentation and
public-source upgrade/readback on the retained #54 launchd installation. It
supersedes earlier “full Desktop menu/restart unverified” statements only for
that observed scope. The original attachment/PDF and CLI results above are
reused, not replayed; outgoing PDF/email transports and physical-sleep behavior
retain their separate qualifications.

## Claude Desktop PDF fallback

The fallback was verified in a fresh private runtime on macOS with Claude
Desktop 1.46388.4 / embedded Claude Code 2.1.260: a three-page text PDF hit the
native missing-`pdftoppm` error, then the packaged helper and native image reads
completed its import/save. A twelve-page scanned PDF followed the same helper
path and preserved all twelve source pages in a separate draft. Exact source
quantities, unknown measures, profile/cart state and non-favorite status were
checked. Both generated launchers also passed local tests with only the existing
bootstrap Python on PATH. Other hosts' native PDF workflows remain unverified.

## OpenClaw

The reproducible [native probe](../tests/openclaw_runtime_probe.py) uses the
actual installed CLI with Python 3.12.12, `mcp==2.1.1` and `mcp-types==2.1.1`.
Initial tests exercise registration, canonical skill discovery, current served
tool catalog, include filtering, disabled-server refusal, repeated CLI bridge
connections and bridge cleanup with the original service still healthy.
External provider responses are
synthetic; live provider behavior is not verified by this probe. A deliberately
stalled native probe also verifies cleanup of its exact detached bridge after a
timeout, while the independently owned service remains healthy.

Isolated model runs verified native embedded `openai/gpt-5.4-mini` with
subscription OAuth and no fallback. Native setup kept the existing settings.
Text and photo reads, plus the PDF utility over all three pages of one document,
produced typed import previews through the shared MCP contract. The PDF utility
used local extraction and model analysis; this does not establish provider-native
PDF transport. Source instructions requesting checkout or favorites remained inert.

After the serving-evidence correction, one native invocation imported all three
retained extraction records through the actual MCP interface. Every request
preserved its original quoted data. All three previews returned schema 2, two
portions supported by `Page 1: Serves 2`, unchanged 200 g and 1.5 dl amounts, and
an unknown, nonscalable tomato quantity. Each suggested draft status and created
no personal entry. This verifies corrected native imports using retained records;
it does not repeat photo or PDF extraction.

Native draft save/read, seven-dinner menu plan/save and exact stored-menu readback
passed. The saved menu contains seven dates and two portions per dinner. The
planning response retained unavailable-source and unknown-quantity facts. A saved
recipe response displayed its managed image and credit in the native UI;
immediate display during the preceding save transition was not established.

One native weekly timer reached synthetic `cart_ready` without payment. Separate
native email occurrences verified image-free fallback and inline-image delivery
to a local SMTP fixture. Each occurrence delivered exactly once, with a durable
receipt matching the accepted MIME bytes and frozen recipe/image credits in plain
text and HTML. The inline message contained the exact managed JPEG, matching
Content-ID and HTML `cid:` reference. An injected lost `mark_sent` acknowledgment
recovered through native replay of its original token and receipt, without
another delivery. Completed occurrences were not resent.

Inline capability is per call: an image-capable sender must explicitly supply
`images_supported=true` to both `due` and `begin_send`. A true value in `due`
is not inherited by `begin_send`; omission uses the conservative image-free
fallback. The actual acceptance used the production payload and MIME builder,
with an exact sender executable allowed by native policy.

All four task jobs were removed through native APIs and acknowledged through the
shared scheduler cleanup protocol. Revoked and wrong-job weekly invocations
produced no provider or SMTP effects; unrelated disabled native jobs remained
unchanged. Temporary model profiles and owned runtime processes were removed.
These acceptance runs used synthetic provider responses and recipients, and do
not establish live retailer or production email-provider behavior.

Client-local attachment paths are not automatically readable by the service.
Keep extraction on the client and transfer typed, quoted source records through
the bounded import interface. Do not expose household databases or credentials
through a broad filesystem mount.

Upstream contracts: [MCP registry and cleanup](https://docs.openclaw.ai/cli/mcp)
and [skill loading](https://docs.openclaw.ai/tools/skills).

## NanoClaw integration and native model checks

`tests/test_nanoclaw_integration.py` exercises native template parsing and
stamping, native group mount configuration, configuration materialization,
the actual Docker driver/runner, and the container's real MCP JavaScript SDK
against the production Python bridge, Unix server and Application. Only the
external Mathem responses are synthetic. The service rejects network access;
agent containers use `--network none`.

Prepare a fresh task root, a tracked-source export of the pinned NanoClaw commit
and its locked dependencies, and the locked Meal Concierge test venv. Do not
copy an existing NanoClaw database, groups, `.env`, provider state or credentials.
Put the task Node executable at `$MC08_SCRATCH/bin/node`. Run with a fresh
temporary root containing the source checkout and an explicitly selected agent
image built for the pinned NanoClaw version. For a new source checkout and
image, use the upstream [pinned Dockerfile](https://github.com/nanocoai/nanoclaw/blob/b76fcb3db0236b36a4d50bed02e89eff472d0e67/container/Dockerfile)
with its `container/` build context:

```sh
NANOCLAW_ROOT=/tmp/meal-native-test/nanoclaw
git clone --no-checkout https://github.com/nanocoai/nanoclaw.git "$NANOCLAW_ROOT"
git -C "$NANOCLAW_ROOT" checkout --detach b76fcb3db0236b36a4d50bed02e89eff472d0e67
docker build \
  --build-arg AGENT_RUNNER_LOCK_SHA256="$(sha256sum "$NANOCLAW_ROOT/container/agent-runner/bun.lock" | cut -d ' ' -f1)" \
  -t nanoclaw-agent:meal-test-b76fcb3d "$NANOCLAW_ROOT/container"
```

This builds from the pinned source and runner dependency lock. The upstream
base image and operating-system packages are not pinned to immutable digests,
so a later build need not produce identical image bytes. Install the checkout's
locked host dependencies before running the test:

```sh
MC08_SCRATCH=/tmp/meal-native-test \
NANOCLAW_ROOT=/tmp/meal-native-test/nanoclaw \
NANOCLAW_TEST_IMAGE=nanoclaw-agent:meal-test-b76fcb3d \
NANOCLAW_INSTALL_ID=meal-test-unique \
  /path/to/locked-venv/bin/python -I -B tests/test_nanoclaw_integration.py
```

Choose a unique installation ID of at most 20 lowercase letters, digits or
hyphens; omission generates one. The host process needs the group that owns
`/var/run/docker.sock` (detected from the socket), or equivalent root access.
Use a temporary process group assignment when appropriate; the test does not
change account groups or existing containers/images. All participants of the
test run use the configured owner UID.

The host processes construct their environment explicitly. HOME, caches,
temporary files, groups, databases and helper journals remain in the task root.
Cleanup reconciles recorded container names and immutable IDs against their
installation, role, session labels and launch attempt. Existing installations
and services are outside the test's scope.

The test checks the same MCP transport in the same live container after an
Application restart, persisted setup/profile/recipe state in a new session and
new host process, native skill availability, current tool discovery, an
unattached group, rejection of another UID, and native scheduled task retry
with the same idempotency key. It does not certify model-driven attachment
normalization, the full NanoClaw daemon/adoption path, model OAuth continuity,
provider availability or a real email sender. Verify those separately through
the installation's actual supported model and delivery paths. Configure model
authentication through the host's established provider integration, preserving
one refresh owner; the Meal Concierge template does not install model credentials
or claim to verify their continuity.

## Native model acceptance

The completed isolated MC-08 acceptance used the published compact-reference
product at `8333ae16c1231dad2ff127a52ad0473acc8bc0ca`. Native tests demonstrated
session adoption, original text/photo/PDF import, managed covers, explicit
estimate acceptance and scaling, exact single-dinner save/readback, and seven
distinct saved dinners for four portions using `plan.save_ref` as `planner_ref`.
Two revoked old-owner schedule gates made no provider calls. The replacement
native weekly occurrence reached synthetic `checkout auto` / `cart_ready`.

The model completed one successful `begin_send`, one token-only local SMTP send
and one `mark_sent`, retaining the original image in the MIME message. An earlier
claim expired without dispatch; a subsequent cold turn made no Application calls.
After explicit operator reconciliation, native `due` recovered the expired claim
for the same occurrence. This verifies recovery with operator assistance, not
uninterrupted first-attempt automation or sending through a production mail
provider. No real purchase or recipient send was performed.

The original managed cover also reached a human-visible Mattermost message and
rendered in authenticated Chrome. The first model answer was text-only; one
human follow-up triggered native `send_file`. Native delivery receipts, the
human download and the managed-image hash matched. This test used the upstream
Mattermost adapter at `6d5c1d0893bcd7d6f9eabeaac629d445ab23d154` and one isolated
NanoClaw test-copy change binding its webhook listener to `127.0.0.1:19290`.
It does not certify an unmodified upstream runtime or add a production core patch.

Independent final correctness and adversarial reviews passed. Task-owned
containers, networks, listeners and helpers were removed; original failed
attempts, household state and private evidence were retained. Earlier model
copying errors and recovery steps remain part of the acceptance record. Live
provider behavior and private-network destination isolation are not established
by these synthetic tests.

## Grok retained installation — 13 September 2026

An owner-approved native Grok Bot 0.44.0 conversation reconciled the retained
incomplete Dean installation before changing it. The normal stopped update path
fetched immutable public commit
`bb34755c988dd41fa15b7e7da9e1e76005104531`, started the exact external service
and attached only the dedicated Dean MCP and pointer skill. The service was
listening with PID `329354` and reported start identity `9781417`; native MCP
discovery found 27 tools. Key installed files matched the fetched release.

The invoked installed skill then read household `MC09-DEAN-20260912`, provider
`mathem`, setup `needs_review`, authentication `awaiting_login` with no tokens,
and browser `not_configured`. It read recipe pack `wikibooks-themealdb-en`
version `2026-09-06.5` with 4,599 recipes and 1,570 assets, plus Arrabiata
revision 1 and its actual managed JPEG. The unrelated broken
`user-meal-concierge` registration was preserved. This is a current-source
update/start/attach of a retained home, not a clean empty-VM or fully unattended
installation. Mathem login still requires the owner to complete OAuth in the
dedicated visible browser; no credentials can be copied from another host.

The same test conversation saved one bank-only seven-dinner week for two and
created one frozen same-chat delivery with request ID
`issue52-grok-final-20260913-a`. Its exact menu is
`menu_0162193d6ce0eb841f317d45` revision 1, digest
`5657fde91b2d4a108fae3ddcef9ddd3f9370eae01c2c541aabc5e75260836f87`.
The 15,719-byte complete text part was accepted once with native `SendToUser`
evidence. It is produced by the maintained deterministic plain-text renderer,
which separates section titles with line breaks, prefixes list rows with bullets
and retains source URLs in parentheses. The final delivered group-room bubble
was not exposed in the available Bot transcripts, so exact visual styling and a
recipient-read result were not independently observed. The 585,065-byte PDF and
three managed images of 148,547, 194,232 and 79,209 bytes were each definitively
`not_sent`: the native client reported that Bot group rooms drop attachments.
No part remained unknown and no dispatch was retried. This verifies the
structured text payload, accepted transport submission and a concrete
current-client attachment limit; repeating the same request cannot turn that
transport into a PDF/image-capable destination. Use another independently
verified native client or the owner-accepted text-only scope until the platform
adds that support.

## Earlier Grok guided checks

Grok reported the relative-interpreter Shell workaround accepted through normal
review on 2026-09-12 at commit `95990976384b0de1f6804ac1ebe39537c58533ef`.
That older installer completed the then-automatic 4,599-recipe import. This is
historical behavior, not the current separate-import workflow.

Guided installation, original text/photo/PDF import, pooled seven-day planning
and same-MCP reconnect after a controlled restart were reported on retained
frozen installations. The operator observed desktop formatting. A native
645-second read timed out even though the service completed it. A separate
outgoing-PDF fixture passed native preview, download and byte comparison, but
did not establish faithful saved-menu PDF generation or group-room support.

A native routine control emitted start/final messages and one saved-menu read.
An app-closed control reported execution/read timestamps before reopening, and
the result was visible on reopening before another message. Configuration
survived pause/resume. These controls did not establish scheduler reliability,
causal pause suppression or VM sleep/wake recovery. Fully unattended setup and
real Grok checkout remained unverified.
