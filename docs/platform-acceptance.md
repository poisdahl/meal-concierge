# Platform acceptance — 8 September 2026

This is the final MC10 aggregation of the resumed client tasks. It distinguishes
published source from the source actually exercised by each native client.
**Issue [#52](https://github.com/poisdahl/meal-concierge/issues/52) remains open**
for the explicit unverified gates below. MC01–08 remain complete within their
recorded scope; the additional Desktop and scheduler checks do not certify every
host, sender, provider or physical-sleep scenario.

## Source and release

The selected public checkout is `20bbfc47a0e2995d9031fca5b27e5f45237c9025`.
Its runtime and tests are byte-identical to #50's accepted final runtime
`b8cc15f3cbaa54fc86d14d1c921e69da6c423503`; only four documentation files differ.
The reviewed #50 public suite accounted for 1,262 tests with nine skips, and its final
installed native Hermes/Mathem conversation and source-parity checks passed.
Three Grok probe tests requiring exactly Python3.12.12 were rerun successfully
in a local standard pinned venv after the initial Python3.12.13 run. This is a
composite result, not an uninterrupted single-environment run. The separate
date-sensitive pantry test correction was already published and verified.

Relative to the earlier Desktop `e36815fd` installation, changes concern Mathem
product evidence and package descriptions, compact digest-bound product apply,
existing-order additions/delivery/cancellation and their MCP/skill instructions.
The installer, recipe schemas, managed assets, recipe pack and menu core are
unchanged. Their accepted shared tests are reused; new native Desktop evidence
exercises the affected installation/skill-update boundary and preserved menu.
No runtime implementation was added by this aggregation.

The immutable recipe release remains `recipes-2026-09-06.5`: 4,599 recipes
(3,807 Wikibooks and 792 TheMealDB), 1,570 JPEG files and 1,580 cover references.
Its actual default HTTPS acquisition, native macOS/Linux installation, retained
bank upgrade, migration/conflict handling, exact scaling and recovery results
are reused. It is not rebuilt. Original pending-effect backup/restore,
provider-binding and historical recipe/menu-reference checks remain evidence
from their actual synthetic scenarios, not proof of a new live payment outcome.

## Actual client and host matrix

| Surface | Actually exercised source and host | Accepted result and boundary |
|---|---|---|
| Codex CLI 0.153.4 | MC05 public `4f7a027`, macOS; #54 repo-URL install `3982f62`, macOS 26.6.2/launchd | Original text/photo/PDF imports, explicit drafts, seven dated dinners for two, saved refs/images and subprocess restart; separately, fresh/repeat install, second-client attachment and native manager lifecycle. Host CLI cover upload and native observability qualifications remain. This is the named CLI, not every Codex surface. |
| Claude Code CLI 2.1.241 | MC05 private `058aabe4`/public runtime `b58618e`; #54 `3982f62`, macOS 26.6.2/launchd | Original imports, saved seven-dinner menu, exact image/ref readback after subprocess restart; separately repo-URL install/reuse/cross-client and native manager checks. Session-local scheduling is not durable weekly scheduling. |
| Claude Desktop 1.46388.4, embedded Code 2.1.260 | Original import/PDF fallback packages; #54 `e36815fd`, macOS 26.6.2 Apple Silicon/launchd; MC10 current-source upgrade below | Original native attachment/image tests and #54 fresh/repeat install, full app restart and separate service restart are reused. New menu, local image presentation and upgrade readback are recorded below. |
| Hermes/Mathem | #50 final runtime `b8cc15f3`, actual installed Linux services and ordinary Hermes conversation | Two batch sources plus one fresh dinner, real cart and one systemd-owned checkout occurrence; recovery-assisted addition, UI-prepared/native-confirmed free delivery change, and native cancellation with independent terminal verification. Bank charge/refund and older slot-reservation release remain unknown. See [provider acceptance](acceptance.md). |
| OpenClaw | Recorded MC07 native client/synthetic provider/local sender; scheduler component from OpenClaw 2026.9.2 `3928bad9` on macOS | Accepted native workflow/local sender remains qualified. New real-clock CronService/process/store tests use synthetic local callbacks, not a complete current Gateway/model/sender installation. |
| NanoClaw | Recorded MC08 native isolated container/synthetic provider/local sender; NanoClaw 2.3.0 `b76fcb3d` SQLite/scheduler components | Accepted workflow is recovery-assisted; image presentation required a human follow-up. New pause/resume/recurrence persistence uses native components and synthetic completion acknowledgment, not a complete current container/model/sender run. |
| Grok Bot 0.44.0 | Historical guided canonical install `9599097`; synthetic `8333ae16`; Oda `7ad6e5a`, 35 pins/26 tools | Original text/photo/three-page PDF imports, pooled seven-dinner week, external service restart/readback and synthetic lost-write reconciliation passed within the recorded scope. Guided browser/OAuth/catalog/cart reads and bounded native routine results are separate. These frozen results do not certify current source or unattended installation; current cloud retention is not asserted. See [Grok evidence](grok.md). |

[Client installation](client-install.md#verified-installation-lifecycle),
[original CLI/Desktop workflows](../clients/README.md#client-contract-and-checks)
and [scheduler acceptance](scheduler-acceptance.md) retain the detailed versions,
failed attempts, recovery paths and observational limits.

## New Desktop menu and upgrade acceptance

The retained #54 household was intentionally unauthenticated Mathem, with no
pending checkout, cancellation, order change, email job or occurrence. Native
Desktop loaded its registered skill, planned without a supplied shortlist,
saved once and read back seven distinct dinners for 14–20September at two
portions each. All selected recipes came from the installed bank, with exact
recipe revisions, source credits and unresolved quantity/nutrition/safety facts.
The unchanged enabled Mathem source attempted discovery but stopped locally at
missing login and contributed no recipe; this is not a sources-disabled test.

The operator opened the actual Bunny Chow `recipe_image` result through
Desktop's **View screenshot**. Its 32,546-byte managed JPEG matched
`6eaecf38a02ac442d68151e64711e091c329d8a1421a9e44c3ff71ef79a889dd`.
Luke Comins/CC BY-SA3.0 image credit remained separate from the adapted
Wikibooks recipe's CC BY-SA4.0 attribution. Five other dinners correctly
reported no managed cover; Obe Ata had its own available cover.

After all visible Desktop tasks were idle, normal app Quit removed all observed
Claude app processes while the launchd-owned service remained running. The
normal installer then stopped that service, updated the clean public checkout
from `e36815fd` to `20bbfc47`, fetched the pinned default HTTPS recipe release,
and created its ordinary offline backup. All 4,599 recipe IDs/revisions/documents,
all 14 SQLite tables, entire household state, saved menu and config were
unchanged. Of 1,577 state files, 1,575 were byte-identical; the other two were the
import reports changing only 4,599 outcomes from created to unchanged. Their
exact original bytes remain in the complete pre-update backup. All 52 staged
runtime/skill/requirements files matched the selected public source.

The service was explicitly started under launchd. The same-household native
Claude package was rebuilt and normally reinstalled, changing version
`0.1.0+1163046511d5` to `0.1.0+dc47ce7b23b1`; unrelated registrations were
unchanged. A real SDK/stdio/socket connection found 27 tools and read the exact
menu/image. After normal app relaunch, a fresh native Desktop conversation
loaded the updated canonical skill, called menu get once and image get once,
and displayed the seven correct dates, distinct recipe refs at revision1 and
two portions each. Its 169,434-byte menu-get payload was byte-identical to the
pre-update native read. The operator again opened the same native image.
This is current-source readback after a preserved-state upgrade, not a new
current-source planning or import run. Original #54 separate app/service
lifecycle checks and unchanged CLI workflows are reused.

The native model was Opus 4.8/Extra with unchanged Auto permissions. It parsed
oversized MCP-result files using host commands; the first run did so despite
the initial no-shell-extraction wording. A later shell-quoting failure was
recovered by parsing the same result with a benign local script, whose bytes
were retained durably; no business tool was replayed for that recovery. The
operator's first post-update comparison also used incorrect report filenames;
after inspecting the two actual report files, only that verifier was corrected.
The model's prose mislabeled one weekday and rejected bank entries, and its
readback table had a mistyped digest abbreviation before the correct full
value. Exact saved fields and the seven-row tables were correct; flawless
prose is not claimed. No recipe, preference, favorite, provider, cart, delivery,
order, payment, recipient or scheduler setting was changed. The upgraded test
installation, original backup, menu, images and two native conversations remain
available.

## Scheduler and delivery integration

The completed [scheduler matrix](scheduler-acceptance.md) is reused without
replaying its jobs. Codex's native heartbeat configuration survived pause/resume;
Desktop's local routine survived normal app restart while paused and subsequently
performed one actual scheduled synthetic read. OpenClaw/NanoClaw checks exercise
native scheduling components and persistent state. Grok's explicit-message and
closed-local-app read controls passed their bounded observation criterion; its
original silent run still has an unverified 600-second completion criterion.
Local-app absence is not physical VM sleep. Queue delay, causal pause suppression,
full-host sender availability and physical-sleep behavior are not inferred from
those narrower checks.

#53's original recipient confirmed that the actual Codex PDF opened and was
readable, and its image appeared as a native preview. That completed native
presentation gate supersedes the old “recipient unknown” summary. The original
journal still contains accepted text and unknown PDF/image transport
acknowledgments. It was neither rewritten, resent nor retargeted. This result
certifies that occurrence and destination, not every client's outgoing PDF,
email, recurring chat or sender transport. Grok's separately opened three-page
PDF verifies attachment transport; it is not a saved-menu-to-PDF workflow.

## Remaining gates

- Grok's current-target whole-VM repo-URL installation and any required
  unattended completion remain separate from its historical guided success.
  The owner's separate clean-install work is not taken over by MC10. Current
  website-login/provider workflows and full saved-menu PDF presentation remain
  unverified where not covered by the bounded MC09 results.
- Actual physical-sleep/wake and complete current-host model/sender behavior
  remain unverified where the scheduler matrix records only component or
  local-app tests. A host that requires its application/computer awake has that
  operating condition; these tests do not add a wake guarantee.
- Native outgoing frozen weekly-recipe PDF/image delivery is demonstrated for
  #53's original Codex destination. Other transports must retain their own
  supported text fallback or unverified status until actually exercised.

These are explicit open or qualified surfaces, not new purchase requirements
for the completed synthetic milestones. No new real recipient, checkout,
provider-login change, production deployment or historical-effect replay is
included in MC10. Close #52 only when its remaining required gates are
observed or the owner explicitly rescopes them.
