# Lifecycle family layout and today's harness

Load at Step 0 and Step 3 when choosing the world, fixtures or file
location. Re-verify any path with `ls` or `grep` before citing it in a
PR; this map can go stale.

## TODAY -- harness in the tree (authoritative while `tests/lifecycle/` is absent)

Reference module: `tests/integration/test_required_lifecycle_state_machine.py`.
Read `_new_scenario`, `_Scenario`, `_publish`, `_run_success` and
`_assert_same_state` there before writing a new test. Note that its
`_INSTALL_ARGS` carries `--no-policy`; do not copy it.

| Need | Use |
|---|---|
| run the real CLI with evidence and timeouts | `tests/utils/apm_lifecycle_runner.py` `ApmLifecycleRunner(command).run(args, cwd=, env=)`, `run_sequence(...)`; `CommandResult` keeps `returncode`, `stdout`, `stderr` |
| hermetic HOME, APM_HOME, cache, temp, git config, loopback-only network guard | `tests/utils/isolated_apm_environment.py` `IsolatedApmEnvironment.create(root, base_env=)` (root must not exist yet), `subprocess_env()` |
| durable-state snapshot and equality | `tests/utils/lifecycle_state.py` `LifecycleStateSnapshot.capture(root, targets=, external_roots=)`, `LifecycleStateRoot` for user-scope roots; `_assert_same_state` in the reference module |
| exact write sets per root | `tests/utils/artifact_snapshot.py` `ArtifactSnapshotSet.capture({...})`, `assert_snapshot_changes_within`, `assert_only_snapshot_paths_changed`, `assert_snapshot_set_unchanged`, `assert_paths_absent` |
| packages | `tests/utils/local_package.py` `LocalPackageFactory.create(name, targets=, dependencies=, mcp_dependencies=, lsp_dependencies=)`, `add_skill`, `add_agent`, `add_instruction`, `add_prompt`, `add_command`, `add_hook`, `add_canvas`, `add_relative_dependency`, `write_policy`, `set_targets`. One factory must own both parent and child for `add_relative_dependency`. |
| git remotes | `tests/utils/local_git_repository.py` `LocalGitRepositoryFactory` (commit, tag, insteadOf URL rewrite), `tests/utils/local_git_http_server.py` |
| GitHub-shaped package for packaged-binary tests | `tests/utils/hermetic_packaged_sample.py`, fixture `hermetic_packaged_sample` |
| marketplace, MCP registry | `tests/utils/local_marketplace.py`, `tests/utils/local_mcp_registry.py` |
| org policy over loopback HTTPS | `tests/integration/_policy_scope_kit.py` (`PolicyScopeWorld`), `tests/integration/_target_policy_kit.py` |
| egress and header evidence | `tests/utils/local_scripted_https_proxy.py` (`requests_to`, `hosts_receiving`), `tests/utils/local_header_capture_proxy.py` |
| credential isolation | `tests/utils/git_credential_shim.py`, `tests/utils/git_credential_sentinel.py` |
| ADO auth chain | `tests/integration/_ado_auth_kit.py` |
| fault injection, smallest world | `tests/integration/_fault_injection_kit.py` `build_world(...)` -> `FaultWorld` (`apm()`, `install()`, `lockfile_bytes()`, `snapshot()`, `assert_user_file_intact()`, `assert_converges()`) |
| other shared worlds | `_containment_kit.py`, `_harness_output_kit.py`, `_install_audit_replay_kit.py` (I3), `_lockfile_chain_kit.py`, `_marketplace_pack_kit.py`, `_mcp_chain_kit.py`, `_path_corner_world_kit.py`, `_target_compile_kit.py` (all under `tests/integration/`) |
| skill-bundle graphs, declarative rows | `tests/utils/skill_selection_world.py`, `tests/utils/scenario_rows.py` |
| model-based exploration | `tests/integration/test_generated_lifecycle_state_machine.py` (Hypothesis) |
| CLI fixtures | `apm_binary_path` (packaged; `APM_BINARY_PATH` or a built binary, else skip) and `apm_engine_command` (`python -m apm_cli.cli`) in `tests/integration/conftest.py` |

Markers (`pyproject.toml`):

- Behavioral: exactly one module-level marker; lifecycle tests use
  `e2e`.
- Scheduling and prerequisite: `integration`, `requires_apm_binary`,
  `requires_e2e_mode` (needs `APM_E2E_TESTS=1`), `platform_sensitive`
  where behavior varies by OS.
- `lifecycle_smoke` only when the test belongs in the required
  PR-time Lifecycle Smoke job (`.github/workflows/ci.yml`) and is fast.
  `lifecycle_merge_group` keeps unique contracts in the merge-group
  suite. Markers schedule; they are not evidence.

Placement: `tests/integration/test_<promise>_lifecycle.py`, or extend
the module that already owns the promise
(`grep -rln '<promise words>' tests/integration`).

## TARGET -- `tests/lifecycle/` (in progress; use only when present)

Until `ls tests/lifecycle/conftest.py` succeeds this section describes
intent, not code. When it is present, verify each name below with
`ls`/`grep` before citing it.

```text
tests/lifecycle/
  conftest.py   world fixture, receipts plugin, global output hook
  world/        template world built once per xdist worker, cloned
                copy-on-write per test; packages (skill, executables with
                hooks + bin + MCP stdio, LSP, parent/child graph, native
                Claude and Codex plugins, canvas) and local services (git
                HTTPS server, marketplace, MCP registry, policy repo
                variants, scripted HTTPS proxy with egress log, host aliases)
  driver.py     driver.run(world, argv, scope=...); APM_LIFECYCLE_DRIVER
                selects engine (PR default) or binary (nightly)
  model/
    state.py    ModelState (pure)
    expected.py expected_deployed, expected_lock_rows, expected_exit,
                EXIT_CONTRACTS
  oracles.py    operation_for(args); check_output (global output hook);
                cross-cutting I1-I9 checks
  receipts.py   receipts.emit, callable only by the oracles; any other
                caller raises ReceiptForgeryError
  obligations.py  promise catalog, required/baseline ratchet, witness check
  rows.py       RowReplay, the replay-row runner
  replay/rows/*.yaml   replay rows
tests/quality/promise_obligations.json   P-INV-I1..I9 obligations
```

Planned, not yet present: state machines, scenario suites (history,
ownership, platform, failure phase, cache, graph/version,
concurrency), security tables and docs journeys. Use today's harness
for those until `ls` shows them.

Rules when present:

- Run commands only through `driver.run`; never pick the entry point.
- Row promise ids resolve only as `P-INV-I1`..`P-INV-I9` and
  `P-LAW-<ledger property id>` until a promise catalog exists; owner
  ids (`P-OWN-*`) resolve once it does.
- Row schema: `id`, `promises`, `cell`, `world{packages, targets,
  scope}`, `given`, `when`, `then{exit, deployed: expected, announce:
  <substring>}`. A step is a plain command line, `{run, exit}`, or
  `{pair: [<audit --ci line>, <install --frozen line>]}` (the I3
  probe). Scope is `project` only for now. Template in
  `assets/templates.md`.
- No `--no-policy` unless it is the subject.
- A new command, flag, target, source kind or owner regenerates
  obligations; new obligations fail the ratchet until covered.

Where a new test goes: an uncovered cell -> a replay row; anything
the row schema cannot express -> today's harness until the matching
suite exists.
