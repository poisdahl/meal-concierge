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
2. **Program requested:** [select the latest stable program release](runtime.md#choose-a-program-release),
   or the exact published version requested. Record its tag and full commit once
   and retain that clean checkout. Do not select `main` or a `recipes-*` release.
   If the same unmodified build is already running, verify it and report that no
   program update is needed. Otherwise run the target checkout's `check-browser`
   against the existing home before stopping.
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

Return the selected program tag and running commit, collection version if
requested, concise result counts, service/connection health and any unresolved issue. Never describe a
partial import as complete. Refresh the maintained skill for subsequent meal work;
there is no need to demonstrate meal planning during maintenance.

## Why the bank is not replaced wholesale

The installer processes an archive in program code, without model calls per
recipe. Pack entries share the bank with local recipes, edits, favorites and
other collections. Replacing the database or deleting/recreating the pack would
lose or break that state. The existing authoritative sync preserves retained
entries and removes withdrawn same-pack entries only after the complete record
pass. That deterministic work is not an agent reading and rewriting each recipe.

## Identifying the running build

From the retained checkout, inspect the selected home:

```sh
./install.sh discover --home /absolute/data-home
```

Its `installed_build` describes the staged installation. In the actual connected
agent, call `meal_concierge_status` to read the running service's `build` object;
direct service `health` and `status` reads expose the same identity. Compare
`build.source_commit` with the full commit recorded for your selected program
tag, and require `build.source_modified=false` for an unmodified release. Checking
only `git rev-parse HEAD` in a source directory does not verify the running service.

The build does not contain a release tag or a separate program version string.
Keep the tag-to-commit mapping from release selection and report that alongside
the service result. A matching runtime identity also does not prove that the
client reloaded its skill; verify the connection through the host guide.

It contains the packaged source/skill digest, dependency-file digest, pinned
Python and interface version. A Git commit is included when staging from a Git
checkout; `source_modified=true` means that commit alone does not identify the
build. Archive installations may have an unknown commit. Older releases report
`identity_status=unavailable`. These reads need no Git checkout or network lookup.

Status also warns when planning history or recipe-usage history approaches its
existing capacity. These are planning diagnostics, not a complete storage audit.
Do not delete history to silence a warning: retained menus, feedback and uncertain
external operations can depend on it. Explicit [history archival and recovery](runtime-reference.md#history-retention)
can move eligible, detached history into a private archive after a stopped-service
backup. It never trims records merely to meet the capacity limit. A new installation
or an upgrade starts conservative last-change clocks; old menu dates alone do not
establish eligibility. Ordinary maintenance does not run archival automatically.
