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
