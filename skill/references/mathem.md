# Mathem operations

For Mathem, amounts are SEK. Product/recipe search, carts, delivery selection and
order reads use its MCP. With a configured dedicated Mathem browser, checkout
verifies the same selected account/address, products, delivery, final fee rows
and selected saved card before preparing or submitting. Use the returned
confirmation policy and exact confirmation/idempotency key. If prepare returns
`manual_checkout_required`, show its summary and store URL; never treat that
handoff as a submitted order. For additions, use `orders change_begin` on the
exact modifiable order before staging goods. Checkout inherits its receipt
address/delivery and reviews the original, added and combined amounts in SEK.
Cancellation uses its own fresh exact-order review. Pass both its exact
`order_id` and `confirmation_id` to `orders cancel_confirm`; an uncertain result
uses `cancel_reconcile` with that same confirmation ID. For a delivery change, begin
the exact order edit with `delivery_only=true` and an empty addition cart, select the requested available
window, then prepare its fresh original/final-total review under the shared
rule below. Unavailable merchant controls or unverified full totals require a
manual handoff. Weekly auto-checkout requires the same configured browser,
standing/fresh policy, dietary permissions and delivery guards. `maximum_total`
is an optional budget policy, not a prerequisite; enforce it when configured.
A missing or changed prerequisite stops the attempt. Confirm purchase only when
its bound submit/reconcile returns `confirmed=true`; Mathem receipt reconciliation
also checks the exact order's address in the browser because MCP omits it.
`confirmed` establishes the matched accepted order. Report `payment` separately:
merchant tracking status alone does not establish bank authorization or a settled
charge. Unknown remains unknown on replay. A confirmed cancellation likewise
does not establish release of a card reservation or a refund; show the returned
`payment_resolution` limits.
