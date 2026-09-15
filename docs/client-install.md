# Codex and Claude Code

Use the same setup in a desktop app or terminal. Install Meal Concierge once
on the computer running your agent, then connect the clients you want to use.
They share the same recipes, settings and store connection. The household
service keeps running when you close a conversation.

## Install with an AI agent

Paste this into your agent:

> Set up Meal Concierge from https://github.com/poisdahl/meal-concierge
> for my existing [Codex / ChatGPT Work Local / Claude Code] environment.
> Follow docs/client-install.md.
> Connect to my existing household if present; otherwise install the latest
> version. Ask which host, store and household to use as needed.
> Preserve my data and settings. Verify the service, tools, skill and store
> connection, and help me complete login and activation.

This connects to the existing household when one is already installed.
For a program update, use the [update prompt](../README.md#update-meal-concierge).

## Choose your client

- **Codex:** use a local Codex session in the ChatGPT desktop app, or Codex CLI.
  Both use the `codex` package below.
- **Claude Code:** use **Code → Local** in Claude Desktop, or Claude Code CLI.
  Both use the `claude-code` package below.
- **ChatGPT Work Local:** use the same `codex` package and installation steps.
  Before relying on it, complete [Check and first use](#check-and-first-use) in
  the Work conversation; its local tools, skill and file access must be available.

The package connects to a service on the same computer and under the same user.
Regular ChatGPT Chat, Work Cloud and Claude Desktop Chat are not covered by
this plugin setup. They need their own connection and skill setup; installing
this local plugin does not provide those automatically.

See the platform guides for [ChatGPT local plugins](https://developers.openai.com/plugins/build/plugins#build-your-own-curated-plugin-list)
and [Claude Code shared configuration](https://code.claude.com/docs/en/desktop#shared-configuration).

## Requirements

- An installed, working client on Linux with user systemd or Apple Silicon macOS
  with a logged-in desktop user. Other host combinations need separate verification.
- Python 3.10+, `uv`, and the selected store's
  [runtime prerequisites](runtime.md#install-and-attach).
- Oda and Mathem use the same required, locally validated browser executables;
  their store authorization and browser login happen only after installation.
- Permission to register a local plugin and run its tools. The client connection
  must run as the household service owner on the same computer.

## Manual setup

### 1. Install or reuse the household service

Follow [Install and attach](runtime.md#install-and-attach), using a retained
checkout of the latest `main` resolved to a full commit for a new installation.
Keep source and household data in separate directories. Record the source commit.

Before creating anything, inspect the chosen home, `MEAL_CONCIERGE_HOME`,
`~/.local/share/meal-concierge`, `~/.hermes/meal-concierge` and known existing
service/plugin registrations. `install.sh discover` checks conventional homes,
not every installation on the computer. Reuse the intended healthy service with
`attach`; repeated setup is not an update. Resolve a different or uncertain
household registration before changing it.

Installation leaves the service stopped. Start it through its existing service
manager, then run `attach`. Keep the installer running until it reports completion.
For interrupted setup, inspect the original attempt before retrying; see
[recovery](runtime.md#updates-failures-and-recovery).

### 2. Build the client plugin

Run the builder from the retained product checkout matching the installed
runtime; the installed `current` directory does not contain the builder.
Replace the example paths with your actual home and a **new** output directory:

```sh
python3 clients/package.py codex --home /absolute/data-home \
  --output /absolute/codex-package
```

For either Claude Code surface, use:

```sh
python3 clients/package.py claude-code --home /absolute/data-home \
  --output /absolute/claude-package
```

The builder checks the running service and packages its connection and shared
skill. It does not start or upgrade the service. Reuse a matching package on
repeat setup. Keep generated packages on this computer: they contain local paths
and are not portable downloads.

### 3. Register and load the plugin

Register the generated marketplace and enable `meal-concierge` in your chosen
client. Reuse an existing registration for the same household and preserve
unrelated plugins. Use either desktop controls or the corresponding CLI:

| Client family | Add the marketplace | Install the plugin |
|---|---|---|
| Codex | `codex plugin marketplace add /absolute/codex-package` | `codex plugin add meal-concierge@meal-concierge` |
| Claude Code | `claude plugin marketplace add /absolute/claude-package` | `claude plugin install meal-concierge@meal-concierge` |

These commands can be run by your installation agent. Check `plugin --help`
for the installed client version. You do not need to install a separate CLI
just to use a desktop app:

- **ChatGPT desktop:** open the generated Codex marketplace directory as a local
  project and restart the app. In **Plugins**, choose the Meal Concierge
  marketplace and install its plugin. For access from other projects, your
  installation agent can register the same source using the CLI above or a
  [personal marketplace](https://developers.openai.com/plugins/build/plugins#install-a-local-plugin-manually).
- **Claude Desktop:** in **Code → Local**, use **+ → Plugins → Add plugin** to install from
  the configured marketplace. Your installation agent can register the source
  using the CLI above or the client's
  [marketplace configuration](https://code.claude.com/docs/en/plugin-marketplaces).

Follow the client's reload instructions and start a new conversation in the
mode you intend to use. Verify both tools and skill with the steps below.
Switching between desktop and terminal does not require another service or
another package for the same client family and household.

For a single Claude Code terminal session, an alternative is:

```sh
claude --plugin-dir /absolute/claude-package/plugins/meal-concierge
```

## Check and first use

In a new conversation, ask:

> Load the Meal Concierge skill and show my setup, store connection and available
> recipe sources. Keep my existing settings.

Verify the actual native tools and skill in this conversation, not just the
presence of files. Missing store login does not mean the core installation
failed. Follow [store login](runtime.md#provider-oauth), then verify an
authenticated cart read; product search alone is not a login check.

A new local recipe bank may be empty. Recipes from your selected, connected
store are available without the optional collection.
You can also add your own recipes or connect an optional library. See
[first use](usage.md) and [recipe import](recipe-import.md).

PDF reading uses the client's native reader or the bundled local renderer;
no separate Poppler or Homebrew installation is normally needed. The client
still needs image reading and permission to run the helper. See
[PDF input](recipe-import.md#pdf-attachments-without-system-packages).

[Email connection](recipe-delivery.md#email-connection-setup) is optional and
separate. Reuse an existing sender where available; installation does not enable
orders, outgoing messages or schedules.

## Updates and help

Ask your agent to update the **existing** installation using the
[runtime update procedure](runtime.md#updates-failures-and-recovery). Code
prerequisites are checked before the working service is stopped. Program updates
preserve recipes, including collections imported by older versions. To refresh that
collection, update the code first, then separately ask to
[import the latest collection](runtime.md#versioned-recipe-package-integration).

After a runtime update, rebuild the plugin from the matching checkout into a new
output directory. Update the registered marketplace source to that new directory
in the same client environment, then reload the plugin. Desktop users can follow
the same registration steps as during installation. For CLI registration, verify
the existing marketplace belongs to this household, remove that marketplace with
`codex plugin marketplace remove meal-concierge` or
`claude plugin marketplace remove meal-concierge`, then repeat the relevant
registration commands with the new output path. For `--plugin-dir`, use the new
path. Start a new conversation and check both MCP tools and the loaded skill.

If a tool times out after a cart change, checkout or send, ask the agent to check
the original operation before retrying. Reinstalling or restoring old data can
lose the information needed to establish what happened. Report the source
commit, client version, last confirmed step and exact error without sharing
credentials.
