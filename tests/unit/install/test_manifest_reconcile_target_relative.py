"""Regression tests for target-relative user-scope compatibility paths."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.core.scope import InstallScope
from apm_cli.install.manifest_reconcile import reconcile_deployed_block, union_preserving
from apm_cli.integration.cleanup import CleanupResult, remove_stale_deployed_files
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.diagnostics import DiagnosticCollector


def test_reconcile_reconstructs_external_opencode_user_locator(tmp_path: Path, monkeypatch) -> None:
    external_root = tmp_path / "opencode-config"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "changed-default"))
    target = replace(
        KNOWN_TARGETS["opencode"].for_scope(user_scope=True),
        root_dir=external_root.as_posix(),
    )

    files, _, ledger = union_preserving(
        current_files=["skills/reviewer/SKILL.md"],
        current_hashes={},
        prior_files=[],
        prior_hashes={},
        targets=[target],
        declared_targets=[target],
        include_ledger=True,
        user_scope=True,
        owner="pkg",
    )

    record = next(iter(ledger.records.values()))
    assert files == ["skills/reviewer/SKILL.md"]
    assert record.locator.kind.value == "target-relative"
    assert record.locator.target == "opencode"
    assert record.locator.scope == InstallScope.USER.value


def test_reconcile_does_not_accept_parent_traversal_as_target_relative(tmp_path: Path) -> None:
    target = replace(
        KNOWN_TARGETS["opencode"].for_scope(user_scope=True),
        root_dir=str(tmp_path / "opencode-config"),
    )

    _, _, ledger = union_preserving(
        current_files=["../outside/SKILL.md"],
        current_hashes={},
        prior_files=[],
        prior_hashes={},
        targets=[target],
        declared_targets=[target],
        include_ledger=True,
        user_scope=True,
        owner="pkg",
    )

    record = next(iter(ledger.records.values()))
    assert record.locator.kind.value == "project-relative"


def test_reconcile_falls_back_when_external_targets_share_relative_path(tmp_path: Path) -> None:
    external_root = tmp_path / "shared-config"
    first_target = replace(
        KNOWN_TARGETS["opencode"].for_scope(user_scope=True),
        root_dir=external_root.as_posix(),
    )
    second_target = replace(
        KNOWN_TARGETS["agent-skills"].for_scope(user_scope=True),
        root_dir=external_root.as_posix(),
    )

    _, _, ledger = union_preserving(
        current_files=["skills/reviewer/SKILL.md"],
        current_hashes={},
        prior_files=[],
        prior_hashes={},
        targets=[first_target, second_target],
        declared_targets=[first_target, second_target],
        include_ledger=True,
        user_scope=True,
        owner="pkg",
    )

    record = next(iter(ledger.records.values()))
    assert record.locator.kind.value == "project-relative"


def test_reconcile_tracks_same_value_by_target_identity(tmp_path: Path) -> None:
    value = ".cursor/config.json"
    deleted = DeploymentLocator(LocatorKind.PROJECT_RELATIVE, "claude", value, None, "project")
    retained = DeploymentLocator(LocatorKind.PROJECT_RELATIVE, "cursor", value, None, "project")
    prior_ledger = DeploymentLedger(
        records={
            deleted.key: DeploymentRecord(deleted, ("pkg",), "pkg", None),
            retained.key: DeploymentRecord(retained, ("pkg",), "pkg", None),
        }
    )

    cleanup_result = CleanupResult(
        deleted=[value],
        retained_locators=[retained],
        deleted_locators=[deleted],
    )

    with patch(
        "apm_cli.integration.cleanup.remove_stale_deployed_files",
        return_value=cleanup_result,
    ):
        _, _, ledger = reconcile_deployed_block(
            project_root=tmp_path,
            dep_key="pkg",
            current_files=[value],
            current_hashes={value: "sha256:current"},
            prior_files=[value],
            prior_hashes={},
            active_targets=[KNOWN_TARGETS["cursor"]],
            declared_targets=[KNOWN_TARGETS["cursor"]],
            diagnostics=DiagnosticCollector(),
            prior_ledger=prior_ledger,
            include_ledger=True,
        )

    assert deleted.key not in ledger.records
    assert retained.key in ledger.records


def test_cleanup_same_value_removes_only_deleted_locator(tmp_path: Path) -> None:
    """Compatibility values must not substitute for locator identity."""
    target_a = DeploymentLocator(
        LocatorKind.PROJECT_RELATIVE, "claude", ".claude/same.md", None, "project"
    )
    target_b = DeploymentLocator(
        LocatorKind.PROJECT_RELATIVE, "cursor", ".claude/same.md", None, "project"
    )
    path = tmp_path / ".claude" / "same.md"
    path.parent.mkdir()
    path.write_text("owned", encoding="utf-8")
    result = remove_stale_deployed_files(
        [".claude/same.md"],
        tmp_path,
        dep_key="pkg",
        targets=[KNOWN_TARGETS["claude"]],
        diagnostics=DiagnosticCollector(),
        locator_mapping={".claude/same.md": (target_a,)},
    )
    assert not path.exists()
    assert result.deleted_locators == [target_a]
    assert target_b not in result.deleted_locators


def test_cleanup_records_locator_for_already_missing_path(tmp_path: Path) -> None:
    locator = DeploymentLocator(
        LocatorKind.PROJECT_RELATIVE, "claude", ".claude/missing.md", None, "project"
    )
    result = remove_stale_deployed_files(
        [".claude/missing.md"],
        tmp_path,
        dep_key="pkg",
        targets=[KNOWN_TARGETS["claude"]],
        diagnostics=DiagnosticCollector(),
        locator_mapping={".claude/missing.md": locator},
    )
    assert result.deleted_locators == [locator]


def test_cleanup_legacy_result_keeps_string_deleted_values(tmp_path: Path) -> None:
    result = remove_stale_deployed_files(
        [".claude/legacy.md"],
        tmp_path,
        dep_key="pkg",
        targets=[KNOWN_TARGETS["claude"]],
        diagnostics=DiagnosticCollector(),
    )
    assert result.deleted_locators == []
    assert result.deleted_values == []
