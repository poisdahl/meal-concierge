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
  <a href="https://www.mathem.se/"><img src="assets/icons/mathem.png" alt="Mathem" title="Mathem" width="40" height="40"></a>
  <a href="#requirements"><img src="assets/icons/meny.png" alt="MENY" title="MENY" width="80" height="40"></a>
</p>

**Plan meals, save recipes and shop for groceries through your AI agent.**

Ask for a weekly menu, adjust portions and preferences, and turn ingredients
into a shopping cart. Your recipes and menus are saved in your installation.
The included offline collection contains 4,599 recipes; Mealie and RecipeSage
are optional.

## Installation

For **Codex, Claude Code CLI or Claude Desktop’s Code section**, send:

> Install Meal Concierge from https://github.com/poisdahl/meal-concierge.
> Follow docs/client-install.md. Preserve existing installations and data.
> Ask which host, store and household to use; let me complete native approvals.
> Verify the service, recipes and this client’s tools/skill, and report what remains.

For **Grok Bot**, send this prompt:

> Install Meal Concierge from https://github.com/poisdahl/meal-concierge.
> Follow docs/grok.md on your cloud computer. Preserve any existing installation
> and data. Ask which store and household to use, and let me complete login and
> required approvals. Verify the tools and tell me what remains incomplete.

For other agents, use the guide below. The service runs on Linux or Apple Silicon
macOS, or a supported cloud computer; a desktop chat alone does not host it.

## Agent support

| Agent | Setup and current scope |
|---|---|
| Hermes Agent | [Installation and connection](docs/reference.md#provider-login-and-startup); established integration |
| Codex / Claude Code | [Client packages](clients/README.md); native setup and recipe/menu workflows tested |
| Claude Desktop, Code mode | [Client packages](clients/README.md); setup and recipe import tested |
| Grok Bot | [Cloud setup](docs/grok.md); guided installation, recipes/menus and live Oda reads tested; checkout and PDF delivery unverified |
| OpenClaw | [Setup](docs/openclaw.md); native connection and lifecycle tested |
| NanoClaw | [Setup](docs/nanoclaw.md); native connection and lifecycle tested |

## Requirements

Choose **one store per installation**. For shopping, you need your own store
account, complete contact details and an address in its delivery area.
Installation does not create an account or add a payment card.

| Store | What you must set up |
|---|---|
| Oda (Norway) | Authorize the connection and log into the same account in the dedicated browser. Saved-card checkout needs a usable card in Oda. |
| Mathem (Sweden, SEK) | Authorize the connection. Saved-card checkout also needs the same account logged into the dedicated browser; otherwise use the website checkout link. Change or cancel existing orders on Mathem's website. |
| MENY (Norway) | Log into the dedicated browser, set up home delivery and your Vipps phone number, and approve payments on your phone. |

Enter passwords, cards and approvals only in the store's trusted interface,
never in chat. A connected store does not by itself confirm that checkout is
ready. Complete end-to-end payment testing is still pending for Mathem.

## First use

> Plan next week's seven dinners for two.

Review the household settings, then the proposed menu, and ask to save it.
You can change portions, preferences and recipe sources in chat. Then try:

- “We already have rice. Show me the groceries we need.”
- “Show my cart and delivery windows.”
- “Prepare checkout.”

Planning and preparing checkout do not place an order. By default, review and
confirm the final summary before ordering or cancelling. If a submission's
result is unclear, ask the agent to check it before trying again.

Ask to receive the saved menu's recipes. New installations default to chat text,
with PDF and available images where the agent supports them. Email is optional
and needs a connected sender and your chosen recipient. Delivery channels and
formats are configurable; automatic schedules need support in the host agent.
See [recipe delivery](docs/recipe-delivery.md) for host-specific limits.

## Updates and help

Ask the installing agent to follow the [update guide](docs/runtime.md#updates-failures-and-recovery)
and preserve your existing data and login. Reinstalling or resetting the cloud
computer is not an update procedure.

If tools are missing or login fails, ask the agent to check the installation
and store connection. Report unresolved errors in a
[GitHub issue](https://github.com/poisdahl/meal-concierge/issues), including your
agent, operating system, store and a redacted error message.

Household data and sessions stay in the installation; your configured AI model,
store and recipe services still process relevant requests. Product matching
does not guarantee the cheapest basket or allergen safety; review ingredients
and the final checkout price yourself.

[Technical setup](docs/runtime.md) · [Reference](docs/reference.md) ·
[Verified capabilities](docs/acceptance.md) · [MIT License](LICENSE)
