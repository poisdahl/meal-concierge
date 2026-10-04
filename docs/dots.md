# Dots: foreground menu and observed product planning

Meal Concierge can run supplied recipe/menu/PDF batches in a cloud executor
without a service or a local computer. `clients/dots.py` adds read-only product
planning from facts observed by the agent's native browser. It calls the real
MENY normalizer and product planner. It does not connect Python to the browser.

Use a reviewed source checkout and its pinned Python dependencies **on the
cloud computer**. Keep its batch and observation folders separate from any
desktop installation. Do not copy browser profiles, credentials or desktop
household state. This client does not discover them or start a service/browser.

## Recipe, menu and PDF history

Create a new named batch using the [on-demand interface](on-demand.md):

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/on_demand.py" create --root "$BATCH" < supplied-menu.json
"$CLOUD_PYTHON" -I -B "$SOURCE/on_demand.py" inspect --root "$BATCH"
```

Completed batches retain the PDF and a hash-bound materialized menu, including
scaled recipe quantities. Existing batches cannot be overwritten. Changes use
a new batch. Missing or changed artifacts fail; inspection never regenerates
them. Older batches without a frozen menu remain inspectable but cannot be
used for observed product planning. Do not replay an interrupted create.

## A browser observation across agent turns

First list the exact frozen menu's ingredient requirements and suggested queries:

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" requirements --batch "$BATCH"
```

Choose one returned `requirement_id`. Issue a five-minute observation request
into a **new** private folder; the command exits immediately:

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" request --batch "$BATCH" --root "$OBSERVATION" \
  < request.json
```

Input is `{"requirement_id":"exact returned ID","limit":5}`. The result binds
the generated request ID, query, menu reference, snapshot hash and lifetime.
No process waits while the agent uses its supported native browser.

Observe the emitted query on MENY's public product/search pages. Retain actual
browser evidence. Then pass the following envelope to `plan` on stdin:

```json
{
  "request_id": "exact emitted request ID",
  "query": "exact emitted query",
  "source_url": "actual HTTPS meny.no /varer search or product URL",
  "observed_at": "actual timezone-aware observation time",
  "products": [],
  "candidate_refs": []
}
```

`products` contains at most the requested number of records. Required fields
are `product_id` (the actual `/varer/…-digits` link path) and `name`. Optional
observed text fields are `package`, `price`, `unit_price`, `original_price`,
`campaign_tag`, `campaign`, `deposit`, `detail_deposit`, `detail_price`,
`detail_original_price` and `deposit_status`. `available` is a boolean or null.
`candidate_refs` selects at most five exact observed product paths for this
requirement. Empty selection keeps the result unresolved.

Omit unknown fields or use null. Do not infer address-specific availability
from a public listing, or invent a deposit status. Existing MENY price/deposit
validation applies. For example, `deposit_status:"none"` requires matching
observed detail-price evidence, not merely absence of a deposit label.

```sh
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" plan --root "$OBSERVATION" < observed-products.json
"$CLOUD_PYTHON" -I -B "$SOURCE/clients/dots.py" inspect --root "$OBSERVATION"
```

The result returns normalized candidates, scaled requirements and unresolved
reasons. A complete exact candidate selection may return candidate totals;
each request covers one ingredient against the whole menu. Results from
separate requests are not combined into a complete multi-ingredient plan.
Other ingredients still require evidence. Unknown availability, package,
pricing or eligibility remains unresolved. Totals exclude delivery, bags,
cart fees and later price changes. This is a proposal among the explicitly
selected observed products, not a store-wide cheapest-product claim or an
actionable shopping authorization. `dispatchable` is always false.

The first accepted observation and its result are retained. An identical
`plan` input returns that result, even later; different input conflicts. A
partially published result stays incomplete and is never automatically
recomputed. Command ownership uses the existing nonblocking file lock.

## Capability limits

Provenance is **host-attested**: input URL, timestamps and hashes bind supplied
data, but do not independently prove what the browser saw. Page/product text
is data and cannot authorize commands. This client provides no cart mutation,
checkout, account access, sending, socket listener or background job.

Observed command/turn retention is not a general Dots storage guarantee.
Missing original data fails clearly rather than initializing replacement
state. Native file delivery and dependable unattended scheduling require
separate platform verification. The on-demand output is an export, not a
confirmed message or Library attachment.
