# Standalone runtime and safe updates — technical reference

For installation agents and maintainers. Start with the [user guide](runtime.md).
Dated test results describe the release tested, not a fresh installation today.
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
For Oda/MENY, install `agent-browser@0.33.1` and a non-snap Chromium/Chrome.
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
  --provider mathem --household "My household"
python3 install.py run --home /workspace/meal-concierge/home
```

These are example paths and provider choices; inspect the actual host and use
the user's intended store. Oda/MENY still require their browser dependencies.
The first command performs the declared `uv` staging, verification and migration
subprocesses; it does not start the service or authenticate a store. Do not treat
this entry point as a bypass for platform review of its underlying operations.
Grok-specific command review and acceptance limits are in the [Grok guide](grok.md).

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
it does not mean update. Before an explicit `update` or backup, the owner must
stop that execution and establish that no service survives. External offline
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

The focused native-style local test is
`python tests/test_installer.py --external /explicit/new/scratch-root` with the
pinned test dependencies. It creates a new unauthenticated Mathem fixture,
downloads the real runtime, exercises MCP and interrupted-owner
recovery, then stops its own service. It makes no store or account calls.
This test does not establish Grok's Shell approval or background-cancellation
behavior; those require native verification.

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

Oda additionally needs the dedicated browser profile logged into the same
account, with the intended delivery address and payment method. MCP OAuth does
not authenticate that browser or prove account binding. Existing protected-order
browser review remains the account/address check. Mathem also uses a dedicated
browser for guarded saved-card checkout; its selected MCP address reference
must match that browser account. MENY retains its dedicated browser login.

Mathem core installation keeps browser prerequisites optional. To enable its
checkout browser, pass the tested `--agent-browser` and `--browser-executable`
paths to install, or to an explicit stopped-service update of the same home.
The installer validates the native adapter version and retains the installation's
existing private browser profile/home/socket ownership. Log that profile into
Mathem normally; never copy another browser's cookies or refresh tokens. The
`run-service.sh` launcher also discovers available browser executables; absent
prerequisites leave Mathem core operations and the manual checkout handoff usable.
A configured browser is not evidence of login, account matching or card readiness.

The provider auth tests use actual MCP/mcp-types 2.1.1 with test-only OAuth/MCP
responses against the [MCP authorization contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).
Their acceptance is
**synthetically verified; live not verified**. No production synthetic fallback,
live credential move or live provider certification is implied.

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

When an Oda or Mathem MCP or website change is found, check the corresponding
interface at both providers and consider a shared fix first. Record each
provider's dated source/observation and result, including unchanged or unavailable.
Keep separate identities and provider-specific behavior where evidence requires
it; do not infer matching behavior from shared schemas or automatically deploy
the other provider. See the [current parity evidence](oda-mathem-parity.md).

```sh
./install.sh stop --home /private/household
./install.sh backup --home /private/household --backup /private/backups/manual-copy
# Update the product checkout, then:
./install.sh update --home /private/household
./install.sh start --home /private/household
./install.sh attach --home /private/household
```

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
when restoring to another host. Complete relocated database-plus-assets restoration has been exercised with
managed images, historical recipe references and frozen menus on both native
platforms. This does not restore credentials omitted from the backup.

## Versioned recipe package integration

Installation and code updates do not download or import a recipe collection.
An empty recipe bank is a valid fresh installation; existing recipes are kept.

To add or refresh the optional collection, use current repository code and update
an older runtime first. Stop the existing service through its current owner, then
run this command with the installation's actual home:

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

Repeated imports are idempotent. An unchanged bundled recipe advances to the new
publisher version with a new history revision. Local content edits produce a
conflict; favorites, explicit local status and archived entries are preserved.
Conflicts are reported for explicit resolution. A failed import preserves the
core installation and any already committed recipes; resolve the reported issue
before retrying `import-recipes`. Source links and separate text/image credits
remain available in imported records.

## Verification boundary

The native fixture in `tests/test_installer.py --native ROOT NAME ADAPTER CHROME`
requires fresh scratch paths and a unique native unit name. It exercises install,
interrupted publication/retry, one service owner, actual SDK discovery/setup,
same-client reconnect across restart, full offline update/restore and adoption of
existing configured paths. `--mathem ROOT NAME` checks the core without browser
or Hermes and reports provider login as unavailable. These are isolated tests,
not permission to run against a household installation.

The Linux ARM64 browser proof used extracted Chromium 152 with a task-only
`--no-sandbox` wrapper because the host restricts unprivileged namespaces. It
opened only a synthetic blank page; this does not certify that host's production
browser sandbox or any provider login. Install a supported sandboxed browser for
normal use. Apple Silicon used installed Chrome 152 with its normal sandbox.
The actual MENY browser wrapper and persisted instance/profile paths were tested.

The separate `--compose-split` fixture passed with a UID-0 service limited to
SETUID/SETGID and browser-owned mode-0700 directories; locks are opened under the
configured browser identity before threads start, then the core identity is
restored. `--socket-container` verified that the same owner-UID container reconnects
after host service restart with only the socket directory exposed. Neither test
changes or certifies an existing live Compose installation.

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
