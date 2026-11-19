"""Audit replay applies the install gate only to gated lock entries (#3924)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.exec_gate import ReplayExecAllowMap


def _write_manifest(root: Path) -> None:
    (root / "apm.yml").write_text(
        "name: p\nversion: 1.0.0\nexecutables:\n  deny:\n    ../hooky:\n      hooks: true\n",
        encoding="utf-8",
    )


@pytest.fixture
def discovered(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    calls: list[str | None] = []

    def _discover(project_root, *, policy_override=None, **_kwargs):
        calls.append(policy_override)
        return SimpleNamespace(policy=None)

    monkeypatch.setattr("apm_cli.policy.discovery.discover_policy_with_chain", _discover)
    return calls


@pytest.mark.parametrize("status", [None, "absent", "deployed"])
def test_deployed_entries_replay_ungated(
    tmp_path: Path, discovered: list[str | None], status: str | None
) -> None:
    _write_manifest(tmp_path)
    allow = ReplayExecAllowMap(tmp_path).for_dependency(SimpleNamespace(exec_status=status))
    assert allow is None
    assert discovered == []


def test_gated_entry_replays_the_gate_without_policy_under_no_policy(
    tmp_path: Path, discovered: list[str | None]
) -> None:
    _write_manifest(tmp_path)
    gate = ReplayExecAllowMap(tmp_path, no_policy=True)
    allow = gate.for_dependency(SimpleNamespace(exec_status="denied"))
    assert allow is not None
    assert "hooks" not in allow.get("../hooky", {})
    assert discovered == []


def test_explicit_policy_source_reaches_discovery(
    tmp_path: Path, discovered: list[str | None]
) -> None:
    _write_manifest(tmp_path)
    gate = ReplayExecAllowMap(tmp_path, no_policy=True, policy_override="acme/policy")
    gate.for_dependency(SimpleNamespace(exec_status="gated_pending_approval"))
    gate.for_dependency(SimpleNamespace(exec_status="denied"))
    assert discovered == ["acme/policy"]
