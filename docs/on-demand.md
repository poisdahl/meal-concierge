# On-demand recipe planning and PDF export

Turn supplied recipes into a dated menu and one PDF with a single command. This
entry uses Meal Concierge's recipe validation, portion scaling and PDF renderer
without starting the household service. It creates a separate private batch and
uses default household preferences; it does not confirm dietary needs.

The host needs Python 3.12.12, the pinned dependencies in
`runtime-requirements.txt`, ordinary process execution and a writable filesystem
with Unix file locking. Use the repository's existing environment; do not point
the command at an installed household's state. No browser, store account or
network connection is used during the batch. Dependency installation is separate.

## Create a menu

Save a JSON document like this as `menu.json`. Source and rights metadata describe
the supplied content; include accurate attribution for your own recipes.

```json
{
  "request_id": "carrot-menu-1",
  "household": "My menu",
  "recipes": [{
    "key": "soup",
    "recipe": {
      "name": "Carrot soup",
      "portions": 2,
      "ingredients": [
        {"raw": "200 g carrot", "item": "carrot", "quantity": 200, "unit": "g", "scalable": true},
        {"raw": "1 l water", "item": "water", "quantity": 1, "unit": "l", "scalable": true}
      ],
      "steps": ["Chop the carrots.", "Simmer in the water."],
      "source": {"kind": "user", "publisher": "My kitchen", "relationship": "user_supplied"},
      "rights": {"storage": "full", "credit": "Recipe: my kitchen"}
    }
  }],
  "menu": [{"recipe": "soup", "date": "2026-10-05", "portions": 6}]
}
```

Run from the product checkout using its prepared Python environment:

```sh
python -I -B on_demand.py create --root /your/private/output/carrot-menu-1 < menu.json
```

The parent output directory must already exist. The batch root must not exist;
the command creates it with mode `0700`. Use your host's ordinary execution
timeout, recommended **90 seconds**, and preserve the root if the process is
interrupted. The command does not start a daemon or install a supervisor.

On success, JSON stdout contains `ok: true`, `status: completed`, an exact saved
menu reference, build identity (or `identity_status: unavailable` for an
unpackaged checkout), warnings, and the PDF path, byte count and SHA-256.
`export.pdf` is frozen once. Its `sent: false` means no delivery occurred.

Optional covers are supplied local files. Add `"cover": "soup.png"` beside an
entry's `key` and `recipe`, and supply `recipe.image` with `alt`, `creator`,
`credit` and `license`. Pass `--input-directory /your/supplied/images`.
The command imports and sanitizes the confined image, then assigns its managed
asset ID. Do not supply an asset ID from another installation. URLs in source
attribution remain text/links; they are never fetched.

## Inspect after completion or interruption

```sh
python -I -B on_demand.py inspect --root /your/private/output/carrot-menu-1
```

Inspection reads only the fixed batch result and PDF, verifies the frozen size
and checksum, and does not initialize the application, change state or regenerate
content. It works without the original input images. Exit status is `0` for a
verified completed export, `2` for an incomplete batch, and `1` for an error
(including changed or missing completed output).

An interrupted batch can contain saved recipes or a PDF without a committed
completion result. Those files are preserved and remain incomplete. Creating
again at that root refuses; there is no automatic replay or rollback across the
recipe database and household state. Keep the original evidence and inspect it.
A deliberately new planning request must use a new root.

## Limits and host integration

- Input: one JSON document, at most 2 MiB; 1–14 recipes and 1–14 menu entries
  within one ISO week. Each menu entry references a different supplied recipe,
  with an integer portion count from 1 to 100. Existing product validation may
  impose stricter content limits. Arbitrary operation/capability fields fail.
- Covers: at most one per recipe, 12 MiB per supplied file and 24 MiB total;
  existing image dimension/pixel limits and local-file containment apply.
- PDF: at most 32 MiB; result metadata: at most 64 KiB. Generation failure is an
  error, not a successful PDF with a silent text fallback.
- State: one fresh owned batch root and one writer holding the normal state
  ownership locks. Existing installations and configurations are not adopted.
- Capabilities: supplied recipes, a dated menu and export only. Grocery actions,
  external recipe discovery, email, scheduling and native sending are unavailable.

A host integration must demonstrate supported command/file execution, bounded
termination and a way for the user to retrieve the actual PDF. A path visible
only to the agent does not establish user access. Export creates no delivery job,
capability, destination, token or receipt. Any later sending must use a separately
authorized real transport and reconcile uncertain outcomes before another send.
Storage retention across future sessions and compatibility with a particular
cloud agent require separate verification; this command makes neither promise.
