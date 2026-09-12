---
title: "Development Guide"
description: "APM development setup, testing, coding guardrails, and extension recipes."
sidebar:
  order: 1
---

Start with [CONTRIBUTING.md](https://github.com/microsoft/apm/blob/main/CONTRIBUTING.md)
for issue-first scope approval, PR eligibility, small corrections, private
security reporting, and the transition for existing contributions.
[GOVERNANCE.md](https://github.com/microsoft/apm/blob/main/GOVERNANCE.md) names
the responsible human maintainers and explains decisions and responsibilities.
Its [roadmap and release planning model](https://github.com/microsoft/apm/blob/main/GOVERNANCE.md#roadmap-and-release-planning)
separates priorities from scope approval and release targets; Project rollout
is pending.
Those root documents own contribution policy; this guide covers technical work
within the approved scope. Reporting and investigation need no prior permission.

## Development Environment

This project uses uv to manage Python environments and dependencies:

```bash
# Clone the repository
git clone https://github.com/microsoft/apm.git
cd apm

# Install all dependencies (creates .venv automatically)
uv sync --extra dev
```

## Optional agent tools

No AI tool, harness, or repository skill is required to contribute. APM
dogfoods its own primitives from
[`.apm/skills/`](https://github.com/microsoft/apm/tree/main/.apm/skills) and
[`.apm/agents/`](https://github.com/microsoft/apm/tree/main/.apm/agents), with
local package dependencies declared in
[`apm.yml`](https://github.com/microsoft/apm/blob/main/apm.yml).
Automated recommendations are advisory, not scope approval or a substitute
for human review.

Issue triage produces a recommendation and a **proposed** scope, done-when,
exclusions, and review-needs brief. Maintainers still decide acceptance,
priority, contributor invitations, and milestones. The
[triage label contract](https://github.com/microsoft/apm/blob/main/packages/apm-triage-panel/assets/label-contract.json)
separates those decisions from advisory processing. During compatibility
rollout, both `status/triaged` and `triage/recommended` mean completed
automated advice, not human review. The new writer uses `triage/recommended`
after the canonical label is provisioned and this code is deployed.
`status/needs-triage` can remain after advice while awaiting a human decision.
No label or milestone migration is performed by the advisory workflow.

### Eligibility evidence and automation

The [scope record format](https://github.com/microsoft/apm/blob/main/CONTRIBUTING.md#scope-record-format)
provides explicit human evidence on an issue. The deterministic
`PR eligibility (advisory only)` check is always neutral: `record-present`,
`withdrawn`, `needs-evidence`, or `error` are not implementation permission.
Private security/dependency tracking and claimed trivial corrections receive
manual-review states. Real issue references are checked through GitHub, not
inferred from arbitrary numbers, PR links, upstream references, or labels.
Humans still decide whether the change matches the linked scope.

After the workflow reaches the default branch, PR updates and relevant
issue/comment changes refresh evidence without invoking an LLM panel.
Forks and stacked PRs use default-branch governance and code, never the head
or a feature-branch approval roster. A permissionless queue signal wakes the
default-branch reporter through `workflow_run`; the reporter verifies GitHub's
run, repository, event, and workflow metadata without consuming artifacts or
contributor code. Commit associations do not prove complete queue membership;
missing association/read access is reported as unknown/error. The check is
not part of the required merge gate, and this PR does not deploy itself.
Native event delivery and queue limits still apply. Maintainers can request a
default-branch recheck without selecting a branch-controlled workflow definition:

```bash
gh api --method POST repos/microsoft/apm/dispatches \
  -f event_type=pr-eligibility-recheck -F 'client_payload[pr_number]=123'
```

There is no `workflow_dispatch` entrypoint. These workflow-definition
guarantees target GitHub.com, not older GitHub Enterprise Server versions.

For a read-only local assessment, run the tool from a trusted default-branch
checkout with Node.js and an authenticated `gh` installed outside the project.
The tool excludes project PATH entries and symlinks back into the project:

```bash
node scripts/governance/eligibility.cjs --repo microsoft/apm --pr 123
node scripts/governance/eligibility.cjs --help
```

The tool reads current policy from the default branch. Until the new roster
format is deployed there, it deliberately reports a policy error rather than
using a feature branch as authority. A snapshot cannot recover deleted
withdrawals, so manual automation still requires fresh responsible-human
confirmation of the bounded issue scope. No historical acceptance is restored.

Each assessment is bounded: 25 visible unique issue references per PR, 10
metadata pages per collection, 100 matched PR targets, 500 total metadata reads,
and 32 MiB of metadata per operation. Issue events may inspect more than 100
open PRs; only matched targets count toward that cap. A full final page is incomplete evidence.
Exceeding a bound reports `error`, never a truncated `record-present` result.
Reads are cached only within one operation; the final PR head/body refresh
bypasses that cache. This accommodates the current 62-PR policy refresh while
bounding genuinely oversized operations. Hidden HTML comments are not issue
traceability or approval nominations.

Once deployed, Daily Docs Updater runs discovery only, without issue/PR creation
or auto-merge capabilities. Scheduled and manual runs stop at a human handoff.
A docs-sync confirmation label likewise requests consideration; it cannot
authorize a companion PR. Bug sweeps read canonical and legacy bug labels once
per issue, then require the same human checkpoint before implementation.
During rollout, keep Daily Docs disabled until the replacement source and
compiled capabilities are deployed and verified. Opening or merging the PR
does not authorize resuming the disabled workflow; a maintainer does that
separately.

The eligibility workflow verifies that `main` is still the repository default
branch before checking out that literal ref, then uses the checked-out commit
for both implementation and policy. A default-branch rename stops the workflow
until maintainers review this boundary; it never falls back to a PR or event ref.

### Installing optional skills

To use these tools, [install APM](../../getting-started/installation/) if needed,
then run from the repository root:

```bash
apm install
```

The manifest's `includes: auto` picks up `.apm/`. Its pinned `copilot` target
deploys to the committed `.github/` and `.agents/skills/` tree, regardless of
which harness your machine detects. Your harness can then discover and invoke
the installed skills by name.

For a different harness, exclude its generated roots locally before overriding
the pinned target. For example, with Claude Code:

```bash
printf '.claude/\n' >> "$(git rev-parse --git-path info/exclude)"
apm install --target claude
```

The local exclusion avoids changing repository-wide ignore rules. This install
also adds deploy paths to `apm.lock.yaml`; leave that local override uncommitted.
Check the [target catalogue](../../concepts/primitives-and-targets/#target-catalogue)
for other targets; some write to more than one root.

## Testing

After setup, use pytest for focused feedback:

```bash
# Unit suite
uv run pytest tests/unit tests/test_console.py -x

# Focused file; replace with the test relevant to your change
uv run pytest tests/test_console.py -x

# Full suite, including integration and acceptance tests
uv run pytest

# Verbose unit results
uv run pytest tests/unit -x -v
```

`pytest-xdist` is available: add `-n auto` for parallel execution or `-n0`
to force serial execution. The default selection in `pyproject.toml` excludes
`benchmark` and `live` tests.

Without `uv`, use a standard Python venv and pip:

```bash
# create and activate a venv (POSIX / WSL)
python -m venv .venv
source .venv/bin/activate

# install this package in editable mode and test deps
pip install -U pip
pip install -e '.[dev]'

# run unit tests
pytest tests/unit tests/test_console.py -x
```

### Running integration tests

Tests under `tests/integration/` declare preconditions with `requires_*`
markers. The `_MARKER_CHECKS` registry in `tests/integration/conftest.py`
skips tests with missing prerequisites at collection time and reports why.
Use the [marker registry](../integration-testing/#the-marker-registry) to
find the token, runtime setup command, or opt-in flag each family needs.

```bash
# Run tests whose prerequisites your environment satisfies
uv run pytest tests/integration -v

# Select a prerequisite family
uv run pytest tests/integration -m requires_github_token -v
```

When adding a precondition, add its check to `_MARKER_CHECKS` and declare the
marker in `pyproject.toml`; do not duplicate the check in each test. For install,
compile, pack, or audit lifecycle changes, reuse the
[hermetic lifecycle fixtures](../integration-testing/#hermetic-lifecycle-fixtures)
to exercise the real CLI with a sanitized child environment and reviewed local
Git sources.

### Coverage policy

Both suites have hard CI coverage gates that must pass before merge:

| Suite | Gate | Enforced in |
| --- | --- | --- |
| Unit | 80% | `pyproject.toml` (`fail_under`) and combined coverage in `.github/workflows/ci.yml` |
| Integration | 70% | Combined coverage in `.github/workflows/ci-integration.yml` (`--fail-under`) |

Gates only move upward. When actual coverage exceeds the gate by at least
5 percentage points, raise the gate to `actual - 3` in the next release PR.
CI's coverage summaries include a "Lowest-coverage files" section, rendered by
`scripts/coverage-summary.py`, to identify where new tests would help.

### Running the bounded mutation pilot

The advisory mutation pilot covers five stable owners: dependency subset
selection, update-plan construction, cached-policy serialization, canonical
in-package link projection, and lockfile field normalization (the fail-closed
`host_type`/`exec_status` normalizers, not the `@dataclass` reconstruction
methods `to_dict`/`from_dict`/`to_dependency_ref` -- mutmut cannot mutate
`@dataclass` methods; those are defended by PR #2246's manual mutation-break
twins instead). It runs nightly or by manual workflow
dispatch, not as required PR CI, and has a 20-minute hosted job budget.

Run the exact-function allowlist locally:

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --output mutation-pilot-report.json
```

The command fails on new survivors, timeouts, suspicious results, unchecked
mutants, and incomplete outcomes. It writes a sorted, timestamp-free JSON
report even when survivor comparison fails. Pass `--reuse-cache` only when
the allowlisted source, test seams, configuration, runner, and lockfile
are unchanged.

To inspect existing mutmut metadata without executing mutants:

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --report-only --output mutation-pilot-report.json
```

The reviewed survivor allowlist lives in
[`tests/mutation/baseline.json`](https://github.com/microsoft/apm/blob/main/tests/mutation/baseline.json).
Do not update it to make a run green. Review every surviving diff with
`mutmut show` and add behavioral tests for real contract gaps.
Use `--update-baseline` only when the baseline change itself has been reviewed:

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --update-baseline --output mutation-pilot-report.json
```

## Coding Style

APM follows [PEP 8](https://pep8.org/) and uses
[Ruff](https://docs.astral.sh/ruff/) for linting and formatting. Follow the
canonical [lint contract](https://github.com/microsoft/apm/blob/main/.apm/instructions/linting.instructions.md)
for local commands, auto-fixes, and common diagnostics. The actual
[`Lint` job in `ci.yml`](https://github.com/microsoft/apm/blob/main/.github/workflows/ci.yml)
defines the complete enforced step list; mirror it before pushing or claiming
green CI.

Ruff lint and format are only part of that job. It also checks YAML I/O,
file length, portable relative paths, duplication, auth-protocol boundaries,
and architecture boundaries. Its Python scope includes the architecture-linter
scripts as well as `src/` and `tests/`. CI checks the PR merge result, so changes
on `main` can introduce failures even when the branch alone passes.

### Architecture guardrails

Durable architecture decisions have one canonical owner. Executable owner metadata
lives in `.apm/architecture/owners/index.json` and the six shards it lists:

- `core-runtime.json`
- `install-deployment.json`
- `hooks-integrations.json`
- `transport-auth-platform.json`
- `marketplace-plugins.json`
- `contracts-tooling.json`

For an ordinary owner addition, edit the appropriate shard. Do not edit
`.apm/instructions/architecture.instructions.md`, `.github/instructions/...`
files, or `apm.lock.yaml`. Each owner entry has `id`, `decision`, `owner`,
`selectors`, and `guards` fields; this metadata is an ownership registry, not a
rule DSL.

When centralizing behavior, add a behavioral test and register a semantic static
guard. Run the stable architecture check with:

```bash
bash scripts/lint-architecture-boundaries.sh
```

The check fails closed when metadata is malformed, missing, or not listed in the
index.

`InstallTransaction` owns one acquisition of the shared lifecycle `FileLock`.
Commit, rollback, and context exit are the normal release paths; explicit release
invokes the same `weakref.finalize` callback used for abandoned transactions.
This fallback releases only the transaction's outstanding acquisition, never
runs filesystem rollback, and leaves other owners' acquisitions intact. Keep
transaction lifetime and completion on the acquiring thread: filelock uses
thread-local state, so there is no cross-thread lifecycle guarantee. In lifecycle
release regression tests, retain the shared lock handle so `FileLock` destruction
cannot mask a missed release.

### Optional: local pre-commit hooks

For instant feedback before pushing, install the pre-commit hooks:

```bash
uv run pre-commit install
```

This is optional -- CI is the authoritative gate. The pre-commit hook rev may lag behind the CI version; check `.pre-commit-config.yaml` against `uv.lock` if you see discrepancies.

## CI and merging

### How merging works

A maintainer adds an approved PR to GitHub's native merge queue. The queue
builds a tentative merge against the latest `main`, runs checks including the
integration suite, and merges on success or ejects the PR on failure.
There is no manual "Update branch" step just to enter the queue. If a real
failure ejects your PR, push a fix and ask a maintainer to re-queue it.

Fast unit and build checks (Tier 1) run on PR updates. The required Lifecycle
Smoke check also runs on PRs and merge-queue commits. It selects
`lifecycle_smoke and not lifecycle_merge_group` contracts with no network,
credentials, or frozen binary required. See
[Integration Testing](../integration-testing/) for the bounded selection,
timeout, prerequisites, and local command.
The full integration suite (Tier 2) runs in the queue rather than on every
WIP push.

### Workflow dependency updates

When updating actions in generated `.github/workflows/*.lock.yml` files,
keep their `gh-aw-manifest` headers, human-readable action lists, and
`.github/aw/actions-lock.json` entries aligned with the runtime `uses:` pins.
Dependabot does not update those metadata records. Preserve the compiler
version and source hashes for dependency-only edits; recompile with
`gh aw compile` when changing workflow source.

Run `uv run --frozen --extra dev pytest tests/unit/test_triage_panel_lock.py`
to check setup and app-token action pin consistency across the manifest-bearing
workflows.

### Code scanning on pull requests and merge queues

`codeql.yml` analyzes `python`, `actions`, and `javascript-typescript` on pull
requests, pushes to `main`, merge-group `checks_requested` events, and weekly.
JavaScript adds coverage; preserve stable workflow, job, and category identities.
Explicit `upload-database: true` and `wait-for-processing: true` retain existing
defaults; local queries and database uploads remain enabled. Diff-informed
queries are disabled so PR scans include findings outside changed lines.

A separate scan producer consumes default-branch database uploads and returns
the separate `default` API configuration on `main` only. Preserve these uploads
for compliance; PR and queue workflow analyses do not replace that producer.

`.github/workflows/merge-gate.yml` still requires only the `gate` check and adds
`actions: read` and `security-events: read`. The base-checked-out
`merge_gate_wait.sh` unconditionally calls `.github/scripts/ci/codeql_policy.py`
before polling and immediately before success, within the shared deadline.
Runner event metadata selects the exact PR merge
or merge-queue ref and SHA, not the PR head or a moving branch tip.

The policy requires the latest CodeQL workflow run and every `Analyze` job
(`python`, `actions`, `javascript-typescript`) to succeed. Analyses must match
the exact ref and commit, full stable workflow key, category, and environment,
with positive `rules_count`. Small, per-language evidence artifacts bind each
SARIF upload ID to its workflow run and attempt; older uploads cannot satisfy
a rerun. These artifacts are parsed as data, never executed. They expire after
14 days; rerun CodeQL before rerunning `gate` when evidence has expired.
The policy paginates API results and checks per-alert
instances. It blocks **all open, undismissed workflow-scoped alerts with
high/critical security severity or error severity**, including pre-existing
alerts—not just new findings. GitHub dismissals are honored; the separate
external `default` API configuration is excluded. API failures, timeouts, and
stale state fail closed. Enabling another language may expose existing findings
that must be fixed or reviewed before rollout.

**Post-merge rollout (maintainer action):**

1. Keep the native `code_scanning` rule enabled for the initial PR: its gate
   runs the **old trusted base script**, not the proposed policy.
2. After merge, verify the new base gate enforces the policy and succeeds on
   both a fresh PR and a merge-queue run. Verify `main` publishes all three
   language databases and analyses before changing protection.
3. Back up `main-protection` (ruleset ID `9294522`) through the GitHub rulesets
   API. Re-read and compare immediately before updating; abort on concurrent changes.
4. Only then may a maintainer remove **only CodeQL** from the rule's
   `parameters.code_scanning_tools`
   via the API. Retain the rule if other tools remain; remove it only if empty.
   Preserve `gate` and every other rule, threshold, tool, and bypass setting.
   Verify the saved ruleset afterward. Neither this PR nor this documentation
   changes live settings or removes the live blocker.

**Rollback:** Reinstate the prior native code-scanning rule **before** reverting
the custom gate.

## Documentation

If your changes affect how users interact with the project, update the documentation accordingly.
Public top-level CLI commands and rendered reference pages are a matched contract. When you add,
remove, or rename a command, create, remove, or rename its matching page under
`docs/src/content/docs/reference/cli/` and update the command table in
`docs/src/content/docs/reference/index.md`, then run:

```bash
npm --prefix docs run build
uv run --frozen python scripts/check_cli_docs.py docs/dist
```

## Extending APM

### Adding or modifying an MCP client adapter

Adapters in `src/apm_cli/adapters/client/` inherit shared utilities from
`MCPClientAdapter` in `base.py`:

- Reuse `_apply_pypi_homebrew_generic_config`, `_apply_auth_and_headers_impl`,
  and `_resolve_env_vars_with_prompting` rather than copying sibling adapters.
- The pylint R0801 similarity threshold is 10 lines; duplicated blocks fail CI.
- For marketplace tag parsing, use `marketplace._shared.iter_semver_tags`
  rather than reimplementing the refs-iteration loop.

### How to add an experimental feature flag

Use an experimental flag for a user-visible behavior change that needs early
adopter feedback, not a bug fix, internal refactor, or change that should ship
as the default. Flags are ergonomic/UX toggles only. They MUST NOT gate
security-critical behavior: content scanning, path validation, lockfile
integrity, token handling, MCP trust, or collision detection.

1. Register the flag in `src/apm_cli/core/experimental.py`'s `FLAGS` dict with
   a frozen `ExperimentalFlag(name=..., description=..., default=False, hint=...)`.
2. Import and call `is_enabled` at function scope to avoid import cycles and
   config I/O at module import time. For example, the existing `verbose_version`
   flag can be checked with:

   ```python
   def show_runtime_details():
       from apm_cli.core.experimental import is_enabled

       return is_enabled("verbose_version")
   ```

3. Test both enabled and disabled paths.
4. Update the [experimental command reference](../../reference/experimental/).

Use `snake_case` in the registry and config, and `kebab-case` for display and
other user-facing strings. The CLI accepts both forms on input. Persist flag
state only in `~/.apm/config.json` through `update_config`.

When a flag graduates to the default, remove its gate and `FLAGS` entry in the
same PR. Add a `CHANGELOG.md` entry under `Changed`, with a migration note if
the previous default differed.

## Adding or changing a normative requirement (OpenAPM v0.1)

The [OpenAPM v0.1 specification](../../specs/openapm-v01/) and APM's
implementation evolve together. Every normative change MUST include three
coupled edits in the same PR:

1. **Spec:** add or change a `<a id="req-XXX"></a>` anchor and its prose in
   `docs/src/content/docs/specs/openapm-v0.1.md`, plus the matching Appendix C row.
2. **Manifest:** update
   `docs/src/content/docs/specs/manifests/openapm-v0.1.requirements.yml`
   to remain a byte-equivalent projection of the canonical anchors.
3. **Test:** add or extend a `@pytest.mark.req("req-XXX")` test under
   `tests/spec_conformance/`. If a real assertion is not yet possible, call
   `waive("...")` from `_helpers.py` with a one-line rationale. The waiver
   appears in `CONFORMANCE.md` as visible debt, not test coverage.

Regenerate the conformance statement after these edits:

```bash
uv run --extra dev python -m tests.spec_conformance.gen_statement
```

Include the resulting root `CONFORMANCE.md` and `CONFORMANCE.json` in the
same PR; CI requires a clean generated diff.

The workflow distinguishes three modes:

- **Mode A (silent regression):** a code change breaks an assertion bound to
  a `req-XXX`. The spec-conformance pytest job fails. Fix the code, not the spec.
- **Mode B (silent extension):** new behavior under a normative critical path
  lacks a spec citation. The four-way `orphan_check` catches a requirement
  marker missing its anchor, manifest row, or Appendix C row. The Mode B
  detector catches substantive critical-path code with no spec artifacts at
  all. Add the anchor, manifest row, and marker, with the Appendix C row as
  above. For a true refactor, performance rewrite, or internal cleanup with
  no observable behavior change, add `apm-spec-waiver: <one-line rationale>`
  to the PR body or a commit message. The rationale must be at least
  16 characters; CI echoes the waiver verbatim for reviewer inspection.
  The critical-path allowlist lives in
  `tests/spec_conformance/critical_paths.txt`; changes to that list are
  themselves critical-path edits.
- **Mode C (stale spec):** the prose misstates intended behavior. Amend the
  anchor, Appendix C row, and manifest entry, with a test proving the intended
  behavior in the same PR.

These checks cannot detect every semantic drift. Choosing the appropriate
mode remains a human decision; the harness exposes the choice, not the answer.
