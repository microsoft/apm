<!--
  scenario-evidence-rubric.md

  Loaded by pr-description-skill at synthesis time (Validation
  section) AND by .apm/agents/test-coverage-expert.agent.md as the
  evaluation lens for "does this PR prove it works for the user
  promises it touches". Single source of truth -- if you change the
  rubric here, both consumers pick it up.

  CHARSET: ASCII (lives in the repo, subject to
  .github/instructions/encoding.instructions.md). The PR body it
  governs is UTF-8 GitHub-Flavored Markdown.
-->

# Scenario Evidence Rubric -- Proof That It Works

## What this is

A structured mapping in every non-trivial PR that answers ONE
question from the maintainer's chair:

> For each user promise this PR touches, which test exercises the
> scenario, and which APM principle does that scenario serve?

This is NOT pure coverage (lines / functions / branches). It is
SCENARIO coverage: the user-facing flows that, if they silently
break, erode trust in APM as a tool. Pure coverage metrics are not
the gate; scenario-test mapping is.

## APM principle taxonomy

Every scenario in a PR maps to AT LEAST ONE of these principles
(named in the README and MANIFESTO). The taxonomy is the lens
contributors and reviewers share:

| Principle | What a scenario in this lane looks like |
|---|---|
| **Portability by manifest** | Same `apm.yml` produces the same primitive layout across targets (Codex, Claude, Copilot, Cursor, ...). New targets, manifest fields, or compile paths land here. |
| **Secure by default** | User-controlled input cannot escape its sandbox. Path traversal, dependency confusion, token leakage, signature/integrity failures, lockfile tampering. |
| **Governed by policy** | Lockfile determinism, `apm audit` exit codes, allow/deny enforcement, CI-mode gates, dependency resolution rules. |
| **Multi-harness support** | Same primitive runs across Codex, Claude, Copilot, and any new harness. Routing, target detection, harness-specific adapters. |
| **Vendor-neutral** | No special privilege for any vendor's package, host, or harness in the resolution / install / audit paths. Generic Git hosts (GHES, GitLab, Bitbucket, self-hosted) are first-class. |
| **DevX (pragmatic as npm)** | First-run flow, error wording, exit codes, install idempotency, helpful diagnostics. The 30-second-to-productive promise. |
| **OSS / community-driven** | Contributor onboarding, doc accuracy, public extension points (skills, agents, hooks, integrators) stay stable for external authors. |

