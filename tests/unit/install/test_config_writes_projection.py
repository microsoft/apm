"""install-commit-order owner: the config files an install projects match what it writes (#4956)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.adapters.client.vscode import VSCodeClientAdapter
from apm_cli.core.target_detection import EffectiveTargetDecision
from apm_cli.install.commit_gate import refuse_unmanaged_files
from apm_cli.install.config_writes import (
    _materialized_roots,
    mcp_config_files,
    writing_runtimes,
)
from apm_cli.policy.schema import UnmanagedFilesPolicy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-install-commit-order")]

_KIRO = ".kiro/settings/mcp.json"


def test_an_explicitly_selected_target_root_is_projected_before_it_exists(tmp_path: Path) -> None:
    decision = EffectiveTargetDecision(["kiro"], "--target")

    roots = _materialized_roots(decision, user_scope=False)

    assert mcp_config_files(["kiro"], project_root=tmp_path, user_scope=False) == ()
    assert mcp_config_files(
        ["kiro"], project_root=tmp_path, user_scope=False, materialized_roots=roots
    ) == (_KIRO,)
    assert _materialized_roots(decision, user_scope=True) == []


def test_projecting_a_vscode_config_never_creates_the_directory(tmp_path: Path) -> None:
    mcp_config_files(["vscode"], project_root=tmp_path, user_scope=False)

    assert not (tmp_path / ".vscode").exists()
    assert VSCodeClientAdapter(project_root=tmp_path).peek_config_path() == str(
        tmp_path / ".vscode" / "mcp.json"
    )


def test_a_dependency_restricted_to_claude_does_not_reach_kiro() -> None:
    restricted = SimpleNamespace(name="srv", targets=["claude"])

    assert writing_runtimes(["claude", "kiro"], [restricted], None) == ["claude"]
    assert writing_runtimes(["claude", "kiro"], [SimpleNamespace(name="all")], None) == [
        "claude",
        "kiro",
    ]


def test_a_runtime_that_holds_recorded_servers_stays_projected() -> None:
    restricted = SimpleNamespace(name="srv", targets=["claude"])
    lock = SimpleNamespace(mcp_target_servers={"kiro": ["old"], "claude": []})

    assert writing_runtimes(["claude", "kiro"], [restricted], lock) == ["claude", "kiro"]


class _Logger:
    def __init__(self) -> None:
        self.violations: list[tuple[str, str]] = []

    def policy_violation(self, dep_ref: str, reason: str, severity: str, **_kw: object) -> None:
        self.violations.append((severity, f"{dep_ref} {reason}"))


def test_unmanaged_files_warn_reports_a_projected_config_without_blocking(tmp_path: Path) -> None:
    (tmp_path / ".kiro").mkdir()
    policy = SimpleNamespace(
        enforcement="block",
        unmanaged_files=UnmanagedFilesPolicy(action="warn"),
    )
    logger = _Logger()

    refuse_unmanaged_files(
        logger,
        policy,
        tmp_path,
        None,
        dry_run=False,
        projected_writes=(_KIRO,),
    )

    assert [severity for severity, _ref in logger.violations] == ["warn"]
    assert _KIRO in logger.violations[0][1]
