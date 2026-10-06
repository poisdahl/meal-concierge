# Proposal: evaluate the existing first-plan experience

Status: experiment design only. No demo mode, fake store connection, installer
change, additional telemetry or revised household defaults are implemented.

## Hypothesis and current capability

The product already supports saved local recipes without store-account access,
editable setup defaults and optional automation/delivery. The current guides
describe those choices. A first-plan experience might be easier if a prospective
user can evaluate it before completing retailer setup, but no onboarding funnel,
abandonment rate or user demand was measured in this review.

Do not assume a new demo mode is the answer. First measure the existing journey,
using the current isolated household fixtures and conversation probe. Separate
installation/browser prerequisites from recipe planning and from authorization
to shop; account readiness is not payment readiness.

## Experiment boundaries

Use a fresh synthetic household, owned synthetic recipes and an unavailable
provider that rejects every shopping operation. The existing authenticated
native-agent probe needs a deliberately configured test client; run it only in
that controlled environment. It can consume that client's normal model resources.
Do not attach it to a real household or reuse browser profiles, credentials,
recipients, scheduler bindings or a live retailer cart.

Exercise a baseline first, then a candidate presentation using the same fixtures,
client/model/version and task. Record the host's tool discovery/loading behavior
as observed or unknown. A complete MCP catalog measurement does not show what
the model received; modern hosts may defer tools until needed.

## Journey prompts and outcome checks

1. Ask for two dinners for two using an available ingredient, with no shopping.
   The saved plan must have the requested dates and portions, real fixture recipe
   references, appropriate source attribution and no retailer/cart/payment calls.
2. Request one faster replacement and change portions for guests. The requested
   slots change, while household defaults and unrelated slots remain unchanged.
3. Ask for the recipes. Return usable ingredients and steps in the requested
   supported form; never claim an unsupported attachment or email was delivered.
4. Ask what is needed to shop later. Explain real missing prerequisites without
   creating a cart, payment, delivery job, login session or recurring schedule.
5. Resume in a fresh conversation. Read and describe the saved plan rather than
   recreating recipes, resetting setup, or initiating purchases.

The acceptance oracle checks saved outcomes and provider call traces. A short,
fluent conversation that silently omitted a restriction or failed to save is
not a successful journey.

## Measurements

Record task completion, fidelity to dates/portions/restrictions, unauthorized
effect count (must be zero), number of avoidable clarification turns, actual
tool calls, wall-clock time and whether the user could identify the next action.
Record disagreements about what counts as an avoidable question. Asking about
an actual allergy or missing authorization must not be penalized for efficiency.

Use multiple runs for native models and report individual outcomes plus summary
distributions. Host/model/loading policy, warm/cold sessions and available tools
must be recorded when known. Missing context/token telemetry remains unknown.
Do not compare raw latency across different hosts as if it measures product UX.
Do not commit private transcripts or account information; synthetic summaries
should contain only the facts needed to assess the experiment.

## Candidate change and go/no-go decision

Start with a small guide or first-response improvement using existing behavior.
Present the saved meal plan, dates/portions, source and next available action.
Explain optional delivery and automation when relevant without turning a
planning-only request into a setup survey or shopping mandate.

Set the success thresholds with the maintainer before collecting comparative
results. Require preserved task correctness and zero authorization regressions;
seek a repeatable improvement in completion or unnecessary interaction. If the
baseline is already clear or results are inconclusive, retain current behavior
and record the result. The experiment does not require shipping a demo.

Only if the evidence supports a separate demo should a follow-up design specify
isolated disposable data, clearly labeled synthetic recipes/results, no live
effects, cleanup, and the explicit transition to real setup. A demo must never
report retailer, account, stock, price or payment readiness based on fixtures.

## Validation and publication limits

This proposal is source-reviewed against the user guide and existing synthetic
conversation probe. It does not claim a native-agent/user study was run. Before
an implementation is accepted, attach the agreed procedure, reproducible fixture
identity, actual results, failures and limitations. Keep model-dependent runs
separate from deterministic CI; do not introduce a flaky performance gate.

Merge records the experiment protocol only. It changes no user-facing setup
behavior and is not evidence that onboarding needs a new product mode.
