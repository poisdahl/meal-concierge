# Codex and Claude Code

Use Codex CLI, Claude Code CLI, or **Claude Desktop → Code → Local** on the
computer that will run Meal Concierge. Claude Desktop Chat is a different
integration. The household service keeps running when you close a conversation.

## Install with an AI agent

Paste this into your agent:

> Set up Meal Concierge from https://github.com/poisdahl/meal-concierge
> for my existing Codex or Claude Code installation. Follow docs/client-install.md.
> Connect to my existing household if present; otherwise install the latest
> version. Ask which host, store and household to use as needed.
> Preserve my data and settings. Verify the service, tools, skill and store
> connection, and help me complete login and activation.

This connects to the existing household when one is already installed.
For a program update, use the [update prompt](../README.md#update-meal-concierge).

## Requirements

- An installed, working client on Linux with user systemd or Apple Silicon macOS
  with a logged-in desktop user. Other host combinations need separate verification.
- Python 3.10+, `uv`, and the selected store's
  [runtime prerequisites](runtime.md#install-and-attach).
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

Check your client's `plugin --help` and existing registrations first. Preserve
unrelated plugins and reuse an enabled Meal Concierge registration for the same
household.

**Codex CLI:**

```sh
codex plugin marketplace add /absolute/codex-package
codex plugin add meal-concierge@meal-concierge
```

**Claude Code CLI:**

```sh
claude plugin marketplace add /absolute/claude-package
claude plugin install meal-concierge@meal-concierge
```

For a single Claude Code session, you can instead use:

```sh
claude --plugin-dir /absolute/claude-package/plugins/meal-concierge
```

**Claude Desktop → Code → Local:** open a local folder on the service host and
use that Code environment's plugin controls to add the generated marketplace and
activate `meal-concierge`. A separate terminal installation does not establish
that Desktop loaded the plugin. Follow its reload or new-session instructions.

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
updates preserve recipes, including collections imported by older versions. To refresh that
collection, update the code first, then separately ask to
[import the latest collection](runtime.md#versioned-recipe-package-integration).

After a runtime update, rebuild the plugin from the matching checkout into a new
output directory. For a requested plugin update, verify the existing marketplace
belongs to this household, remove that marketplace with
`codex plugin marketplace remove meal-concierge` or
`claude plugin marketplace remove meal-concierge`, then repeat the relevant
registration commands with the new output path. For `--plugin-dir`, use the new
path. Start a new conversation and check both MCP tools and the loaded skill.

If a tool times out after a cart change, checkout or send, ask the agent to check
the original operation before retrying. Reinstalling or restoring old data can
lose the information needed to establish what happened. Report the source
commit, client version, last confirmed step and exact error without sharing
credentials.
