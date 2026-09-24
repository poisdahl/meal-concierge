# Standalone runtime and safe updates — technical reference

For installation agents and maintainers. Start with the [user guide](runtime.md).
Installation and code updates do not import the optional recipe collection.

The runtime runs independently of agent conversations on Linux/user-systemd,
Apple Silicon macOS/launchd, or an explicit external process owner.
Installation, service lifecycle and agent attachment
are separate commands. The installer never registers an agent, logs in, transfers
schedulers, sends messages or performs grocery actions.

## Install and attach

Use Python 3.10+ for the installer and install `uv` on PATH, or pass its verified
executable path with `--uv`. The installed runtime
uses Python 3.12.12 and all versions in `runtime-requirements.txt`, including
`mcp==2.1.1` and `mcp-types==2.1.1`. Installation verifies both SDK versions and
loaded module paths inside its own virtual environment. Hermes is not required.
For Oda, Mathem and MENY, install `agent-browser@0.33.1` and a non-snap
Chromium/Chrome.
The adapter may need Node.js 24+ on the PATH used to install the service.
Apple Silicon app discovery includes `/Applications/Google Chrome.app` and
`~/Applications/Google Chrome.app`. Linux ARM64 needs a distribution Chromium;
the adapter's Chrome for Testing download does not provide Linux ARM64 builds.

From a product checkout **outside the data directory**:

```sh
./install.sh install --provider meny --household "My household" \
  --agent-browser "$HOME/.local/lib/meal-concierge/node_modules/.bin/agent-browser"
./install.sh start
./install.sh attach
```

