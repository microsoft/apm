"""Audit replay applies the install gate only to gated lock entries (#3924)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.exec_gate import ReplayExecAllowMap


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        "apm_cli.security.executables._user_config_file", lambda: tmp_path / "config.json"
    )
    (tmp_path / "apm.yml").write_text(
        "name: p\nversion: 1.0.0\nexecutables:\n  deny:\n    ../hooky:\n      hooks: true\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def discovered(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def _discover(project_root, **_kwargs):
        calls.append(project_root)
        return SimpleNamespace(policy=None)

    monkeypatch.setattr("apm_cli.policy.discovery.discover_policy_with_chain", _discover)
    return calls


@pytest.mark.parametrize("status", [None, "absent", "deployed"])
def test_deployed_entries_replay_ungated(
    project: Path, discovered: list[Path], status: str | None
) -> None:
    provider_calls: list[int] = []
    gate = ReplayExecAllowMap(project, lambda: provider_calls.append(1))
    assert gate.for_dependency(SimpleNamespace(exec_status=status)) is None
    assert provider_calls == []
    assert discovered == []


def test_gated_entries_use_the_callers_policy_once(project: Path, discovered: list[Path]) -> None:
    provider_calls: list[int] = []

    def provider():
        provider_calls.append(1)

    gate = ReplayExecAllowMap(project, provider)
    allow = gate.for_dependency(SimpleNamespace(exec_status="denied"))
    gate.for_dependency(SimpleNamespace(exec_status="gated_pending_approval"))
    assert allow is not None
    assert "hooks" not in allow.get("../hooky", {})
    assert provider_calls == [1]
    assert discovered == []


def test_without_a_provider_replay_discovers_policy(project: Path, discovered: list[Path]) -> None:
    ReplayExecAllowMap(project).for_dependency(SimpleNamespace(exec_status="denied"))
    assert discovered == [project]


def _hook_package(root: Path) -> SimpleNamespace:
    from apm_cli.deps.lockfile import LockedDependency

    (root / ".apm" / "hooks").mkdir(parents=True)
    (root / ".apm" / "hooks" / "guard.json").write_text('{"hooks": {}}', encoding="utf-8")
    lock_dep = LockedDependency(
        repo_url="_local/hooky", source="local", local_path="../hooky", exec_status="deployed"
    )
    return SimpleNamespace(
        lock_dep=lock_dep,
        package_info=SimpleNamespace(
            dependency_ref=lock_dep.to_dependency_ref(), install_path=root
        ),
    )


@pytest.mark.parametrize("status", [None, "deployed"])
def test_entry_denied_since_install_replays_gated(
    project: Path, discovered: list[Path], tmp_path: Path, status: str | None
) -> None:
    """A deny no install has reconciled withholds the hooks, so leftovers drift (#3958)."""
    package = _hook_package(tmp_path / "hooky")
    package.lock_dep.exec_status = status
    allow = ReplayExecAllowMap(project, lambda: None).for_dependency(
        package.lock_dep, package.package_info
    )
    assert allow is not None
    assert "hooks" not in allow.get("../hooky", {})


def test_deployed_entry_still_allowed_replays_ungated(
    project: Path, discovered: list[Path], tmp_path: Path
) -> None:
    (project / "apm.yml").write_text(
        "name: p\nversion: 1.0.0\nexecutables:\n  allow:\n    ../hooky:\n      hooks: true\n",
        encoding="utf-8",
    )
    package = _hook_package(tmp_path / "hooky")
    gate = ReplayExecAllowMap(project, lambda: None)
    assert gate.for_dependency(package.lock_dep, package.package_info) is None
    package.lock_dep.exec_status = None
    assert gate.for_dependency(package.lock_dep, package.package_info) is None


def test_project_self_entry_replays_ungated_under_deny(
    project: Path, discovered: list[Path], tmp_path: Path
) -> None:
    package = _hook_package(tmp_path / "hooky")
    package.lock_dep.local_path = "."
    gate = ReplayExecAllowMap(project, lambda: None)
    assert gate.for_dependency(package.lock_dep, package.package_info) is None


@pytest.mark.parametrize(
    ("recorded", "current", "mixed", "stale"),
    [
        (None, None, False, False),
        (None, "deployed", False, False),
        ("deployed", None, False, False),
        (None, "gated_pending_approval", False, True),
        ("deployed", "denied", False, True),
        ("gated_pending_approval", "deployed", False, True),
        ("denied", "denied", False, False),
        # One aggregate status cannot show which of a mixed package's types changed.
        ("denied", "denied", True, True),
        ("deployed", "deployed", True, False),
    ],
)
def test_recorded_exec_status_staleness(
    recorded: str | None, current: str | None, mixed: bool, stale: bool
) -> None:
    from apm_cli.install.exec_gate import recorded_exec_status_is_stale

    assert recorded_exec_status_is_stale(recorded, current, mixed=mixed) is stale


def _locked_hook_package(modules: Path, *, codex_native: bool = False) -> SimpleNamespace:
    from apm_cli.deps.lockfile import LockedDependency

    lock_dep = LockedDependency(
        repo_url="_local/hooky", source="local", local_path="../hooky", exec_status="deployed"
    )
    root = lock_dep.to_dependency_ref().get_install_path(modules)
    if codex_native:
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin" / "plugin.json").write_text(
            '{"name": "hooky", "version": "1.0.0"}', encoding="utf-8"
        )
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
    else:
        _hook_package(root)
    return SimpleNamespace(dependencies={lock_dep.get_unique_key(): lock_dep})


@pytest.mark.parametrize("codex_native", [False, True], ids=["split", "codex-native"])
def test_update_probe_flags_a_deny_no_install_reconciled(
    project: Path, tmp_path: Path, codex_native: bool
) -> None:
    from apm_cli.install.exec_gate import lock_exec_decisions_stale

    modules = tmp_path / "apm_modules"
    lock = _locked_hook_package(modules, codex_native=codex_native)
    assert lock_exec_decisions_stale(lock, modules, project / "apm.yml", policy_provider=lambda: None)


def test_update_probe_short_circuits_when_the_decision_holds(project: Path, tmp_path: Path) -> None:
    from apm_cli.install.exec_gate import lock_exec_decisions_stale

    (project / "apm.yml").write_text(
        "name: p\nversion: 1.0.0\nexecutables:\n  allow:\n    ../hooky:\n      hooks: true\n",
        encoding="utf-8",
    )
    modules = tmp_path / "apm_modules"
    lock = _locked_hook_package(modules)
    assert not lock_exec_decisions_stale(lock, modules, project / "apm.yml", policy_provider=lambda: None)


def test_update_probe_runs_the_gate_when_policy_is_unavailable(
    project: Path, tmp_path: Path
) -> None:
    from apm_cli.install.exec_gate import ExecPolicyUnavailable, lock_exec_decisions_stale

    (project / "apm.yml").write_text(
        "name: p\nversion: 1.0.0\nexecutables:\n  allow:\n    ../hooky:\n      hooks: true\n",
        encoding="utf-8",
    )
    modules = tmp_path / "apm_modules"
    lock = _locked_hook_package(modules)

    def unavailable():
        raise ExecPolicyUnavailable("cache_miss_fetch_fail")

    assert lock_exec_decisions_stale(lock, modules, project / "apm.yml", policy_provider=unavailable)
