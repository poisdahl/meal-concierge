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
ChatGPT authorization for that original entry. The owner completes checkout
with the Oda cart link.

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
commit.
