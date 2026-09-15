# Install, update and maintain Meal Concierge

The easiest setup is to use the [installation prompt](../README.md#installation)
and the guide for your already installed agent. This page gives the shared
manual steps and the information an installing agent needs.

Meal Concierge runs as a separate service. Your agent connects to it; recipes,
settings and store login belong to the service installation.

## Install and attach

### What you need

- **Linux** with a working user systemd service manager, or **Apple Silicon
  macOS** with a logged-in user's launchd session. Grok uses its
  [cloud setup](grok.md). Windows, Intel macOS and WSL are not covered here.
- **Python 3.10+** and **uv** for installation. The installer downloads its own
  Python 3.12.12 and dependencies; you do not manage that environment yourself.
- For browser use: **agent-browser 0.33.1** and **Chrome or non-snap Chromium**.
  The npm adapter may need Node.js 24+. Linux ARM64 needs a distribution Chromium.
  The [Grok guide](grok.md#browser-and-login) covers its native browser option.
- A separate data directory for each household/store. Additional trusted agents
  should connect to the same installation instead of making copies.

Oda, Mathem and MENY require the same browser dependencies during installation.
For Oda and Mathem, dependency validation is separate from both MCP authorization
and dedicated-browser login; installation does not open a browser or require a
store account session.

### 1. Find or create the installation

Before creating anything, inspect known services and household data locations.
From an existing source checkout, you can also check conventional locations:

```sh
./install.sh discover
```

This checks selected and conventional locations only. For an existing healthy
household, retain its version and use `attach`; a repeated setup request is not
an update request. Do not replace another household or an existing service.

For a **new** installation, obtain the latest `main` from the
[official repository](https://github.com/poisdahl/meal-concierge) and retain a
checkout at its exact commit. Keep source outside the data directory. Run from
that checkout, replacing the example household and provider (`oda`, `mathem`
or `meny`):

```sh
./install.sh install --provider oda --household "My household" \
  --agent-browser /absolute/path/to/agent-browser \
  --browser-executable /absolute/path/to/chrome
./install.sh start
./install.sh attach
```

`install` creates the service but leaves it stopped. `start` runs it; `attach`
returns the connection details and skill path. Add these using your
[agent's guide](../README.md#agent-support), then verify the tools from a new
conversation. A successful installation does not log into the store.

Data defaults to `~/.local/share/meal-concierge`. For another home, pass
`--home /absolute/data-home` to **every** command. Distinct installations also
need distinct `--name` and `--code-root` values. Keep program and data paths
separate. If a socket path is too long, choose short durable paths with
`--socket` and `--browser-socket-directory` before installing.

`--uv /absolute/path/to/uv` overrides uv discovery. The installer checks the
adapter version and verifies that the other executable is Chrome or non-snap
Chromium. Use the existing installation's paths when updating; never create a
second service to repair the first. See
[existing installation adoption](runtime-reference.md#existing-installations)
for a deliberate move from a legacy supervisor or Compose layout.

### 2. Connect the store

Follow [store login](#provider-oauth) below. Online recipes from your selected,
connected store are available without importing a local collection. A new local
recipe bank can be empty; this does not mean the installation failed.

### 3. Check the result

In the actual agent conversation, ask to show Meal Concierge setup, the selected
household and store, and available recipes. Confirm the skill and tools load.
After login, check a recipe/product search and cart read. Report core setup,
store connection and checkout readiness separately. These checks do not place
an order or send email.

## Provider OAuth

Oda and Mathem use the same connection procedure with separate store accounts.
First authorize the connection using the installed helper. Substitute the
program and token paths recorded in the installation's `runtime.json`:

```sh
/absolute/program/current/venv/bin/python -I /absolute/program/current/provider_oauth.py \
  --provider oda --tokens /absolute/data-home/tokens
```

For Mathem, use `--provider mathem`. Complete the authorization in the browser.
Adding `--status` inspects saved authorization only; ask the service to check
that it can actually reach the store after login.

For checkout, also log into the **same account** in the installation's dedicated
browser and check its delivery address and saved payment method. Authorization
of the connection does not log in that browser. MENY uses the dedicated browser
for its whole store connection and requires home delivery and Vipps setup.

On a remote host, have the installing agent provide a private browser/login
handoff. The [headless login instructions](runtime-reference.md#provider-oauth)
explain `--no-browser` and forwarding the local callback port. Do not put
passwords, tokens or callback URLs in chat, or copy cookies from another browser.
Close the visible login browser before the supervised browser reuses its profile.

An older Mathem installation may not yet record browser executables. Before its
next update, follow the preflight and update below; the update records the
requirements without changing the household, OAuth tokens or existing private
browser paths. Log in only after the program update. See
[browser setup details](runtime-reference.md#provider-oauth).

## Updates, failures and recovery

### Update the program

> Update my existing Meal Concierge installation to the latest main, pinned to
> a specific commit. Preserve my data, login and recipes. Follow docs/runtime.md
> and my agent's guide, refresh the agent connection if needed, and verify it.

Obtain the chosen new source commit first. While the existing service is still
running, validate the dependencies from that checkout:

```sh
./install.sh check-browser --home /absolute/data-home
```

If discovery fails, install the named requirements and retry with
`--agent-browser /absolute/path` and `--browser-executable /absolute/path`.
Repeat those explicit arguments on `update`; `check-browser` is read-only and does
not save them, open a browser or require store login. For a legacy Mathem browser
upgrade or an explicit browser replacement, run the check and update in the same
host environment. The validated update environment takes precedence and any
missing entries from the existing service `PATH` are retained, so an npm adapter
can keep finding Node without discarding previously available helper locations.

After the check passes, inspect active shopping, payment, delivery and email work.
Wait for it to finish and resolve uncertain results before maintenance. For a
native installation, retain the same home and run:

```sh
./install.sh stop --home /absolute/data-home
./install.sh update --home /absolute/data-home
./install.sh start --home /absolute/data-home
./install.sh attach --home /absolute/data-home
```

The update makes an offline backup of state and configuration before migration.
It preserves recipes, local edits, favorites, saved menus, settings and existing
login paths. It does not import or refresh the optional collection. Refresh the
client package or skill using your agent's guide and start a new conversation.
Verify the same household and saved data.

### Recover an interrupted attempt

A timeout does not prove the installer or service stopped. Have the installing
agent inspect the original process and installation before retrying. Keep all
data and login files; do not reinstall, reset the host or delete locks/maintenance
markers to force progress.

An interrupted install/update may need the stopped `update` recovery using the
same source and paths. An interrupted recipe import instead needs inspection of
its report and, once resolved, `import-recipes`. See
[detailed recovery](runtime-reference.md#updates-failures-and-recovery).
Never restore an old backup over a possibly completed order, payment or email;
reconcile the original operation first.

## Versioned recipe package integration

### Add or update the recipe collection

> Synchronize the latest Optional Recipe Collection into my existing Meal
> Concierge installation. Permanently remove collection recipes that are no
> longer included. Preserve every other local recipe and favorite. Tell me which
> version was imported and whether any conflicts need my attention.

The same request **adds the collection for the first time or updates it later**.
If you have an older program version, update the program first. This also applies
to installations that received the old `2026-09-06.5` collection automatically:
a code update leaves that collection unchanged until you request an import.

When no active work will be interrupted, stop the existing service, run from
current source, and start it again:

```sh
./install.sh stop --home /absolute/data-home
./install.sh import-recipes --home /absolute/data-home
./install.sh start --home /absolute/data-home
```

`import-recipes` selects the newest published stable recipe release and verifies
its checksum, size and format. You do not need to find a version number or edit
a configuration file. It reports the import result; review any conflicts before
retrying an incomplete import. Existing collection recipes can advance to the
new publisher version while preserving local edits, favorites and archive state
for records that remain in the collection. Once every incoming record has been
read, an authoritative update permanently deletes same-pack entries absent from
the new release. That deletion includes any local revisions, archive state and
favorite belonging to the removed entry. Other local recipes, collections and
favorites are outside the cleanup. An invalid pack or an interruption before the
complete record pass does not perform absent-entry deletion.

A local `--recipe-pack /absolute/pack.zip` must match that latest release and
still requires internet access for verification. It is not an offline import
mode or a selector for older packs. If the latest release is invalid or
incompatible, the command reports the error instead of choosing an older one.

### Remove the recipe collection

> Permanently remove the Optional Recipe Collection from my Meal Concierge
> installation. Preserve every other local recipe and favorite, and reclaim
> storage used only by the collection.

Update an older Meal Concierge runtime first. When the exact installation is
idle, stop it, run the removal, then start it through the same owner:

```sh
./install.sh stop --home /absolute/data-home
./install.sh remove-recipe-collection --home /absolute/data-home
./install.sh start --home /absolute/data-home
```

The removal command is offline: it does not resolve or download a release. It
hard-deletes all bundled entries carrying the collection's fixed internal
identity, including local edits, archive state and favorites on those exact
entries. Other user recipes, other collections and their favorites remain. It
then removes collection assets that no remaining recipe, household-state
snapshot or retained migration backup references, removes the collection's
stored pack metadata and compacts the recipe database. Delivery artifacts and
household history remain. Rerun the same command if storage cleanup was
interrupted; it is idempotent.

## Externally managed hosts

Grok's cloud service uses `--manager external` and its native background
executor; it does not use `start`/`stop`. Follow the [Grok guide](grok.md) for
stopping and starting its exact execution around updates or recipe imports.
Data must remain in persistent storage. For another explicitly managed host,
see [external ownership](runtime-reference.md#externally-managed-hosts).

## Complete private data backup and relocated restore

Ask the installing agent to back up or move the installation. For a manual
backup, stop the service and choose a new private destination:

```sh
./install.sh backup --home /absolute/data-home --backup /absolute/new-backup
```

Restart the service afterward. The backup includes state, recipes, images and
configuration. It does **not** include browser profiles, OAuth tokens or external
credentials outside the state tree; preserve those separately in private storage.

Restore only to a new empty home. Use the
[restore and adoption instructions](runtime-reference.md#complete-private-data-backup-and-relocated-restore)
to reconnect the service without overwriting newer data or reviving old payments.
For removal, stop the exact service and remove its agent/native registration;
keep the data unless you explicitly want it deleted.
