"""Unit tests for the target-derived skills root helpers and skill-target compatibility."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.deployed_paths import skill_summary_paths
from apm_cli.install.target_filter import (
    explicit_target_failure,
    package_allows_target,
    resolve_effective_package_targets,
)
from apm_cli.integration.cleanup import _is_skill_directory_entry
from apm_cli.integration.skill_support import build_skill_ownership_maps
from apm_cli.integration.targets import KNOWN_TARGETS, skills_root_prefixes
from apm_cli.models.apm_package import APMPackage
from apm_cli.utils.diagnostics import CATEGORY_ERROR, CATEGORY_WARNING, DiagnosticCollector

pytestmark = pytest.mark.unit


class TestSkillsRoot:
    def test_default_targets_use_skills_subdir(self) -> None:
        assert KNOWN_TARGETS["gemini"].skills_rel_root == ".agents/skills"
        assert KNOWN_TARGETS["claude"].skills_rel_root == ".claude/skills"

    def test_deploy_root_wins_over_root_dir(self) -> None:
        assert KNOWN_TARGETS["codex"].skills_rel_root == ".agents/skills"

    def test_grok_bot_uses_workflows_subdir(self) -> None:
        profile = KNOWN_TARGETS["grok-bot"]
        assert profile.skills_subdir == "workflows"
        assert profile.skills_rel_root == "agent-data/workflows"

    def test_absolute_variant_honours_project_root(self, tmp_path: Path) -> None:
        profile = KNOWN_TARGETS["grok-bot"]
        assert profile.skills_deploy_path(tmp_path) == tmp_path / "agent-data" / "workflows"

    def test_absolute_variant_honours_user_scope(self, tmp_path: Path) -> None:
        user = KNOWN_TARGETS["grok-bot"].for_scope(user_scope=True)
        assert user is not None
        assert user.skills_deploy_path(tmp_path) == tmp_path / "agent-data" / "workflows"

    def test_resolved_deploy_root_is_used_verbatim(self, tmp_path: Path) -> None:
        from dataclasses import replace

        resolved = replace(KNOWN_TARGETS["grok-bot"], resolved_deploy_root=tmp_path / "elsewhere")
        assert resolved.skills_deploy_path(tmp_path / "project") == tmp_path / "elsewhere"

    def test_every_skills_target_deploy_path_matches_rel_root(self, tmp_path: Path) -> None:
        for profile in KNOWN_TARGETS.values():
            if not profile.supports("skills") or profile.user_root_resolver is not None:
                continue
            assert profile.skills_deploy_path(tmp_path) == tmp_path / profile.skills_rel_root

    def test_prefix_set_covers_known_roots(self) -> None:
        prefixes = skills_root_prefixes()
        assert {".claude/skills", ".agents/skills"} <= prefixes
        assert "agent-data/workflows" in prefixes


class TestSkillsOnly:
    def test_skills_only_is_derived_from_primitives(self) -> None:
        assert KNOWN_TARGETS["grok-bot"].skills_only
        assert KNOWN_TARGETS["agent-skills"].skills_only
        assert not KNOWN_TARGETS["copilot"].skills_only
        assert not KNOWN_TARGETS["claude"].skills_only


class TestSkillRootConsumers:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("agent-data/workflows/my-skill", True),
            ("agent-data/workflows/my-skill/", True),
            ("agent-data/workflows/my-skill/SKILL.md", False),
            ("agent-data/workflows", False),
            (".github/skills/my-skill", True),
            (".github/skills/my-skill/SKILL.md", False),
        ],
    )
    def test_skill_directory_entry(self, path: str, expected: bool) -> None:
        assert _is_skill_directory_entry(path.rstrip("/")) is expected

    def test_ownership_map_recognises_target_skills_roots(self, tmp_path: Path) -> None:
        import yaml

        lock = {
            "dependencies": [
                {
                    "repo_url": "owner/pkg",
                    "resolved_commit": "abc",
                    "deployed_files": ["agent-data/workflows/s/SKILL.md"],
                }
            ]
        }
        (tmp_path / "apm.lock.yaml").write_text(yaml.dump(lock), encoding="utf-8")

        _, native = build_skill_ownership_maps(tmp_path)

        assert native == {"agent-data/workflows/s/SKILL.md": "owner/pkg"}

    def test_summary_paths_use_target_skills_root(self, tmp_path: Path) -> None:
        deployed = tmp_path / "agent-data" / "workflows" / "s"
        targets = [KNOWN_TARGETS["grok-bot"], KNOWN_TARGETS["claude"]]

        assert skill_summary_paths([deployed], tmp_path, targets) == ["agent-data/workflows/"]
        assert skill_summary_paths([tmp_path / ".claude" / "skills" / "s"], tmp_path, targets) == [
            ".claude/skills/"
        ]


def _selection(
    targets: list[str],
    declared: list[str] | None,
) -> tuple:
    diagnostics = DiagnosticCollector()
    package = APMPackage(name="pkg", version="1.0.0", targets=declared)
    selection = resolve_effective_package_targets(
        [KNOWN_TARGETS[name] for name in targets],
        None,
        package,
        diagnostics,
        "pkg",
    )
    return selection, diagnostics


class TestAgentSkillsCompatibility:
    def test_agent_skills_admits_skills_only_target(self) -> None:
        allowed = frozenset({"agent-skills", "cursor"})
        assert package_allows_target(KNOWN_TARGETS["grok-bot"], allowed)

    def test_agent_skills_does_not_admit_multi_primitive_targets(self) -> None:
        allowed = frozenset({"agent-skills"})
        assert not package_allows_target(KNOWN_TARGETS["claude"], allowed)
        assert not package_allows_target(KNOWN_TARGETS["copilot"], allowed)

    def test_other_skills_only_targets_do_not_gain_compatibility(self) -> None:
        allowed = frozenset({"agent-skills", "cursor"})
        assert KNOWN_TARGETS["hermes"].skills_only
        assert not package_allows_target(KNOWN_TARGETS["hermes"], allowed)

    def test_other_declarations_do_not_admit_skills_only_targets(self) -> None:
        assert not package_allows_target(KNOWN_TARGETS["grok-bot"], frozenset({"cursor"}))

    def test_declared_name_still_matches(self) -> None:
        assert package_allows_target(KNOWN_TARGETS["cursor"], frozenset({"cursor"}))

    def test_resolution_keeps_grok_bot_for_agent_skills_package(self) -> None:
        selection, diagnostics = _selection(["grok-bot"], ["agent-skills", "cursor"])
        assert [t.name for t in selection.targets] == ["grok-bot"]
        assert diagnostics.count_for_package("pkg", CATEGORY_WARNING) == 0

    def test_resolution_keeps_only_compatible_targets(self) -> None:
        selection, _ = _selection(["grok-bot", "claude"], ["agent-skills"])
        assert [t.name for t in selection.targets] == ["grok-bot"]


def _node(name: str, declared: list[str] | None, *children: SimpleNamespace) -> SimpleNamespace:
    node = SimpleNamespace(
        package=APMPackage(name=name, version="1.0.0", targets=declared),
        dependency_ref=SimpleNamespace(target_subset=None, get_unique_key=lambda: name),
        children=list(children),
    )
    return node


def _profiles(*names: str) -> list:
    return [KNOWN_TARGETS[name] for name in names]


class TestAllTargetsFilteredOut:
    def test_no_overlap_is_always_a_warning(self) -> None:
        selection, diagnostics = _selection(["grok-bot"], ["cursor"])

        assert selection.targets == ()
        assert diagnostics.count_for_package("pkg", CATEGORY_ERROR) == 0
        assert diagnostics.count_for_package("pkg", CATEGORY_WARNING) == 1

    def test_partial_overlap_never_warns(self) -> None:
        selection, diagnostics = _selection(["grok-bot", "cursor"], ["cursor"])

        assert [t.name for t in selection.targets] == ["cursor"]
        assert diagnostics.count_for_package("pkg", CATEGORY_ERROR) == 0
        assert diagnostics.count_for_package("pkg", CATEGORY_WARNING) == 0

    def test_failure_when_no_package_deploys(self) -> None:
        only = _node("pkg", ["cursor"])

        message = explicit_target_failure(_profiles("grok-bot"), [only], [])

        assert message is not None
        assert "[cursor]" in message and "[grok-bot]" in message
        assert "Install with --target cursor, or ask the package author" in message
        assert "add grok-bot to its targets" in message
        assert message.isascii()

    def test_no_failure_when_any_package_deploys(self) -> None:
        child = _node("child", ["cursor"])
        parent = _node("parent", None, child)

        assert explicit_target_failure(_profiles("grok-bot"), [parent, child], []) is None

    def test_failure_when_named_package_subtree_is_dead(self) -> None:
        good = _node("good", None)
        bad = _node("bad", ["cursor"], _node("bad-child", ["cursor"]))
        nodes = [good, bad, *bad.children]

        assert explicit_target_failure(_profiles("grok-bot"), nodes, [good]) is None
        message = explicit_target_failure(_profiles("grok-bot"), nodes, [good, bad])
        assert message is not None and message.startswith("bad declares targets [cursor]")

    def test_named_package_with_live_child_is_not_a_failure(self) -> None:
        parent = _node("parent", ["cursor"], _node("child", None))

        assert (
            explicit_target_failure(_profiles("grok-bot"), [parent, *parent.children], [parent])
            is None
        )

    def test_hint_mentions_agent_skills_acceptance(self) -> None:
        message = explicit_target_failure(_profiles("claude"), [_node("pkg", ["agent-skills"])], [])

        assert message is not None
        assert "The grok-bot target accepts packages that declare agent-skills" in message
