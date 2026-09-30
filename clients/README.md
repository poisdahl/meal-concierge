# Connect your agent to Meal Concierge

Start with the [setup prompt](../README.md#installation) or choose your guide:

| Your agent | Installation guide |
|---|---|
| Codex (desktop or CLI) and ChatGPT Work Local | [Local client setup](../docs/client-install.md) |
| Claude Code — CLI or Claude Desktop **Code → Local** | [Local client setup](../docs/client-install.md) |
| Hermes | [Hermes](../docs/hermes.md) |
| OpenClaw | [OpenClaw](../docs/openclaw.md) |
| NanoClaw | [NanoClaw](../docs/nanoclaw.md) |
| Grok Bot | [Grok Bot](../docs/grok.md) |

## Reuse your household

Trusted agents can share the household's recipes, settings, store connection and skill.

Codex, ChatGPT Work Local and Claude Code connect on the **same computer and as
the same user** as the service. Generated packages use local paths and stay on
that computer. Grok uses its own cloud computer.

Setup reuses the existing household. Use the [update prompt](../README.md#update-meal-concierge)
for upgrades or [version instructions](../README.md#program-releases-and-exact-versions)
for an exact release. Build packages from the checkout matching the installed
runtime, then verify the running build, tools, skill and file access in each mode you use.

## Import a recipe from a PDF

Attach a PDF and ask your agent to import the recipe with Meal Concierge.
See [PDF input](../docs/recipe-import.md#pdf-attachments-without-system-packages)
for client requirements.

For output formats, see [recipe delivery](../docs/recipe-delivery.md).
Grok Bot group rooms support text only.
