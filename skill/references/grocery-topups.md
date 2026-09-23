## Everyday grocery top-ups

A clear household message such as “tomt for skivet lettost” requests replenishment.
Use an exact saved product favorite when it identifies the intended variant;
otherwise search and resolve any meaningful brand/package ambiguity. Default to
one package unless the user specifies another amount. Do not create a recurring
purchase or alter the menu merely because something ran out.

Read current orders when delivery may already be booked. For one unambiguous
intended upcoming order, use orders change_begin with its exact returned ID;
clarify if more than one order fits. Oda and Mathem check current paid_and_modifiable status;
MENY checks the real enabled change controls. Never assume a fixed 20:00 or
midnight cutoff. An unavailable order read is not proof there is no order.
If changes are closed, report that the goods cannot join that delivery and
clarify the next delivery when necessary; never cancel/reorder to get around it.

For removal or reduction of goods already on an Oda or Mathem order, use `orders
remove_prepare` with its exact `order_id` and `items=[{product_id,quantity}]`.
Use one stable `idempotency_key` for this removal intent. Here quantity is the desired remaining number of packages: zero removes the
product. Do not send this to cart change: the addition cart is separate from
the paid order. Resolve the requested product from that order, preserve unrelated
goods and staged additions, and use the returned `confirmation_id` with
`remove_confirm` under the user's explicit removal request. Use `remove_reconcile`
after an uncertain response, never repeat the removal. Report the verified
remaining quantities and merchant total; do not promise a settled bank refund.
For both removals and additions, finish and verify the removal first, then prepare
the additions against the updated order. These are separate merchant changes.

Use cart ensure with exact requirements=[{product_id,product_name,quantity}].
Quantity is the desired minimum, not an increment. Existing cart quantities
count; in an Oda or Mathem order edit, already ordered quantities also count. Repeating
ensure rereads stock in the cart/order and adds only the deficit. An explicit
“one more” instead uses cart change with a positive quantity delta; never repeat
an uncertain delta. An interrupted cart write survives restart: use cart
reconcile_change to verify its saved expected result before any new write.
If still uncertain, retain the attempt and report that outcome; never retry it.
Active weekly menus allow these household extras and retain
them separately from menu ingredients. Only report success after verified reads.

A nonempty Oda or Mathem cart is preserved. change_begin returns cart_confirmation_required
with its exact contents and cart_digest. Pass that digest only if the current
request already authorizes all those goods for that exact order; otherwise ask
one destination question. Never empty or silently move unrelated goods. To end an Oda or Mathem edit while keeping
staged goods, use change_abort with retain_cart=true. Outside changes to an
Oda or Mathem addition cart require this retained-cart review before rebinding its destination.

For an existing order, additions are not delivered until checkout confirms the
change. A clear request to add goods to that order authorizes completing that
addition under standing policy; fresh policy still needs its one confirmation.
Reuse the checkout idempotency key for the same intent. If ensure finds everything
already ordered and the Oda or Mathem addition cart is empty, change_abort and report that
it is already included. MENY edits reopen the whole order, may update all prices,
and require finishing checkout and user payment approval through Vipps, the
mobile payment service used by the MENY integration. Resolve a
pending payment or uncertain change before editing; do not discard it.

Meal Concierge uses its own dedicated logged-in browser. A `/shared/browser`
session, desktop browser or general browser tool is not that session. Do not
diagnose the Meal Concierge login from another browser's logged-out page or ask
the user to log in there. Use the adapter's actual result, distinguish a missing
feature or checkout mismatch from an authentication failure, and explain the
concrete blocker without presenting integration restrictions as store policy.

For a weekly shop, retain the user's full request across follow-up messages:
adding sprouts or requesting a PDF does not cancel already requested staples.
Favorite products and recurring products live in their actual service lists;
memory alone is not persistence. Products apply includes due recurring items
once, in addition to menu quantities for shared products. Use cart `weekly` with
the current menu_ref after changes to recurring goods; do not add the same list
again as supplemental goods. Show menu goods, due staples and extras together.
Use checkout `weekly=true` for this intent, so a raw cart cannot masquerade as a
complete menu shop. Existing order edits and ordinary top-ups keep their scope.

A clear “order” after selecting a menu authorizes completing its ingredient
selection, synchronizing the menu and due goods, and proceeding under the active
checkout policy. Do not ask whether to finish the menu, order an incomplete cart,
or stop; continue the requested complete shop. Ask only for a material choice
that remains unresolved, such as a changed delivery date or the active policy's
required final confirmation. A follow-up never erases the selected menu.

When replacing dishes during an active shop, prepare/apply the replacement
menu's products as part of that request. Cart sync removes quantities attributable
to the previous menu and retains explicit extras and starting goods; do not ask
the user to identify old fish, spices and vegetables manually. A planning-only
request does not itself edit the store cart. Genuine outside cart changes still
need reconciliation, but a new menu alone is not outside drift.

For an unavailable generic recurring product, choose a suitable observed
replacement automatically when it preserves the requested food, form, quantity,
preferences and reasonable cost. Ordinary fresh pear varieties, small pears or
organic pears can replace generic fresh pears; canned pears cannot silently do
so. Record recurring `substitute` with the original product_id, this occurrence's
date and exact replacement={product_id,product_name,quantity}, then synchronize
cart weekly or apply the menu products. This replaces the unavailable item for
this shop and fulfils the original occurrence without changing the permanent
list or buying both variants. Mention the substitution briefly; ask only for a
material unresolved difference. Product evidence and checkout checks still apply.
