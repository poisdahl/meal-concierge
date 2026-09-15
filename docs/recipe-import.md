# Add and update recipes

You can plan with recipes from your selected, connected store straight away.
A local recipe collection is optional. Use the local bank to keep your own
recipes, favorites and imported collections together.

## Save a recipe

Give the agent a recipe link, paste the text or attach a supported file:

> Read this recipe, show me the ingredients and portions, then save it when I confirm.

Check the preview before saving. Before importing text or a URL, the agent must
record a concrete basis for storing the full recipe privately: your own recipe,
permission, a verified license or an applicable private-use assessment. Public
access or a source's presence in search settings is not permission. If the
basis is unresolved, a URL can be saved as a link only, without fetching its
contents; that bookmark is not a shopping-ready recipe.

A full preview persists a private technical snapshot but does not create a
personal bank entry. Menus, orders and recipe emails can also retain recipe
content. This is not a no-storage mode or permission to redistribute recipes.
Missing amounts, unclear servings and unsupported fields are reported;
they are not silently invented. You can keep an incomplete recipe as a draft
and fill in what is missing later.

Recipes saved through Meal Concierge go into its local bank. Source links and
credits are retained. Changes to an original webpage do not silently replace
your saved version. Reimporting the same source can produce a conflict for you
to review instead of overwriting your local edits.

## Web recipes in weekly menus

Automatic recipe discovery defaults to MatPrat, Vegetarentusiast, Frukt.no,
Godfisk, TINE Kjøkken, Godt and Trines Matblogg. You can disable individual
domains, replace the list or turn web discovery off. Searching the wider web is
off by default and can be enabled separately. Disabled domains remain excluded
even in broad search. These are search preferences, not licensed integrations.

The agent uses its available web-search tool and submits selected, assessed
recipes to the normal menu planner alongside local and store recipes. Search
or page failures are reported; they do not disable local/store planning. Exact
portions and ingredients must come from the recipe, not a search snippet.
User-supplied recipe links and text still work with automatic web search off.

## Sources and files

| Source | How to use it |
|---|---|
| Oda, Mathem or MENY | Ask for recipes from your selected, connected store. No offline pack is needed. |
| Recipe webpage | Send the link. The agent reads supported recipe data or visible text; inaccessible or incomplete pages may need pasted text. |
| Your own text | Paste ingredients, portions and steps. |
| Photo or PDF | Attach it to an agent that can read the file. Review extracted quantities and all relevant pages. |
| Mealie | Import through a configured connection or a supported recipe JSON/recipe-export ZIP. This is not a full account restore. |
| RecipeSage | Import through a configured connection or its supported JSON-LD export. |

Mealie and RecipeSage are optional import sources. You do not need either service
to save or use recipes. Ask the installing agent to configure a connection if
you want to import from your account; enter credentials through its private
setup, never in chat. New saves and edits stay in Meal Concierge rather than
writing back to those services.

Photos and covers depend on available source images and your agent's attachment
support. A link to an image does not mean the image was downloaded. The agent
should tell you what was included and what could not be imported.

## Add or update the recipe collection

The **Optional Recipe Collection** adds recipes to the local bank for use without
fetching their source pages. To **install it or update an existing copy**, send:

> Synchronize the latest Optional Recipe Collection into my existing Meal
> Concierge installation. Permanently remove collection recipes that are no
> longer included. Preserve every other local recipe and favorite. Report the
> imported version and any conflicts or incomplete results.

The agent uses `import-recipes` to select the newest published stable collection
and verify its checksum and size. It waits for active work to finish, stops the
service for import, then starts it again. You do not have to find a release
number or download the file yourself.

**Updating the program does not update the collection.** If you have an older
installation, update the program first and then request the collection separately.
This includes installations with the old `2026-09-06.5` collection.

Repeated imports do not create another copy of unchanged collection entries.
Recipes that remain can receive the newer publisher version; their local edits,
favorites and archived status are preserved when conflicts require your review.
After the complete new collection has been read, entries withdrawn by the
publisher are permanently deleted. This includes local changes and the favorite
on that exact withdrawn entry. Your own recipes, entries from other collections
and all of their favorites remain untouched. An invalid or interrupted import
does not delete entries merely because they have not yet been seen; absent-entry
cleanup starts only after the complete record pass.

For the exact command and recovery steps, see
[manual collection import](runtime.md#versioned-recipe-package-integration).
A local ZIP supplied to that command must match the latest published release
and still needs internet access for verification.

## PDF attachments without system packages

The Codex and Claude packages include a local fallback reader for PDFs when the
client cannot read them directly. A current service and rebuilt client package
are needed; replacing only the plugin cannot add dependencies to an old service.
The agent needs permission to run the reader and view the generated page images.

Other agents depend on their own PDF/image tools; NanoClaw's attachment template
does not include this fallback reader. Large or scanned files may need page-by-page
reading. Ask the agent to identify unread pages or uncertain text before saving.
See the [agent PDF workflow](recipe-import-reference.md#pdf-attachments-without-system-packages)
if the installer needs to diagnose an import.

## Preserve or move your recipes

Use an [installation backup](runtime.md#complete-private-data-backup-and-relocated-restore)
to preserve recipes, revisions, menus and managed images together. A recipe
export is not a backup of store login, orders or your whole agent installation.
Ask the agent to inspect existing data before importing a backup or moving hosts.

Installation agents can find the exact source formats and extraction rules in
the [recipe import technical reference](recipe-import-reference.md).
