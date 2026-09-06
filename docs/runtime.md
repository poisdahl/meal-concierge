# Standalone runtime and safe updates

The runtime runs independently of agent conversations on Linux/user-systemd and
Apple Silicon macOS/launchd. Installation, service lifecycle and agent attachment
are separate commands. The installer never registers an agent, logs in, transfers
schedulers, sends messages or performs grocery actions.

## Install and attach

Use Python 3.10+ for the installer and install `uv` on PATH. The installed runtime
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
lifecycle. Platform-specific packages and complete real-client workflows remain
separate integration work. Additional clients share the same service.

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
or claim live parity. MENY login and its private `vipps_phone_number`
configuration require the authorized provider setup.

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
browser review remains the account/address check. Mathem retains its manual
website checkout; MENY retains its dedicated browser login.

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

The installer stages and verifies a release-pinned archive before taking the
offline installation locks, then imports it into the built-in recipe bank using
the shared bounded archive codec. It verifies the descriptor's hash, size and
format; `--recipe-pack PATH` accepts only a local artifact matching that descriptor
and only during `install` or `update`. Archive data does not pass through RPC.

This runtime pins [recipe pack 2026-09-06.5](https://github.com/poisdahl/meal-concierge/releases/tag/recipes-2026-09-06.5).
It contains 4,599 English recipes: 3,807 from Wikibooks and 792 from TheMealDB,
with 1,570 compressed JPEGs used by 1,580 recipes. Twenty additional Wikibooks
pages could not be parsed and are excluded. One TheMealDB placeholder without an
actionable source method is also excluded. Existing saved copies are preserved.

All included recipes have quantified ingredients and person-serving values.
Publisher estimates remain labelled as estimates, with assumptions available;
they do not represent personal user acceptance or nutritional validation.
Recipes with source omissions include explicit editorial adaptations.
Source links, revision information where available, and separate
text and image credits are included. TheMealDB content uses attribution-based
redistribution; its supplied upstream recipe links are retained.

Use the latest repository code when installing or updating. The installer
verifies the published digest and format for both the default HTTPS download
and a local `--recipe-pack` file. Repeated imports are idempotent. An unchanged bundled recipe advances to the new
publisher version with a new history revision. Local content edits produce a
conflict; favorites, explicit local status and archived entries are preserved. Conflicts are
reported for explicit resolution. A pack download failure leaves the core
runtime usable and reports that the recipe collection needs attention.

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
