<p align="center">
  <img src="assets/meal-concierge-banner.png" alt="Meal Concierge — a retro cooking-pot mascot delivering groceries. Plan meals. Save recipes. Shop smarter." width="1000">
</p>

<p align="center">
  <a href="#agent-support"><img src="assets/icons/hermes.png" alt="Hermes Agent" title="Hermes Agent" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/openclaw.png" alt="OpenClaw" title="OpenClaw" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/nanoclaw.png" alt="NanoClaw" title="NanoClaw" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/codex.png" alt="Codex" title="Codex" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/claude-code.png" alt="Claude Code" title="Claude Code" width="48" height="48"></a>
  <a href="#agent-support"><img src="assets/icons/grok-bot.png" alt="Grok Bot" title="Grok Bot" width="48" height="48"></a>
</p>

<p align="center">
  <a href="#supported-stores"><img src="assets/icons/oda.png" alt="Oda" title="Oda" width="40" height="40"></a>
  <a href="https://www.mathem.se/"><img src="assets/icons/mathem.png" alt="Mathem" title="Mathem" width="40" height="40"></a>
  <a href="#supported-stores"><img src="assets/icons/meny.png" alt="MENY" title="MENY" width="80" height="40"></a>
</p>

**Plan meals, save recipes and shop for groceries through Hermes Agent.**

