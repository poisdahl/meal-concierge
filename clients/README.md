# Agent connections

Start with the guide for your already installed agent. Each guide explains how
to install Meal Concierge once and connect the agent to that household.

| Agent | Installation guide |
|---|---|
| Codex CLI, Claude Code CLI, Claude Desktop Code | [Codex and Claude Code](../docs/client-install.md) |
| Hermes | [Hermes](../docs/hermes.md) |
| OpenClaw | [OpenClaw](../docs/openclaw.md) |
| NanoClaw | [NanoClaw](../docs/nanoclaw.md) |
| Grok Bot | [Grok Bot](../docs/grok.md) |

Connections use the same household service and shared skill. Adding another
agent does not require another household installation or a copy of its store
credentials. The service continues independently of conversations. Generated
client packages contain local paths and belong on their owner's computer.

## PDF attachments

Use your client's native attachment workflow and the installed skill. The
Codex and Claude Code packages include a launcher for the runtime's local PDF
renderer; image reading and permission to run it are still required. NanoClaw
currently needs its container's native PDF reader. See
[PDF input](../docs/recipe-import.md#pdf-attachments-without-system-packages).
Cloud and group-chat attachment support varies by client; consult its guide.
