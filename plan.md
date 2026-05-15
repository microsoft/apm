# Duplicate Code Reduction Plan

CI guardrail + 4-phase strangling fig to progressively eliminate
duplicated code blocks in `src/apm_cli/`.

## Tool

**pylint R0801** (cross-file similarity detection).

Ruff has no duplicate-code rule. pylint is Python-native, supports
`--min-similarity-lines` as a tuneable lever, and integrates with the
existing `uv run --extra dev` workflow.

## Strangling Fig Phases

| Phase | Threshold | Violations | Status |
|-------|-----------|-----------|--------|
| 0 | 50 | 0 | This PR -- guardrail only |
| 1 | 25 | 2 | Separate PR |
| 2 | 15 | 6 | Separate PR |
| 3 | 10 | 19 | Separate PR |

Each phase: lower the `--min-similarity-lines` threshold, fix the
violations that surface, then update CI to enforce zero violations
at the new level.

## Phase 0 (this PR)

Add pylint R0801 to CI with `--min-similarity-lines=50`. Zero
violations exist at this threshold, so no code changes are needed.
This prevents new large duplicated blocks from being introduced.

## Phase 1 -- threshold 25 (2 violations)

1. `copilot.py:617-696 <-> cursor.py:200-261` -- registry package
   config (npm/docker/pypi/homebrew/generic dispatch). ~51 lines.
2. `codex.py:419-476 <-> copilot.py:813-882` -- environment variable
   resolution + prompting logic. ~57 lines.

Fix: extract `_build_package_config()` and
`_resolve_environment_variables()` into `MCPClientAdapter` base class.

## Phase 2 -- threshold 15 (4 additional violations)

3. `copilot.py:568-591 <-> cursor.py:158-180` -- MCP auth/header
   injection. ~23 lines.
4. `codex.py:300-325 <-> copilot.py:670-696` -- pypi/homebrew/generic
   config subset. ~26 lines (subset of V1).
5. `copilot.py:617-646 <-> cursor.py:200-223` -- package extraction
   preamble. ~29 lines.
6. `claude_formatter.py:281-308 <-> distributed_compiler.py:542-565`
   -- instruction rendering + footer. ~24 lines.

Fix: extract auth injection and instruction rendering into shared
helpers. V4-V5 are subsets of V1 and resolve when V1 is fixed.

## Phase 3 -- threshold 10 (13 additional violations)

7-19. Smaller clones across adapter clients, integrators, deps,
marketplace, policy, and runtime modules. See the full impact report
in the PR description for per-violation details.

Fix: lift shared logic to base classes, remove deprecated shims,
consolidate utility helpers.

## Tests

Tests (`tests/`) have 4.64% duplication. This is expected for test
fixtures and parametrised setups. Test deduplication is deferred to
a separate effort after `src/` stabilises. Tests can later be added
to the pylint scope with a higher threshold.

## Hotspot

70% of duplication (13/19 violations) is in `adapters/client/`.
The root cause: `codex.py` extends `MCPClientAdapter` directly
instead of `CopilotClientAdapter`, duplicating env resolution,
package config, and argument processing.