# Codex and Claude Code

These local plugins connect to an already running [standalone service](../docs/runtime.md).
Each plugin contains a native manifest, MCP attachment configuration and a copy
of that installation's shared skill. Client exit, plugin removal or a new chat
does not stop the service. Household state, provider credentials and browser
profiles stay in the service installation, outside the plugin cache.

Build from the product checkout after installing and starting the service:

```sh
python3 clients/package.py codex --home "$HOME/.local/share/meal-concierge" \
  --output "$HOME/.local/share/meal-concierge-clients/codex"
python3 clients/package.py claude-code --home "$HOME/.local/share/meal-concierge" \
  --output "$HOME/.local/share/meal-concierge-clients/claude-code"
```

Use a new output directory. The builder calls the installer's `attach` command,
which checks the running service contract. It fails if the service is unavailable
or incompatible. It never installs, starts, restarts or changes the service.
For an existing household, pass its actual installation home. Do not create a
second installation just to add another client.

Install the generated Codex plugin:

```sh
codex plugin marketplace add "$HOME/.local/share/meal-concierge-clients/codex"
codex plugin add meal-concierge@meal-concierge
```

For a Claude Code session without saved plugin registration:

```sh
claude --plugin-dir "$HOME/.local/share/meal-concierge-clients/claude-code/plugins/meal-concierge"
```

Or install it through Claude Code's native marketplace commands:

```sh
claude plugin marketplace add "$HOME/.local/share/meal-concierge-clients/claude-code"
claude plugin install meal-concierge@meal-concierge
```

Start a new conversation and ask to show Meal Concierge setup. Keep the client's
normal tool permissions. The packages do not auto-approve tools or change the
household's confirmation policy. A client permission denial does not authorize
using another tool to perform the same action. An uncertain cart or checkout
response must be reconciled through the existing service workflow before any
new attempt. Mathem payment remains a manual website handoff.

The attachment uses the installation's stable `current` paths. Rebuild packages
from the updated running release into a new output directory. Attachment or skill
changes produce a different content-based plugin version. To replace an installed
marketplace, run `codex plugin marketplace remove meal-concierge` or
`claude plugin marketplace remove meal-concierge`, then repeat that client's
marketplace-add and plugin-install commands above with the new output path.
For Claude's session-only `--plugin-dir`, pass the new plugin path instead.
Native reinstall and changed cached skill content were checked on both clients.
Existing conversations can retain old instructions until restarted. Use
the service's explicit update procedure; replacing a plugin is not a service
upgrade or a data rollback. Generated configuration contains local installation
paths and belongs to its owner/host. Distribute the builder, not a household's
generated attachment.

## PDF attachments

