# Native scheduler acceptance

This matrix separates Meal Concierge's completed shared scheduler contract from
what each native client actually demonstrated. The 2026-09-08 checks add bounded
persistence, pause and timing evidence; they do not install another scheduler.
A source release, successful outer task status or a saved timer alone does not
prove that a meal workflow or email finished.

| Surface and observed version | Mechanism and completed evidence | Remaining limits |
|---|---|---|
| Codex in desktop host 26.901.51231 (build 8109) | Native same-task heartbeat repeatedly woke the dependency monitor. Native pause/resume persisted the same ID, interval, prompt and target. | This is a monitor, not a managed meal job, sender or app-restart test. Local execution needs the app running and computer on. |
| Claude Code CLI 2.1.241 | #54 verifies installation and service lifecycle. Native CLI scheduled tasks are documented as session scoped, with seven-day recurring expiry and local-time jitter. | Durable unattended weekly scheduling is not certified. No additional scheduler was built to hide the session boundary. |
| Claude Desktop 1.46388.4 / embedded Code 2.1.260 | Native Local Routines retained the isolated file-read routine's exact identity, body, folder and pause across normal app exit/relaunch. After a post-due paused observation, the same ID was rescheduled: native history showed 09:33 local for 09:32 nominal due; a single native Read returned the exact fixture nonce/menu, verified within 294 seconds of nominal due. The completed test timer was paused then removed. | This file read used native model Opus 5/Extra, not MCP or a managed meal/sender workflow. Exact backend start/read seconds were unavailable; the model explicitly declined to invent a clock time. Local routines require an awake, online computer. |
| OpenClaw 2026.9.2 | MC07 already verifies actual native weekly occurrence, old/wrong-owner fencing, local frozen-image email, lost completion-ack replay and exact cleanup. Additional unmodified CronService at upstream 3928bad9 uses real clocks and persistent storage: one overdue event after process restart, paused event suppressed, same ID resumed once, completed events not replayed on another restart, unrelated sentinel unchanged. Oslo conversion across the autumn offset change passed. | New tests invoke synthetic local system-event callbacks, not a Gateway/model/email integration. Process downtime is not physical sleep. Historical local SMTP is not production delivery. |
| NanoClaw 2.3.0 / upstream b76fcb3d | MC08 already verifies native occurrence/ownership/local sender and recovery with operator assistance. Additional native SQLite/scheduling components preserve a paused original occurrence across process exit and past its due time; resume retains its identity, completion re-arms one next occurrence, a repeated sweep does not clone it, original-series pause persists, and exact cleanup preserves the sentinel. Oslo conversion and next local 09:00 passed, including offset-change conversion. | Component checks do not run the full host/container/model. The harness initially omitted its separate config schema after completion ACK; recurrence resumed from that same completed occurrence after applying native migrations. No task action was replayed. Historical operator recovery and human image follow-up remain qualifications. |
| Grok desktop 0.44.0 / tested installer 9599097 | Native scheduled saved-menu controls succeeded with app open and during measured app closure: reported due-to-start delays 442.311/397.448 seconds and reads 77.438/55.456 seconds after start. Correct results were observed before any controller wake. Exact routines were reconciled and paused. | Original silent occurrence and causal pause suppression remain unverified. Outer success/finish precedes the actual read and is not workflow latency. No physical host/VM sleep or reliability guarantee. Newer product source is not certified by these historical frozen installations. |
| Hermes / Mathem runtime b8cc15f3 | #50 records one actual systemd-owned scheduled occurrence, local before/result notices, eventual terminal reconciliation and exact owned-timer removal/acknowledgment. | This does not certify Hermes-native cron, production email, bank refund/release, or unassisted order-change recovery. Do not repeat real effects for this matrix. |

## Recovery, content and timing

The unchanged [shared contract](email-scheduler.md) remains authoritative for
duplicate invocations, unavailable old owners, lost registration acknowledgments,
revoked generations, delivery changes/cancellation and uncertain sender outcomes.
Native checks above complement that contract; they do not infer ownership from
copied jobs. Keep the original occurrence, provider/order/recipient, frozen menu
and dispatch token through pause, transfer, restart and reconciliation. A timeout
or missing log never establishes no-send.

The weekly local-week identity and 30-minute admission window are unchanged.
Claude Desktop's native Edit → Save re-enabled the paused test routine when its
time changed. Inspect actual native state after editing; do not assume a saved
edit preserves pause or acknowledge an unobserved state to Meal Concierge.

Measure due-to-start delay separately from tool/workflow duration. A platform
that wakes too late must report the missed admission; do not widen the window or
replay another purchase to mask scheduling delay.

#53's shared frozen chat/PDF/optional-email implementation is published at
71b4c8ea. The original Codex recipient confirmed the PDF and image presentation;
its historical unknown transport acknowledgments remain unchanged. This does not
certify every platform's renderer or sender. OpenClaw/NanoClaw local sender
results and Grok's separate existing-PDF transport retain their stated limits.
No production recipient was added or used by the new scheduler checks.

Physical host/VM sleep was not tested. Claude's local awake requirement and
Codex's running-app requirement are operational limits. Grok's app-closed cloud
execution and the local components' process-restart checks establish their
respective narrower behavior. These are not interchangeable sleep certificates.

## Evidence and platform references

- [OpenClaw native acceptance](openclaw.md) and [NanoClaw native acceptance](nanoclaw.md).
- [Grok results](grok.md), including the separately published [scheduled-read result](https://github.com/poisdahl/meal-concierge/issues/52#issuecomment-5578313930).
- [Mathem completion](https://github.com/poisdahl/meal-concierge/issues/50), [original PDF/image confirmation](https://github.com/poisdahl/meal-concierge/issues/53), and [native installation/lifecycle completion](https://github.com/poisdahl/meal-concierge/issues/54).
- Official [Codex local automations](https://learn.chatgpt.com/docs/automations?surface=app), [Claude CLI scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks), and [Claude Desktop local routines](https://code.claude.com/docs/en/desktop-scheduled-tasks), consulted 2026-09-08. Documentation is a capability reference, not a substitute for the observed versions above.

This scheduler result feeds MC10. It does not independently close the broader
[platform acceptance issue](https://github.com/poisdahl/meal-concierge/issues/52).
