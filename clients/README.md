# Connect your agent to Meal Concierge

Install once, then connect your existing agents to the same household. Start
with the [setup prompt](../README.md#installation) or choose your guide:

| Your agent | Installation guide |
|---|---|
| Codex (desktop or CLI) and ChatGPT Work Local | [Local client setup](../docs/client-install.md) |
| Claude Code — CLI or Claude Desktop **Code → Local** | [Local client setup](../docs/client-install.md) |
| Hermes | [Hermes](../docs/hermes.md) |
| OpenClaw | [OpenClaw](../docs/openclaw.md) |
| NanoClaw | [NanoClaw](../docs/nanoclaw.md) |
| Grok Bot | [Grok Bot](../docs/grok.md) |

## Reuse your household

Multiple trusted agents with access to the service host can share its recipes,
settings, store connection and skill. Adding an agent does not require copying
recipes or credentials; household data persists between conversations.

Codex, ChatGPT Work Local and Claude Code connect on the **same computer and as
the same user** as the service. Their generated packages contain local paths and
stay there. Grok uses its separate cloud computer. Regular ChatGPT Chat,
Work Cloud and Claude Desktop Chat are not covered by this local package.

Repeating setup attaches to the existing household. Use the
[update prompt](../README.md#update-meal-concierge) to upgrade it, and
[version instructions](../README.md#program-releases-and-exact-versions) to choose
an exact stable program release. Build packages from the checkout matching the
installed runtime, then verify the running build, native tools and loaded skill
in each client you use.

## Import a recipe from a PDF

Attach the PDF and ask the agent to use the installed Meal Concierge skill.
The Codex and Claude Code packages include a launcher for the runtime's PDF
renderer; the agent needs image-reading capability and permission to run it.
NanoClaw needs its native container reader.
See [PDF input](../docs/recipe-import.md#pdf-attachments-without-system-packages).

To receive recipes, use chat, optional email or supported PDF delivery. Attachment
support varies; Grok Bot group rooms support text only. See
[recipe delivery](../docs/recipe-delivery.md).
