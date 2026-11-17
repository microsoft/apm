"""Mapping and rewrite contract for ``apm manifest migrate`` (#3762)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.core.target_detection import EffectiveTargetDecision
from apm_cli.models.manifest_target_migration import (
    ManifestMigrationError,
    plan_manifest_migration,
    plan_target_migration,
)
from apm_cli.models.manifest_targets import ManifestTargetDeclaration

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_ALL_INSTALL_SET = (
    "claude",
    "codex",
    "cursor",
    "gemini",
    "grok-build",
    "kiro",
    "opencode",
    "vscode",
    "windsurf",
)


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("claude", ("claude",)),
        ("vscode", ("vscode",)),
        ("copilot", ("copilot",)),
        ("agents", ("copilot",)),
        ("agy", ("antigravity",)),
        ("AGY", ("antigravity",)),
        ("claude,vscode", ("claude", "vscode")),
        ("claude, copilot, vscode", ("claude", "copilot", "vscode")),
        (["claude", "agents"], ("claude", "copilot")),
        (["cursor", "agy"], ("cursor", "antigravity")),
        ("agent-skills", ("agent-skills",)),
        ("all", _ALL_INSTALL_SET),
        ("all,agent-skills", (*_ALL_INSTALL_SET, "agent-skills")),
    ],
)
def test_legacy_target_maps_to_equivalent_targets_list(legacy, expected):
    plan = plan_target_migration(legacy)

    assert plan.targets == expected
    legacy_decision = EffectiveTargetDecision(
        ManifestTargetDeclaration.from_mapping({"target": legacy}).selection, "apm.yml"
    )
    migrated_decision = EffectiveTargetDecision(
        ManifestTargetDeclaration.from_mapping({"targets": list(plan.targets)}).selection,
        "apm.yml",
    )
    assert set(migrated_decision.canonical_targets) == set(legacy_decision.canonical_targets)
    assert set(migrated_decision.runtime_targets) == set(legacy_decision.runtime_targets)
    assert set(migrated_decision.lsp_targets) == set(legacy_decision.lsp_targets)


def test_vscode_keeps_its_mcp_runtime_instead_of_folding_to_copilot():
    plan = plan_target_migration("vscode")

    decision = EffectiveTargetDecision(list(plan.targets), "apm.yml")
    assert decision.runtime_targets == ("vscode",)


def test_target_all_uses_the_effective_decision_expansion():
    plan = plan_target_migration("all")

    assert plan.targets == EffectiveTargetDecision("all", "apm.yml").runtime_targets


@pytest.mark.parametrize(("legacy", "expanded"), [("all", True), ("all,agent-skills", True)])
def test_target_all_is_flagged_as_expanded(legacy, expanded):
    assert plan_target_migration(legacy).expanded_all is expanded


@pytest.mark.parametrize("token", ["intellij", "copilot-app", "copilot-cowork", "openclaw"])
def test_unmappable_tokens_are_refused_with_recovery(token):
    with pytest.raises(ManifestMigrationError) as excinfo:
        plan_target_migration(token)

    assert str(excinfo.value) == (
        f"Target '{token}' has no 'targets:' equivalent. Remove '{token}' from 'target:' "
        f"in apm.yml and pass '--target {token}' to 'apm install' instead."
    )


def test_unmappable_token_inside_a_list_is_refused():
    with pytest.raises(ManifestMigrationError, match="Target 'intellij'"):
        plan_target_migration("claude,intellij")


def test_invalid_legacy_value_fails_like_install():
    with pytest.raises(ValueError, match="not a valid target"):
        plan_target_migration("nope")


def _write(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def test_rewrite_preserves_comments_and_dependency_subsets(tmp_path):
    manifest = _write(
        tmp_path / "apm.yml",
        """
        # Project manifest
        name: demo
        version: 1.0.0
        # Where to deploy
        target: claude,vscode  # two harnesses
        dependencies:
          apm:
            - path: ./pkg
              targets: [claude]
        """,
    )

    migration = plan_manifest_migration(manifest)

    assert migration.changed
    assert migration.migrated_text == textwrap.dedent(
        """\
        # Project manifest
        name: demo
        version: 1.0.0
        # Where to deploy
        targets: [claude, vscode] # two harnesses
        dependencies:
          apm:
            - path: ./pkg
              targets: [claude]
        """
    )
    assert "+targets: [claude, vscode] # two harnesses" in migration.diff()
    assert manifest.read_text(encoding="utf-8").count("target: claude,vscode") == 1


def test_rewrite_keeps_block_list_item_and_trailing_comments(tmp_path):
    manifest = _write(
        tmp_path / "apm.yml",
        """
        name: demo
        version: 1.0.0
        target:
          - claude  # first
          - agents  # second
        # trailing
        description: x
        """,
    )

    migration = plan_manifest_migration(manifest)

    assert migration.migrated_text == textwrap.dedent(
        """\
        name: demo
        version: 1.0.0
        targets:
          - claude  # first
          - copilot # second
        # trailing
        description: x
        """
    )


def test_long_expansion_is_written_as_block_list(tmp_path):
    manifest = _write(tmp_path / "apm.yml", "name: demo\nversion: 1.0.0\ntarget: all\n")

    migration = plan_manifest_migration(manifest)

    expected_items = "".join(f"  - {name}\n" for name in _ALL_INSTALL_SET)
    assert migration.migrated_text == f"name: demo\nversion: 1.0.0\ntargets:\n{expected_items}"


def test_empty_legacy_key_is_removed(tmp_path):
    manifest = _write(tmp_path / "apm.yml", "name: demo\nversion: 1.0.0\ntarget:\n")

    migration = plan_manifest_migration(manifest)

    assert migration.plan.targets is None
    assert migration.migrated_text == "name: demo\nversion: 1.0.0\n"


def test_migrated_manifest_is_a_no_op(tmp_path):
    manifest = _write(tmp_path / "apm.yml", "name: demo\nversion: 1.0.0\ntarget: agy\n")
    plan_manifest_migration(manifest).write()

    again = plan_manifest_migration(manifest)

    assert (
        manifest.read_text(encoding="utf-8")
        == "name: demo\nversion: 1.0.0\ntargets: [antigravity]\n"
    )
    assert again.changed is False
    assert again.diff() == ""


def test_both_keys_are_rejected(tmp_path):
    manifest = _write(
        tmp_path / "apm.yml", "name: demo\nversion: 1.0.0\ntarget: claude\ntargets: [claude]\n"
    )

    with pytest.raises(ValueError, match="target"):
        plan_manifest_migration(manifest)


def test_plan_does_not_write(tmp_path):
    original = "name: demo\nversion: 1.0.0\ntarget: claude\n"
    manifest = _write(tmp_path / "apm.yml", original)

    plan_manifest_migration(manifest)

    assert manifest.read_text(encoding="utf-8") == original
