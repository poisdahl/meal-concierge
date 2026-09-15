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

Ask for a weekly menu, adjust portions and preferences, and turn ingredients
into a shopping cart. Meal Concierge can find and use online recipes from
your selected, connected store: Oda, Mathem or MENY.
You can also save your own recipes and add the **Optional Recipe Collection**.
The collection is not needed to get started.

[Install](#installation) · [Update Meal Concierge](#update-meal-concierge) ·
[Add or update recipes](#add-or-update-the-recipe-collection) · [User guide](docs/usage.md)

## Installation

Your agent platform should already be installed. Send this to **Codex,
Claude Code or another installation-capable AI agent** with access to its host.
For **Grok Bot**, send it to Grok itself. Replace the bracketed agent name:

> Set up Meal Concierge from https://github.com/poisdahl/meal-concierge
> for my existing [Hermes / OpenClaw / NanoClaw / Codex / ChatGPT Work Local / Claude Code / Grok Bot].
> Follow the matching installation guide. Connect to my existing household if
> present; otherwise install the latest version. Ask which host, store and
> household to use as needed. Preserve my data and settings. Verify the service,
> tools, skill and store connection, and help me complete login and activation.

If Meal Concierge is already installed, the agent connects to the same recipes,
settings and store connection. To update the program itself, use [Update Meal Concierge](#update-meal-concierge)
below. Connecting another agent does not require reinstalling the service.

Meal Concierge runs on Linux, Apple Silicon macOS, or Grok's cloud computer.
It runs separately from the conversation, so closing a chat does not remove
saved recipes or settings. The guides below cover the required host setup.

### Agent support

| Your existing agent | Installation guide | What to know |
|---|---|---|
| Hermes Agent | [Hermes](docs/hermes.md) | Runs alongside your Hermes installation. |
| OpenClaw | [OpenClaw](docs/openclaw.md) | Connects to a service on the same host. |
| NanoClaw | [NanoClaw](docs/nanoclaw.md) | The service runs on the host; trusted agent groups connect from containers. |
| Codex — ChatGPT desktop app or CLI | [Codex and Claude](docs/client-install.md) | Use a local session on the service host. |
| Claude Code — desktop app or CLI | [Codex and Claude](docs/client-install.md) | Use a local session on the service host. |
| Grok Bot | [Grok](docs/grok.md) | Uses Grok's cloud computer; group-room recipe delivery supports text only. |

Desktop and terminal clients use the same installation and household. In
Claude Desktop, choose **Code → Local**. **ChatGPT Work Local** uses the same
Codex package; follow the [client guide](docs/client-install.md#choose-your-client)
to check that its tools and skill are available in your Work conversation.
Regular Chat and cloud sessions need a different connection. For manual service
installation, see [shared setup](docs/runtime.md).

## Update Meal Concierge

Send this to an installation-capable agent with access to your existing
installation (or to Grok for its cloud installation):

> Update my existing Meal Concierge installation from
> https://github.com/poisdahl/meal-concierge to the latest version on main.
> Follow docs/runtime.md and my agent's installation guide. Preserve my recipes,
> settings, login and saved data. Refresh the connected agent's tools and skill
> as needed, verify that everything works, and report the installed version.

A program update keeps your existing recipe collection. To refresh the
collection too, request [the latest recipe collection](#add-or-update-the-recipe-collection)
separately after updating the program. You keep the same household and saved data.

## Requirements

Choose **one store per installation**. Several agents can share the same
household installation. Shopping requires your own store account, complete
contact details and an address in its delivery area.

**Oda and Mathem follow the same setup:** authorize the store connection, then
log into the same account in the dedicated browser to enable checkout. Add a
usable payment card on the store's website if you want saved-card payment.

| Store | Country / currency | Payment through Meal Concierge |
|---|---|---|
| Oda | Norway / NOK | Saved card or Vipps; complete any required approval yourself. |
| Mathem | Sweden / SEK | Saved card; complete any required approval yourself. |
| MENY | Norway / NOK | Vipps; configure home delivery and your Vipps phone number, then approve on your phone. |

MENY uses its dedicated browser for the store connection as well as checkout.
Mathem can also run without a checkout browser and hand you over to its website
to finish the order. The current Oda installer requires the browser dependencies;
see [installation requirements](docs/runtime.md#install-and-attach).

Installation does not create store accounts or add payment cards. Enter
passwords, payment details and approvals in the trusted store or payment
interface, never in chat.

## First use

> Plan next week's seven dinners for two.

Review the household settings and recipe sources, then the proposed menu.
Ask to save it when you are happy. The local bank may initially be empty;
online store recipes remain available through the connected sources.
You can add your own recipes at any time.

Then try:

- “Use what we already have: rice, carrots and lentils.”
- “Show the groceries for this menu and add what is missing.”
- “Show my cart and delivery windows.”
- “Prepare checkout.”
- “Give me the saved menu's recipes.”

Planning and preparing checkout do not place an order. By default, you review
and confirm the final summary before ordering or cancelling. You can separately
configure standing authorization. If an order or payment result is unclear,
ask the agent to check it before trying again.

See the [user guide](docs/usage.md) for favorites, portions, leftovers,
recurring plans, order changes and dietary preferences.
[Recipe delivery](docs/recipe-delivery.md) explains chat, PDF and optional email.

### Add or update the recipe collection

The **Optional Recipe Collection** is not required. Installation and code
updates do not download or import it. To add or refresh it, ask:

> Synchronize the latest Optional Recipe Collection into my existing Meal
> Concierge installation. Permanently remove collection recipes that are no
> longer included. Preserve every other local recipe and favorite.

The agent uses `import-recipes`, which selects the newest published stable
recipe pack and verifies its checksum and size. It briefly stops the service
when no active work will be interrupted, then starts it again.
An authoritative collection update permanently deletes entries removed by the
publisher, including the local revisions and favorite attached to that exact
entry. Recipes and favorites outside this collection are never part of that
cleanup. An invalid pack or an interruption before the complete record pass
does not delete absent entries.
See [recipe import](docs/recipe-import.md) for this and other ways to add recipes.

## Help and privacy

For missing tools, login problems or interrupted setup, ask the agent to follow
[updates and recovery](docs/runtime.md#updates-failures-and-recovery).
Report unresolved errors in a [GitHub issue](https://github.com/poisdahl/meal-concierge/issues)
with your agent, operating system, store and an error message stripped of
private data.

Household data and sessions stay in your installation; your configured AI,
store and recipe services process relevant requests. Product matching does
not guarantee the cheapest basket or allergen safety: check ingredients and
the final checkout price.

[Contributing and technical documentation](CONTRIBUTING.md) · [MIT License](LICENSE)
