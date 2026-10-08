# Proposal: consistent, bounded operation diagnostics

Status: design draft. No new logging, export command, telemetry destination,
retention policy or public error field is implemented by this PR.

## Existing behavior

The service transports business failures as errors while preserving uncertain
operation journals. MENY already attaches a closed `provider_failure` structure;
`rpc_client.normalize_provider_failure` validates provider, class, source, phase
and `daemon_request_ending=unknown`. Tests explicitly reject arbitrary provider
text and synthetic secrets. Build identity and workflow continuation are also
already exposed. Extend those patterns instead of claiming diagnostics are absent.

The remaining opportunity is consistent correlation and recovery guidance across
local validation, persistence, transports and providers. No production incident
rate or support-time improvement has yet been measured.

## Proposed first increment

Agree on an additive, bounded diagnostic envelope for a small set of common
service failures. Keep existing result fields and closed MENY metadata readable
by old clients. Illustrative fields are `code`, `phase`, `operation_ref`,
`elapsed_ms` and `recovery_action`; these names are not implemented tool arguments.

Define every code in a maintained registry with meaning, owner, allowed phase,
compatibility policy and evidence needed to emit it. Unknown codes must remain
unknown to older clients rather than becoming a generic safe-to-retry signal.
An opaque local correlation reference must not encode a household name, email,
account identifier, payment ID or a reversible fragment of one.

The first slice should reuse existing validation and provider classifications;
do not invent broad automatic exception-to-payment-state mappings. Phase measures
where the service was executing, not whether a merchant accepted an effect.

## Retry and outcome semantics

For each diagnostic, distinguish these independent facts:

- whether local validation rejected the request before any possible dispatch;
- what dispatch evidence the original journal actually contains;
- whether the external outcome is confirmed, absent with evidence, or unknown;
- which exact existing read/reconcile action is available under current guards.

Never mark an operation safe to resend merely because a socket timed out,
an adapter exited, a receipt was missing, or directory synchronization failed.
Do not convert an unknown dispatched action into `not_sent`. Diagnostic display
must not become a second authority for purchase, cancellation, recipient selection
or changing the route of an uncertain delivery.

## Privacy and export boundary

Initially expose only allowlisted summaries through the existing authorized
local interface. No automatic upload or external error tracker is proposed.
Do not serialize full exceptions, HTTP bodies, headers, browser snapshots,
household state, command arguments, recipe prose or complete journal records.

Any future support export requires its own explicit operator action and preview.
Construct it from allowed fields, rather than dumping state and removing familiar
secret patterns. Exclude tokens, cookies, contact/address information, merchant
and payment identifiers, recipients and free-form source text. Hashing a sensitive
low-entropy value does not make it safe to publish. Bound all collections, string
lengths and total output bytes; include omitted counts without hidden raw fields.

Correlation references are local diagnostic handles, not credentials. They must
not authorize retrieval from a publicly accessible endpoint. Do not add logging
inside critical dispatch sections that can block or raise after an external
effect without preserving the original outcome.

## Acceptance scenarios

| Case | Expected result |
|---|---|
| Local validation rejection | Stable code; no provider dispatch; original human-readable explanation retained |
| MENY closed provider failure | Existing sanitized classification preserved through CLI and MCP |
| Lost reply after dispatch | Unknown/reconcile guidance; no resend instruction inferred from timeout |
| Failure writing diagnostic output | Purchase journal and original outcome unchanged |
| Unknown future code at older client | Displayable fallback; no new write authority |
| Synthetic secrets nested in errors, receipts or metadata | None reaches the allowlisted summary/export |
| Oversized diagnostic input | Bounded output with omission indication; no raw fallback dump |
| Repeated request or restart | Same logical attempt remains correlatable without duplicate side effects |

Extend `tests/test_meny_provider_diagnostics.py`, wire-level tests and selected
provider fixtures as each code is introduced. Native clients must preserve the
existing distinction between a service rejection and uncertain transport loss.

## Decisions before implementation

Select the first codes, reference lifetime, compatibility/versioning rules and
whether summaries are computed on demand or stored. Prefer existing journals to
a new event store. Define retention only if new persistent data is necessary.
Measure support usefulness on synthetic failures before broad rollout.

Acceptance of this proposal does not implement or authorize a diagnostic export.
A later implementation PR should list exactly which codes and fields it adds,
with actual tests and any unresolved provider evidence clearly recorded.