A scenario MAY map to multiple principles (e.g., "install on GHES
with credential helper" hits multi-harness + vendor-neutral + DevX).
That is signal, not noise -- record all that apply.

## Required mapping shape

Every PR that changes behavior MUST ship the following table in its
Validation section. (Pure refactor PRs that change no behavior MAY
omit it -- but the burden of proof is on the author to claim that.)

| # | Scenario (user promise) | Principle(s) | Test(s) proving it | Type |
|---|------------------------|--------------|--------------------|------|
| 1 | <One-line user-facing scenario, in the user's words.> | <One or more from the taxonomy above.> | `<path/to/test_file.py::test_name>` (line ref optional) | unit / integration / e2e |
| 2 | <Scenario.> | <Principle.> | `<path>` | <type> |

Rules:

- **Scenario column is in USER words**, not implementation words.
  Good: "Run `apm install` on a GHES repo with credential helper
  configured -- it succeeds without prompting." Bad: "Tests
  `_preflight_auth_check` returns early when host is generic."
- **Principle column names AT LEAST ONE** principle from the
  taxonomy. Cite the principle name verbatim so reviewers can
  pattern-match.
- **Test column is a real file path**, ideally with `::test_name`
  or a line range. The reviewer must be able to click through.
- **Type column** is `unit`, `integration`, or `e2e`. A scenario
  proven only by a unit test that mocks the boundary it claims to
  exercise should be flagged in trade-offs.
- **One row per scenario**, not one row per test. If three tests
  exercise the same scenario from different angles, list them on
  one row separated by `<br/>`.
- **No row for "we added test X for function Y"** unless function Y
  IS a user-facing scenario boundary (e.g., a CLI command). Pure
  unit-test coverage of internal helpers does NOT belong here.
- **Bug-fix PRs MUST include the regression-trap test** -- the test
  that, had it existed before, would have caught the bug. Mark it
  with `(regression-trap for #<issue>)` in the test column.

## Anti-patterns -- refuse these

- **Implementation-language scenarios.** "`_preflight_auth_check`
  returns early when host is generic" is not a user promise. The
  user promise is "install on GHES does not prompt for credentials".
- **Empty principle column.** Every behavior change touches at
  least one APM principle, even if it is only "DevX". An empty
  column means the author has not asked the question.
- **Coverage-only mapping.** "Added 12 unit tests" without naming
  what user scenario each one proves is not scenario evidence; it
  is line-coverage theater.
- **All-unit, no-integration on a cross-module PR.** A change that
  reshapes how two modules interact (resolver + downloader, install
  + auth, marketplace + governance) needs at least one integration
  test that exercises the boundary, not just unit tests on each
  side.
- **Mocked boundary on a security scenario.** A "secure by default"
  scenario proven by a test that mocks the security boundary it is
  asserting on (e.g., mocking `validate_path_segments` to assert it
  was called) is not proof; it is tautology. Use a real malicious
  input.
- **Generic "tested manually".** Manual testing does not belong in
  this table. If the only proof is manual, the row goes in
  trade-offs ("scenario X covered by manual test only -- automated
  coverage tracked in #<issue>").

## Worked example

For a PR that adds GHES credential-helper support to
`apm install --update` (the PR #1084 shape):

| # | Scenario (user promise) | Principle(s) | Test(s) proving it | Type |
|---|------------------------|--------------|--------------------|------|
| 1 | `apm install --update` on a GHES repo with credential helper succeeds without prompting | Multi-harness support, Vendor-neutral, DevX | `tests/unit/install/test_pipeline_auth_preflight.py::test_ghes_credential_helper_succeeds` (regression-trap for #1082) | unit |
| 2 | `apm install --update` on github.com still locks down probe env (no helper inheritance) | Secure by default, Governed by policy | `tests/unit/install/test_pipeline_auth_preflight.py::test_github_locks_down_env` | unit |
| 3 | `is_github_hostname('ghes.corp.example.com')` correctly classifies as non-GitHub generic host | Vendor-neutral | `tests/unit/auth/test_host_classification.py::test_ghes_classified_as_generic` | unit |
| 4 | Auth failure on a generic host still raises (relaxed env != silent failure) | Secure by default, DevX | `tests/unit/install/test_pipeline_auth_preflight.py::test_generic_host_auth_failure_still_raises` | unit |

Notice: every row is a USER promise, every row names AT LEAST ONE
principle, and the regression-trap row explicitly cites the issue
it would have caught. A reviewer with 30 seconds can verify the
shape covers the change.

## Test-coverage-expert use

When the panel runs, the test-coverage-expert persona uses this
rubric in reverse: read the PR body's Scenario Evidence table,
then audit:

1. Does every behavior-change file in the diff appear in at least
   one scenario row's test? If a file is touched but no test in
   the table exercises a scenario through it, that is a finding
   (severity calibrated per the persona's contract).
2. Does the principle column reflect the actual surfaces touched?
   A PR that changes lockfile resolution and only claims "DevX"
   under principles is mis-mapped.
3. For bug fixes, is the regression-trap test present, and does it
   actually fail without the fix applied?

The persona's `view`/`grep` probe discipline still applies: never
emit "no test exists" without confirming via the test tree. Use
the Scenario Evidence table as the starting point, not the only
source.

## Skip clause

A PR may omit the Scenario Evidence table only if ONE of the
following is true (state which one in trade-offs):

- Pure docs change (no code touched).
- Pure asset bump (lockfile regeneration, dependency version pin
  in lockfile only, no behavior change).
- Pure refactor with NO behavior change AND existing tests still
  pass without modification (the existing tests ARE the scenario
  evidence; cite the suite that ran green).

Any other PR that omits the table is incomplete.
