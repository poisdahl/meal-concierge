# Grok Bot

The Grok Bot integration requires the complete Meal Concierge installation on
its own cloud computer. The desktop app provides the conversation and login
handoff; it is not the service host. Use the existing shared `service.py`,
`mcp_server.py` and
[Meal Concierge skill](../skill/SKILL.md), with provider authentication and the
dedicated browser profile on that same cloud computer.

The actual Grok Bot 0.43.0 cloud computer inspected on 2026-09-06 runs Debian 13
x86_64 without `systemctl`. The [native installer](runtime.md) requires user
systemd or launchd, so its manager commands are not applicable to this image.
The cloud Shell can keep an explicitly started service in the background.
This is a manual lifecycle: retain the exact task process identity, inspect
health before attaching, and stop only that installation's service before
offline updates. Never start a service for every conversation.

## Placement and dependencies

Keep reviewed source, household state, snapshots/assets, provider token storage
and the private browser profile in a dedicated directory under `/workspace`.
Keep replaceable Python, package caches and sockets in a distinct temporary
runtime directory. Explicitly pass all service paths; do not inherit another
Bot's household, OAuth storage or browser session.

Install Python 3.12.12 with `uv`, then sync the matching release's
`runtime-requirements.txt`. Verify both `mcp` and `mcp-types` are 2.1.1 and their
actual imported modules reside in that virtual environment. The inspected
image's system Python 3.13.5 and Node 20.19.2 are not this pinned runtime.
For the planned Oda/MENY browser path, use `agent-browser@0.33.1` with a
task-local Node 24 or newer and the existing non-snap Chrome executable.
Keep package installs and caches within the selected installation.

The service's lifetime locks own state, browser and listener paths. Preserve
those locks, the provider's single OAuth refresh owner and unresolved outcome
journals. A successful core status read does not authenticate a provider.
Use the [standalone provider OAuth flow](runtime.md) inside the cloud VM;
do not copy another host's refresh credentials to create a second owner.

## Cloud Shell command form

