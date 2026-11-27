# ChatGPT Dots foreground tools

Use the exact private household home and its installed `current` program path
from setup. Run the commands on the Dot’s cloud computer. If the household or
provider is ambiguous, find the intended binding before invoking a tool.

```sh
"$program/venv/bin/python" -I -B "$program/foreground.py" --home "$household" tools
"$program/venv/bin/python" -I -B "$program/foreground.py" --home "$household" call REQUEST_UUID < request.json
"$program/venv/bin/python" -I -B "$program/foreground.py" --home "$household" receipt REQUEST_UUID
```

Discover the household, build and installed skill with `tools`. Use the
operations it lists. Take the action and arguments from the shared skill and `docs/reference.md` in
the retained source checkout at the reported commit. For example,
`meal_concierge_status` becomes
`{"operation":"status"}`. Use `{"operation":"recipes","action":"import",…}`
for recipe import, `recipes.save` for a reviewed discovery, `menu.get` for the
saved menu, and `shopping_list.get` for its saved shopping list. Pass exact
references, operation-specific idempotency keys, and returned continuation
arguments as instructed by the shared skill. Generate a canonical UUID and
retain it before calling. A new UUID is for a new command, not for repeating
an uncertain one. Treat errors and incomplete receipts as recovery steps.

When discovery lists the public MENY catalog, use `catalog.products` or
`catalog.recipes` with `provider:"meny"` and a search query. Use
`catalog.recipe_get` with the exact `recipe_id` from search. Present prices as
public catalog figures. Use a separately verified signed-in store connection
for stock, cart actions and checkout.

Use native MENY `orders.get` with the exact order number for a current or
archived receipt. For a current receipt, use the signed-in canonical path and
the displayed `Ordrenummer`. Read the dedicated status and the expanded
goods. An affirmative cancellation can verify the displayed status. An absent
marker leaves it unknown. A fresh observation uses a new command UUID.

For current MENY cart or order reads while an original write is pending, use
`clients/dots_session.py observe` as documented in `docs/dots.md`. Work in a
separate tab on the same verified cloud browser and account. Follow the emitted
read frames and read the first displayed receipt only if requested. Present
the results as current rendered observations. Preserve the original pending
operation and its disabled write policy.

An ordinary native MENY `ensure` or `clear` frame can advertise a pre-click
stop result. Return it only for the original live frame when no click has
been issued and none remains queued or in flight. Send it before expiry.
Keep the original journal when a click might have happened or the reply is lost.

Use `reset-legacy-cart` only after the household owner explicitly approves
archiving that exact eligible MENY attempt with its historical outcome unknown.
Verify that the earlier reply window expired, the executor stopped and its old
tab was retired. The reset flags record your attestation of those checks and
the owner's approval. Follow the fresh same-account empty-cart checks in
`docs/dots.md`, retain the original receipts, and leave writes disabled.

When the owner approves archiving an exact failed removal after a separately
restored single-unit MENY test cart, follow `cart.accept_current` in the Dots
guide. Verify the retired frame and fresh empty cart in the active bound tab.
Record the historical outcome as unknown and keep the original records.

When discovery returns `provider:"oda"` and `transport:"hosted_mcp"`, bind
this installation to its reported official Oda plugin. Use the discovered
cart, catalog, delivery and order read actions documented in `docs/dots.md`
in the retained source checkout. Start with `call` and the exact read request.
Invoke only the emitted tool in the attached plugin. Run `complete-hosted
REQUEST_UUID` with the structured reply on stdin. Treat the response and its
observation time as host-reported. The runtime checks the plugin entry and the
request binding. The owner confirms the underlying Oda account when authorizing
it. Backend freshness remains unverified. A receipt is historical. Use a new ID
for a fresh read after expiry. If the plugin is unavailable, use the private
ChatGPT authorization for that original entry. Checkout uses the Dot's
signed-in Oda cloud browser.

