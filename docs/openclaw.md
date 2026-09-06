# OpenClaw

The adapter targets OpenClaw **2026.9.2**'s embedded runtime and native
`mcp.servers` registry. It connects to the independently managed Meal Concierge
installation through the shipped stdio bridge. One conversation starts one
bridge, while the existing household service owns provider authentication,
browser, recipes and operation journals.

## Connect an existing installation

Start and verify the installation using the [runtime instructions](runtime.md).
Use its existing owner account on the same host. Obtain the health-checked
attachment description and translate it into an OpenClaw configuration fragment:

```sh
python3 /path/to/meal-concierge/install.py attach --home /path/to/household \
  | python3 /path/to/meal-concierge/clients/openclaw.py
```

Merge the returned `mcp.servers.meal-concierge` entry into OpenClaw's configuration
and append the returned skill directory to `skills.load.extraDirs`, preserving
existing entries. OpenClaw can also register the server object using
`openclaw mcp set meal-concierge '<server-object>'`. The shared skill is loaded
directly from the installed release; it contains the maintained workflow.

Verify the actual connection and skill:

```sh
openclaw mcp doctor meal-concierge --probe --json
openclaw mcp probe meal-concierge --json
openclaw skills info meal-concierge --json
```

The probe reports Meal Concierge tools plus OpenClaw-generated resource/prompt
utilities when those MCP capabilities are advertised. A successful registry
write alone does not establish a working connection. New sessions discover the
current tool catalog. `openclaw mcp reload` affects only that CLI process; use
OpenClaw's applicable Gateway/session reload path for an already running client.
Updating client registration does not restart the household service.

## Permissions and recovery

OpenClaw's `coding` and `messaging` profiles expose configured MCP tools;
`minimal` and an explicit `tools.deny: ["bundle-mcp"]` hide them. Server-level
`toolFilter.include` and `toolFilter.exclude` further restrict discovery.
Use the native policy appropriate to the installation. The fragment adds no
approval bypass. Its optional upstream `codex` approval settings apply only to
that runtime projection, not to embedded OpenClaw generally.

The household service trusts its owner UID. A gateway or shared chat must not
give unrelated participants this owner's mutation authority. Native tool
visibility does not grant permission to buy, change recipients or send email.
Keep the shared skill and service confirmation rules in force.

A timeout or closed session does not prove that an operation stopped. Reconnect
to the same service and reconcile the original operation identity before
retrying. OpenClaw tears down its stdio process tree; the separately started
household service must survive that cleanup. Do not launch `service.py` from
the MCP registration.

## Verification boundary

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