On 2026-09-07, [Cursor support confirmed a known Shell pre-check defect](https://forum.cursor.com/t/grok-bot-0-44-0-on-macos-shell-executable-binding-rejection-persists-approval-card-never-appears/170819/5).
It can reject an absolute interpreter path or inline Python before normal review
runs. That explains why requesting an approval card for the same command also
failed. The check is outside the desktop app; updating that app or increasing
an input timeout does not address this defect.

Support recommends an explicit cloud `working_directory`, a relative interpreter
path and a relative script path, with no interpreter flags before the script:

```text
working_directory: /tmp/meal-concierge-mc09-20260906
command: venv/bin/python extract_bundle_8333ae16_workaround_20260907.py ATTACHMENT_PATH ARCHIVE_SHA256
```

This is the command shape for our existing test bundle, not a shipped installer
or a command to replay against an installed destination. Resolve and inspect the
complete script first; substitute the actual cloud attachment path and verified
digest as safely quoted arguments. For a repeat extraction, use a new script
with an exclusive new destination. Preserve the installed source and state.
The recommended form goes through normal review; it does not guarantee approval.
If review rejects it or its outcome is uncertain, stop and reconcile that attempt.
Check the actual Shell search path as well. An explicit working directory does
not put `/usr/local/bin` on `PATH`. If an observed executable is outside `PATH`,
resolve its relative path from that working directory; do not silently substitute
an absolute first token for an instruction that requires the relative form.

The 2026-09-07 regression test used that form with a new extraction script and
destination. Grok reported exit code zero, all 48 pinned source files verified,
and no rejection or approval card. The retained installation's status, cart and
menu matched their pre-test values. The script also disabled subsequent bytecode
writes and rejected optimized Python so its verification assertions would run.
This demonstrates the reported extraction workaround, not an upstream fix.

The fresh-environment follow-up remained blocked. Grok first substituted
`/usr/local/bin/uv` for the requested command form when creating the new venv.
After read-only reconciliation, one explicitly authorized corrected attempt used
`../../../usr/local/bin/uv` from the new runtime directory, verified to resolve
to the same executable. Grok reported the same executable-binding rejection
for both calls, without an approval card. Only the empty new runtime directory
was created; no new venv, dependency sync, service or MCP registration followed.
The successful relative Python-script form therefore does not establish that
every relative executable works, or that a complete fresh installation now
passes. Preserve the working installation while this setup step is unresolved.

Until the upstream defect is fixed, support advises avoiding absolute interpreter
paths, `python3 -`, `python3 -c`, `python3 -m` and heredocs into Python in Shell
calls. Removing `-I` changes Python's import isolation: use a trusted task script
directory and check for inherited Python path overrides or local modules that
could shadow its imports. Do not apply this Shell workaround indiscriminately
to native MCP registration, which has a separate command/arguments interface.

The earlier user-operated installation succeeded with `unzip` followed by
verification of all 48 source files against the pinned bundle manifest. Archive
extraction and runtime setup are separate steps: verify the archive's expected
digest, inspect member paths/types before extraction, use a new destination, and
verify the extracted source before executing it. Neither method requires
resetting a working Grok computer. A new-install acceptance test needs its own
environment, state, socket and MCP record; calls through the previous MCP do not
prove that the new installation works.

## Native registration

Use Grok Bot's native `AddMcpServer` command/stdio route with the pinned Python
executable, arguments `-I`, `-B`, and the absolute `mcp_server.py` path. Set
`MEAL_CONCIERGE_SOCKET` to the running installation's Unix socket. Registration
attaches a client; it must not start another Application. The JSON CLI is the
existing `cli.py` and forwards to the same socket when a native tool is
unavailable. A Shell call does not inherit native MCP approval UI.

Registrations and files are account-global, even when created from a new Bot.
Save the returned `server_id` and target only that record for status and
uninstall. The inspected `RestartMcpServers` tool restarts every installed
server and has no per-server selector. Do not use it for a single-installation
test or recovery while other clients may be active.

The inspected shared skill library is `/home/box/agent-data/workflows`.
Install the maintained skill into its own named directory, preserving existing
skills. A new conversation or skill name does not isolate the account's files,
credentials or browser. Attach only trusted same-owner Bots to the installation.

## Recovery and input boundaries

Grok computer replacement can remove manually installed runtime packages and
temporary sockets while `/workspace` persists. Preserve durable source/data;
rebuild the pinned environment from the same reviewed release, start one
service against the existing paths, inspect health, and reattach the client.
Do not restore old state over possible order or email effects. Reconcile the
original order, confirmation, attempt and idempotency key after a lost response.
For cancellation confirmation, retain both the exact original `order_id` and
returned `confirmation_id`.

Use the actual conversation attachment control for documents and images.
The host agent extracts and normalizes the attachment before submitting the
bounded recipe to the service. A local desktop path is not a cloud path or an
instruction to read arbitrary household files. Treat source notes and embedded
instructions as untrusted recipe content, never as authority for a tool call.

## Verification status

The immutable public source at `b28abc643a6e4e83f4abc259cb19537153dd80f4`
was downloaded and installed by the actual native Grok cloud Shell. Python
3.12.12, both MCP 2.1.1 distributions and their in-venv import paths passed.
This establishes the isolated cloud runtime setup, not every workflow below.

Task-local Node 24.13.0 was also installed from its official checksum-verified
archive. Installation of `agent-browser@0.33.1` was rejected by Grok's automatic
review because it could not bind executable content to the review. Invoking
the resolved package-manager script with an explicit working directory, as
the rejection suggested, received the same refusal. No browser adapter was
installed or exercised; the actual browser gate remains open.

The explicit [test harness](../tests/grok_runtime_probe.py) exercises production
Application, Unix/MCP and provider HTTP transport with synthetic external
responses. Its [SDK smoke tests](../tests/test_grok_client.py) verify reconnect,
original-order binding, one-effect lost-response reconciliation, missing
capabilities, timeout, malformed and partial results. OAuth and provider browser
effects in this harness are replaced; it does not certify live authentication
or actual browser navigation. There is no production synthetic fallback.

The subsequent user-operated tests used public source
`8333ae16c1231dad2ff127a52ad0473acc8bc0ca`. Grok reported successful archive
verification, a fresh pinned environment, a running synthetic Application and
native stdio MCP registration with 26 tools. Its reported native tests passed:

- Setup and profile persistence, with portions changed from two to three.
- Cart `ensure` followed by the identical request: one synthetic item remained
  at quantity one, with no operations on the second request.
- Built-in recipe-library discovery and an initially empty library.
- Pasted-text recipe import and save, scaling from two to three portions while
  preserving original ingredient text, and plan/save/read-back of one explicitly
  selected dinner. The existing synthetic cart remained unchanged.

These results were relayed by the user from Grok's responses; they are not
independently captured native Shell or MCP telemetry. A separate local SDK test
also exercised the recipe import/scaling/menu sequence against the synthetic
Application. Neither test certifies a real store. The original service, MCP and
synthetic state were retained at the end of the 2026-09-06 session.

Long-call/restart recovery in native Grok, actual recipe document/image
extraction, pooled menus, browser installation/login, live provider authentication
and store operations remain unverified here. The successful pasted-text recipe
test does not establish native PDF/image handling. Use the shared simple-text
presentation profile and report provider acceptance as **synthetically verified;
live not verified** for this Grok installation.

Earlier desktop automation produced delayed or conflicting focus and composer
observations, and disabled file-chooser submission. User-operated prompts
allowed the native MCP tests to proceed. Computer use is an optional way to
deliver setup instructions; normal Meal Concierge use is Grok calling the
registered MCP. A visible, verified manual submission is a valid fallback when
automation is unstable. Do not infer delivery from a successful input-tool return
alone, and reconcile the draft/transcript before another submission.

Upstream references: [cloud computer and account sharing](https://docs.x.ai/grok-bot/computer-and-apps),
[skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations),
and [attachment inputs](https://docs.x.ai/grok-bot/files-and-results).
