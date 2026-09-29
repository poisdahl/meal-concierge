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
  <a href="#requirements"><img src="assets/icons/oda.png" alt="Oda" title="Oda" width="40" height="40"></a>
  <a href="#requirements"><img src="assets/icons/mathem.png" alt="Mathem" title="Mathem" width="40" height="40"></a>
  <a href="#requirements"><img src="assets/icons/meny.png" alt="MENY" title="MENY" width="80" height="40"></a>
</p>

**Plan meals, save recipes and shop for groceries through your AI agent.**

Plan for the dates you need, use food you already have, or shop without a meal
plan. Meal Concierge keeps your recipes and preferences between conversations
and connects to **Oda, Mathem or MENY**. Start with store recipes or add your own;
the Optional Recipe Collection is an extra.

[Install](#installation) · [First use](#first-use) ·
[Update](#update-meal-concierge) · [Recipe collection](#add-or-update-the-recipe-collection) ·
[User guide](docs/usage.md)

## Installation

Use an existing agent on **Linux, Apple Silicon macOS, or Grok's cloud computer**.
Choose the guide for the environment where you will use Meal Concierge.

### Agent support

| Your agent and installation guide | Where it connects |
|---|---|
| [Hermes Agent](docs/hermes.md) | Service on the same host. |
| [OpenClaw](docs/openclaw.md) | Service on the same host. |
| [NanoClaw](docs/nanoclaw.md) | Service on the same host; trusted agent groups connect from containers. |
| [Codex](docs/client-install.md) | Desktop app or CLI; local session on the service host. |
| [ChatGPT Work Local](docs/client-install.md#choose-your-client) | Codex package; verify tools and skill in the Work conversation. |
| [Claude Code](docs/client-install.md) | CLI or Claude Desktop **Code → Local**, on the service host. |
| [Grok Bot](docs/grok.md) | Grok's cloud computer; group-room recipe delivery is text only. |

Regular ChatGPT Chat, Work Cloud and Claude Desktop Chat are not covered by
this local setup. See [shared setup](docs/runtime.md#install-and-attach) for exact
host and browser prerequisites.

### Requirements

Choose **one store per installation**; multiple trusted agents can share that
household. Shopping needs your own store account, complete contact details and
an address in its delivery area. Complete login in the installation's dedicated browser.

| Store | Country / currency | Payment through Meal Concierge |
|---|---|---|
| Oda | Norway / NOK | Saved card or Vipps. |
| Mathem | Sweden / SEK | Saved card. |
| MENY | Norway / NOK | Vipps; configure home delivery and your Vipps phone number. |

Installation does not create accounts or add payment cards. Enter passwords,
payment details and required approvals in the trusted store or payment interface,
never in chat.

### Set up with your agent

Send this to an installation-capable agent with access to the intended host, or
to **Grok Bot** for its cloud installation. Replace the bracketed name:

> Set up Meal Concierge from https://github.com/poisdahl/meal-concierge
> for my existing [Hermes / OpenClaw / NanoClaw / Codex / ChatGPT Work Local / Claude Code / Grok Bot].
> Follow the matching installation guide. Connect to my existing household if
> present; otherwise install the latest published stable program release using
> docs/runtime.md#choose-a-program-release. Pin its tag to a full commit; do not
> install main. Ask which host, store and household to use as needed. Preserve my
> data and settings. Verify the running build, tools and skill. Help me complete
> store login and activation, then verify the store connection. Report the release
> tag, running commit and any incomplete setup.

Already installed? Setup connects to that household; it does not upgrade it.
Use the [update prompt](#update-meal-concierge) for upgrades, or
[choose an exact version](#program-releases-and-exact-versions) before setup.

## First use

> Plan four dinners for two, starting Thursday. Use the carrots we already have.

Review portions, cooking time, dietary goals, recipe sources and pantry basics.
New households start with an **editable Norwegian dietary-guideline preset**:
keep it, change it or remove it. Updates retain your saved preferences.

A meal-plan request normally creates an editable saved proposal with dates,
portions, known cooking times and recipe sources. The agent marks adaptations
and offers full ingredients and steps; if saving is blocked, it should explain why.

Try these next:

- 🍽️ “Replace Thursday's dinner with something quicker, then show the full recipe.”
- 🛒 “Show the groceries and estimated cost before changing the cart.”
- 🧾 “Show my cart and delivery windows, then prepare checkout.”

Shopping without a meal plan? “Just add milk, apples and crispbread to my cart.”

**Planning and preparing checkout do not place an order.** By default, you
confirm the final summary before ordering or cancelling. Standing authorization
and automatic ordering are separate choices; bank or phone approvals still apply.
If an order, payment or cancellation result is unclear, check the original attempt
before trying again.

Automation starts off. Scheduled planning needs a persistent scheduler and an
available host; automatic ordering is a separate opt-in. The [user guide](docs/usage.md)
covers pantry assumptions, leftovers, recurring groceries, preferences and order changes.

### Find and receive recipes

Use your saved bank, connected store or the configurable Norwegian recipe sites
enabled by default. These sites need no search API key. Broader web search is
optional. You can also add a recipe link, text or supported attachment. See [recipe search](docs/recipe-search.md)
and [adding recipes](docs/recipe-import.md).

Ask for full recipes in chat at any time. Optional email can deliver them after
a confirmed menu purchase; grocery-only purchases do not send recipes. PDFs depend
on your client's file-delivery support. See [recipe delivery](docs/recipe-delivery.md)
for formats and timing.

## Update Meal Concierge

Send this to an agent with access to the existing installation, or to Grok for
its cloud installation:

> Update my existing Meal Concierge installation from
> https://github.com/poisdahl/meal-concierge to the latest published stable program
> release using docs/runtime.md#choose-a-program-release. Pin its tag to a full
> commit; do not install main. Follow docs/maintenance.md and my host's update steps.
> Preserve my recipes, settings, login, browser profile and saved data. Check
> prerequisites before stopping; use the bounded verification in that guide.
> Report the release tag, running commit and health. Do not update the recipe collection.

To update **both program and collection**, replace the last sentence with:

> Then synchronize the latest Optional Recipe Collection. Permanently remove
> withdrawn collection entries, including their local revisions and attached
> favorites. Preserve all other recipes and favorites. Report the collection
> version, result counts and any conflicts or incomplete results.

The program updates first; collection synchronization is a separate choice.
See [bounded maintenance](docs/maintenance.md) for the checks and recovery steps.

### Program releases and exact versions

[Program releases](https://github.com/poisdahl/meal-concierge/releases) use tags
such as **v0.1.1**; `recipes-*` tags belong to the separate collection.
New installations and updates use the latest published stable program release
by default; repeating setup retains the installed version.

For a specific version, replace “the latest published stable program release”
with “program release v0.1.1” (or your chosen published version). The installer
stages the selected checkout; it does not select or download a program version.
See [release selection](docs/runtime.md#choose-a-program-release).

To verify an existing installation without updating it, ask:

> Show the running Meal Concierge build and full source commit from service
> status. Compare it with my selected release tag and report any mismatch or
> unavailable identity. Do not update anything.

## Add or update the recipe collection

Installation and program updates do not download the **Optional Recipe Collection**.
To add it or synchronize the latest published version, ask:

> Synchronize the latest Optional Recipe Collection into my existing Meal
> Concierge installation. Permanently remove withdrawn collection entries,
> including their local revisions and attached favorites. Preserve all other
> recipes and favorites. Follow docs/maintenance.md; use the installer's pack
> operation, not individual recipe tools. Report the version, result counts and
> any conflicts or incomplete results.

The service stays available during download and validation, then stops when idle
for the offline import and restarts afterward. No GitHub account or API token is
required. Retained entries are updated, with conflicts reported for review;
**withdrawn entries are permanently deleted**. See [recipe import](docs/recipe-import.md#add-or-update-the-recipe-collection)
for preservation rules and interrupted-import behavior.

### Remove the recipe collection

> Permanently remove the Optional Recipe Collection from my Meal Concierge
> installation: all collection entries (including archived ones), their local
> revisions and attached favorites. Preserve all other recipes and favorites, and reclaim
> storage used only by the collection. Follow docs/runtime.md#remove-the-recipe-collection
> and use the installation's removal command.

The agent uses the installation's removal command, updating an older runtime if
required, and stops the service when idle. See
[collection removal and recovery](docs/runtime.md#remove-the-recipe-collection).

## Help and privacy

For missing tools, login problems or interrupted setup, follow
[updates and recovery](docs/runtime.md#updates-failures-and-recovery).
Report unresolved errors in a [GitHub issue](https://github.com/poisdahl/meal-concierge/issues)
with your agent, operating system, store and an error message stripped of private data.

Household data is stored in your installation; your configured AI, store and
recipe services process relevant requests. Product matching does not guarantee
the cheapest basket or allergen safety: check ingredient labels and the final price.

[Contributing and technical documentation](CONTRIBUTING.md) · [MIT License](LICENSE)
