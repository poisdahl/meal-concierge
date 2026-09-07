# Install from your agent

Paste the [README installation prompt](../README.md#installation) into Codex,
Claude Code CLI, or **Claude Desktop → Code → Local**. Let the agent inspect the
host and ask for your store and household. Complete native approvals yourself
when requested. A new conversation or client restart may be needed before tools
appear. The Desktop Chat section is a separate integration.

This guide uses the existing [installer and service](runtime.md) and
[client package builder](../clients/README.md). It does not enable purchases,
outgoing messages or schedules. Store login is separate from core installation.

## Agent procedure

### Identify the host and existing installation

Record the actual client surface/version, execution host, OS and architecture.
The native runtime targets **Linux with working user systemd** and **Apple
Silicon macOS with a GUI launchd domain**. The client’s stdio process must run
as the trusted owner on that same host and reach its Unix socket. Do not infer
Windows, WSL, remote IDE, cloud or container support from a local success.

Before creation, inspect the selected home, `MEAL_CONCIERGE_HOME`, conventional
`~/.local/share/meal-concierge` and `~/.hermes/meal-concierge`, known previous
installation records, and relevant service definitions/active owners. The
installer’s `discover` command checks only selected/conventional homes; an empty
result does not establish that the host is empty. Inspect native marketplace,
plugin and MCP registrations too. Do not dump credentials or whole client
configuration into logs.

Ask for the intended host/home, store (`oda`, `meny` or `mathem`) and household
when they are missing or ambiguous. For a new isolated installation, agree on
a distinct service name and disjoint durable data/code paths. Inspect those
exact names and paths before creating them. Existing data or a different owner
requires a deliberate adoption decision under the runtime guide, not replacement.

### Pin the source and check prerequisites

For a fresh installation, resolve the public repository to one full commit SHA,
retain a checkout at that SHA outside the data directory, and read its matching
instructions. Record the SHA before execution. Do not execute a moving `main`
download, overwrite unrelated checkout changes or use `curl | sh`.

For reuse, retain the installed version. Find its original source record and
compare the installed release’s Python files, `runtime-requirements.txt` and
`skill/` bytes with that checkout. `runtime.json` records the selected release
path, but its UUID is not a Git revision. If provenance cannot be established,
report it and ask before changing code. A setup prompt is not an upgrade request.

The bootstrap needs Python 3.10+ and `uv`; the installer stages its private
Python 3.12.12 and pinned dependencies. Oda/MENY also need the tested
`agent-browser@0.33.1`, its Node runtime and non-snap Chrome/Chromium. Mathem’s
browser is optional for core use. Check the actual native manager and executable
paths before installation. Report a missing prerequisite and the specific
installation step needed; preserve ordinary platform approvals.

### Install once, or attach to the existing core

Use the pinned checkout’s `python3 install.py --help`. For a genuinely new
household, substitute the agreed values in:

```sh
python3 install.py install --home /absolute/data-home \
  --code-root /absolute/program --name UNIQUE_SERVICE_NAME \
  --provider STORE --household "CHOSEN HOUSEHOLD"
python3 install.py start --home /absolute/data-home
python3 install.py attach --home /absolute/data-home
```

Supply verified `--uv`, `--agent-browser` and `--browser-executable` paths when
needed. Long homes can exceed the Unix socket limit: select a short **durable**
`--socket` and, if needed, `--browser-socket-directory` before creating anything.
Do not put retained state in `/tmp`. Native install leaves the core stopped;
`start` starts its persistent unit. Verify that exact unit and service health.

On the same prompt again, inspect `runtime.json`, pending/maintenance markers,
the selected release, service owner and health first. For the correct healthy
household, use `attach`; **do not run `install` or `update` again**. A stopped
service needs its existing native start action, with applicable authorization.
An unhealthy or uncertain service needs diagnosis before another start.

### Register this client

Build from the immutable product checkout matching the installed runtime.
The installed `current` release does not contain `clients/package.py`.
On repeat, reuse a matching existing output and registration; build only when
no matching package exists. Use the builder in that retained checkout and a new
owner-local output path:

```sh
python3 clients/package.py codex --home /absolute/data-home --output /absolute/codex-package
# Or, for either Claude Code surface:
python3 clients/package.py claude-code --home /absolute/data-home --output /absolute/claude-package
```

The builder verifies attachment and copies the installed skill. It never starts
the service. Its generated paths are host-specific; do not publish the package
as a portable plugin. Preserve the output directory for native cache reloads.

Inspect version-specific `plugin --help`, marketplace lists and installed
plugins before registration. The fixed names are marketplace `meal-concierge`,
plugin `meal-concierge@meal-concierge` and MCP server `meal_concierge`.
Compare an existing registration’s source, attached socket, package version
and skill with this household. Reuse a matching enabled registration. Stop on a
foreign or ambiguous collision; never remove it merely to make setup succeed.

For Codex CLI, the existing native registration commands are:

```sh
codex plugin marketplace add /absolute/codex-package
codex plugin add meal-concierge@meal-concierge
```

For Claude Code CLI:

```sh
claude plugin marketplace add /absolute/claude-package
claude plugin install meal-concierge@meal-concierge
```

In **Claude Desktop’s Code section**, select a local folder on the service host.
Use that Code environment’s native plugin controls/commands to add the generated
marketplace and activate the plugin. Inspect its actual embedded version and
scope: a terminal CLI install is not proof the Desktop engine loaded it. Follow
its normal reload/new-session instructions, then verify MCP and skill in the
Code conversation itself. `--plugin-dir` or successful package generation alone
does not establish persistent registration. Never substitute Desktop Chat.

Keep normal tool permissions on every surface. If activation is denied, stop
that operation and report the denial. Do not rewrite it or switch tools to bypass
review. If a new conversation is necessary, give the user the exact next step.

### Verify, then report separately

In the actual native conversation, load the packaged Meal Concierge skill and
use its discovered MCP tools to show status/setup, verify the selected household
and provider, and read stored recipes. Check the installed pack’s reported count,
version and managed assets; distinguish a metadata reference from a readable
image. Keep existing household settings unless the user chooses a change.

Report these independently: source SHA; core installation; native persistent
service; this client’s registration/MCP; loaded skill; recipes/assets; store
authentication; and exact manual steps or errors. Missing store login or an
optional pack failure must not hide a working core. Do not imply login grants
purchase, delivery or scheduler authority.

For lifecycle acceptance, repeat the same README prompt and verify unchanged
household/data, release, unit and registration. Attach another client to the
**same** service and read the same saved state without creating another credential
owner. Verify a new conversation and a real client exit/relaunch separately from
a controlled restart of the exact task-owned native service. Check for active
work first; restarting someone else’s service or client is not a setup side effect.
Read back the same recipe revisions/assets and verify MCP/skill after each step.

### Incomplete setup and recovery

Record the last confirmed step and exact paths/version before retrying. A timeout
is not proof that the child process or installer stopped. Reconcile the original
process, native unit, runtime/pending/maintenance markers and registration first.
Never remove ownership, browser, OAuth or installer locks to make progress.

If the same source’s initial publication was interrupted and left
`pending-install.json` or `maintenance.json`, follow the runtime guide’s stopped
`update --home` recovery using the original paths and preserved data. This is
explicit recovery of that attempt, not permission to fetch newer code. If only
staging/owner files exist, inspect that original attempt before choosing a retry.

A recipe-pack error may occur **after** the core was published successfully.
Inspect the markers and core before classifying installation as failed. Preserve
committed recipes/conflicts and report the pack’s partial result; a later repair
uses the controlled stopped update path. Do not blindly reinstall or reset data.
Native refusal remains a refusal, distinct from a missing dependency or crashed
process. Resume only after the underlying cause or native approval is resolved.

## Acceptance boundary

The [existing client evidence](../clients/README.md#client-contract-and-checks)
establishes component workflows with the exact limitations recorded there.
It does not establish the full fresh repo-URL prompt lifecycle for all three
surfaces. Record actual client versions, source revisions, approvals and remaining
bounds for each new trial; never infer Desktop Code success from CLI success.
