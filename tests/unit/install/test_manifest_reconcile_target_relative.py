"""Regression tests for target-relative user-scope compatibility paths."""

from dataclasses import replace
from pathlib import Path

from apm_cli.core.scope import InstallScope
from apm_cli.install.manifest_reconcile import union_preserving
from apm_cli.integration.targets import KNOWN_TARGETS


def test_reconcile_reconstructs_external_opencode_user_locator() -> None:
    external_root = Path("/tmp/opencode-config")
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


def test_reconcile_does_not_accept_parent_traversal_as_target_relative() -> None:
    target = replace(
        KNOWN_TARGETS["opencode"].for_scope(user_scope=True),
        root_dir="/tmp/opencode-config",
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


def test_reconcile_falls_back_when_external_targets_share_relative_path() -> None:
    external_root = Path("/tmp/shared-config")
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