Meal Concierge connects [Hermes Agent](https://github.com/NousResearch/hermes-agent)
to Oda or MENY in Norway, or Mathem in Sweden. Tell Hermes what you want to cook,
build a weekly menu,
and turn it into a grocery cart you can review and order. Your household
preferences, saved recipes and menus are stored locally.

> “Plan next week's dinners for two.”
>
> “Replace Wednesday's dinner with something vegetarian.”
>
> “Show me the groceries we need and prepare checkout.”

[Installation](#installation) · [First use](#first-use) ·
[Troubleshooting](#troubleshooting) · [Technical reference](docs/reference.md) ·
[![MIT License](assets/badges/mit.svg)](LICENSE)

## What you can do

- **Plan your week:** choose portions and preferences, replace meals, plan
  leftovers and adjust the remaining week when plans change.
- **Build your recipe collection:** discover, save, search, scale and favorite
  recipes. Use the included local recipe bank or connect Mealie or RecipeSage.
- **Prepare your shopping:** match ingredients to products, account for pantry
  items, manage your cart and remember favorite or recurring purchases.
- **Manage delivery and orders:** compare delivery windows, prepare checkout,
  add goods to supported existing orders, move delivery or cancel an order.
- **Set up recurring help:** schedule weekly planning through Hermes. Recipe
  emails additionally require a configured Hermes email account/tool.

The default is **seven different dinners for two people**. Recipe discovery
supports the local bank, Oda, Mathem, MENY, TheMealDB and Wikibooks Cookbook;
you can adjust
portions, preferences and enabled sources during first-use setup.

Meal Concierge runs as a local background service with a skill and an MCP
connection that gives Hermes its meal and grocery tools. You use it through
your usual Hermes chat. No separate web app or database server is required.

## Agent support

**Available now:** Hermes Agent.

**Coming soon:** OpenClaw, NanoClaw, Codex, Claude Code and Grok Bot integrations.
These integrations are planned and are not available to install yet. The standalone runtime below can serve trusted local MCP clients; this does not
certify the pending platform packages or their complete workflows.

## Supported stores

Choose one store per installation. All three support product and recipe search,
cart management and delivery selection. Checkout and existing-order actions
vary by store.

| | Oda | Mathem | MENY |
|---|---|---|---|
| Connection | Oda MCP, plus browser for protected order actions | Mathem MCP | Logged-in MENY website |
| Sign-in | Hermes OAuth **and** browser login to the same Oda account | Separate Mathem OAuth through Hermes | Persistent browser login |
| Checkout | Configured Oda payment method | Review the cart in chat, then pay on Mathem's website | Home delivery and payment via Vipps (a Norwegian mobile payment service), approved on your phone |
| Existing orders | Read, supported changes and cancellation | Read and track; change or cancel on Mathem's website | Read, supported changes and cancellation |

Mathem uses Swedish kronor (SEK). Its `checkout prepare` returns a cart summary
and a link to finish on Mathem. Automatic payment, order changes and cancellation
are not supported for Mathem; weekly runs can prepare a draft or ready cart.

By default, Hermes asks you to confirm the prepared summary before checkout or
cancellation. An optional standing-authorization policy is described in the
[reference](docs/reference.md#upgrade-and-confirmation-details). Provider and
payment approvals still apply, and uncertain submissions are reconciled before
any further action.

## Requirements

The standalone core requires Linux with a running user systemd manager or Apple
Silicon macOS, Python 3.10+ to bootstrap, and `uv`. It installs its own pinned
Python 3.12.12 runtime with `mcp==2.1.1` and `mcp-types==2.1.1`.
Oda/MENY also require `agent-browser@0.33.1` and non-snap Chrome/Chromium.
Existing Oda/Mathem OAuth still uses Hermes helpers; standalone OAuth and the new
agent packages remain separate integration work.

## Installation

Clone this repository outside the private data directory. Install the browser
adapter if using Oda/MENY:

```sh
npm install --prefix "$HOME/.local/lib/meal-concierge" agent-browser@0.33.1
./install.sh install --provider meny --household "My household" \
  --agent-browser "$HOME/.local/lib/meal-concierge/node_modules/.bin/agent-browser"
./install.sh start
./install.sh attach
```

Installation leaves the service stopped; start and agent registration are separate.
`attach` prints the running service's MCP configuration and skill path without
changing either. Provider login is not performed by installation. See the
[standalone runtime guide](docs/runtime.md) for exact paths, prerequisites,
provider limitations, existing-installation adoption and safe data handling.

## First use

Start with a planning request:

> “Plan next week's seven dinners.”

The first interactive planning or recipe-discovery request asks you to review
the household settings. Keep the defaults or change the portions, preferences
and recipe sources. Review the proposed menu and ask Hermes to save it.

Then try:

- “Find vegetarian dinners and save this recipe.”
- “Favorite this recipe and plan next week from my favorites.”
- “We already have rice. Show me what else we need.”
- “Show my cart and available delivery windows.”
- “Prepare checkout.”

Planning and preparing a checkout do not place an order. With the default
confirmation policy, review the final summary and confirm when you are ready.
For MENY, also approve the payment in Vipps when prompted.

## Configuration and optional features

New standalone data defaults to `~/.local/share/meal-concierge`. The separate
program directory is replaceable; state, recipe snapshots and assets are retained.
Existing installations keep their configured paths through explicit adoption.

Use Hermes to change everyday meal preferences. See
[example-config.json](example-config.json) for configuration fields; the
installer creates the working config for you. Each installation's state is
bound to its store: use a separate installation to connect another provider.

| Optional setup | Guide |
|---|---|
| Mealie or RecipeSage recipe library | [Connect a recipe library](docs/reference.md#personal-recipe-library-connections) |
| Importing and managing local recipes | [Private recipe bank](docs/reference.md#private-recipe-bank) |
| Weekly schedules and recipe emails | [Workflow and email requirements](docs/reference.md#natural-language-workflow) |
| Custom paths, service management and uninstall | [Service lifecycle](docs/reference.md#provider-login-and-startup) |
| Planner behavior and product-price limits | [Technical reference](docs/reference.md) |

For custom paths and service ownership, use the [runtime guide](docs/runtime.md).

## Updating

Stop the exact installation before updating its core:

```sh
./install.sh stop
./install.sh update
./install.sh start
./install.sh attach
```

The updater makes a full offline state/config backup and migrates JSON and SQLite.
It does not register agents, change provider logins, or restart automatically.
A failed migration blocks startup until repaired. Never restore older outcome
journals after possible orders or sends. See [recovery and restore](docs/runtime.md#updates-failures-and-recovery).

## Troubleshooting

| Symptom | What to check |
|---|---|
| `awaiting_login` | Complete authorized provider setup using the configured dedicated browser profile. |
| `unavailable` | Check the service logs and whether the configured store and browser dependencies are reachable. |
| Hermes cannot find the tools | Run `hermes mcp test meal_concierge` and restart Hermes. If you restrict `platform_toolsets`, include `meal_concierge` for that platform. |
| Standalone runtime missing | Install with `uv` available on PATH; inspect the runtime guide. |
| Browser missing or snap rejected | Install non-snap Chrome/Chromium; pass `--browser-executable` for a custom location. |
| MENY is waiting for payment | Check Vipps on your phone, then let Hermes reconcile the result. An uncertain result must not trigger another payment attempt. |

On Linux, inspect logs with `journalctl --user -u meal-concierge.service -n 100`.
On macOS, standalone logs are `service.out.log` and `service.err.log` in the
private installation home. Existing installations retain their configured logs.

## Privacy and limitations

Household data and provider sessions are stored locally, outside the source
repository. Requests still use your configured Hermes model and the relevant
store or recipe service. Keep credentials and private data out of Git and
public issue reports.

Product selection compares the candidates it can verify; it does not guarantee
the cheapest basket in the store. The checkout summary determines the final
price. Current integrations cannot establish authoritative allergen safety;
configured allergy or avoidance rules may require further input and block
product selection.

This project is not affiliated with Oda, Mathem or MENY. Website changes can require
adapter updates. Email sending needs an existing Hermes email integration;
connecting an optional recipe library also requires your own account/server.

## Help and contributing

For bugs or feature requests, open an
[issue](https://github.com/poisdahl/meal-concierge/issues). Include your operating
system, Hermes version, store, repository commit (`git rev-parse --short HEAD`),
reproduction steps and a redacted error message.

Contributions are welcome through
[pull requests](https://github.com/poisdahl/meal-concierge/pulls). Read the
[technical reference](docs/reference.md) for implementation details. From the
repository root, run the test suite:

```sh
MC_TEST_ENV="$(mktemp -d)"
uv venv --python 3.12.12 "$MC_TEST_ENV"
uv pip sync --python "$MC_TEST_ENV/bin/python" tests/mcp-requirements.txt
"$MC_TEST_ENV/bin/python" -I -m unittest discover -s tests
"$MC_TEST_ENV/bin/python" -I tests/test_mcp_runtime.py
```

Licensed under the [MIT License](LICENSE).
