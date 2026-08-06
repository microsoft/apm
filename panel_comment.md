## APM Review Panel: `ship_now`

> Clean GA graduation of Copilot Cowork target. Eight panelists, zero blocking findings. Ship now.

cc @sergio-sisternes-epam @danielmeppiel -- a fresh advisory pass is ready for your review.

All eight active panelists returned unanimous non-blocking verdicts. No specialist disagreements to resolve. The PR is a textbook owner-extension graduation: two lines removed from the experimental gate, with correct ripple handling across project-scope gating, ghost-deployment cleanup, fuzzy-match routing, and the GRADUATED_FLAGS registry. The supply-chain finding (sec-001) references a file outside this PR's diff and is discarded. The growth-hacker's grw-001 (drop Experimental badge from integration page title) is confirmed addressed: astro.config.mjs sidebar label was updated in the PR diff, so this finding is resolved at HEAD. The integration-test timeout that ejected the PR from the merge queue is a pre-existing infrastructure flake in test_architecture_authorities.py (15-min shard limit), not a regression from this PR; all HEAD checks are green.

The doc-writer's doc-001 (cross-link mismatch between detection table and config section) and tst-001 (integration-tier fixture gap for project-scope gating) are the only two recommended findings with real substance. Both are low-risk deferred items: doc-001 is a docs polish that does not block correctness, and tst-001 adds defense-in-depth but the unit-tier tests already cover the behavioral surface. Neither justifies holding a GA graduation that unlocks the feature for the entire user base.

**Aligned with:** MANIFESTO and PRD -- graduating a validated-in-production feature removes friction for users who depend on Copilot Cowork. Architecture ownership is correct -- modifies canonical owners in target_catalog.py and target_detection.py, no parallel registries created. GRADUATED_FLAGS enforces the breaking-change contract with clear migration instructions.

### Panel summary

| Persona | B | R | N | Takeaway |
|---|---|---|---|---|
| Python Architect | 0 | 0 | 2 | Clean owner-extension graduation. The PR modifies target_catalog.py and target_detection.py without creating parallel authority. GRADUATED_FLAGS registry is well-designed with correct precedence logic. No blocking findings. |
| CLI Logging Expert | 0 | 0 | 2 | CLI output changes are solid. The warning/error split between explicit CLI and implicit selection is the right UX. The graduated-flag migration messages are clear and actionable. |
| DevX UX Expert | 0 | 0 | 2 | Well-executed graduation. Migration path is clear: stale scripts fail loudly with the correct next command. The fuzzy-match fix for graduated flags is a genuine UX win. |
| Supply Chain Security | 0 | 0 | 0 | No supply-chain or security regressions. No new attack surface. Copilot-cowork remains explicit-only and user-scope-only. |
| OSS Growth Hacker | 0 | 2 | 3 | Strong GA graduation with good migration docs and smoke-test package. Breaking changes are well-communicated. |
| Doc Writer | 0 | 1 | 2 | Documentation for the Cowork GA graduation is structurally sound. One recommended fix on cross-link mismatch in detection table. |
| Test Coverage Expert | 0 | 1 | 1 | All six behavioral surfaces have dedicated test coverage with correct tier placement. No blocking gaps. |
| Performance Expert | 0 | 0 | 4 | No measurable performance regression. All new code paths are O(k) where k is the number of resolved targets (1-5). |

> B = blocking-severity findings, R = recommended, N = nits.
> Counts are signal strength, not gates. The maintainer ships.

### Top 5 follow-ups

1. **[Doc Writer]** Fix detection-table cross-link -- The macOS OneDrive detection table says "recommends APM_COPILOT_COWORK_SKILLS_DIR" but its cross-link points to a section that shows only `apm config set`. Add the env var to the First-run setup section or update the table text to match. (`docs/src/content/docs/integrations/copilot-cowork.md`)
2. **[Test Coverage Expert]** Add integration-tier fixture test for project-scope gating -- The project-scope gating tests are unit-tier; a single integration test running `apm install` against a fixture apm.yml containing `targets: [copilot-cowork]` at project scope would close the floor gap. (`tests/integration/`)
3. **[OSS Growth Hacker]** Lead the integration guide with the smoke-test package as a 60-second proof -- The smoke-test package is an underused adoption accelerator; make it the opening 'Verify it works' step in the integration guide. (`docs/src/content/docs/integrations/copilot-cowork.md`)
4. **[OSS Growth Hacker]** CHANGELOG Removed entry should name the replacement command inline -- "Removed X. **Migration:** use Y instead." pattern prevents support issues from users who don't follow through to the migration guide. (`CHANGELOG.md`)
5. **[Doc Writer]** migration.md exposes internal snake_case JSON key -- Replace `copilot_cowork` (raw JSON key) with "the experimental config entry" or the user-facing `copilot-cowork` in the migration note. (`docs/src/content/docs/troubleshooting/migration.md`)

