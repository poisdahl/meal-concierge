# NanoClaw

Connect an existing NanoClaw installation to Meal Concierge on the host.
The household service keeps its data, store login and browser outside disposable
agent containers. NanoClaw receives a connection and the shared skill.

## Install with an AI agent

Send this to Codex, Claude Code or another agent with access to your NanoClaw host:

> Install Meal Concierge from https://github.com/poisdahl/meal-concierge for my
> existing NanoClaw installation. Follow `docs/nanoclaw.md`. Inspect the actual
> host and existing installations first. Reuse my existing household and installed
> version if present; preserve its data, settings and connections. For a new
> installation, use the latest `main`, resolve it to a full commit SHA and install
> from that checkout. Ask for my store, household and trusted NanoClaw group if
> needed. Set up the persistent host service, native template, required read-only
> mounts and shared skill. Do not import the optional local recipe collection
> unless I request it separately. Verify the connection, household, loaded skill
> and available recipe sources. A new local bank may be empty; online recipes do
> not require the optional collection. Keep normal platform approvals and tell
> me which login or activation steps I must complete.

## Requirements

- A working NanoClaw host and container runtime. The template targets NanoClaw
  **2.3.0**, commit `b76fcb3db0236b36a4d50bed02e89eff472d0e67`, using Agent Plugins
  1.0. Check compatibility before using another version.
- A supported [host service and prerequisites](runtime.md#install-and-attach).
- Linux Python **3.12.12** and the [pinned dependencies](../runtime-requirements.txt)
  for the container's architecture. The template does not install them; a macOS
  host's Python binary cannot serve as the container's Linux runtime.
- Containers running with the household service owner's numeric UID. Every
  participant in an attached group shares that authority; use a trusted group.

## Manual setup

### 1. Install or reuse the household service

Follow [Install and attach](runtime.md#install-and-attach). Inspect existing
installations first; connect to the intended household without installing it
again. Keep the checkout and full source commit recorded for future updates.

The service needs a **dedicated socket directory**, containing only
`service.sock` and its empty `service.sock.owner.lock`. Configure this separately
from household state, credentials and browser files. Mount the directory, not
just the socket file, so a connection can survive service restarts. Keep this
directory dedicated for the whole lifetime of the attachment.

### 2. Generate and register the template

From the retained product checkout, use a new output directory:

```sh
python3 clients/nanoclaw.py \
  --output /absolute/new-attachment \
  --python-base /absolute/linux-python-base \
  --site-packages /absolute/locked-venv/lib/python3.12/site-packages \
  --socket-directory /absolute/dedicated-socket-directory
```

Copy the generated `template/` into the existing NanoClaw installation's
`templates/meal-concierge/`. For a new trusted group:

```sh
ncl groups create --template meal-concierge --name 'Meal Concierge' --yes --json
```

Keep the returned group ID. For an existing group, inspect its configuration and
use the installed version's template/restamp procedure; do not create duplicate
groups on repeated setup.

### 3. Add the required mounts

Read the generated `attachment.json`. Append its three `allowlistRoots` to the
host's existing NanoClaw mount allowlist. For each `additionalMounts` entry,
substitute the returned paths in:

```sh
ncl groups config add-mount --id GROUP_ID \
  --host HOST_PATH --container CONTAINER_PATH --ro
```

Preserve other entries and inspect existing destination mounts for conflicts.
Do not mount the whole household, provider tokens, browser endpoint, source
checkout or Docker socket. Restart only the affected group when idle. The native
template supplies the MCP registration and group-private skill; it must not
start another household service.

## Check and first use

In the attached group's actual conversation, ask NanoClaw to load the Meal
Concierge skill and show setup, the selected household/store, store connection
and available recipe sources. Verify that an unrelated group has no attachment.
Complete [store login](runtime.md#provider-oauth) on the host as needed, then
verify an authenticated cart read.

An empty new local bank is normal. Recipes from your selected, connected store
are available without the optional collection. See
[first use](usage.md) and [adding recipes](recipe-import.md). Installation does
not enable orders, outgoing messages or schedules.

Use the installed skill for text and photo input. PDF input needs a working
native reader in the NanoClaw container: the current template does not include
the standalone PDF fallback helper. If the native reader cannot read the PDF,
report that limit and use another supported client or provide the pages as
images. For cover transfer, the
package includes `bridge/cli.py` beside the MCP bridge; it uses the same mounted
Python runtime, `PYTHONPATH` and `MEAL_CONCIERGE_SOCKET`. `recipes/cover_import`
accepts prepared image bytes through stdin; `recipes/cover_get` requires
`--image-output` with a new explicit filename. Keep encoded image data out of
model text. Attachment display and delivery depend on the group's native chat
adapter; a filename in chat is not a delivered image.

## Updates and help

Update the host service through the [runtime procedure](runtime.md#updates-failures-and-recovery).
Regenerate the attachment from the matching product release, apply NanoClaw's
native template restamp procedure and recheck tools and skill in the group.
Preserve household data, store ownership and existing operation records.

Code updates preserve recipes, including collections imported by older
versions. Update the
code before separately requesting
[the latest optional collection](runtime.md#versioned-recipe-package-integration).

After an uncertain cart change, checkout or send, check the original operation
before retrying. Scheduled retries must keep the same occurrence identity.
Configure model authentication through NanoClaw's established provider setup;
Meal Concierge's template does not install or maintain model credentials.

<a id="isolated-integration-test"></a>
<a id="native-model-acceptance"></a>

## Verification boundary

Developer instructions and results are in
[installation evidence](installation-evidence.md#nanoclaw-integration-and-native-model-checks)
and the [platform matrix](platform-acceptance.md). They record the tested
container, chat adapter and delivery limits, without establishing live retailer
or production email behavior.
