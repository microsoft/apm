# Family map: where a lifecycle test lives and which helpers it uses

Load at Step 0 and Step 3. Two sections: TODAY (authoritative while
`tests/lifecycle/` does not exist) and TARGET (the family under
construction). Re-verify any path with `ls` or `grep` before citing it
in a PR; this map can go stale.

## TODAY -- harness in the tree now

### Canonical module

`tests/integration/test_required_lifecycle_state_machine.py` is the
reference shape. Read `_new_scenario`, `_Scenario` and
`_assert_same_state` there before writing a new test. The bounded
model-based companion is
`tests/integration/test_generated_lifecycle_state_machine.py`
(Hypothesis `RuleBasedStateMachine`).

### Core helpers (`tests/utils/`)

| File | Use |
|---|---|
| `apm_lifecycle_runner.py` | `ApmLifecycleRunner(command).run(args, cwd=, env=)` and `run_sequence(...)` drive the REAL CLI as a subprocess; `CommandResult` keeps `returncode`, `stdout`, `stderr` separately. Not Click's `CliRunner`, not Python APIs. |
| `isolated_apm_environment.py` | `IsolatedApmEnvironment.create(root, base_env=)` builds a hermetic HOME, APM_HOME, cache, temp and git config with a loopback-only network guard; `subprocess_env()` returns the child env. |
| `lifecycle_state.py` | `LifecycleStateSnapshot.capture(root, targets=, external_roots=)` records manifest bytes, lockfile bytes, deployment records, MCP and LSP state, file bytes and semantic bytes. `LifecycleStateRoot` adds user-scope roots (for example `root_id="apm-home"`). |
| `artifact_snapshot.py` | `assert_unchanged`, `assert_only_paths_changed`, `assert_paths_absent`, `assert_snapshot_changes_within` for write-set checks (I5). |
| `local_package.py` | `LocalPackageFactory.create(name, targets=, dependencies=, mcp_dependencies=, lsp_dependencies=)` plus `add_skill`, `add_agent`, `add_instruction`, `add_prompt`, `add_command`, `add_hook`, `add_canvas`, `add_relative_dependency`, `write_policy`, `set_targets`. One factory must own both parent and child for relative dependencies. |
| `local_git_repository.py` | `LocalGitRepositoryFactory` commits, tags and installs insteadOf URL rewrites so remote-shaped refs resolve to local bare repos. |
| `hermetic_packaged_sample.py` | GitHub-shaped package for packaged-binary tests (fixture `hermetic_packaged_sample` in `tests/integration/conftest.py`). |
| `local_marketplace.py`, `local_mcp_registry.py` | Hermetic marketplace and MCP registry sources. |
| `local_git_http_server.py`, `local_github_https_fixture.py`, `local_scripted_https_proxy.py`, `local_header_capture_proxy.py` | Loopback HTTP(S) origins; the header-capture and scripted proxies give an egress log for absence checks. |
| `git_credential_shim.py`, `git_credential_sentinel.py` | Credential isolation checks. |
| `skill_selection_world.py`, `scenario_rows.py` | Skill-bundle graphs and declarative scenario records. |

### Shared worlds (`tests/integration/_*_kit.py`)

| Kit | World |
|---|---|
| `_fault_injection_kit.py` | `build_world(...)` -> `FaultWorld` with `apm()`, `install()`, `lockfile_bytes()`, `snapshot()`, `assert_user_file_intact()`, `assert_converges()`. Smallest entry point. |
| `_containment_kit.py` | Project-containment guards. |
| `_harness_output_kit.py` | Harness-output scenarios on the installed binary. |
| `_install_audit_replay_kit.py` | Install vs `audit --ci` replay chains (I3). |
| `_lockfile_chain_kit.py` | Two-tag package for lockfile and cold-restore chains. |
| `_marketplace_pack_kit.py` | Marketplace, pack, export-patch and view chains. |
| `_mcp_chain_kit.py` | MCP project chain. |
| `_path_corner_world_kit.py` | Paths with spaces and non-ASCII. |
| `_policy_scope_kit.py` | Org policy and user-scope chains. |
| `_target_compile_kit.py`, `_target_policy_kit.py` | Multi-target compile and target-policy chains. |

### Fixtures and markers

- `apm_binary_path` (session fixture, `tests/integration/conftest.py`):
  resolves `APM_BINARY_PATH` or a built binary, else skips.
- `apm_engine_command`: `python -m apm_cli.cli` for engine contracts
  that need in-process instrumentation. Not packaged-binary proof.
- Markers (`pyproject.toml`): `integration`, `e2e`,
  `lifecycle_smoke` (required PR-time Lifecycle Smoke job in
  `.github/workflows/ci.yml`), `lifecycle_merge_group`,
  `requires_apm_binary`, `requires_e2e_mode`, `platform_sensitive`.
  Add `lifecycle_smoke` only when the test must gate every PR and is
  fast; the marker is a scheduling choice, not evidence.

### Placement

New file: `tests/integration/test_<promise>_lifecycle.py`, module-level
`pytestmark`. Prefer extending an existing module that already owns the
promise (`grep -rln '<promise words>' tests/integration`).

## TARGET -- `tests/lifecycle/` (in progress)

Use this section only after `ls tests/lifecycle/conftest.py` succeeds.
Until then it describes intent, not code.

```text
tests/lifecycle/
  conftest.py        driver selection, world fixture, receipts plugin,
                     global output oracle
  world/
    builder.py       template world built once per xdist worker
    packages.py      p-skill, p-exec (hooks + bin + MCP stdio), p-lsp,
                     p-parent / p-parent2 -> p-child, p-native-claude,
                     p-native-codex, p-canvas
    services.py      local git HTTPS server, local marketplace, local
                     MCP registry, policy repo variants, scripted HTTPS
                     proxy with egress log, host aliases
    clone.py         copy-on-write clone per test
  model/
    state.py         ModelState
    expected.py      expected_deployed(), expected_lock_rows(),
                     expected_exit()
  driver.py          APM_LIFECYCLE_DRIVER=engine|binary
  oracles.py         check_cross_cutting(before, after, result, model, op)
                     for I1-I9
  receipts.py        written by the oracle only
  machines/          test_m_spine, test_m_gate, test_m_source,
                     test_m_scope_lock; <= 10 rules each; assert
                     deployed == expected_deployed(model) plus I1-I9
                     after every step
  replay/rows/*.yaml + test_covering_replay.py
  scenarios/         history_upgrade, ownership_siblings, platform,
                     failure_phase, cache_presence, graph_version,
                     concurrency
  security/          gate removal, policy outcomes, integrity, egress,
                     origin and credentials tables
  journeys/{consumer,producer,enterprise}/*.yaml, goldens/*.txt,
                     test_journeys.py, regen_goldens.py
tests/quality/test_promise_obligations.py   quality gate
tests/fixtures/lifecycle_bug_ledger.json    ledger (exists today)
```

Rules for the target family:

- Every replay row names promise ids that resolve in the promise
  catalog. Template: `assets/templates/replay-row.yaml`.
- No `--no-policy` unless that flag is the subject.
- A new command, flag, target, source kind or owner regenerates the
  obligations; new obligations fail the quality gate until covered.
- Machines stay small (<= 10 rules); split by shared code entry, not
  by command name.