<details>
<summary>Merge-queue integration failure diagnosis</summary>

**Root cause.** Integration Tests Shard 1 timed out in the merge queue (15-min limit) because `test_architecture_authorities.py` tests each spawn `shutil.copytree` + `subprocess.run scripts/lint-architecture-boundaries.sh`, taking ~60s each. At the observed rate the shard reaches only 12% completion in 15 minutes.

**Is this PR the cause?** No. The same timeout affects every PR currently in the queue (pr-2360, pr-2492, pr-2494, pr-2495, pr-2496 all show the same pattern). PR 2503 does NOT add any architecture authority tests. The run ID for PR 2503's Integration Tests failure is 31083531222.

**Resolution.** The PR was removed from the queue after the timeout. The author rebased and addressed the Copilot review comments (commit 00ecee9), then re-enqueued. All HEAD checks are green. Re-enqueue to retry the Integration Tests with the current head.

</details>

<details>
<summary>Full per-persona findings</summary>

**python-architect**
- py-001 (nit): `validate_flag_name` generator expression reuses `name` which shadows the function parameter -- rename to `candidate` for clarity.
- py-002 (nit): ValueError in validate_flag_name carries 4 positional args decoded by index in commands/experimental.py -- consider a small dataclass for future-proofing.

**cli-logging-expert**
- cli-001 (nit): Redundant `symbol="warning"` kwarg on `ctx.logger.warning()` in `_gate_cowork_target` -- CommandLogger.warning() already defaults to symbol="warning".
- cli-002 (nit): Graduated-flag error emits a trailing period ("'copilot-cowork' is no longer an experimental flag.") but the hard-error path omits it -- pick one convention (codebase omits periods).

**devx-ux-expert**
- dx-001 (nit): Project-scope implicit-skip warning does not name the file that selected the target; adding the source (apm.yml vs config default) would help users with both configured.
- dx-002 (nit): compile.md has a dangling line after reflow -- tighten to <=80 chars.

**doc-writer**
- doc-001 (recommended): Detection table says "recommends APM_COPILOT_COWORK_SKILLS_DIR" but cross-link points to a section showing only `apm config set` -- add the env var or update the table text.
- doc-002 (nit): migration.md surfaces the internal snake_case key `copilot_cowork` -- use "the experimental config entry" or the user-facing `copilot-cowork`.
- doc-003 (nit): compile.md: "so it is also excluded" -- drop "also" since explicit-only IS the only reason.

**test-coverage-expert**
- tst-001 (recommended): Project-scope gating tests are unit-tier only -- add one integration fixture test running `apm install` against a fixture apm.yml with copilot-cowork to close the floor gap.
- tst-002 (nit): Near-miss routing precedence test could be parametrized with additional edge cases to strengthen the regression trap against future graduated entries.

**performance-expert**
- perf-001 through perf-004 (all nits): Two sequential any() scans, deferred import, difflib on error path, and two v2 filter comprehensions -- all measured at < 100ns total on the hot path. No action needed.

**oss-growth-hacker**
- grw-001 (resolved at HEAD): Integration page Experimental badge -- confirmed removed in astro.config.mjs diff.
- grw-002 (recommended): Smoke-test package should be the '60-second proof' opening of the integration guide.
- grw-003 (nit): CHANGELOG Removed entry should inline the replacement command.
- grw-004 (nit): README target list could mention Copilot Cowork as a story beat.
- grw-005 (nit): Verify migration.md Cowork section has a before/after code block for the --target all --global behavior change.

**auth-expert**
- Not activated: No auth-surface files touched.

</details>

---
*Shepherd iteration 1. Owner-evidence gate: ordinary-fix on Accepted target vocabulary (core/target_catalog.py) and Effective install target selection (core/target_detection.py). Lint: CLEAN. Architecture boundary lint: CLEAN.*
