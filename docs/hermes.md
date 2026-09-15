# Hermes

Connect an existing Hermes installation to Meal Concierge on the same host.
The household service runs independently of Hermes conversations.

## Install with an AI agent

Send this to Codex, Claude Code or another agent with access to your Hermes host:

> Install Meal Concierge from https://github.com/poisdahl/meal-concierge for my
> existing Hermes installation. Follow `docs/hermes.md`. Inspect the actual host
> and existing installations first. Reuse my existing household and installed
> version if present; preserve its data, settings and connections. For a new
> installation, use the latest `main`, resolve it to a full commit SHA and install
> from that checkout. Ask for my store and household if needed. Set up the
> persistent service, Hermes MCP connection and shared skill in the intended
> Hermes profile. Do not import the optional local recipe collection unless I
> request it separately. Verify the connection, household, loaded skill and
> available recipe sources. A new local bank may be empty; online recipes do not
> require the optional collection. Keep normal platform approvals and tell me
> which login or activation steps I must complete.

## Requirements

- A working Hermes installation and access to its intended profile configuration.
- A supported [runtime host and prerequisites](runtime.md#install-and-attach).
- The Hermes MCP process must run on that host as the household service owner.
  A separate container or remote execution environment needs an explicit,
  supported attachment arrangement.

## Manual setup

### 1. Install or reuse the household service

Follow [Install and attach](runtime.md#install-and-attach). Inspect existing
installations first; connect to the intended household without installing it
again. Keep the checkout and full source commit recorded for future updates.

From that checkout, obtain the running service's connection details:

```sh
python3 install.py attach --home /absolute/data-home
```

### 2. Configure Hermes MCP

Identify the active Hermes profile's `config.yaml` (normally
`~/.hermes/config.yaml`; profiles or `HERMES_HOME` can change this). Merge an
entry into `mcp_servers`, preserving existing servers. Copy `command`, `args`
and **all** `env` values from `attach`. For example:

```yaml
mcp_servers:
  meal_concierge:
    command: /absolute/program/current/venv/bin/python
    args:
      - -I
      - /absolute/program/current/mcp_server.py
    env:
      MEAL_CONCIERGE_SOCKET: /absolute/data-home/run/service.sock
    timeout: 700
```

Use the returned paths and arguments, not the example values. If `attach`
includes `MEAL_CONCIERGE_EMAIL_CONFIG`, keep it too. The connection launches the
bridge to the existing service. Do not register a second direct Oda or Mathem
client using the same token files. See the
[Hermes MCP configuration](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool.py).

### 3. Install the shared skill

Link the directory containing the returned `skill` path into the active
profile's `skills/meal-concierge/`. Use the installed `current/skill` path so the
shared instructions and PDF helper stay connected to the private runtime. For
a new skill in the default profile, after checking the destination is unused:

```sh
mkdir -p "$HOME/.hermes/skills"
ln -s /absolute/program/current/skill "$HOME/.hermes/skills/meal-concierge"
```

For another profile, use that profile's actual skills directory. Preserve a
matching existing link; inspect any different copy before replacing it.
Copying only `SKILL.md` omits helpers, and copying the skill away from its
runtime breaks the PDF helper's relative paths. Hermes discovers linked profile
skills from this directory; see its
[skill implementation](https://github.com/NousResearch/hermes-agent/blob/main/tools/skills_tool.py).

## Check and first use

Open a new Hermes session, or use the installed version's normal MCP reload
flow. For a running gateway, apply its normal reload/restart procedure when idle.
Ask Hermes to load the Meal Concierge skill and show setup, the selected
household/store, store connection and available recipe sources.

An empty new local bank is normal. Recipes from your selected, connected store
are available without the optional collection. Follow
[first use](usage.md), [store login](runtime.md#provider-oauth) and, if wanted,
[adding recipes](recipe-import.md). Verify authenticated cart access after login.
Installation does not enable orders, outgoing email or schedules.

Only trusted household users should access an attached Hermes chat: the service
uses the host owner's authority, not separate identities for each chat member.

## Updates and help

Update the existing service through the [runtime procedure](runtime.md#updates-failures-and-recovery),
then reload Hermes to pick up the linked installed skill.
Check the MCP connection and skill again. Code updates keep existing recipes,
including collections imported by older
versions; update the code before separately requesting
[the latest optional collection](runtime.md#versioned-recipe-package-integration).

After an uncertain cart change, checkout or send, check the original operation
before retrying. For attachment errors, compare the active profile, returned
paths, owner and service status. The [platform matrix](platform-acceptance.md)
records tested scope; this guide does not imply a fresh native Hermes lifecycle
test was performed for every version.
