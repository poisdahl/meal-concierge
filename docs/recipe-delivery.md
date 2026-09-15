# Receive your recipes

Ask for recipes from a saved menu whenever you need them. A grocery purchase
is not required.

> Give me the saved menu's recipes, with portions and cooking instructions.

New installations use chat text by default, with PDF and available images where
the agent supports them. You can choose formats and channels independently.
Older installations keep their existing delivery preferences.

## Chat, PDF and images

- “Show the recipes here.”
- “Give me a PDF of the saved menu.”
- “Include recipe photos where available.”
- “Use text only from now on.”

Recipes use the portions and ingredients saved in that menu. Source credits
and missing information remain visible. Photos are included only when available.
For long menus, text or attachments may be split into several parts.

| Agent or destination | Practical limitation |
|---|---|
| Codex CLI | Can create local files; the terminal itself does not show a PDF/image preview. |
| Codex desktop / Claude Desktop Code | PDF and image presentation have been verified in specific desktop workflows. The actual client still needs file access and attachment support. |
| Grok Bot group rooms | Recipe text is supported; PDF and image attachments are not delivered through that room transport. |
| Hermes, OpenClaw, NanoClaw and other destinations | File delivery depends on the configured chat/sender. Have the agent check support rather than promise attachments. |

If a PDF or image cannot be delivered, the agent should identify the omission
and offer the supported format. It must not report a local file path as a
successfully delivered attachment on another computer.

## Email connection setup

> Set up recipe email using my existing email connection. Let me choose the
> sender, recipient and whether to send on request, on delivery day, or both.

Email is optional and starts disabled in a new installation. Setup checks the
agent host's existing email connection and records your choices; it does not
send a message. Installing Meal Concierge does not create a mailbox.

The sender must be available on the computer running the Meal Concierge agent
tools. An email connector available only in another app or cloud session is
not automatically usable there. The setup supports an existing Gmail connection,
SMTP configuration or a compatible host sender. If none is available, the
installing agent can follow the
[email setup reference](recipe-delivery-reference.md#email-connection-setup).
Keep credentials in that connection's private setup, never in chat.

After setup, request a send:

> Email me the recipes for the saved menu, including the PDF if supported.

The agent should show the selected sender, recipient and timing. Enabling email
does not turn off chat. An immediate email and a later delivery-day email are
separate choices; choose both only if you want both.

## Scheduled delivery

Delivery-day recipe email requires a supported persistent scheduler, an available
host and a sender that can run unattended. Email setup alone does not create
the schedule. Ask your agent to set it up and show the saved timing.

If a confirmed order's delivery date changes, its follow-up must be rescheduled.
If the order is cancelled, the follow-up should be removed. Ask the agent to
reconcile changes made directly on the store website.

Automatic chat delivery is not provided by the shared service itself. It needs
separately verified scheduling and destination support in the host agent.
A sleeping or unavailable host cannot be assumed to send on time.

## Change, pause or stop delivery

- “Show my recipe delivery settings and pending messages.”
- “Turn off recipe email.”
- “Pause recipe delivery.”
- “Resume delivery, and show which held messages still need a decision.”

Changes affect future deliveries. Messages already sent cannot be recalled.
Turning a channel back on does not automatically send an old backlog; ask to
release or discard the specific held messages. Pausing recipe delivery does not
pause grocery planning or ordering schedules.

## If a send fails

Ask the agent to check the original delivery before trying again. A timeout can
mean the email or attachment was already accepted. Unknown outcomes remain
unresolved until there is enough evidence; do not send a duplicate just because
a receipt is missing. A confirmed failure before sending can be retried explicitly.

Email acceptance means the mail service accepted the message, not that the
recipient read it. Keep private delivery records during upgrades and recovery.
The [technical delivery reference](recipe-delivery-reference.md) covers sender
integration and troubleshooting for installation agents and maintainers.