For delivery selection, follow the Oda reservation workflow in the retained
source’s `docs/dots.md`. Review one exact date, window and listed fee. Wait
for the owner’s explicit reservation approval before `approve_delivery`.
Only a fresh one-shot `dispatch: true` grants the actual reservation. Finish
the independent readback in the original account. A historical or uncertain
receipt grants no new store action.

Use `begin_saved_card_checkout` when the owner wants the Dot to submit a new
Oda saved-card order. Follow the guide's exact payable review, owner approval,
fresh checks and one-time dispatch. Hand off bank approval privately. Use
`begin_checkout` with `owner_handoff: true` when the owner will complete a
saved-card or Vipps purchase. Resolve the original attempt before another
checkout. A historical receipt grants no action. Use
`orders.change_handoff` for an exact eligible order and let the owner
complete the change using its verified receipt. Check a fresh merchant
receipt before reporting any completed action.

For saved-menu Oda shopping, use the discovered `menu_shopping_command`
and the saved-menu workflow in the retained source’s `docs/dots.md`.
Use `shopping_list.get` in the original planning home. Resolve or review
its stock rows, select exact Oda matches for measured requirements, and
show the packages already in the cart, the proposed additions, merchandise
prices and named unanswered stock rows. Use only the owner's explicit reply to
that quote to approve a cart write. Continue in the separate Oda home with
`preserve_existing:true` on the same authorized unreserved cart. Revalidate
the baseline and prices with the emitted reads before applying the shortfall.
Use `menu_shopping_restore` only at the owner's request, with the exact latest
verified application and a fresh matching cart. After `verify_remove`, complete
the independent cart read before checking the final restoration flags. Keep
writes out of scheduled planning.

When the owner explicitly designates the connected active cart for one
reversible product test and discovery shows `cart_write_command`, use the
same UUID and the one-item test workflow in the retained source’s `docs/dots.md`.
Use the original attached mutation response and independent cart reads
before each next stage. Stop on an unknown ending. Keep test writes out of
scheduled planning.

For a saved-menu delivery, prepare the actual job and export its exact frozen part
to a new file in a private directory. Pass the
`recipe_delivery.read` `job_id` and `part_id` using `call REQUEST_UUID
--delivery-output ABSOLUTE_NEW_FILE`. Match the job, part, digest and requested
conversation. Start the original part with `recipe_delivery.begin` immediately
before one native attachment send, with a fresh `dispatch:true`. Inspect the
actual file card and acknowledge `accepted` only with evidence of the accepted
frozen bytes, such as the retrieved original file’s verified digest. If staging
reveals a byte rewrite, stop that exact-file send. If a rewritten card already
posted, report what opened and acknowledge `unknown` for the original token;
reconcile that same attempt before any retry. See the
[native delivery contract](recipe-delivery.md#native-chat-or-an-unmanaged-supported-sender).

For a standalone recipe image, use
`recipes.cover_get` with its exact `recipe_ref` or `discovery_ref` and
`--image-output ABSOLUTE_NEW_FILE`, attach it to the requested conversation,
and verify the card with its attribution. Use the existing PDF/photo input workflow to
read supplied attachments on the cloud computer before passing a transcript.

For a plan-only native schedule, name the planning household, task and chat in
the schedule. Reuse the same installed paths on its later turn. Check the
destination for the actual result. For the separate native MENY interface, follow its
account binding and original receipt recovery in the Dots guide for this
commit. Review an exact calendar window and obtain reservation approval before
`delivery.select`. Use the verified MENY tab for `checkout.prepare` with Vipps.
For an addition to an existing MENY order, use `orders.change_begin`,
`change_review`, `change_approve` and `change_reconcile` as documented in the
[MENY item edit protocol](../../docs/dots.md#review-and-verify-an-item-addition-to-a-meny-order).
The owner reviews and submits the exact update in the private shop handoff.
Use `orders.change_handoff` for eligibility and other order changes.
