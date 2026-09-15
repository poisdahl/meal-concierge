# OpenClaw

Connect an existing OpenClaw installation to Meal Concierge on the same host.
The household service runs independently of OpenClaw conversations.

## Install with an AI agent

Send this to Codex, Claude Code or another agent with access to your OpenClaw host:

> Install Meal Concierge from https://github.com/poisdahl/meal-concierge for my
> existing OpenClaw installation. Follow `docs/openclaw.md`. Inspect the actual
> host and existing installations first. Reuse my existing household and installed
> version if present; preserve its data, settings and connections. For a new
> installation, use the latest `main`, resolve it to a full commit SHA and install
> from that checkout. Ask for my store and household if needed. Set up the
> persistent service, OpenClaw MCP connection and shared skill. Do not import the
> optional local recipe collection unless I request it separately. Verify the
> connection, household, loaded skill and available recipe sources. A new local
> bank may be empty; online recipes do not require the optional collection. Keep
> normal platform approvals and tell me which login or activation steps I must
> complete.

## Requirements

- A working OpenClaw installation. The adapter was tested with **2026.9.2**'s
  embedded runtime and native `mcp.servers` registry; check compatibility with
  your installed version.
- A supported [runtime host and prerequisites](runtime.md#install-and-attach).
- The OpenClaw MCP process must run as the household service owner on that host.

## Manual setup

### 1. Install or reuse the household service

Follow [Install and attach](runtime.md#install-and-attach). Inspect existing
installations first; connect to the intended household without installing it
again. Keep the checkout and full source commit recorded for future updates.

### 2. Register the connection and skill

From the retained product checkout, generate an OpenClaw configuration fragment:

```sh
python3 install.py attach --home /absolute/data-home \
  | python3 clients/openclaw.py
```

Merge its `mcp.servers.meal-concierge` entry into OpenClaw's configuration and
append its skill directory to `skills.load.extraDirs`. Preserve existing entries
and all returned environment values, including an optional email configuration
path. Reuse a matching registration; resolve a different household under the same
name before changing it.

OpenClaw also supports `openclaw mcp set meal-concierge '<server-object>'` for the
server entry; the skill directory still needs configuring. The shared skill is
loaded from the installed release. Follow OpenClaw's
[MCP registry](https://docs.openclaw.ai/cli/mcp) and
[skill configuration](https://docs.openclaw.ai/tools/skills-config) for your version.

### 3. Check the registration

```sh
openclaw mcp doctor meal-concierge --probe --json
openclaw mcp probe meal-concierge --json
openclaw skills info meal-concierge --json
```

Use the Gateway's normal reload procedure or a new session for an already
running client. `openclaw mcp reload` only affects that CLI process; a successful
configuration write alone does not prove the conversation connected.

## Check and first use

In an actual OpenClaw conversation, ask it to load the Meal Concierge skill and
show setup, the selected household/store, store connection and available recipe
sources. Follow [store login](runtime.md#provider-oauth) if needed, then verify an
authenticated cart read.

An empty new local bank is normal. Recipes from your selected, connected store
are available without the optional collection. See
[first use](usage.md) and [adding recipes](recipe-import.md).

Keep OpenClaw's normal tool permissions. In the tested version, `coding` and
`messaging` profiles expose configured MCP tools; `minimal` or
`tools.deny: ["bundle-mcp"]` hide them. Per-server tool filters can also limit
access. Tool visibility does not authorize orders, outgoing email or schedules.
Only trusted household users should access an attached gateway/chat because
it acts with the service owner's authority.

Attachments are read in the client, then passed through Meal Concierge's recipe
import tools. Client-local paths are not automatically readable by the service;
use the shared skill's attachment workflow.

## Updates and help

Update the existing service through the [runtime procedure](runtime.md#updates-failures-and-recovery).
Regenerate the connection fragment from the matching checkout when attachment
settings change, reload OpenClaw and verify its tools and shared skill. The MCP
registration must launch only the bridge, not a second household service.

Code updates preserve recipes, including collections imported by older
versions. Update the
code before separately asking to
[import the latest optional collection](runtime.md#versioned-recipe-package-integration).

A timeout or closed conversation does not prove an operation stopped. Reconnect
to the same service and check the original cart change, checkout or send before
retrying. Client cleanup should leave the independent household service running.

## Verification boundary

See [historical OpenClaw evidence](installation-evidence.md#openclaw) and the
[platform matrix](platform-acceptance.md) for tested versions and limits.
