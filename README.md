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

Plan for the dates you need, use food you already have, or shop for groceries
without a meal plan. Meal Concierge keeps your recipes and preferences between
conversations and connects to **Oda, Mathem or MENY**.

Choose meals around your preferences and available time, save favorite recipes,
and turn the ingredients into a shopping cart.

The **Optional Recipe Collection** is an extra source of recipes, not a
requirement. You can start with your selected store's recipes and add your own.

[Install](#installation) · [First use](#first-use) ·
[Update](#update-meal-concierge) · [Recipe collection](#add-or-update-the-recipe-collection) ·
[User guide](docs/usage.md)

## Installation

Send this to an installation-capable AI agent with access to your existing
agent's host. For **Grok Bot**, send it to Grok itself. Replace the bracketed name:

> Set up Meal Concierge from https://github.com/poisdahl/meal-concierge
> for my existing [Hermes / OpenClaw / NanoClaw / Codex / ChatGPT Work Local / Claude Code / Grok Bot].
> Follow the matching installation guide. Connect to my existing household if
> present; otherwise install the latest published stable program release using
> docs/runtime.md#choose-a-program-release. Pin its tag to a full commit; do not
> install main. Ask which host, store and household to use as needed. Preserve my
> data and settings. Verify the running build, tools, skill and store connection,
> report the release tag and running commit, and help me complete login and activation.

Already installed? This connects to the same household. Use the
[update prompt](#update-meal-concierge) to update the program instead.

### Program releases and exact versions

[Program releases](https://github.com/poisdahl/meal-concierge/releases) use tags
such as **v0.1.1**. New installations and program updates use the latest published
stable program release by default. The `recipes-*` releases on the same page are
the separate Optional Recipe Collection.

To choose an exact version, replace “the latest published stable program release”
in the setup or update prompt with “program release v0.1.1” (or your chosen
published version). The installing agent retains that release's exact checkout;
the installer does not select or download a program version itself. See
[release selection and checkout](docs/runtime.md#choose-a-program-release).

To check an existing installation, ask:

> Show the running Meal Concierge build and full source commit from service
> status. Compare it with my selected release tag and report any mismatch or
> unavailable identity. Do not update anything.

### Agent support

| Your existing agent | Installation guide | What to know |
|---|---|---|
| Hermes Agent | [Hermes](docs/hermes.md) | Runs alongside your Hermes installation. |
| OpenClaw | [OpenClaw](docs/openclaw.md) | Connects to a service on the same host. |
| NanoClaw | [NanoClaw](docs/nanoclaw.md) | The service runs on the host; trusted agent groups connect from containers. |
| Codex — desktop app or CLI | [Codex and Claude Code](docs/client-install.md) | Use a local session on the service host. |
| Claude Code — desktop app or CLI | [Codex and Claude Code](docs/client-install.md) | Use a local session on the service host. |
| Grok Bot | [Grok](docs/grok.md) | Uses Grok's cloud computer; group-room recipe delivery supports text only. |

**ChatGPT Work Local** uses the Codex package; see
[Choose your client](docs/client-install.md#choose-your-client).
In Claude Desktop, use **Code → Local**. Regular ChatGPT Chat, Work Cloud and
Claude Desktop Chat are not covered by this local setup.

### Requirements

Meal Concierge runs on Linux, Apple Silicon macOS, or Grok's cloud computer.
Choose **one store per installation**. Multiple trusted agents can share the
same household.

Shopping requires your own store account, complete contact details and an
address in its delivery area. The installation agent checks browser requirements;
you complete store login in the installation's dedicated browser.

| Store | Country / currency | Payment through Meal Concierge |
|---|---|---|
| Oda | Norway / NOK | Saved card or Vipps; complete any required approval yourself. |
| Mathem | Sweden / SEK | Saved card; complete any required approval yourself. |
| MENY | Norway / NOK | Vipps; configure home delivery and your Vipps phone number, then approve on your phone. |

Installation does not create store accounts or add payment cards. Enter passwords,
payment details and approvals in the trusted store or payment interface, never
in chat. Installation agents can find exact prerequisites and commands in
[shared setup](docs/runtime.md).

## First use

> Plan four dinners for two, starting Thursday. Use the carrots we already have.

On first use, the agent helps you review portions, cooking time, dietary goals,
recipe sources and pantry basics. New households start with an **editable
Norwegian dietary-guideline preset**: keep it, change it or remove it. Existing
households keep their saved preferences during updates.

You can choose to treat salt, pepper and cooking oil as normally available.
An accepted pantry list avoids repeated questions for ordinary quantities;
the agent checks periodically about restocking. Other staples, such as butter
and sugar, are checked together for the meal plan before buying them.

A meal-plan request normally creates an editable saved proposal with dates,
portions, known cooking times and recipe sources. The agent marks adaptations
and offers full ingredients and instructions. Ask for changes whenever needed;
if saving is blocked, the agent should explain why.

Try these next:

- “Show all ingredients and cooking steps for this plan.”
- “Replace Thursday's dinner with something quicker.”
- “Find suitable offers on dinner ingredients and suggest meals around them.”
- “Show the groceries and estimated cost before changing the cart.”
- “Just add milk, apples and crispbread to my cart. I don't need a meal plan.”
- “Show my cart and delivery windows, then prepare checkout.”
- “Show everything Meal Concierge can do and all my settings and preferences.”

**Planning and preparing checkout do not place an order.** By default, you
confirm the final summary before ordering or cancelling. Standing authorization
and automatic ordering are separate choices; required bank or phone approvals
still apply. If an order or payment result is unclear, ask the agent to check the
original attempt before trying again.

Automation starts off. Scheduled planning needs a persistent scheduler and an
available host; automatic ordering is a separate opt-in. Recurring groceries
can follow elapsed time between confirmed purchases. See the
[user guide](docs/usage.md) for leftovers, recurring items and order changes.

### Find and receive recipes

Recipe discovery can use your saved bank and connected store. New installations
also enable **seven Norwegian recipe websites**: MatPrat,
Vegetarentusiast, Frukt.no, Godfisk, TINE, Godt and Trines. These sites need no
search API key. You can change the sources during setup or later.

For unusual dishes or cuisines, optional broader search can use your agent's
available search tool, Brave Search API or Firecrawl. You can also provide a
specific recipe link, text or supported attachment. See
[recipe search](docs/recipe-search.md) and [adding recipes](docs/recipe-import.md).

Ask for your recipes in chat whenever you need them. Optional email can deliver
the complete recipes after a confirmed menu purchase, without a duplicate PDF
by default. A PDF is available through supported chat/file delivery when email
is unavailable or you ask for one. A grocery-only purchase does not send recipes.
See [recipe delivery](docs/recipe-delivery.md) for supported formats and timing.

## Update Meal Concierge

Send this to an installation-capable agent with access to the existing
installation, or to Grok for its cloud installation:

> Update my existing Meal Concierge installation from
> https://github.com/poisdahl/meal-concierge to the latest published stable program
> release using docs/runtime.md#choose-a-program-release. Pin its tag to a full
> commit; do not install main. Follow docs/maintenance.md and the applicable host
> update steps.
> Preserve my recipes, settings, login, browser profile and saved data. Check
> prerequisites before stopping; use the bounded verification in that guide.
> Report the release tag, running commit and health. Do not update the recipe
> collection.

To update **both the program and collection**, replace the last sentence with:

> Then synchronize the latest Optional Recipe Collection, permanently removing
> withdrawn collection entries while preserving every other recipe and favorite.
> Report its version, result counts and any conflicts or incomplete results.

Program and collection updates are separate choices. A combined request updates
the program first and reuses installation details. The
[maintenance guide](docs/maintenance.md) keeps agent work and verification bounded.

## Add or update the recipe collection

Installation and program updates do not automatically download the
**Optional Recipe Collection**. To add it or synchronize the latest published
version, ask:

> Synchronize the latest Optional Recipe Collection into my existing Meal
> Concierge installation. Permanently remove collection recipes that are no
> longer included. Preserve every other local recipe and favorite. Follow
> docs/maintenance.md; use the installer's pack operation, not individual recipe
> tools. Report the version, result counts and any conflicts or incomplete results.

The collection downloads and is checked while the service stays available.
Once no active work will be interrupted, the service stops for the offline
import and then restarts. No GitHub account or API token is required.

**Withdrawn collection entries are permanently removed**, including their local
revisions and attached favorites. Retained entries are updated, with conflicts
reported for review. Recipes and favorites outside this collection are preserved.
An invalid pack or an interruption before the complete record pass does not
trigger removal of entries that have not yet been seen.
See [recipe import](docs/recipe-import.md) for details and other ways to add recipes.

### Remove the recipe collection

To remove the whole optional collection, ask:

> Permanently remove the Optional Recipe Collection from my Meal Concierge
> installation. Preserve every other local recipe and favorite, and reclaim
> storage used only by the collection.

This removes the collection's recipes, their local revisions and attached
favorites, including archived entries. Your other recipes and favorites remain.
The agent uses the installation's removal command, updating an older runtime if
required, and briefly stops the service when it is idle. See
[collection removal and recovery](docs/runtime.md#remove-the-recipe-collection).

## Help and privacy

For missing tools, login problems or interrupted setup, ask the agent to follow
[updates and recovery](docs/runtime.md#updates-failures-and-recovery).
Report unresolved errors in a [GitHub issue](https://github.com/poisdahl/meal-concierge/issues)
with your agent, operating system, store and an error message stripped of
private data.

Household data and sessions stay in your installation; your configured AI,
store and recipe services process relevant requests. Product matching does
not guarantee the cheapest basket or allergen safety: check ingredient labels
and the final checkout price.

[Contributing and technical documentation](CONTRIBUTING.md) · [MIT License](LICENSE)
