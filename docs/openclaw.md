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

An isolated model run also verified the native embedded `openai/gpt-5.4-mini`
route with subscription OAuth on this OpenClaw version. The native trace records
successful Meal Concierge status and menu tool calls against the synthetic
household, with no fallback. A native workspace-file read produced recipe JSON
preserving the text fixture's servings, amounts, units and method; its hostile
checkout/favorite instructions triggered no mutation tools. This establishes
model access, MCP reads and text extraction, not a completed recipe import.

Full model-driven setup/menu/presentation, file/photo/document attachment extraction
through the shared import contract, durable scheduling and local-sender email
acceptance remain pending. A client-local attachment path is not automatically
readable by the service. Keep normalization on the client and use the declared
bounded import boundary when available; do not expose household databases or
credentials through a broad filesystem mount.

Upstream contracts: [MCP registry and cleanup](https://docs.openclaw.ai/cli/mcp)
and [skill loading](https://docs.openclaw.ai/tools/skills).
