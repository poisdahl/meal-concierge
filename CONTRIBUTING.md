# Contributing to Meal Concierge

Bug reports, documentation fixes and focused improvements are welcome. Discuss
new store adapters, major features, dependencies or architectural changes in an
[issue](https://github.com/poisdahl/meal-concierge/issues) before building them.
Keep discussion respectful and changes small enough to understand and review.

## Before opening a pull request

- Start from current `main` in your fork. Explain the problem, the resulting
  behavior and how you verified it; include reproduction steps for bugs.
- Keep unrelated cleanup separate. Prefer existing code and direct solutions.
- Add a focused regression test for a behavior fix where practical. Documentation
  edits do not need artificial tests. Update documentation made stale by a change.
- Use synthetic fixtures. Never submit credentials, cookies, browser profiles,
  payment details, account identifiers, household databases or unredacted logs.
- Include source, attribution and redistribution rights for contributed recipes,
  images and other third-party material. The code's MIT license does not establish
  rights to third-party content. Only contribute material you may share under the
  applicable license; code contributions use this repository's [MIT license](LICENSE).
- AI-assisted contributions follow the same requirements. You are responsible
  for understanding, reviewing and testing everything you submit.

Report security vulnerabilities through the private channel in
[SECURITY.md](SECURITY.md), rather than public issues or pull requests.

## Local checks

From a fresh clone, use [uv](https://docs.astral.sh/uv/) to create an unseeded
environment with the exact Python and dependencies used by the synthetic tests:

```sh
uv venv --python 3.12.12 /tmp/meal-concierge-tests
uv pip sync --python /tmp/meal-concierge-tests/bin/python tests/mcp-requirements.txt
/tmp/meal-concierge-tests/bin/python -I -B -m unittest discover -s tests -p 'test_*.py'
/tmp/meal-concierge-tests/bin/python -I -B tests/test_mcp_runtime.py
```

Choose a new environment path if that directory already exists. Keep it outside
the checkout. Do not seed it with pip or setuptools: the MCP probe checks the
exact installed distributions. The final command runs the real SDK/stdio/Unix
socket path with a synthetic store; unittest discovery does not run that probe.

These checks require no store login, payment, real recipient or installed agent.
Standalone native-client and installer integration modes require separate setup
and are not part of the default CI run. Describe separately any synthetic tests,
authenticated reads and explicitly authorized live operations you performed;
a synthetic pass does not establish live payment or delivery success.

## Behavioral boundaries

Changes must preserve the installation's authorization policy and store, bank,
device and payment-provider approvals. Preparing checkout must not place an
order. An uncertain purchase, cancellation or delivery result must be reconciled
before another attempt; do not weaken these boundaries to make a test pass.
Preserve household data and existing installations during updates. Changes to
state formats need a tested migration and failure/recovery behavior.

Use your own isolated test installation for any separately authorized live work.
Submitting a PR does not authorize purchases, sending messages or access to a
maintainer's accounts. Do not add real sessions or secret-backed tests to CI.

## Review and publication

All changes go through a pull request. External contributions require approval
from the code owner, resolved review conversations and passing `Meal Concierge CI`
checks on an up-to-date branch. New commits invalidate prior approvals. Changes
to workflows, tests, installation, authorization and checkout need particular
attention: a green check does not replace reviewing what was executed.

The sole maintainer may use the administrator's PR-only review exception for
their own PRs, since GitHub does not permit self-approval. Required CI and the
protections against deleting or rewriting `main` still apply. The exception is
not the normal path for accepting external contributions. Fork workflow runs
require maintainer approval before execution.

Maintainers also synchronize a reviewed internal source mirror. Accepted public
contributions must be brought back into that mirror before the next publication.
Publish from current public `main`, retain contributor attribution and history,
and apply only intended changes. Preserve public-only files and contribution
guidance; never replace the public tree with an older snapshot.
