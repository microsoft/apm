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

## TARGET -- `tests/lifecycle/` (in progress; use only if it exists)

Until `ls tests/lifecycle/conftest.py` succeeds this section describes
intent, not code. Do not cite these files as present.

```text
tests/lifecycle/
  conftest.py  driver selection, world fixture, receipts plugin, global output oracle
  world/       builder.py: template world built once per xdist worker
               packages.py: p-skill, p-exec (hooks + bin + MCP stdio), p-lsp,
                 p-parent / p-parent2 -> p-child, p-native-claude,
                 p-native-codex, p-canvas
               services.py: local git HTTPS server, local marketplace, local
                 MCP registry, policy repo variants, scripted HTTPS proxy
                 with egress log, host aliases
               clone.py: copy-on-write clone per test
  model/       state.py ModelState; expected.py expected_deployed(),
               expected_lock_rows(), expected_exit()
  driver.py    APM_LIFECYCLE_DRIVER=engine|binary
  oracles.py   check_cross_cutting(before, after, result, model, op) for I1-I9
  receipts.py  written by the oracle only, never by the caller
  machines/    test_m_spine, test_m_gate, test_m_source, test_m_scope_lock
               (<= 10 rules each; deployed == expected_deployed(model) and
               I1-I9 after every step)
  replay/      rows/*.yaml (given / when / then) + test_covering_replay.py
  scenarios/   history_upgrade, ownership_siblings, platform, failure_phase,
               cache_presence, graph_version, concurrency
  security/    gate removal, policy outcomes, integrity, egress,
               origin and credentials tables
  journeys/    consumer/, producer/, enterprise/ *.yaml, goldens/*.txt,
               test_journeys.py, regen_goldens.py
tests/quality/test_promise_obligations.py   quality gate
```

Where a new test goes in the target family:

- an uncovered cell: a replay row;
- a transition rule: the matching machine;
- a defect outside the factor vocabulary: a scenario suite;
- a documented flow: a journey step.

Rules: every replay row names promise ids that resolve in the promise
catalog; no `--no-policy` unless it is the subject; a new command,
flag, target, source kind or owner regenerates obligations, and new
obligations fail the quality gate until covered.
