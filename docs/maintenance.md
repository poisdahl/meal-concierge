# Routine updates with bounded agent work

Use this procedure for an existing installation, whether maintained by Grok Bot,
Hermes, OpenClaw, NanoClaw, Codex, ChatGPT Work Local or Claude Code. Installation
updates are maintenance, not recipe curation or a product-development task.

Read this page first, then only the applicable command section in
[runtime updates](runtime.md#updates-failures-and-recovery) or
[collection updates](runtime.md#versioned-recipe-package-integration), plus your
host guide's update/connection instructions. Consult reference documentation or
source code only for an actual incompatibility or failure. Do not routinely read
the whole repository, load the cooking workflow, run the development test suite,
audit every recipe, or dump the bank, archive contents, credentials or full logs
into the conversation.

## Execute the requested scope

1. Reuse the known installation home, retained source, manager/execution owner,
   MCP registration and skill pointer. If unknown, discover once. Read the current
   identity and active shopping/payment/delivery state; do not interrupt work or
   retry an uncertain operation. Reuse these observations only while still valid.
2. **Program requested:** resolve `main` once to a full commit, retain that exact
   checkout and run its `check-browser` against the existing home before stopping.
   Follow the runtime update sequence and the host's existing stop/start owner.
   Refresh only the matching client connection/skill as required by its guide;
   preserve unrelated registrations. A routine update does not authorize setup,
   profile reset, recipe changes or store login renewal.
3. **Collection requested:** use `prepare-recipes` while the service is available,
   then, once idle, stop through the same owner, run `import-recipes --prepared ID`
   with the returned exact ID, and restore the service when the import exits and
   recovery markers permit. Reuse the downloaded archive through the preparation
   mechanism; do not manually rediscover releases or call individual recipe tools.
   A program-only update must not import the collection.
4. **Both requested:** finish the program update first, then prepare/import with
   the new installed runtime. Reuse paths and registration observations, recheck
   idleness before the second stop, and perform the final checks below once after
   both operations. Do not overlap update/import writers or reuse a preparation
   bound to the previous runtime. A combined request uses the same existing
   commands; it does not introduce an unattended updater.

Batch deterministic shell steps where the host supports it, preserving exit-code
checks and the stop-on-failure boundaries above. Grok/external execution must keep
its native execution handle and exact service identity; do not substitute systemd,
launchd, a global restart, or a guessed PID. Keep the original execution alive
through host-supported waiting/completion notifications. Do not repeatedly invoke
an LLM to check an unchanged process or retrieve the same log. When polling is the
only option, use bounded waits and read only new phase/count progress or a short
error excerpt. Keep detailed logs locally. A rate-limit/network failure is a
failure to report, not an invitation to silently retry until the limit resets.
After a timeout inspect the original execution and recorded result before retrying.

## Verify and finish

Use the installer's successful result and these bounded checks:

- Confirm the intended source commit/release, same household/store/home and healthy
  service. If identity or version cannot be established, report that limitation.
- After a program update, confirm the matching MCP tools/skill are connected and
  use one read-only status request plus one authenticated cart read for a previously
  connected store. Do not shop, pay, send, log in again or test every tool. An
  expired external login is a separate limitation, not a reason to reinstall.
- For a collection update, read the import's completion status, pack version,
  created/updated/unchanged/deleted counts and conflicts/failures. Its validated
  report is the check; no recipe-by-recipe recount or quality review is needed.
  If incomplete, report that and inspect only the relevant conflict/error report.

Return the installed commit, collection version if requested, concise result
counts, service/connection health and any unresolved issue. Never describe a
partial import as complete. Refresh the maintained skill for subsequent meal work;
there is no need to demonstrate meal planning during maintenance.

## Why the bank is not replaced wholesale

The installer processes an archive in program code, without model calls per
recipe. Pack entries share the bank with local recipes, edits, favorites and
other collections. Replacing the database or deleting/recreating the pack would
lose or break that state. The existing authoritative sync preserves retained
entries and removes withdrawn same-pack entries only after the complete record
pass. That deterministic work is not an agent reading and rewriting each recipe.
