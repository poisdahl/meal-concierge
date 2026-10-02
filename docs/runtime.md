# Install, update and maintain Meal Concierge

The easiest setup is to use the [installation prompt](../README.md#installation)
and the guide for your already installed agent. This page gives the shared
manual steps and the information an installing agent needs.

Meal Concierge runs as a separate service. Your agent connects to it; recipes,
settings and store login belong to the service installation.

## Choose a program release

For a new installation or a requested program update, use the
[official releases](https://github.com/poisdahl/meal-concierge/releases):

1. Choose the highest published stable program version with a tag of the form
   `vMAJOR.MINOR.PATCH`, comparing the three numbers numerically. Exclude drafts,
   prereleases and `recipes-*` collection releases. Read the selected release's
   notes; the repository's generic “Latest” release may refer to a recipe pack.
2. If the user names a version, select that exact published program release
   instead. If it cannot be found or fetched, report the problem without
   substituting another version or `main`.
3. Resolve its tag to a full commit and retain a clean checkout at that commit,
   outside the household data directory. Record both the tag and commit. Use
   that checkout's installer and matching client builders; consult its host
   guide for version-specific requirements. Keep the selected commit even if
   an older tagged guide still says to obtain “latest main”.

For example, this checks out **v0.1.2**, not a moving “latest” version. With Git
installed, replace the tag and the new, unused source path for your selection:

```sh
git clone --branch v0.1.2 --depth 1 \
  https://github.com/poisdahl/meal-concierge.git /absolute/meal-concierge-v0.1.2
cd /absolute/meal-concierge-v0.1.2
git checkout --detach
git rev-parse HEAD
git status --short
```

Compare the full commit with the selected release's tag/recorded commit; the
status output should be empty. Retain this source directory for client packaging
and maintenance. A commit-specific GitHub source archive is also usable, but
does not carry Git metadata: retain its provenance separately and expect the
[running build](maintenance.md#identifying-the-running-build) to lack a Git commit.

`install.sh install` and `install.sh update` stage the source they run from.
They do not fetch the latest program or accept a program `--version` flag.
Do not use a plain clone of `main`, `git pull`, or a recipe release as a substitute
for selecting a program release. A development build requires an explicit request
and its own pinned commit. Repeating setup for an existing household retains its
installed version and attaches to it; it does not select a newer release.

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

For a **new** installation, [choose a program release](#choose-a-program-release)
and run from its retained checkout, replacing the example household and provider
(`oda`, `mathem` or `meny`):

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
Compare the service's running build with the selected release commit using
[build verification](maintenance.md#identifying-the-running-build).
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

Start with [bounded maintenance](maintenance.md): read only the relevant update
sections, reuse installation details and verify the defined checks rather than
running a full product audit.

> Update my existing Meal Concierge installation to the latest published stable
> program release using docs/runtime.md#choose-a-program-release. Pin its tag to
> a full commit; do not install main. Preserve my data, login and recipes. Follow
> docs/maintenance.md and my agent’s update steps, refresh the connection if needed, and perform the
> bounded verification. Report the release tag and running commit. Leave the
> collection unchanged unless I request its update.

[Choose the target program release](#choose-a-program-release) first; use an exact
published version when requested. If the same unmodified build is already running,
verify it and report that no program update is needed. Otherwise retain the target checkout
before stopping anything. While the existing service is still running, validate
the dependencies from that checkout:

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
Verify the same household and saved data, and compare the
[running build](maintenance.md#identifying-the-running-build) with the target commit.
Selecting an older tag is not a supported shortcut for undoing state migrations;
use the recovery guidance below for an interrupted update.

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
> version was imported and whether any conflicts need my attention. Follow
> docs/maintenance.md and use the installer’s pack operation, not individual recipe tools.

The same request **adds the collection for the first time or updates it later**.
If you have an older program version, update the program first. This also applies
to installations that received the old `2026-09-06.5` collection automatically:
a code update leaves that collection unchanged until you request an import.

From current source, prepare first while the service stays running. Keep the
`prepared` ID from the JSON output. Only after successful preparation, and when
no active work will be interrupted, stop through the existing owner, import that
exact ID and restart. Native-manager example:

```sh
./install.sh prepare-recipes --home /absolute/data-home
# Use the returned prepared ID below; do not stop if preparation failed.
./install.sh stop --home /absolute/data-home
./install.sh import-recipes --home /absolute/data-home --prepared PREPARED_ID
./install.sh start --home /absolute/data-home
```

Preparation reads the publisher-maintained collection descriptor, then verifies
checksum, size, format and installed-runtime compatibility. Ordinary updates
make no GitHub REST API request. Grok and other external hosts retain their
existing execution owner for stop/start. You do not need to find a version number
or edit a configuration file. Import reports its result; review any conflicts before
retrying an incomplete import. Existing collection recipes can advance to the
new publisher version while preserving local edits, favorites and archive state
for records that remain in the collection. Once every incoming record has been
read, an authoritative update permanently deletes same-pack entries absent from
the new release. That deletion includes any local revisions, archive state and
favorite belonging to the removed entry. Other local recipes, collections and
favorites are outside the cleanup. An invalid pack or an interruption before the
complete record pass does not perform absent-entry deletion.

Use `prepare-recipes --recipe-pack /absolute/pack.zip` to reuse a downloaded
archive; it must match the current publisher descriptor. `import-recipes
--prepared ID` is fully offline and rechecks the prepared bytes. It never
silently switches to a newer release. Preparations survive failures for exact
retry; a changed runtime or an intervening different collection update/removal
requires fresh preparation. A network failure leaves the service running: report
the error and any retry time promptly, without polling or silently waiting.
Use the host execution handle and stderr phase/count progress to track a long
operation; if detached, inspect that same execution before retrying. Once an
import has exited (including an error), restore the service through its owner
when no update/recovery marker prevents starting it. Do not claim a partial import
completed. See [preparation and recovery details](runtime-reference.md#versioned-recipe-package-integration).

For an independently approved **local** collection archive that is not on the
publisher channel, use the applicable reviewed import path for that pack. A
user-selected pack follows the
[local collection procedure](runtime-reference.md#user-selected-collection-packs);
an unpublished candidate for the reserved publisher pack needs its separately
reviewed maintenance path. If a host file transfer caps each file below the
archive size, split it with the
standard-library helper on the source host, transfer the manifest and every
part as files, then reassemble on the destination host:

```sh
python3 recipe_pack_transfer.py split --source /absolute/approved.zip \
  --parts-dir /absolute/transfer-parts --expected-bytes APPROVED_BYTES \
  --expected-sha256 APPROVED_SHA256
# Transfer transfer-parts/pack-transfer.json and every transfer-parts/part-* file.
python3 recipe_pack_transfer.py assemble \
  --manifest /absolute/transfer-parts/pack-transfer.json \
  --output /absolute/inbox/approved.zip --expected-bytes APPROVED_BYTES \
  --expected-sha256 APPROVED_SHA256
```

The expected whole-archive size and SHA-256 must come from the independent
approval, not the transferred manifest. The default parts are 64 MiB, below
Grok Bot's observed 100 MiB `CopyToBox` per-file cap; `--max-part-bytes` can
be lowered for another transport. Reassembly checks part order, size and
digest and the whole archive before exposing the output path. It refuses to
overwrite an existing file. The helper does not import or publish the archive;
after reassembly, use the appropriate read-only pack inspection and stopped
import with the same approved SHA-256. Do not send archive bytes
through model context or publish a private collection to work around a host
transfer limit.

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
