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
public operation and action from the shared skill and `docs/reference.md` in
the retained source checkout at the reported commit. For example,
`meal_concierge_status` becomes
`{"operation":"status"}`. Use `{"operation":"recipes","action":"import",…}`
for recipe import, `recipes.save` for a reviewed discovery, `menu.get` for the
saved menu, and `shopping_list.get` for its saved shopping list. Pass exact
references, operation-specific idempotency keys, and returned continuation
arguments as instructed by the shared skill. Generate a canonical UUID and
retain it before calling. A new UUID is for a new command, not for repeating
an uncertain one. Treat errors and incomplete receipts as recovery steps.

For an attachment, prepare the actual managed delivery and export the exact
`recipe_delivery.read` `job_id` and `part_id` using `call REQUEST_UUID
--delivery-output ABSOLUTE_NEW_FILE`. For a managed recipe image, use
`recipes.cover_get` with its exact `recipe_ref` or `discovery_ref` and
`--image-output ABSOLUTE_NEW_FILE`. Use the native Library and chat attachment
tools to deliver the verified file to the requested conversation. Only record
delivery when there is evidence from the native sender. A locally exported
file is not delivery evidence. Use the existing PDF/photo input workflow to
read supplied attachments on the cloud computer before passing a transcript.

For a plan-only native schedule, name the intended household, task and chat in
the schedule. Reuse the same installed paths on its later turn. Check the
destination for the actual result. The foreground runner works with a
store-free household. For the separate native MENY interface, follow its
account binding and original receipt recovery in the Dots guide for this
commit.
