# Contributing to APM

Thank you for considering contributing to APM! This document outlines the process for contributing to the project.

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). Please read it before contributing.

## How to Contribute

### Reporting Bugs

Before submitting a bug report:

1. Check the [GitHub Issues](https://github.com/microsoft/apm/issues) to see if the bug has already been reported.
2. Update your copy of the code to the latest version to ensure the issue hasn't been fixed.

When submitting a bug report:

1. Use our bug report template.
2. Include detailed steps to reproduce the bug.
3. Describe the expected behavior and what actually happened.
4. Include any relevant logs or error messages.

### Suggesting Enhancements

Enhancement suggestions are welcome! Please:

1. Use our feature request template.
2. Clearly describe the enhancement and its benefits.
3. Provide examples of how the enhancement would work.

### Author your PR with the agent skills shipped in this repo (APM dogfoods APM)

This repo *uses* APM to ship its own author and review skills. The
canonical sources live under [`.apm/skills/`](.apm/skills/) and
[`.apm/agents/`](.apm/agents/) -- the same primitive layout any APM
package uses. They are not magically loaded by your editor; you have
to install them like any other APM dependency.

After cloning, run APM against this repo the way you would against any
other APM project:

```bash
# 1. Install APM itself if you haven't already.
#    See https://github.com/microsoft/apm#install for all install options.
curl -sSL https://aka.ms/apm-unix | sh        # macOS / Linux
# irm https://aka.ms/apm-windows | iex        # Windows PowerShell

# 2. From the root of this repo:
apm install
```

`apm install` reads this repo's [`apm.yml`](apm.yml) (`includes: auto`),
picks up everything under `.apm/`, and deploys it into the harness
directories your coding agent already watches -- `.github/skills/`,
`.github/agents/`, `.claude/skills/`, `.cursor/`, etc. -- depending on
which targets are detected on your machine. Once that is done, your
harness (Claude Code, GitHub Copilot CLI, Cursor, OpenCode, Codex,
Gemini, ...) can discover and invoke the skills by name.

For most PRs, two of those skills carry most of the weight:

| Skill | When to use it |
|---|---|
| [`pr-description-skill`](.apm/skills/pr-description-skill/SKILL.md) | **Every PR.** Drafts a self-sufficient PR body (TL;DR, Problem / Approach / Implementation, mermaid diagrams, validation evidence, How-to-test) that anchors every WHY-claim to PROSE / Agent Skills. Avoids the "what does this PR even do?" round-trip with reviewers. |
| [`apm-review-panel`](.apm/skills/apm-review-panel/SKILL.md) | **Non-trivial PRs** (new behaviour, security-relevant code, CLI UX changes, manifest/schema changes). Runs the same multi-persona panel CI runs in `pr-review-panel.yml` -- locally, on your working tree, before you push. Surfaces the `required` findings while the cost of fixing is still cheap. |

Typical local flow (after `apm install`):

1. Implement your change against `main`.
2. Ask your agent: *"Run the apm-review-panel skill on my working tree."*
   The panel fans out to the architectural, CLI-logging, DevX,
   supply-chain, growth, and (if relevant) auth personas, and returns
   a single verdict with `required` findings split from `nits`.
   Address the `required` items in-place.
3. Ask your agent: *"Use the pr-description-skill to draft the PR body
   for this branch."* Review the draft, paste it into
   `gh pr create --body-file`.
4. Push and open the PR. The same panel runs in CI on label, but most
   `required` findings will already be addressed -- the comment thread
   stays focused on substance instead of correctness debt.

You don't have to use these skills, but the panel verdict in CI applies
the same rubric either way, and PRs that have already been through it
locally tend to merge faster.

The full persona roster lives in [`.apm/agents/`](.apm/agents/) -- you
can also summon any single persona (e.g. `python-architect`,
`supply-chain-security-expert`) for a focused review of a specific file
or design question without running the full panel.

#### When to summon which persona during design and implementation

Don't wait for the panel verdict to discover you should have talked to
a specialist. The same personas the panel runs are the ones to consult
*while* you are designing and building. Recommended pairings:

| Situation | Persona to summon | Why |
|---|---|---|
| Any new feature or feature change | [`devx-ux-expert`](.apm/agents/devx-ux-expert.agent.md) **first** | Validate the user-facing approach (flags, defaults, error messages, manifest shape) *before* you write code. Cheaper than re-doing the implementation after the panel rejects it. |
| Anything that prints to the terminal | [`cli-logging-expert`](.apm/agents/cli-logging-expert.agent.md) | Always include this. Keeps log levels, colours, prefixes, and progress indicators consistent across the CLI. |
| Refactor, new module, or non-trivial architecture decision | [`python-architect`](.apm/agents/python-architect.agent.md) | Get the boundaries / interfaces / dependency direction right up front. |
| Anything that fetches packages, evaluates manifests, scans content, signs / verifies / locks, or touches `apm install` | [`supply-chain-security-expert`](.apm/agents/supply-chain-security-expert.agent.md) **mandatory** | A core promise of APM is that `apm install` blocks compromised packages before agents read them. This persona is **non-optional** for any PR that touches the supply chain -- the panel will reject it otherwise. |
| Any change touching authentication, tokens, credential resolution, or remote host auth (GitHub, GHE, ADO, EMU, GitHub Apps) | [`auth-expert`](.apm/agents/auth-expert.agent.md) | Auth bugs are silent and expensive. Run this persona on the design and again on the diff. |
| New primitive type, manifest schema change, or cross-target deployment behaviour | [`apm-primitives-architect`](.apm/agents/apm-primitives-architect.agent.md) | Keeps the primitive model coherent across Copilot, Claude, Cursor, OpenCode, Codex, Gemini. |
| Public-facing copy, README, docs site, or release notes | [`doc-writer`](.apm/agents/doc-writer.agent.md) and/or [`oss-growth-hacker`](.apm/agents/oss-growth-hacker.agent.md) | Voice consistency and positioning for new-user moments. |

Rule of thumb: ask the matching persona to **critique your plan before
you implement**, then ask it again to **review the diff before you
push**. Two cheap, focused passes per persona beat one expensive panel
rejection. The `apm-review-panel` skill at the end is then a sanity
check, not a redesign.



1. Fork the repository.
2. Create a new branch for your feature/fix: `git checkout -b feature/your-feature-name` or `git checkout -b fix/issue-description`.
3. Make your changes.
4. Run tests: `uv run pytest tests/unit tests/test_console.py -x`
5. Mirror the CI `Lint` job locally before pushing -- both commands must be silent:
   ```bash
   uv run --extra dev ruff check src/ tests/
   uv run --extra dev ruff format --check src/ tests/
   ```
   Auto-fix with `ruff check --fix` and `ruff format` (without `--check`). The full contract -- including common surprises like `RUF043`, `UP006`, `I001` -- lives in [`.apm/instructions/linting.instructions.md`](.apm/instructions/linting.instructions.md), the canonical source of truth that CI, the `pr-description-skill`, and the dogfood `apm compile -t copilot` all mirror.
6. Commit your changes with a descriptive message.
7. Push to your fork.
8. Submit a pull request.

### Pull Request Process

1. Fill out the PR template - describe what changed, why, and link the issue.
2. Ensure your PR addresses only one concern (one feature, one bug fix).
3. Include tests for new functionality.
4. Update documentation if needed.
5. PRs must pass all CI checks before they can be merged.

### How merging works

This repo uses GitHub's native **merge queue**. Once your PR is approved, a
maintainer adds it to the queue. The queue then:

1. Builds a tentative merge of your PR against the latest `main` - no manual
   "Update branch" needed.
2. Runs the integration suite against that tentative merge.
3. Auto-merges if checks pass; ejects from the queue if they fail.

What this means for contributors:

- You don't need to keep your branch up to date with `main` manually.
- The fast unit + build checks (Tier 1) run on every push to your PR.
- A hermetic ~65-70s Lifecycle Smoke check also runs on every push, validating
  core install/lock-convergence/policy-enforcement contracts via a declarative
  `lifecycle_smoke` pytest marker -- no network, credentials, or built binary
  required, so you get real regression feedback before the queue instead of
  only after.
- The full integration suite (Tier 2) only runs once your PR is in the queue,
  not on every WIP push.

If your PR is ejected from the queue because of a real failure, push a fix and
ask a maintainer to re-queue.

### Issue Triage

Every new issue is automatically labeled `needs-triage`. Maintainers review incoming issues and:

1. **Accept** - remove `needs-triage`, add `accepted`, and assign a milestone.
2. **Prioritize** - optionally add `priority/high` or `priority/low`.
3. **Close** - if it's a duplicate (`duplicate`) or out of scope, close with a comment explaining why.

Labels used for triage: `needs-triage`, `accepted`, `needs-design`, `priority/high`, `priority/low`.

## Development Environment

This project uses uv to manage Python environments and dependencies:

```bash
# Clone the repository
git clone <this-repo-url>
cd apm

# Install all dependencies (creates .venv automatically)
uv sync --extra dev
```

## Testing

We use pytest for testing with `pytest-xdist` for parallel execution. After completing the setup above:

```bash
# Run the unit test suite (recommended - matches CI, fast)
uv run pytest tests/unit tests/test_console.py -x

# Run a specific test file (fastest, use during development)
uv run pytest tests/unit/path/to/relevant_test.py -x

# Run the full test suite (includes integration & acceptance tests)
uv run pytest

# Run with verbose output
uv run pytest tests/unit -x -v
```

Tests run in parallel automatically (`-n auto` is configured in `pyproject.toml`). To force serial execution, add `-n0`.

### Running the bounded mutation pilot

The advisory mutation pilot checks four stable owners: dependency subset
selection, update-plan construction, cached-policy serialization, and canonical
in-package link projection. It is intentionally separate from required PR CI
and runs nightly or by manual workflow dispatch with a 20-minute job budget.

Run the same exact-function allowlist locally:

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --output mutation-pilot-report.json
```

The command exits nonzero for a new survivor, timeout, suspicious result,
unchecked mutant, or any other incomplete outcome. It writes a sorted,
timestamp-free JSON report even when the survivor comparison fails. Reuse the
`mutants/` cache only when the source, test seams, configuration, runner, and
lockfile are unchanged:

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --reuse-cache --output mutation-pilot-report.json
```

To inspect existing mutmut metadata without executing mutants:

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --report-only --output mutation-pilot-report.json
```

The accepted survivor allowlist is
[`tests/mutation/baseline.json`](tests/mutation/baseline.json). Do not update it
to make a run green. Review every surviving diff with `mutmut show`, add a
behavioral test for real contract gaps, and use `--update-baseline` only for a
reviewed baseline change.

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --update-baseline --output mutation-pilot-report.json
```

If you don't have `uv` available, you can use a standard Python venv and pip:

```bash
# create and activate a venv (POSIX / WSL)
python -m venv .venv
source .venv/bin/activate

# install this package in editable mode and test deps
pip install -U pip
pip install -e .[dev]

# run unit tests
pytest tests/unit tests/test_console.py -x
```

### Running integration tests

Integration tests under `tests/integration/` use a marker-driven
discovery system: each test declares the precondition it needs
(token, runtime, opt-in flag, ...) as a `pytest.mark.requires_*`
marker, and `tests/integration/conftest.py` auto-skips at collection
time when the precondition is missing. Without any setup,
`uv run pytest tests/integration` is silent rather than red -- every
test reports as `SKIPPED` with a one-line reason, so you can see
exactly what is missing.

The full marker reference (one row per `_MARKER_CHECKS` entry, with
the env-var or `apm runtime setup` command that satisfies it) lives
in
[Integration Testing](docs/src/content/docs/contributing/integration-testing.md#the-marker-registry).
For install, compile, pack, or audit lifecycle changes, reuse the
[hermetic lifecycle fixtures](docs/src/content/docs/contributing/integration-testing.md#hermetic-lifecycle-fixtures)
to exercise the real CLI with a sanitized child environment and reviewed local
Git sources.
A typical local run looks like:

```bash
# Run only what your current env satisfies
uv run pytest tests/integration -v

# Run only one marker family (e.g. tests that need a GitHub token)
uv run pytest tests/integration -m requires_github_token -v
```

When adding a new precondition, add an entry to `_MARKER_CHECKS` and
declare the marker in `pyproject.toml`; that is the only place the
precondition needs to live.

### Coverage policy

Both suites enforce a hard coverage gate in CI. A PR that drops
coverage below the gate cannot merge.

| Suite       | Gate | Enforced in |
|-------------|------|-------------|
| Unit        | 80%  | `pyproject.toml` (`fail_under`) |
| Integration | 70%  | `.github/workflows/ci-integration.yml` (`--fail-under`) |

**Ratchet rule.** Gates only move upward. When actual coverage
exceeds the gate by 5 or more percentage points, raise the gate to
`actual - 3` in the next release PR.

**Finding low-coverage files.** Every CI run publishes a
"Lowest-coverage files" collapsible section in the job summary
(rendered by `scripts/coverage-summary.py`). Check there to see
which files would benefit most from new tests.

## Coding Style

This project follows:
- [PEP 8](https://pep8.org/) for Python style guidelines
- We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting

CI enforces all lint and formatting rules automatically. The CI `Lint` job runs the following two commands -- both must be silent before you open a PR:

```bash
uv run --extra dev ruff check src/ tests/         # lint (CI-mirror)
uv run --extra dev ruff format --check src/ tests/ # format check (CI-mirror)
```

Auto-fix locally with:

```bash
uv run --extra dev ruff check src/ tests/ --fix   # lint with auto-fix
uv run --extra dev ruff format src/ tests/        # apply formatter
```

The canonical lint contract (with common diagnostics and lifecycle binding for skills that claim green CI) lives in [`.apm/instructions/linting.instructions.md`](.apm/instructions/linting.instructions.md). Do not redefine these commands elsewhere -- honor that instruction.

### Optional: local pre-commit hooks

For instant feedback before pushing, install the pre-commit hooks:

```bash
uv run pre-commit install
```

This is optional -- CI is the authoritative gate. The pre-commit hook rev may lag behind the CI version; check `.pre-commit-config.yaml` against `uv.lock` if you see discrepancies.

## Documentation

If your changes affect how users interact with the project, update the documentation accordingly.

## Extending APM

### Adding or modifying an MCP client adapter

The MCP client adapters (`src/apm_cli/adapters/client/`) inherit shared
utilities from `MCPClientAdapter` in `base.py`.  When adding a new adapter
or modifying an existing one:

- Inherit from `MCPClientAdapter` and reuse the shared helpers
  (`_apply_pypi_homebrew_generic_config`, `_apply_auth_and_headers_impl`,
  `_resolve_env_vars_with_prompting`).
- Do **not** copy-paste logic from sibling adapters -- the pylint R0801
  similarity threshold is 10 lines and CI will fail on duplicated blocks.
- For marketplace tag-parsing, use `marketplace._shared.iter_semver_tags`
  rather than re-implementing the refs-iteration loop.

### How to add an experimental feature flag

Use an experimental flag to de-risk rollout of a user-visible behavioural change that may need early adopter feedback. Do not add a flag for a bug fix, internal refactor, or any change that should simply ship as the default behaviour.

Experimental flags MUST NOT gate security-critical behaviour (content scanning, path validation, lockfile integrity, token handling, MCP trust, collision detection). Flags are ergonomic/UX toggles only.

When adding a new experimental flag:

1. Register it in `src/apm_cli/core/experimental.py` in the `FLAGS` dict with a frozen `ExperimentalFlag(name=..., description=..., default=False, hint=...)`.
2. Gate the code path with a function-scope import (avoids import cycles):
   ```python
   def my_function():
       from apm_cli.core.experimental import is_enabled
       if is_enabled("my_flag"):
           ...
   ```
3. Add tests that cover both the enabled and disabled code paths.
4. Update the experimental command reference page at `docs/src/content/docs/reference/experimental.md`.

Naming rules:

- Use `snake_case` in the registry and config.
- Use `kebab-case` for display and other user-facing strings.
- The CLI accepts both forms on input.

Graduation and retirement:

1. When a flag becomes the default, remove the gate and remove the matching `FLAGS` entry in the same PR.
2. Add a `CHANGELOG.md` entry under `Changed` with a migration note if the previous default differed.

Avoid these anti-patterns:

- Do not gate security-critical behaviour behind an experimental flag.
- Do not read `is_enabled()` at module import time.
- Do not persist flag state anywhere other than `~/.apm/config.json` via `update_config`.

## Adding or changing a normative requirement (OpenAPM v0.1)

The OpenAPM v0.1 spec (`docs/src/content/docs/specs/openapm-v0.1.md`)
and APM the implementation are co-evolved in this repo. APM is the
sole implementation of the spec. To prevent the spec from rotting
into a document of lies, every normative change MUST land as three
coupled edits in the same PR. There is NO automated CI detector for
"APM behaviour drifted from the spec" beyond the 4-way orphan_check
gate -- the ritual below is the only safeguard.

Three-step ritual:

1. **Spec edit.** Add or change a `<a id="req-XXX"></a>` anchor with
   prose in the spec body. Add or change the matching Appendix C row.
2. **Manifest edit.** Add or change the entry in
   `docs/src/content/docs/specs/manifests/openapm-v0.1.requirements.yml`
   so it stays a byte-equivalent projection of the canonical anchors.
3. **Test edit.** Add or extend a `@pytest.mark.req("req-XXX")` test
   under `tests/spec_conformance/`. If a real assertion is not yet
   possible, call `waive("...")` from `_helpers.py` with a one-line
   rationale; the waiver will surface in `CONFORMANCE.md` as honest
   debt, not invisible coverage.

After the three edits, regenerate the conformance statement:

```
uv run --extra dev python -m tests.spec_conformance.gen_statement
```

and commit the resulting `CONFORMANCE.{md,json}` at repo root. CI
gates a clean diff.

Common modes the ritual catches:

- **Mode A (silent regression)** -- a code change in `src/apm_cli/**`
  breaks an assertion bound to a `req-XXX`. The spec-conformance
  pytest job fails; fix the code, do not touch the spec.
- **Mode B (silent extension)** -- a new APM behaviour lands under a
  normative critical path (`primitives/`, `deps/`, `policy/`,
  `registry/`, `runtime/`, `install/`, `integration/`) with no spec
  citation. Two gates catch this: (a) `orphan_check` fails if you
  added a `@pytest.mark.req` marker but forgot the anchor, manifest
  row, or Appendix C row; (b) the **Mode B detector** fails if you
  added substantive code under a critical path and added NO spec
  artifacts at all (no anchor, no manifest row, no marker). The fix
  is to add the anchor + manifest row + marker, or -- for a true
  refactor / perf rewrite / internal cleanup with no observable
  behaviour delta -- add a single line `apm-spec-waiver: <one-line
  rationale, >= 16 chars>` to the PR body or a commit message. The
  waiver is echoed verbatim to the CI log and is reviewer-auditable.
  The critical-path allowlist itself lives at
  `tests/spec_conformance/critical_paths.txt`; edits to it are
  themselves critical-path edits.
- **Mode C (stale spec)** -- the spec prose is wrong about APM's
  intended behaviour. Amend the anchor + Appendix C row + manifest
  entry. The same PR carries both the spec edit and the test that
  proves it.

Choosing between modes is a human call. The harness exposes the
choice; it does not pick.

## License

By contributing to this project, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).

## Questions?

If you have any questions, feel free to open an issue or reach out to the maintainers.

Thank you for your contributions!