`--browser-executable /absolute/path/to/chromium` overrides browser discovery.
Installation leaves the service stopped. `attach` checks the running service and
prints its stdio MCP command/args/env and skill path; register those with each
trusted owner's agent. It changes neither client configuration nor service
lifecycle. Follow the appropriate [agent guide](../README.md#agent-support) to complete registration. Additional clients share the same service.

New data defaults to `~/.local/share/meal-concierge`; set `--home` (or
`MEAL_CONCIERGE_HOME`) for another installation. Code defaults to
`~/.local/lib/meal-concierge/<service-name>`. `--code-root` must be disjoint from
data and belongs to one installation. Each release has its own venv; `current`
selects code. New installations use a short browser instance name; socket paths
are checked against the native Unix limit before staging. Use shorter explicit
`--socket`/`--browser-socket-directory` paths when adopting a long legacy layout. Old releases are retained. `--name` selects the native service name
(default `meal-concierge` on Linux, `com.meal-concierge` on macOS).

| Private path | Contents |
|---|---|
| `config.json` | Household/provider configuration; preserved during updates |
| `state/state.json` | Household state and protected outcome/email journals |
| `state/recipes.sqlite3` | Own bank, revisions, snapshots and library-operation journals |
| `state/recipe-assets/` | Managed recipe images; copied with the entire state tree |
| `browser/` | Dedicated browser home/profile and daemon socket directory |
| `tokens/` | Private provider OAuth state; populated only by explicit login |
| `run/` | Socket-only directory for agent connection; no household data |
| `backups/` | Private, complete offline state/config copies made before migration |
| `runtime.json` | Exact installation paths, owner and selected/previous code release |

Oda/Mathem OAuth uses the installed MCP SDK without Hermes. Provider readiness
is separate from service health. Existing Compose and explicit legacy runner
paths remain supported by `service.py`; native adoption does not convert Compose
or claim live parity. MENY login and the private `vipps_phone_number` used by
Oda or MENY require the authorized provider setup.

## Externally managed hosts

Use `--manager external` for a host such as the Grok cloud computer that can
keep a foreground command running as a native background execution but has no
user systemd/launchd manager. This uses the same release staging, pinned Python
and dependencies, configuration, migration and ownership
locks as native installations. It writes no systemd unit or launchd plist and
does not install another supervisor. A normal installation without this option
retains the native manager behavior.

From the reviewed source directory, for example:

```sh
python3 install.py install --manager external --uv /usr/local/bin/uv \
  --home /workspace/meal-concierge/home --code-root /tmp/meal-concierge/program \
  --socket /tmp/meal-concierge/service.sock \
  --browser-socket-directory /tmp/meal-concierge/browser \
  --agent-browser /absolute/path/to/agent-browser \
  --browser-executable /absolute/path/to/chromium \
  --provider mathem --household "My household"
python3 install.py run --home /workspace/meal-concierge/home
```

These are example paths and provider choices; inspect the actual host and use
the user's intended store. Oda, Mathem and MENY require the same validated
browser dependencies.
The first command performs the declared `uv` staging, verification and migration
subprocesses; it does not start the service or authenticate a store. Do not treat
this entry point as a bypass for platform review of its underlying operations.
Grok-specific setup and limitations are in the [Grok guide](grok.md).

Run the second command through the platform's normal background-execution
facility and retain its exact execution ID and service PID/start identity.
`run` waits for the selected release's foreground service and holds the installer
lock until that child exits. The service independently holds its data/listener
locks. `attach` remains available while it runs:

```sh
python3 install.py attach --home /workspace/meal-concierge/home
```

`start`, `stop` and `restart` deliberately refuse this mode: the external owner
must control its exact execution. Terminating the launcher alone may leave the
service child alive. Reconcile both the native execution and actual service
identity before stopping a surviving task-owned process or starting another.
A timeout, missing output or vanished parent is not proof that the service
stopped. Never kill by a broad command/name match.

Repeated setup should discover the matching installation, inspect its identity
and attach to its healthy service. `install` refuses an existing installation;
it does not mean update. Before stopping a healthy execution for `update`, run
`check-browser` from the new source while the exact execution remains active.
Repair missing prerequisites first. Then the owner must stop that execution and
establish that no service survives. Before a backup, stop it directly. External offline
checks use the existing ownership locks; acquiring those checks can create lock
files and remove a proven-stale socket, so they are not read-only inventory.
Busy, invalid or uncertain targets fail without permission to take them over.
Manager choice remains in `runtime.json`; install/update and lifecycle commands
reject a conflicting `--manager` rather than changing ownership.

`run` refuses pending installation or maintenance state. Resume an interrupted
publication through the existing stopped `update` path, using its original home
and paths. Retained recipes/assets and outcome journals must not be replaced.
After cloud runtime loss, rebuild the missing replaceable runtime from the
matching reviewed source; do not restore older household data. Changing from a
previous supervisor is a separate explicit ownership transfer, not a side effect
of selecting external mode.

## Provider OAuth

Run the helper with the installation's `current/venv/bin/python` and
`current/provider_oauth.py`. Use the exact token path from `runtime.json`; the
following example uses explicit installation paths:

```sh
/private/program/current/venv/bin/python -I /private/program/current/provider_oauth.py \
  --provider oda --tokens /private/household/tokens --status
/private/program/current/venv/bin/python -I /private/program/current/provider_oauth.py \
  --provider oda --tokens /private/household/tokens
```

Use `--provider mathem` for the separate Mathem login. `--status` reads only
presence, remaining expiry and pending-exchange status; it does not create files,
normalize a registration, refresh tokens, contact the provider or certify a
working connection. The login command opens the system browser and waits up to
300 seconds (`--timeout` accepts 1–600). It performs OAuth and MCP discovery,
without cart/order actions. A saved login may still report connection
`unavailable` when the provider's MCP endpoint fails. Run normal service status
for a fresh provider connection check.

For a headless host, add `--no-browser`. The helper writes an authorization URL
to a private `*.authorize.json` file and prints only its path and callback port.
Privately open that URL in your browser; do not paste the file or callback URL
into chat or logs. Forward the printed port from your local loopback to that
host's loopback with `ssh -L PORT:127.0.0.1:PORT HOST` before authorizing. Keep the
login helper and forward running until the callback completes. The callback
listener binds only `127.0.0.1`; it verifies the exact path/state, and the SDK
verifies PKCE and any authorization-response issuer. The temporary URL file and
listener are removed when the command exits. Existing registrations reuse their
exact supported `http://localhost:PORT/...` or `http://127.0.0.1:PORT/...` redirect;
a busy port fails instead of changing that registration.

Ordinary service calls never open an authorization browser or register a new
client. They refresh an expired token under the same provider lock used by the
login helper and then dispatch each MCP request once. A competing operation
returns busy. Authorization rejection requires explicit login and does not
silently replay the provider request. Missing or invalid optional provider auth
does not stop the core own-bank path.

The existing `oda-weekly` and `mathem-weekly` token, `.client.json`, `.meta.json`
and per-provider lock names remain unchanged. Legacy `expires_at` is honored;
older records use original file modification time plus `expires_in`. Keep the
original token directory in place when adopting an installation. Failed or
cancelled login leaves the previous token/registration intact until a complete
new grant is available. A private `.pending.json` records token-exchange
uncertainty or a complete replacement awaiting local publication. Ready
publication resumes under the lock after restart. An uncertain exchange blocks
another refresh and requires explicit login. Preserve this file with the other
auth files; do not delete it or restore old refresh tokens after a possible
rotation. The helper never restores household/order/email journals.

Before an authorized cutover, inspect active services/jobs and identify every
process that can use the same provider credentials. Stop/retire the old direct
OAuth owner and its automatic restart path before the standalone service takes
over. Hermes must connect through Meal Concierge; a separate direct provider
registration must not keep refreshing the same token files. The file lock cannot
coordinate an older client that ignores it. Keep each provider's token directory
available to its original-provider follow-ups when changing the active store.
Rollback preserves the newest auth transaction and outcome journals: complete a
ready publication with this runtime before any older code reads the legacy files;
resolve an uncertain exchange by explicit login rather than replaying a refresh.
Do not clone refresh credentials across installations.

Oda and Mathem additionally need the dedicated browser profile logged into the
same account, with the intended delivery address and payment method. MCP OAuth
does not authenticate that browser or prove account binding. Their protected
browser review remains the account/address check; Mathem's selected MCP address
reference must match that browser account. MENY retains its dedicated browser login.

New Oda and Mathem installations use the same discovery and validation. An older
Mathem installation may have no `browser_binary` or `browser_executable` in
`runtime.json`. From the new checkout, validate while its healthy service still
runs:

```sh
./install.sh check-browser --home /private/household \
  --agent-browser /absolute/path/to/agent-browser \
  --browser-executable /absolute/path/to/chromium
```

Omit explicit paths when ordinary discovery should find both. The check reads
the existing provider/paths and performs only bounded local executable/version
checks; it does not take service/data ownership, change metadata, launch a browser
or require store login. If explicit paths were needed, repeat them on `update`.
Only after this passes should the owner finish or reconcile active and uncertain
work, stop the exact service/external execution, and update the same home. Update
retains browser home/profile/socket paths, OAuth tokens, state, manager, service
name/unit and outstanding operation journals. When adding the browser to a legacy
Mathem installation, or replacing an executable explicitly, it also records the
validated current `PATH` first and retains any missing entries from the existing
service `PATH`. Run the check and update from the same host environment so an npm
adapter keeps its required Node executable without discarding previously available
helper locations. A configured browser is not evidence of login, account matching
or card readiness. Log in separately after the update and never copy another
browser's cookies or refresh tokens.

## Existing installations

`./install.sh discover` reports the selected home and conventional Hermes home.
It does not scan arbitrary disks or live Compose services. A detected config or
state prevents silent replacement. Inspect the old unit/container command and
preserve **all** effective paths, including tokens and browser socket directory.
Stop and disable the old supervisor under its owner's authorization first. An
old launchd plist must be retired from LaunchAgents; systemd must report disabled,
masked or not found. The installer will not stop or disable an old owner for you.

Then adopt the same data/config with a distinct new native service name:

```sh
./install.sh install --adopt --legacy-unit OLD_STOPPED_UNIT \
  --home /private/runtime-metadata --code-root /private/program \
  --name meal-concierge-replacement \
  --config /existing/config.json --state /existing/state \
  --tokens /existing/mcp-tokens --socket /existing/run/service.sock \
  --browser-home /existing/browser --browser-profile /existing/browser/profile \
  --browser-socket-directory /existing/browser/run \
  --agent-browser /absolute/path/to/agent-browser \
  --browser-executable /absolute/path/to/chromium
```

Use `--legacy-unit none` only for restored/offline data with no old supervisor.
No source config, provider, primary recipe library or credentials are rewritten.
Explicit `HERMES_HOME` in the legacy shell runner retains its token/data fallback;
new native services pass exact paths and do not use that fallback.

Lifetime locks cover resolved state/JSON/SQLite, browser home/profile/daemon and
listener paths before initialization. The service also inspects old service
process arguments before takeover. Ambiguous implicit legacy browser profiles
require stopping that process. A live listener or non-socket path is never
unlinked by a competing launcher. Locks are not proof that a pre-lock supervisor
cannot later restart: disabling/retiring that old owner remains mandatory.

## Updates, failures and recovery

```sh
./install.sh check-browser --home /private/household
./install.sh stop --home /private/household
./install.sh backup --home /private/household --backup /private/backups/manual-copy
./install.sh update --home /private/household
./install.sh start --home /private/household
./install.sh attach --home /private/household
```

Run `check-browser` from the candidate source before stopping a healthy owner;
when explicit executable paths are needed, pass the same values to both the check
and `update`. The check neither takes ownership nor changes the installation.
The installer refuses updates/backups while the owner is active. It builds and
checks the candidate venv before migration; under offline lifetime locks it copies
the full state tree/config, opens and migrates both JSON and SQLite, publishes
its own native definition and switches code. Updates preserve exact recipe refs,
local edits, histories and outstanding operations. They do not rerun the selective
`migrate.py` importer, change primary libraries or reconcile provider effects.

`maintenance.json` blocks service start after a migration/publication failure.
`pending-install.json` preserves exact paths if first installation is interrupted.
Retry `update --home ...` to finish from the current data. Native registration is
an exclusive link to that home's durable definition: retry cannot overwrite a
foreign service unit. Partial build directories and backups are retained for
inspection, not automatically pruned. A failed migration can have upgraded one
store before the other fails; do not manually remove the maintenance marker and
start old code. Repair the cause and retry, or inspect a private offline restore.

Code rollback is separate from data recovery. Never replace current journals with
a pre-order/pre-send backup after possible external effects. This installer does
not offer an automatic data rollback or downgrade. Preserve latest outcomes and
reconcile their original identities before any recovery decision.

## Complete private data backup and relocated restore

```sh
./install.sh restore --backup /private/backups/manual-copy \
  --home /private/new-empty-home
```

Restore accepts a complete installer backup into a **new** home only; it never
starts a service or overwrites an existing home. It copies SQLite including its
sidecars, JSON, all state snapshots/assets and config together. Symbolic links
and special files in the state tree are rejected: linked files must be deliberately
relocated before backup, never silently followed or omitted. The completion
marker is written last; a failed backup cannot be restored as complete.

Browser profiles, OAuth tokens and external recipe-library credentials outside
the state tree are not included. Preserve their existing paths during adoption;
re-establish or separately manage credentials under the correct provider owner
when restoring to another host.

## Versioned recipe package integration

Installation and code updates do not download or import a recipe collection.
A fresh local bank can be empty; recipes from the selected, connected store
remain available without the collection. Existing recipes are kept.

To add or refresh the **Optional Recipe Collection**, use current repository code
and update an older runtime first. Stop the existing service through its current
owner, then run this command with the installation's actual home:

```sh
./install.sh import-recipes --home /absolute/data-home
```

Start the service again through the same owner after import. For Grok/external
installations use the established host executor; native installations use
`./install.sh stop --home /absolute/data-home` and `start` respectively.
Never interrupt an active shopping, payment or delivery job to import recipes.

`import-recipes` selects the most recently published stable `recipes-` release
from the official GitHub repository, independently of the runtime code version.
Drafts, prereleases and code releases are excluded. It verifies the archive
against GitHub's SHA-256 and byte size, then checks the supported format before
writing. A missing/invalid latest artifact or unsupported format is reported;
there is no silent fallback to an older pack. No release lookup occurs during
ordinary installation or update.

`import-recipes --recipe-pack /absolute/pack.zip` uses a local copy but still
looks up the latest release and verifies the same digest and size. It is not an
offline mode or a way to select an older version. Archive data does not pass
through RPC. Import requires the existing service to be stopped and retains the
normal exclusive ownership locks.

### User-selected collection packs

A separate local path imports a private or otherwise user-selected collection;
it never treats that ZIP as the official publisher bundle. First inspect the
exact file with the installed runtime:

```sh
./install.sh inspect-recipe-pack --home /absolute/data-home \
  --recipe-pack /absolute/family-recipes.zip
```

Inspection is read-only with respect to the installation and may run while the
service is active. It rejects links and special files, verifies the complete
bounded archive, and prints its SHA-256, byte size, pack identity/revision,
membership mode and record count. Recipe prose and metadata remain untrusted
data. A local collection cannot carry store binding, local estimate acceptance,
or Meal Concierge project-review authority. The official
`wikibooks-themealdb-en` pack identity is reserved.

After reviewing the output, stop the exact service owner and import the same
bytes. Pinning the digest from inspection is recommended when another person or
agent supplied the ZIP:

```sh
./install.sh import-recipe-pack --home /absolute/data-home \
  --recipe-pack /absolute/family-recipes.zip \
  --expected-sha256 SHA256_FROM_INSPECTION
```

To remove that exact local collection later, stop the same owner and use the
same ZIP plus the inspected digest:

```sh
./install.sh remove-recipe-pack --home /absolute/data-home \
  --recipe-pack /absolute/family-recipes.zip \
  --expected-sha256 SHA256_FROM_INSPECTION
```

This removes only `entry_origin=collection` records with that local pack ID,
after confirming that the exact ZIP was previously installed, then reclaims only
its unreferenced assets and retained metadata. It is separate from
`remove-recipe-collection`, which removes the publisher's Optional Recipe
Collection only.

### Managed local-pack inbox

An operator-managed host may opt in to an inbox instead of stopping its service
for every local collection change. Pass a private, absolute, service-visible
directory with `service.py --recipe-pack-inbox /absolute/inbox`. Mount that
directory read-only into the service and keep the service state on its existing
private writable volume. A host where the agent and service use different UIDs
may use one setgid directory owned by their dedicated trusted group: it must
have no `other` permissions, and staging grants that group read-only access only
after the complete ZIP is fsynced. The corresponding MCP client needs two absolute paths:
`MEAL_CONCIERGE_RECIPE_PACK_DOWNLOADS` is the agent's direct download directory
and `MEAL_CONCIERGE_RECIPE_PACK_INBOX` is its writable view of the same inbox.
The client first checks `meal_concierge_recipe_pack(action=status)`.

The managed tool accepts only a direct ZIP filename from the configured download
directory. `stage` copies it under a SHA-256-based opaque archive ID; `inspect`
then binds identity, revision, count and digest. `import` and `remove` require
that same archive ID and inspected digest. An authoritative import needs the
explicit `allow_recipe_removals=true` field. The service snapshots the untrusted
inbox member into its private state before reading it, serializes the operation
against recipe planning and cart work, and rejects active cart, checkout,
cancellation or order-change state. It never downloads a URL, accepts a general
path, stops the service, or changes anything beyond the exact local collection.

Do not enable this optional route by mounting a general agent data directory
into the service. The inbox must contain only staged collection archives; the
service still treats every archive as untrusted and validates the complete pack.

The importer derives a descriptor from the selected file, verifies an optional
digest pin, then copies it into a private immutable staging file while checking
the same digest and size. Preflight and application reopen only that staged
copy. Multiple collections coexist by stable `pack_id`; records within one pack
update by stable `recipe_id`. Existing local edits conflict rather than being
overwritten. `pack_revision` is a positive monotonic integer: rollback and reuse
of one revision for different content fail closed. `pack_version` is the
human-facing version label and need not be orderable, but each changed revision
must use a new label because retained metadata is keyed by pack ID and version.

Local collection manifests use `kind: collection`. Use
`membership_mode: merge` unless the file is intentionally a complete snapshot;
merge omission never deletes an installed recipe. An authoritative local pack
is rejected unless the operator also supplies both `--allow-recipe-removals`
and `--expected-sha256` on that exact import. Once authorized and fully read,
it permanently deletes absent
recipes belonging to the same `pack_id`, including their local edits, archive
state and favorites. The flag never widens deletion to another pack or a user
recipe. Local removal likewise has no remove-by-name shortcut: it requires the
selected ZIP and its exact inspected SHA-256.

The minimum collection manifest fields are:

```json
{
  "format": "meal-concierge-recipes",
  "format_version": 1,
  "kind": "collection",
  "pack_id": "family-recipes",
  "pack_version": "2026.1",
  "pack_revision": 1,
  "normalizer_version": "3",
  "recipe_schema_version": 2,
  "records_count": 1,
  "display_name": "Family recipes",
  "membership_mode": "merge"
}
```

The shared archive writer adds the exact `files` inventory. `records.jsonl`
contains canonical rows with `recipe_id`, `status` (`ready` or `draft`) and a
normalized recipe matching `recipe_schema_version`. Pack and recipe identities
must remain stable across revisions. A private recipe-history export with
`kind: private` is a different backup/restore format and cannot be imported as a
shareable collection.

`pack_id` is the collection namespace selected by the operator; there is no
publisher signature for a private pack. Choose a globally unique, stable ID and
inspect it before import. Importing a higher revision under an existing ID is an
explicit authorization to update that namespace. Collection entries are stored
as `entry_origin=collection`, distinct from the official `bundled` collection.
Recipe-only private export/restore preserves the entry origin and record-level
pack identity but intentionally excludes retained installer manifests. Use a
complete installation backup when future monotonic pack updates must remain
immediately available after restore.

Repeated imports are idempotent. A bundled recipe that remains can advance to the
new publisher version with a new history revision. Local content edits produce a
conflict; favorites, explicit local status and archived entries are preserved for
records that remain. After every record in an authoritative snapshot has been
read, the importer permanently deletes same-pack entries absent from the new
release, including their local revisions, archived state and exact favorite.
Other local recipes, collections and favorites are never selected by this
cleanup. A failed or interrupted record pass does not run absent-entry deletion.
Already committed record updates remain available; resolve the reported issue
before retrying `import-recipes`. Source links and separate text/image credits
remain available in imported records.

To remove the whole **Optional Recipe Collection**, update the runtime first,
stop the exact installation and run:

```sh
./install.sh remove-recipe-collection --home /absolute/data-home
```

Start the same service owner again after the command completes. The command uses
the installed runtime under the same offline ownership locks as import, but
performs no GitHub lookup, download or archive selection. Its only collection
selector is the reviewed built-in `wikibooks-themealdb-en` identity; names or
arguments cannot widen the deletion. It transactionally deletes every bundled
entry with that identity, including revisions, archive state, local edits and
the favorite on each exact entry. User entries, other packs and their favorites
do not match. Prior idempotency keys remain as compact tombstones that reject
replay without retaining full removed recipes or their cover references.

Retained manifests for every installed version provide the bounded list of
collection asset candidates. After the database delete, candidates still
referenced by another database record, current or migrated household state, or
a retained recipe-bank migration backup are kept. Unreferenced candidates are
deleted, then SQLite is vacuumed to return free pages to the filesystem.
Retained pack reports follow; manifests are removed last, making
file cleanup resumable and the whole command idempotent. A missing or irregular
manifest fails before database deletion. If cleanup fails after the transaction,
rerun the same command; historical menu/delivery state and completed delivery
artifacts are not deleted.

## Grok executable-binding fallback

If Shell reports the documented executable-binding error, reconcile partial
effects first, then use the [supported interpreter form](https://forum.cursor.com/t/grok-bot-0-44-0-on-macos-shell-executable-binding-rejection-persists-approval-card-never-appears/170819/10)
through normal review. Unpacking obtains the source; it does not replace the
installer or the invocation workaround below.

Use a verified Python 3.10+ interpreter at `venv/bin/python` relative to an
explicit Shell `working_directory`. If needed, create that bootstrap venv with
ordinary `uv --no-config venv --python ACTUAL_PYTHON ACTUAL_WORK_DIR/venv`.
Replace all placeholders with inspected paths and the chosen installation values:

```text
working_directory: ACTUAL_WORK_DIR
command: venv/bin/python /ABSOLUTE/SOURCE/install.py install --manager external --uv /ABSOLUTE/uv --home ACTUAL_HOME --code-root ACTUAL_CODE_ROOT --name ACTUAL_NAME --provider ACTUAL_STORE --household ACTUAL_HOUSEHOLD --socket ACTUAL_SOCKET
```

The interpreter token must not start with `/`, `./` or `../`; the script path
is absolute. Put no Python flags such as `-I -B` between them. Preserve any
additional store/browser arguments required by the normal install instructions.
Verify effective Python/package-source overrides and cache locations before
execution. A partially completed installation requires the normal recovery
procedure, not another `install` into the same home.

Do not rewrite the MCP configuration printed by `attach` to match this Shell form.


## Grok browser and login

The official [agent-browser v0.33.1](https://github.com/vercel-labs/agent-browser/releases/tag/v0.33.1)
native executable avoids Node/npm. For Linux x86_64, the `agent-browser-linux-x64`
asset's SHA256 is
`6e04d06605c4ca62da36e3263086e0f7ceae808b55508de2c3958d4b7fe430aa`.
Verify the architecture and digest before execution. Pass its installed path
as `--agent-browser` and the existing non-snap Chrome path as
`--browser-executable`. Resolve a Chrome shell wrapper to its actual browser
executable before the empty-PATH launcher test below; a wrapper may require
commands that will no longer be on PATH. Use one dedicated session/profile and
the cloud display the user can actually open; another Bot may have a different
display. Start the dedicated login browser headed (`--headed`) from the outset.
Setting DISPLAY alone does not make a headless session visible, and flags on a
later command may not change an already running session.

For Oda/Mathem, use the installed [provider OAuth helper](runtime.md#provider-oauth)
and the installation's exact token directory. Do not copy another host's tokens.
Before starting timed OAuth:

- Test an inert `about:blank#UNIQUE_MARKER` through Python's browser launcher.
  Set `BROWSER` to the verified adapter's absolute path, explicit session/profile/
  Chrome arguments, ending in `open %s &`. Use the matching `DISPLAY` and
  `AGENT_BROWSER_SOCKET_DIR`. Scope a clean environment with an empty task `PATH`
  to this helper invocation so Python cannot select a different default browser;
  retain the installation's browser `HOME`, `TMPDIR` and `XDG_CACHE_HOME`, and
  run from `PROGRAM_ROOT/current` using `venv/bin/python`.
- Require marker read-back and visibility in the user's cloud window. Select
  the exact observed task tab before OAuth if another window covers it. Preserve
  existing browser owners; do not restart shared browsers.
- Wait until the user is ready. Run the helper with up to `--timeout 600` through
  native background execution; return promptly for user takeover and keep the
  helper running during authorization. Capture helper/browser output in a new
  private `0600` log; do not read it or expose authorization/callback URLs.
  Do not run URL-returning tab commands after authorization begins.

Verify helper completion and secret-free `--status`, then normal native service
status and authenticated cart read. A product search alone does not prove login.
On timeout or an uncertain result, check the original helper and stored-grant
status before starting another login. Follow the shared
[session and payment guidance](../skill/SKILL.md#store-setup-and-payment-readiness)
and [confirmation policy](../skill/SKILL.md#delivery-checkout-and-email).

## Checkout identity and Oda payment attempts

Checkout rows retain product name, description, brand and quantity separately.
The matcher first uses IDs from the current rendered payable row when its native
product, quantity and displayed fields agree; it does not infer an ID from a
category URL or another cart. Without IDs it compares complete field
presentations with conservative Unicode, whitespace and numeric unit-spacing
normalization. It preserves raw fields and does not apply generic shared-word
stripping.

Manual new-checkout preparation can return indexed unresolved rows and a digest.
The caller may provide `identity_review={"digest": "…", "decisions":
[{"expected_index": 0, "actual_index": 0, "reason": "specific display evidence"}]}`
to the next `checkout prepare`. This authorizes only an explained cosmetic
mapping for the same full checkout/account binding. Proven IDs, automatically
resolved rows, exact quantities and a complete unique assignment remain fixed.
Indistinguishable unresolved variants cannot be assigned by row order. The
accepted review is stored in that confirmation and rechecked before the single
final click; it is not reusable product metadata. Object key order is irrelevant
to this comparison; all values and array order remain significant.

Oda journals a verified hosted Vipps form before Next separately from the Next
dispatch fence and positive request acknowledgement. Source-bound amountless
forms work for new orders, additions and retries. Read-only order/account
inspection preserves the payment tab. Explicit `checkout switch_payment` accepts
Vipps or an existing saved card. For a new order already created at Oda, a
fresh merchant retry review can offer either method even while an earlier
request's local outcome remains unknown. Preparation verifies the same order,
account, goods, delivery and amount; confirmation rechecks that review and
claims one dispatch under the household lock. The old attempt remains in the
journal. Addition payments have their own obligation and retain terminal
evidence requirements. A missing notification, an unpaid order or an expired
API token is not proof of a terminal payment. Legacy attempts are adopted only
when retained native evidence binds the same gateway and independently verified
order; missing evidence remains unresolved.

For an explicit new-order Oda cancellation with an active unresolved card or Vipps payment,
`orders cancel_prepare` returns the exact current checkout confirmation to
`checkout abort_payment`. The abort journals its native cancellation fence before
the click and retains the checkout; an uncertain result can only be observed on
the same attempt. If the retained card tab is gone, an authenticated Oda
inspection tab may read the same known payment ID's native terminal response;
it never starts another cancellation. Positive payment acceptance is reconciled as a purchase.
Positive closure permits a new same-order cancellation review for a new order. An
unresolved Oda addition can proceed to exact original-order cancellation while
its payment remains uncertain. The review binds the original receipt, account,
order change and checkout journals, and uses an inspection tab so the hosted
payment page survives. A changed receipt requires a new review. The final
cancellation click rechecks both journals. Unknown cancellation keeps them for
reconciliation and never repeats the click. Positive cancellation clears the
matching order change and preserves any separate new-menu usage. The review is bound to the
unchanged checkout and the full merchant receipt is read again before the final
click. Only verified merchant cancellation archives both original
and recovery confirmations as cancelled. Refund and authorization release remain
unknown without separate evidence.

Oda retry discount labels are descriptive data, not a promotion grammar. The
parser reads signed itemized amounts from the bound summary, checks full bill
arithmetic and freezes the exact rows for submission. Unknown/malformed money,
changed rows and inconsistent totals still reject submission.
