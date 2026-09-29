# Grok Bot

Install Meal Concierge on **Grok's cloud computer**. Your desktop and Grok's
cloud filesystem are separate. Other Bots can share that cloud computer's
files, MCP registrations and skills; use only trusted same-owner Bots.

## Install with an AI agent

Paste this into the Grok Bot that will use Meal Concierge:

> Set up Meal Concierge from https://github.com/poisdahl/meal-concierge
> on this Grok cloud computer. Follow docs/grok.md.
> Connect to my existing household if present; otherwise install the latest
> published stable program release using docs/runtime.md#choose-a-program-release.
> Pin its tag to a full commit; do not install main. Ask which store and household
> to use as needed.
> Preserve my data and settings. Verify the running build, tools, skill and store
> connection, report the release tag and running commit, and help me complete
> login and activation.

This connects to the existing household when one is already installed.
For a program update, use the [update prompt](../README.md#update-meal-concierge).
To choose a specific release, use the
[exact-version instructions](../README.md#program-releases-and-exact-versions).

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

For a new installation, [choose a stable program release](runtime.md#choose-a-program-release)
and retain its exact commit and checkout. A commit-specific GitHub ZIP is also
suitable; inspect archive paths before extracting into a new directory. Retain
the release tag and archive provenance separately, since an archive installation
cannot report its Git commit. Keep source separate from household data.

Follow [external service setup](runtime-reference.md#externally-managed-hosts). Use
`./install.sh install --manager external` with the chosen store, household and
explicit paths, including the validated browser adapter and Chrome/Chromium.
Keep data, OAuth tokens and browser profiles in a dedicated
`/workspace` directory; separate replaceable code and short sockets can use
`/tmp`. Installation leaves the service stopped and imports no collection.
For Grok's visible cloud desktop, discover its actual display and install with
`--browser-mode headed --browser-display DISPLAY_VALUE`. If that desktop uses
Xauthority, also pass `--browser-xauthority ABSOLUTE_AUTHORITY_FILE`; the browser
user must have access to that file and display. These launch choices are saved
and reused after service/browser restarts. Do not rely on manually opening a
headed browser once: a later cold launch must use the same settings. Keep the
installation's dedicated profile; do not attach a general desktop Chrome profile
through CDP as a login workaround. Login in a different profile does not transfer
the household's store session.


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

Inspect existing skills and identify every pointer for the same installed path,
MCP and household/store identity. Grok's native `update_state` supports
`target: "skill"`, `action: "write"`, `name`, `description` and Markdown `body`.
Reuse each matching pointer's existing `id`; omit `id` only when none exists and
one new skill must be created. If several match, update all of them and report
the duplicates for separate owner-directed cleanup; do not create another. Create
a short pointer requiring the installed
`PROGRAM_ROOT/current/skill/SKILL.md` to be read before meal work, bound to the
actual MCP namespace and household/store identity. Set the pointer's
`description` to the exact `description` from that installed `SKILL.md`
frontmatter; do not invent or retain a broader Grok-specific description.

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

Start with [bounded maintenance](maintenance.md). Run the installer commands in
native shell executions and keep their handles; do not turn recipe records,
full logs or each unchanged progress check into separate model work. Read only
the applicable update sections. The existing installer performs the pack sync,
including withdrawn-entry cleanup; never replace the whole bank or recreate it
through thousands of recipe tool calls.

Use the [runtime update procedure](runtime.md#updates-failures-and-recovery) and
[external service ownership instructions](runtime-reference.md#externally-managed-hosts).
For a locally approved collection archive larger than Grok's observed 100 MiB
`CopyToBox` per-file limit, use the
[verified split/reassembly procedure](runtime.md#versioned-recipe-package-integration)
with the approved original size and SHA-256. Transfer its small manifest and
parts as files into the exact installation's private download area, reassemble
there, and inspect the verified whole ZIP before the separately approved import.
Published Optional Recipe Collection releases use the runtime's direct HTTPS
download and preparation path; do not route them through `CopyToBox`.
From the new source, run `check-browser` against the existing home before stopping
the healthy execution; repeat any explicit browser paths on `update`. This check
does not open the browser or require store login. Stop only the exact installation
when idle, and confirm its service child also exited. Never use global
`RestartMcpServers` to repair one installation. After an update, reconnect its MCP
registration. Rewrite every matching pointer by its `id`, set its `description`
to the exact `description` from the newly installed `SKILL.md` frontmatter,
preserve or update its body binding to the actual MCP and household/store
identity, report any duplicates for separate owner-directed cleanup, then reload
the pointer skill.

If that targeted reconnect still leaves native host calls failing, distinguish
service health from host registration state. Verify the exact service through a
direct MCP SDK call and a fresh inert control registration through the host. Only
when both work while the old product registration fails, create a fresh registration
with the same verified command, arguments and environment. Cut over only the matching
skill pointer to the new exact MCP identity and household/store, reload it, then
verify native status and a read-only recipe or cart call. Remove the stale
registration and temporary control only after that native verification succeeds.
Keep the original registration available until cutover is proven; do not reset all
MCP servers or recreate household state. Reconcile any uncertain original operation
before retrying it through the new identity.

Code updates preserve recipes, including collections imported by older
versions. If both are requested, update the code first, then
[the optional collection](runtime.md#versioned-recipe-package-integration), reusing
the installation paths and matching registration.
For a collection update, run `prepare-recipes` while the service is available,
then stop only for `import-recipes --prepared ID`. Keep the execution handle,
relay phase/progress or failure promptly, and restart after import exits. A
network error during preparation is not an import still working; do not silently
wait for GitHub or repeatedly rediscover a verified archive.
An explicit request to remove the entire collection requires a current runtime:
stop the exact cloud execution when idle, run
`./install.sh remove-recipe-collection --home ABSOLUTE_DATA_HOME` through the
established host executor, then restore that same execution. This is an offline
installation operation; do not substitute per-recipe archive calls.
After cloud runtime loss, rebuild missing replaceable code while preserving
durable data, credentials and operation records.

A Grok timeout can occur while Meal Concierge continues working. Reconnect and
check the original cart change, checkout or send before retrying; increasing the
bridge timeout alone cannot make Grok wait longer. For browser trouble, inspect
the exact managed session, launch mode, display and service error first. A failed
page read is not proof that login expired. Do not repeatedly restart Chrome,
clear its locks/profile, or ask for login to resolve an undiagnosed timeout.
Change durable launch settings through a reviewed update with the same browser
options, retain the profile, and reconcile the original operation before retrying.
After the exact session is confirmed idle, close only that browser session once
to apply changed launch options; flags cannot change an already-running daemon.

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