The normal standalone installation includes a private PDF renderer. When the
client cannot read a PDF natively, the packaged skill renders its pages locally
and reads the resulting images. No manual Poppler/Homebrew installation or
global PATH change is required. The client still needs native image reading
and permission to run the helper on the attached file. See the
[PDF import limits and page-batch workflow](../docs/recipe-import.md#pdf-attachments-without-system-packages).

For installations predating this helper, update the runtime normally and
rebuild/reload the client package. Installing a new plugin against an old
runtime does not install its missing dependencies. The helper is included in
both generated package formats and participates in their cache version.

The fallback was verified in a fresh private runtime on macOS with Claude
Desktop 1.46388.4 / embedded Claude Code 2.1.260: a three-page text PDF hit the
native missing-`pdftoppm` error, then the packaged helper and native image reads
completed its import/save. A twelve-page scanned PDF followed the same helper
path and preserved all twelve source pages in a separate draft. Exact source
quantities, unknown measures, profile/cart state and non-favorite status were
checked. Both generated launchers also passed local tests with only the existing
bootstrap Python on PATH. Other hosts' native PDF workflows remain unverified.

## Client contract and checks

The package schema targets Codex CLI **0.153.4** and Claude Code **2.1.241**.
Codex's `tool_timeout_sec` is 700; Claude's per-server `timeout` is 700000 ms.
These leave time for the production bridge's maximum 660-second RPC wait.
Timeouts bound the client wait; they do not prove an external operation stopped.

The [OpenAI plugin specification](https://developers.openai.com/plugins/build/plugins)
defines the manifest and local marketplace layout. The [Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp)
defines stdio attachment and native tool permissions/timeouts. The [Claude Code plugin reference](https://code.claude.com/docs/en/plugins-reference)
defines its package and session loading. The tested Claude binary's stdio schema
also accepts the per-server millisecond `timeout` field.

Run the package/service checks in the [pinned standalone Python environment](../runtime-requirements.txt):

```sh
python -I tests/test_client_packages.py -q
```

These exercise both generated MCP commands through the real SDK, bridge and
Application, with synthetic provider responses. They cover shared state between
two clients, bridge teardown, service restart, owner collision, unavailable
attachment and preservation of an existing output directory. They do not use
provider credentials or native service managers.

The separate `client_package_probe.py` exercises installed authenticated native
clients in an explicit fresh scratch directory. Its test-only provider rejects
external networking. The Codex run demonstrated native permission denial before
service dispatch, reconnection within the same conversation after a service
restart, a 645-second synthetic provider read, and recovery after losing a cart
write response with exactly one dispatch. These are subprocess-service checks;
native service-manager installation remains a separate runtime check.
The native Claude Code run also passed status/setup calls, profile updates,
client permission denial before service dispatch, and persisted state after a
service restart. These checks used the authenticated CLI and generated plugin; the bounded
Desktop result is recorded separately below. Model-client acceptance must be
recorded independently of SDK checks or login status.

On 2026-09-07, the isolated native Claude Code workflow also loaded the complete
packaged skill, read original text, a photo and all three PDF pages, and returned
schema-2 import previews. Text and PDF recipes were explicitly saved to builtin;
the photo recipe was saved as a draft after attaching its cover. An automatic
menu selected and saved seven distinct dated dinners for two without a
client-supplied candidate list. A separate native save denial caused no service
save dispatch. Unknown quantities and unrelated favorites/preferences/cart state
were preserved.

Cover bytes crossed the host-file boundary through the existing CLI's stdin;
recipe saves and image reads used native MCP. ImageContent responses before and
after saving matched the managed JPEG and included separate image credits. Fresh
CLI conversations after service restarts retrieved the same recipe revisions,
menu and image bytes. These are synthetic-provider subprocess-service results,
not desktop rendering, native service-manager or scheduler acceptance.

The photo/PDF runs first encountered a test permission mismatch between `/tmp`
and its `/private/tmp` alias; same-file Read retries passed after correcting the
exact path allowance. The final photo read recovered from two nonexistent tool
names before using the discovered tools. Those failures remain recorded. Some
model narration misstated cooking-time, readable-note or source-yield details;
the actual recipe fields remained correct. Flawless presentation is not claimed.
A subsequent bounded Claude Desktop 1.46388.4 test with embedded Claude Code
2.1.260 observed the native packaged skill, MCP status and setup
show/keep_current. Original PNG, TXT and three-page PDF files were attached
through the native chooser, produced three separate import previews and were
explicitly saved as three non-favorite drafts. Source amounts, four steps and
an unknown tomato unit were preserved; a hostile source instruction had no
observed effects. Profile and cart state stayed unchanged, with synthetic
provider reads.

Native recipe_image calls for the preview and saved recipe returned the same
managed JPEG. Opening View screenshot on the saved tool result displayed the
actual image in Desktop. Cover bytes entered through one host CLI stdin
transfer; this does not establish native MCP byte upload.

The project MCP and skill required archive/unarchive of the exact test
conversation to restart its engine; /reload-plugins alone was insufficient.
The PDF pages=1-3 read initially failed because pdftoppm was absent, then native
Read of the complete original PDF succeeded with all three pages. TXT had a
native attachment and exact original transcript, but no separately observed
Read tool call.

This Desktop result does not establish full menu/lifecycle acceptance,
service-restart persistence or native service-manager/scheduler behavior.
Codex's later attachment/preparation attempts timed out before model events;
its full menu/attachment workflow remains unverified. These packages do not
implement a second importer, normalizer, scheduler, sender or credential owner.
