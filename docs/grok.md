# Grok Bot

Install Meal Concierge on **Grok's cloud computer**. Your desktop and Grok's
cloud filesystem are separate. Other Bots can share that cloud computer's
files, MCP registrations and skills; use only trusted same-owner Bots.

## Install with an AI agent

Paste this into the Grok Bot that will use Meal Concierge:

> Set up Meal Concierge from https://github.com/poisdahl/meal-concierge
> on this Grok cloud computer. Follow docs/grok.md.
> Connect to my existing household if present; otherwise install the latest
> version. Ask which store and household to use as needed.
> Preserve my data and settings. Verify the service, tools, skill and store
> connection, and help me complete login and activation.

This connects to the existing household when one is already installed.
For a program update, use the [update prompt](../README.md#update-meal-concierge).

## Requirements

- An existing Grok Bot with cloud Shell access, native background execution, MCP
  registration and skills. Complete setup interactively so you can handle login
  and platform approvals.
- Python 3.10+, `uv`, and the selected store's
  [runtime prerequisites](runtime.md#install-and-attach).
- A visible dedicated cloud browser for the user to complete store login. Login
  on another computer does not authenticate the cloud installation.

## Manual setup

### 1. Install or reuse the household service

Inspect shared files, active services, registrations and skills before creating
anything. Reuse the intended healthy household, not another household or a test
installation. Repeating setup does not authorize an update or reset.

For a new installation, obtain the latest `main` as a full commit SHA and retain
that exact checkout and its instructions. Git checkout or a commit-specific
GitHub ZIP is suitable; inspect archive paths before extracting into a new
directory. Keep source separate from household data.

Follow [external service setup](runtime-reference.md#externally-managed-hosts). Use
`./install.sh install --manager external` with the chosen store, household and
explicit paths. Keep data, OAuth tokens and browser profiles in a dedicated
`/workspace` directory; separate replaceable code and short sockets can use
`/tmp`. Installation leaves the service stopped and imports no collection.

### 2. Start and connect

Run `./install.sh run --home ACTUAL_HOME` through Grok's native background
executor. Keep its execution ID and actual service PID/start identity. Once
healthy, run `./install.sh attach --home ACTUAL_HOME` and register the returned
MCP configuration through native `AddMcpServer`. Reuse an existing matching
registration and keep its server ID. The connection must attach to the existing
service, not start another one.

If Shell rejects a command, report the failure and reconcile partial effects
before recovery. All installer subprocesses remain subject to normal review.
The fallback below handles the documented executable-binding error; it is not
a way to bypass a denied operation.

### 3. Install the shared skill

Inspect existing skills. Grok's native `update_state` supports `target: "skill"`,
`action: "write"`, `name`, `description` and Markdown `body`; omit `id` to create
one new skill. Create a short pointer requiring the installed
`PROGRAM_ROOT/current/skill/SKILL.md` to be read before meal work, bound to the
actual MCP namespace and household/store identity.

Resolve all maintained skill links and helpers against that installed skill
folder. Do not copy its PDF helper into Grok's workflow folder. Select the skill
in Grok's native menu and verify its invocation uses the intended MCP. Reload
instructions after updates. Use only trusted same-owner Bots: a new Bot or skill
is not filesystem, credential or browser isolation.

### Executable-binding fallback

If Grok reports an executable-binding error, inspect any partial installation
before retrying. The [Grok interpreter instructions](runtime-reference.md#grok-executable-binding-fallback)
cover the supported invocation form. This does not override a denied operation.

## Check and first use

Ask Grok to load the installed Meal Concierge skill and show setup, the selected
household/store, store connection and available recipe sources. An empty new
local bank is normal. Recipes from your selected, connected store are available
without the optional collection. See
[first use](usage.md) and [adding recipes](recipe-import.md).

Installation does not enable orders, outgoing messages or schedules. Complete
login below, then check authenticated cart access; product search alone does not
prove login.

### Browser and login

Use a dedicated browser profile in the cloud display you can actually open.
Login on your desktop or in another browser does not connect this installation.
For Oda and Mathem, authorize the store connection and log into the same account
in the dedicated checkout browser. MENY uses that browser for the store connection.

The installing agent should follow the [Grok browser handoff](runtime-reference.md#grok-browser-and-login)
to open the correct window and keep authorization running while you complete it.
The instructions include a native browser adapter for hosts without Node/npm.
Keep passwords and authorization URLs out of chat. After login, verify service
status and authenticated cart access before calling setup complete.

## Updates and help

Use the [runtime update procedure](runtime.md#updates-failures-and-recovery) and
[external service ownership instructions](runtime-reference.md#externally-managed-hosts).
Stop only the exact installation when idle, and confirm its service child also
exited. Never use global `RestartMcpServers` to repair one installation. After an
update, reconnect its MCP registration and reload the pointer skill.

Code updates preserve recipes, including collections imported by older
versions. Update the
code before separately requesting
[the latest optional collection](runtime.md#versioned-recipe-package-integration).
After cloud runtime loss, rebuild missing replaceable code while preserving
durable data, credentials and operation records.

A Grok timeout can occur while Meal Concierge continues working. Reconnect and
check the original cart change, checkout or send before retrying; increasing the
bridge timeout alone cannot make Grok wait longer.

### Attachments and scheduled delivery

Use native conversation attachments; a desktop path is not a cloud file. Follow
the shared skill for input and [recipe delivery](recipe-delivery.md) for output.
Verify the actual file arrived in the intended conversation. Sending a path as
text is not attachment delivery.

**Grok Bot group rooms do not deliver PDF and image attachments through this integration.** Use
complete text with dates, dishes, portions, source links and credits, or another
verified destination. Report this limitation before promising PDF/image delivery.

For routines, check the result in the intended conversation; `Succeeded` alone
does not establish delivery. Inspect any existing result before rerunning work.
Keep the cloud host available and check scheduled outcomes; do not assume that
sleeping or restarting it will preserve on-time delivery.
