# Connect your agent to Meal Concierge

For a copyable setup or update request, start with the
[main README](../README.md#installation). The guides below explain how to
install Meal Concierge once and connect your existing agent to that household.

| Your agent | Installation guide |
|---|---|
| Codex and Claude Code — desktop app or CLI | [Codex and Claude Code](../docs/client-install.md) |
| ChatGPT Work Local | [Choose your client](../docs/client-install.md#choose-your-client) |
| Hermes | [Hermes](../docs/hermes.md) |
| OpenClaw | [OpenClaw](../docs/openclaw.md) |
| NanoClaw | [NanoClaw](../docs/nanoclaw.md) |
| Grok Bot | [Grok Bot](../docs/grok.md) |

## Reuse your household

Connections use the same household service and shared skill. Multiple trusted
agents can share one installation when they have the required access to its host.
Adding an agent does not require copying recipes or store credentials. The service
keeps your data independently of conversations.

Codex and Claude Code connections run on the same computer and under the same
user as the service. Their generated packages contain local paths and stay there. Grok uses its separate cloud computer. Regular
ChatGPT Chat, Work Cloud and Claude Desktop Chat are not covered by the local
client package; see [client modes](../docs/client-install.md#choose-your-client).

Use the [program update prompt](../README.md#update-meal-concierge) to update an
existing installation. Repeating setup connects to it; it does not update it.

New installations and program updates select a published stable **program**
release, such as `v0.1.1`, and retain its exact commit. To request an explicit
version, use the [version instructions](../README.md#program-releases-and-exact-versions).
The `recipes-*` releases are a separate collection. Build or regenerate client
packages from the checkout matching the installed runtime, then verify the
running build and loaded skill in each client you use.

## Import a recipe from a PDF

Attach the PDF through your client's native attachment workflow and ask the
agent to use the installed Meal Concierge skill. Codex and Claude Code packages
include a launcher for the runtime's local PDF renderer; the client still needs
image-reading capability and permission to run it. NanoClaw needs its container's
native PDF reader. Cloud and group-chat attachment support varies by client.
See [PDF recipe input](../docs/recipe-import.md#pdf-attachments-without-system-packages).

To **receive** recipes instead, ask for them in chat, optional email or a supported
PDF attachment. Output support depends on the destination; Grok Bot group rooms
support recipe text only. See [recipe delivery](../docs/recipe-delivery.md).
