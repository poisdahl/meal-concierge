# NanoClaw attachment

Meal Concierge runs as an independently owned service on the NanoClaw host.
Its database, configuration, recipe assets, provider authentication and browser
stay outside disposable agent containers. Use the existing installer/runtime
to manage that service. An agent template installs the MCP bridge, the existing
CLI bridge and the canonical Meal Concierge skill. It never starts another
household service.

The native template targets NanoClaw 2.3.0 at
`b76fcb3db0236b36a4d50bed02e89eff472d0e67`. It uses Agent Plugins 1.0
`plugin.json`, `mcp.json` and `skills/`, including NanoClaw's own plugin path
expansion. Python 3.12.12 and the pinned MCP 2.1.1 dependencies must be available
for the container's Linux architecture. Templates do not install Python or
system packages.

Generate a fresh attachment package on the service host:

```sh
python3 clients/nanoclaw.py \
  --output /path/to/new-attachment \
  --python-base /path/to/linux-python-base \
  --site-packages /path/to/locked-venv/lib/python3.12/site-packages \
  --socket-directory /path/to/dedicated-socket-directory
```

The socket directory must contain only the running `service.sock` and its
empty `service.sock.owner.lock`. Configure the service to place its listener
there, separately from state and configuration. Mount the directory so an
existing agent can reconnect after the listener is replaced. A mount of the
socket file itself retains the old inode. Keep this directory dedicated for
the entire attachment lifetime; the packaging check cannot prevent a host
operator from later putting private files there.

Copy the generated `template/` to the NanoClaw installation's
`templates/meal-concierge/`. Native host CLI registration is:

```sh
ncl groups create --template meal-concierge --name 'Meal Concierge' --yes --json
```

Use the exact returned group ID. The group receives the native MCP registration
and group-private skill automatically. Read `attachment.json`: add its three
`allowlistRoots` to the host's existing NanoClaw mount allowlist without replacing
other entries, then run `ncl groups config add-mount --id GROUP_ID --host HOST_PATH
--container CONTAINER_PATH --ro` for each `additionalMounts` entry. Inspect
existing group mounts before adding a conflicting destination. No whole
household, source checkout, Docker socket, provider token or browser endpoint
belongs in these mounts. Restart only the affected group when it is idle.

For a requested recipe cover, use the packaged `bridge/cli.py` beside the MCP
bridge with the same mounted Python runtime, `PYTHONPATH` and
`MEAL_CONCIERGE_SOCKET`. Host code sends the prepared image bytes through stdin
using `recipes/cover_import`; keep the encoded image out of model text. For an
exact managed image read, `recipes/cover_get` requires `--image-output` and a
new explicit local filename. Both routes use the existing household socket.
See the installed skill for the import, credit and confirmation rules.

The service authenticates the configured owner UID and host root. Run the
NanoClaw agent container with that owner UID. Every participant who can use an
attached group shares this authority; this is not individual participant
authentication. Attach only trusted owner groups. An unrelated group should
have neither this template nor these mounts.

After upgrading the shared service, regenerate the attachment from the same
product release and use NanoClaw's native template restamp procedure. Preserve
the household's state, outcome journals and provider ownership. A transport
failure after a mutation is uncertain: reconcile its existing logical key or
operation reference before another attempt. A native scheduled occurrence must
retain its identity across retries. The bridge never creates a new key or
retries an uncertain mutation for the agent.

## Isolated integration test

`tests/test_nanoclaw_integration.py` exercises native template parsing and
stamping, native group mount configuration, configuration materialization,
the actual Docker driver/runner, and the container's real MCP JavaScript SDK
against the production Python bridge, Unix server and Application. Only the
external Mathem responses are synthetic. The service rejects network access;
agent containers use `--network none`.

Prepare a fresh task root, a tracked-source export of the pinned NanoClaw commit
and its locked dependencies, and the locked Meal Concierge test venv. Do not
copy an existing NanoClaw database, groups, `.env`, provider state or credentials.
Put the task Node executable at `$MC08_SCRATCH/bin/node`. Run with a fresh
temporary root containing the source checkout and an explicitly selected agent
image built for the pinned NanoClaw version. For a new source checkout and
image, use the upstream [pinned Dockerfile](https://github.com/nanocoai/nanoclaw/blob/b76fcb3db0236b36a4d50bed02e89eff472d0e67/container/Dockerfile)
with its `container/` build context:

```sh
NANOCLAW_ROOT=/tmp/meal-native-test/nanoclaw
git clone --no-checkout https://github.com/nanocoai/nanoclaw.git "$NANOCLAW_ROOT"
git -C "$NANOCLAW_ROOT" checkout --detach b76fcb3db0236b36a4d50bed02e89eff472d0e67
docker build \
  --build-arg AGENT_RUNNER_LOCK_SHA256="$(sha256sum "$NANOCLAW_ROOT/container/agent-runner/bun.lock" | cut -d ' ' -f1)" \
  -t nanoclaw-agent:meal-test-b76fcb3d "$NANOCLAW_ROOT/container"
```

This builds from the pinned source and runner dependency lock. The upstream
base image and operating-system packages are not pinned to immutable digests,
so a later build need not produce identical image bytes. Install the checkout's
locked host dependencies before running the test:

```sh
MC08_SCRATCH=/tmp/meal-native-test \
NANOCLAW_ROOT=/tmp/meal-native-test/nanoclaw \
NANOCLAW_TEST_IMAGE=nanoclaw-agent:meal-test-b76fcb3d \
NANOCLAW_INSTALL_ID=meal-test-unique \
  /path/to/locked-venv/bin/python -I -B tests/test_nanoclaw_integration.py
```

Choose a unique installation ID of at most 20 lowercase letters, digits or
hyphens; omission generates one. The host process needs the group that owns
`/var/run/docker.sock` (detected from the socket), or equivalent root access.
Use a temporary process group assignment when appropriate; the test does not
change account groups or existing containers/images. All participants of the
test run use the configured owner UID.

The host processes construct their environment explicitly. HOME, caches,
temporary files, groups, databases and helper journals remain in the task root.
Cleanup reconciles recorded container names and immutable IDs against their
installation, role, session labels and launch attempt. Existing installations
and services are outside the test's scope.

The test checks the same MCP transport in the same live container after an
Application restart, persisted setup/profile/recipe state in a new session and
new host process, native skill availability, current tool discovery, an
unattached group, rejection of another UID, and native scheduled task retry
with the same idempotency key. It does not certify model-driven attachment
normalization, the full NanoClaw daemon/adoption path, model OAuth continuity,
provider availability or a real email sender. Verify those separately through
the installation's actual supported model and delivery paths. Configure model
authentication through the host's established provider integration, preserving
one refresh owner; the Meal Concierge template does not install model credentials
or claim to verify their continuity.
