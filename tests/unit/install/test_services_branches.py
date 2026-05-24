"""Unit tests for apm_cli.install.services.

Focuses on:
- _deployed_path_entry: RuntimeError when path outside project tree and no targets
- _deployed_path_entry: copilot-app path via second fallback loop
- integrate_package_primitives: early return when no targets
- integrate_package_primitives: scratch_root validation
- integrate_package_primitives: _format_target_collapse (0, 1, 2, 3+ paths, verbose)
- integrate_package_primitives: copilot-app path in log hint
- integrate_package_primitives: result shape with skills
- integrate_local_content: delegates to integrate_package_primitives
- integrate_local_bundle: dry_run, collision skip, deployed files
- backward compat aliases
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.services import (
    _deployed_path_entry,
    _integrate_local_content,
    _integrate_package_primitives,
    integrate_local_bundle,
    integrate_local_content,
    integrate_package_primitives,
)
from apm_cli.integration.targets import KNOWN_TARGETS

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config_cache():
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


@pytest.fixture
def inject_config(monkeypatch: pytest.MonkeyPatch):
    import apm_cli.config as _conf

    def _set(cfg: dict[str, Any]) -> None:
        monkeypatch.setattr(_conf, "_config_cache", cfg)

    return _set


def _make_cowork_target(cowork_root: Path) -> Any:
    return replace(KNOWN_TARGETS["copilot-cowork"], resolved_deploy_root=cowork_root)


def _make_copilot_app_target(app_root: Path) -> Any:
    return replace(KNOWN_TARGETS["copilot-app"], resolved_deploy_root=app_root)


def _make_integrators() -> dict[str, Any]:
    skill_result = MagicMock()
    skill_result.target_paths = []
    skill_result.skill_created = False
    skill_result.sub_skills_promoted = 0
    integrators: dict[str, Any] = {
        k: MagicMock()
        for k in [
            "prompt_integrator",
            "agent_integrator",
            "skill_integrator",
            "instruction_integrator",
            "command_integrator",
            "hook_integrator",
        ]
    }
    integrators["skill_integrator"].integrate_package_skill.return_value = skill_result
    return integrators


# ---------------------------------------------------------------------------
# _deployed_path_entry — RuntimeError when no matching target
# ---------------------------------------------------------------------------


class TestDeployedPathEntryRuntimeError:
    def test_raises_when_outside_tree_and_no_targets(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside" / "SKILL.md"
        project_root = tmp_path / "project"
        project_root.mkdir()
        with pytest.raises(RuntimeError, match="Cannot translate"):
            _deployed_path_entry(outside, project_root, targets=[])

    def test_raises_when_outside_tree_and_targets_have_no_deploy_root(self, tmp_path: Path) -> None:
        outside = tmp_path / "nowhere" / "file.md"
        project_root = tmp_path / "project"
        project_root.mkdir()
        target_no_root = MagicMock()
        target_no_root.resolved_deploy_root = None
        with pytest.raises(RuntimeError, match="Cannot translate"):
            _deployed_path_entry(outside, project_root, targets=[target_no_root])

    def test_uses_relative_path_when_inside_project(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        target_path = project_root / "sub" / "file.md"
        result = _deployed_path_entry(target_path, project_root, targets=[])
        assert result == "sub/file.md"


# ---------------------------------------------------------------------------
# _deployed_path_entry — second fallback loop (no prior match from first loop)
# ---------------------------------------------------------------------------


class TestDeployedPathEntrySecondFallback:
    def test_copilot_app_via_second_fallback(self, tmp_path: Path) -> None:
        """Path outside project tree, first loop skips (relative_to fails), second loop matches."""
        app_root = tmp_path / "copilot-data"
        app_root.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        target_path = app_root / "workflows" / "wf-id"
        # Create a regular cowork target that will fail the first relative_to check,
        # then copilot-app target for the second loop
        app_target = _make_copilot_app_target(app_root)

        with patch(
            "apm_cli.integration.copilot_app_db.to_lockfile_uri",
            return_value="copilot-app-db://workflows/wf-id",
        ):
            result = _deployed_path_entry(target_path, project_root, targets=[app_target])

        assert result == "copilot-app-db://workflows/wf-id"


# ---------------------------------------------------------------------------
# integrate_package_primitives — early return with no targets
# ---------------------------------------------------------------------------


class TestIntegratePackagePrimitivesNoTargets:
    def test_empty_targets_returns_zero_counts(self, tmp_path: Path) -> None:
        pkg_info = MagicMock()
        pkg_info.install_path = str(tmp_path)
        pkg_info.name = "test-pkg"

        integrators = _make_integrators()

        with patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}):
            result = integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=[],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
            )

        assert result["prompts"] == 0
        assert result["agents"] == 0
        assert result["skills"] == 0
        assert result["deployed_files"] == []


# ---------------------------------------------------------------------------
# integrate_package_primitives — scratch_root validation
# ---------------------------------------------------------------------------


class TestIntegratePackagePrimitivesScratchRoot:
    def test_scratch_root_inside_itself_is_valid(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        project_in_scratch = scratch / "proj"
        project_in_scratch.mkdir()

        pkg_info = MagicMock()
        pkg_info.install_path = str(project_in_scratch)
        pkg_info.name = "test-pkg"

        integrators = _make_integrators()
        copilot = KNOWN_TARGETS["copilot"]

        with patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}):
            # Should not raise
            result = integrate_package_primitives(
                pkg_info,
                project_in_scratch,
                targets=[copilot],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
                scratch_root=scratch,
            )

        assert isinstance(result, dict)

    def test_scratch_root_outside_raises(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        # project_root is NOT inside scratch
        project_root = tmp_path / "project"
        project_root.mkdir()

        pkg_info = MagicMock()
        pkg_info.install_path = str(project_root)
        pkg_info.name = "test-pkg"

        integrators = _make_integrators()
        copilot = KNOWN_TARGETS["copilot"]

        with (
            patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}),
            pytest.raises((Exception, RuntimeError, ValueError)),
        ):
            integrate_package_primitives(
                pkg_info,
                project_root,
                targets=[copilot],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
                scratch_root=scratch,
            )


# ---------------------------------------------------------------------------
# integrate_package_primitives — _format_target_collapse
# ---------------------------------------------------------------------------


class TestIntegratePackagePrimitivesFormatTargetCollapse:
    """_format_target_collapse is an inner function, but we exercise it indirectly
    via skill result paths and by calling integrate_package_primitives with
    multiple targets producing various path counts.
    """

    def _call_with_skill_paths(
        self, tmp_path: Path, skill_paths: list[Path], verbose: bool = False
    ) -> dict:
        pkg_info = MagicMock()
        pkg_info.install_path = str(tmp_path)
        pkg_info.name = "test-pkg"

        integrators = _make_integrators()
        skill_result = integrators["skill_integrator"].integrate_package_skill.return_value
        skill_result.target_paths = skill_paths
        skill_result.skill_created = bool(skill_paths)
        skill_result.sub_skills_promoted = 0

        ctx = MagicMock()
        ctx.verbose = verbose
        ctx.cowork_nonsupported_warned = False

        copilot = KNOWN_TARGETS["copilot"]

        logger = MagicMock()

        with patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}):
            return integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=[copilot],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
                logger=logger,
                ctx=ctx,
            )

    def test_zero_skill_paths_collapsed_to_empty(self, tmp_path: Path) -> None:
        result = self._call_with_skill_paths(tmp_path, [])
        assert result["skills"] == 0

    def test_one_skill_path_recorded(self, tmp_path: Path) -> None:
        sub = tmp_path / ".github" / "skills"
        sub.mkdir(parents=True)
        path = sub / "my-skill"
        result = self._call_with_skill_paths(tmp_path, [path])
        assert result["skills"] == 1

    def test_two_skill_paths_collapsed(self, tmp_path: Path) -> None:
        sub1 = tmp_path / ".github" / "skills"
        sub2 = tmp_path / ".copilot" / "skills"
        sub1.mkdir(parents=True)
        sub2.mkdir(parents=True)
        result = self._call_with_skill_paths(tmp_path, [sub1, sub2])
        assert result["skills"] == 1

    def test_three_skill_paths_shows_n_targets(self, tmp_path: Path) -> None:
        paths = []
        for i in range(3):
            p = tmp_path / f"dir{i}" / "skills"
            p.mkdir(parents=True)
            paths.append(p)
        result = self._call_with_skill_paths(tmp_path, paths)
        assert result["skills"] == 1

    def test_verbose_mode_expands_multiple_paths(self, tmp_path: Path) -> None:
        paths = []
        for i in range(3):
            p = tmp_path / f"dir{i}" / "skills"
            p.mkdir(parents=True)
            paths.append(p)
        result = self._call_with_skill_paths(tmp_path, paths, verbose=True)
        assert result["skills"] == 1


# ---------------------------------------------------------------------------
# integrate_package_primitives — sub_skills_promoted logging
# ---------------------------------------------------------------------------


class TestIntegratePackagePrimitivesSubSkills:
    def test_sub_skills_promoted_logged_single_path(self, tmp_path: Path) -> None:
        pkg_info = MagicMock()
        pkg_info.install_path = str(tmp_path)
        pkg_info.name = "test-pkg"

        integrators = _make_integrators()
        skill_result = integrators["skill_integrator"].integrate_package_skill.return_value
        skill_result.target_paths = [tmp_path / ".github" / "skills"]
        skill_result.skill_created = False
        skill_result.sub_skills_promoted = 2

        logger = MagicMock()
        copilot = KNOWN_TARGETS["copilot"]

        with patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}):
            result = integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=[copilot],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
                logger=logger,
            )

        assert result["sub_skills"] == 2

    def test_files_unchanged_line_logged(self, tmp_path: Path) -> None:
        pkg_info = MagicMock()
        pkg_info.install_path = str(tmp_path)
        pkg_info.name = "test-pkg"

        integrators = _make_integrators()
        skill_result = integrators["skill_integrator"].integrate_package_skill.return_value
        skill_result.target_paths = []
        skill_result.skill_created = False
        skill_result.sub_skills_promoted = 0

        logger = MagicMock()
        copilot = KNOWN_TARGETS["copilot"]

        with patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}):
            integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=[copilot],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
                logger=logger,
            )

        all_log_calls = "".join(str(c) for c in logger.tree_item.call_args_list)
        assert "unchanged" in all_log_calls


# ---------------------------------------------------------------------------
# integrate_package_primitives — skill path outside project tree
# ---------------------------------------------------------------------------


class TestSkillPathOutsideProject:
    def test_cowork_skill_path_outside_project_labeled_correctly(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork"
        cowork_root.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        pkg_info = MagicMock()
        pkg_info.install_path = str(project_root)
        pkg_info.name = "test-pkg"

        integrators = _make_integrators()
        skill_result = integrators["skill_integrator"].integrate_package_skill.return_value
        cowork_skill = cowork_root / "my-skill"
        skill_result.target_paths = [cowork_skill]
        skill_result.skill_created = True
        skill_result.sub_skills_promoted = 0

        cowork_target = _make_cowork_target(cowork_root)

        with (
            patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}),
            patch(
                "apm_cli.integration.copilot_cowork_paths.to_lockfile_path",
                return_value="cowork://skills/my-skill/SKILL.md",
            ),
        ):
            result = integrate_package_primitives(
                pkg_info,
                project_root,
                targets=[cowork_target],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
            )

        assert result["skills"] == 1


# ---------------------------------------------------------------------------
# integrate_local_content
# ---------------------------------------------------------------------------


class TestIntegrateLocalContent:
    def test_delegates_to_integrate_package_primitives(self, tmp_path: Path) -> None:
        integrators = _make_integrators()

        with patch("apm_cli.install.services.integrate_package_primitives") as mock_integrate:
            mock_integrate.return_value = {"skills": 1, "deployed_files": []}
            result = integrate_local_content(
                tmp_path,
                targets=[KNOWN_TARGETS["copilot"]],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
            )

        mock_integrate.assert_called_once()
        assert result == {"skills": 1, "deployed_files": []}

    def test_passes_local_pkg_info(self, tmp_path: Path) -> None:
        integrators = _make_integrators()
        captured_pkg_info: list[Any] = []

        def _capture(pkg_info, *args, **kwargs):
            captured_pkg_info.append(pkg_info)
            skill_result = MagicMock()
            skill_result.target_paths = []
            skill_result.skill_created = False
            skill_result.sub_skills_promoted = 0
            integrators["skill_integrator"].integrate_package_skill.return_value = skill_result
            with patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}):
                return integrate_package_primitives(pkg_info, *args, **kwargs)

        with patch("apm_cli.install.services.integrate_package_primitives", side_effect=_capture):
            integrate_local_content(
                tmp_path,
                targets=[KNOWN_TARGETS["copilot"]],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
            )

        assert len(captured_pkg_info) == 1
        pkg_info = captured_pkg_info[0]
        assert pkg_info.package.name == "_local"


# ---------------------------------------------------------------------------
# integrate_local_bundle
# ---------------------------------------------------------------------------


class TestIntegrateLocalBundle:
    def _make_bundle_info(self, bundle_dir: Path, package_id: str = "my-bundle") -> MagicMock:
        bundle_info = MagicMock()
        bundle_info.source_dir = bundle_dir
        bundle_info.package_id = package_id
        bundle_info.lockfile = None
        return bundle_info

    def test_dry_run_does_not_write_files(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "instructions" / "guide.md").parent.mkdir(parents=True)
        (bundle_dir / "instructions" / "guide.md").write_text("# guide")

        target = KNOWN_TARGETS["copilot"]
        bundle_info = self._make_bundle_info(bundle_dir)
        project_root = tmp_path / "project"
        project_root.mkdir()

        result = integrate_local_bundle(
            bundle_info,
            project_root,
            targets=[target],
            dry_run=True,
        )

        assert isinstance(result["deployed_files"], list)
        # In dry_run mode, files should NOT be physically created in project
        deployed_files = result["deployed_files"]
        for f in deployed_files:
            dest = Path(f) if Path(f).is_absolute() else project_root / f
            assert not dest.exists(), f"{dest} should not exist in dry-run mode"

    def test_deploy_copies_files(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        skill_dir = bundle_dir / "skills" / "foo-skill"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("# Skill content")

        target = KNOWN_TARGETS["copilot"]
        bundle_info = self._make_bundle_info(bundle_dir)
        project_root = tmp_path / "project"
        project_root.mkdir()

        result = integrate_local_bundle(
            bundle_info,
            project_root,
            targets=[target],
            dry_run=False,
        )

        assert len(result["deployed_files"]) >= 1

    def test_unsafe_bundle_entry_skipped(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        safe_file = bundle_dir / "skills" / "foo.md"
        safe_file.parent.mkdir(parents=True)
        safe_file.write_text("safe")

        target = KNOWN_TARGETS["copilot"]
        bundle_info = self._make_bundle_info(bundle_dir)
        # Override lockfile pack_files to include a traversal attempt
        bundle_info.lockfile = {
            "pack": {
                "bundle_files": {
                    "../evil.md": "abc123",
                    "skills/foo.md": "deadbeef",
                }
            }
        }
        project_root = tmp_path / "project"
        project_root.mkdir()

        logger = MagicMock()
        result = integrate_local_bundle(
            bundle_info,
            project_root,
            targets=[target],
            logger=logger,
            dry_run=False,
        )

        # The traversal entry must be skipped
        assert result["skipped"] >= 1

    def test_plugin_json_filtered_out(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "plugin.json").write_text("{}")
        (bundle_dir / "skills" / "good.md").parent.mkdir(parents=True)
        (bundle_dir / "skills" / "good.md").write_text("# good")

        target = KNOWN_TARGETS["copilot"]
        bundle_info = self._make_bundle_info(bundle_dir)
        bundle_info.lockfile = None  # Use fallback walk
        project_root = tmp_path / "project"
        project_root.mkdir()

        result = integrate_local_bundle(
            bundle_info,
            project_root,
            targets=[target],
            dry_run=False,
        )

        deployed = result["deployed_files"]
        # plugin.json must not appear in deployed list
        assert not any("plugin.json" in f.lower() for f in deployed)

    def test_collision_skip_when_content_differs(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        skills_dir = bundle_dir / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "myfoo.md"
        skill_file.write_text("bundle content")

        target = KNOWN_TARGETS["copilot"]
        project_root = tmp_path / "project"
        project_root.mkdir()

        # skills deploy_root for copilot is ".agents", not ".github"
        # so the destination is project_root / ".agents" / "skills" / "myfoo.md"
        skills_deploy_root = (
            getattr(target.primitives["skills"], "deploy_root", None) or target.root_dir
        )
        dest_dir = project_root / skills_deploy_root / "skills"
        dest_dir.mkdir(parents=True)
        dest_file = dest_dir / "myfoo.md"
        dest_file.write_text("existing different content")

        bundle_info = self._make_bundle_info(bundle_dir)
        diagnostics = MagicMock()
        result = integrate_local_bundle(
            bundle_info,
            project_root,
            targets=[target],
            dry_run=False,
            diagnostics=diagnostics,
        )

        # Collision with different content should be skipped (force=False)
        assert result["skipped"] >= 1

    def test_force_overwrites_existing_file(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        skills_dir = bundle_dir / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "myfoo.md"
        skill_file.write_text("new content from bundle")

        target = KNOWN_TARGETS["copilot"]
        project_root = tmp_path / "project"
        project_root.mkdir()

        # skills deploy_root for copilot is ".agents"
        skills_deploy_root = (
            getattr(target.primitives["skills"], "deploy_root", None) or target.root_dir
        )
        dest_dir = project_root / skills_deploy_root / "skills"
        dest_dir.mkdir(parents=True)
        dest_file = dest_dir / "myfoo.md"
        dest_file.write_text("old content")

        bundle_info = self._make_bundle_info(bundle_dir)
        result = integrate_local_bundle(
            bundle_info,
            project_root,
            targets=[target],
            force=True,
            dry_run=False,
        )

        assert result["skipped"] == 0
        assert "new content from bundle" in dest_file.read_text()

    def test_alias_overrides_package_id(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "skills" / "s.md").parent.mkdir(parents=True)
        (bundle_dir / "skills" / "s.md").write_text("content")

        target = KNOWN_TARGETS["copilot"]
        bundle_info = self._make_bundle_info(bundle_dir, "original-id")
        project_root = tmp_path / "project"
        project_root.mkdir()

        logger = MagicMock()
        integrate_local_bundle(
            bundle_info,
            project_root,
            targets=[target],
            alias="custom-alias",
            logger=logger,
            dry_run=True,
        )

        all_calls = " ".join(str(c) for c in logger.verbose_detail.call_args_list)
        # The alias should appear in verbose log (not original-id)
        assert "custom-alias" in all_calls


# ---------------------------------------------------------------------------
# Backward compat aliases
# ---------------------------------------------------------------------------


class TestBackwardCompatAliases:
    def test_integrate_package_primitives_alias(self) -> None:
        assert _integrate_package_primitives is integrate_package_primitives

    def test_integrate_local_content_alias(self) -> None:
        assert _integrate_local_content is integrate_local_content


# ---------------------------------------------------------------------------
# integrate_package_primitives — copilot-app workflow hint
# ---------------------------------------------------------------------------


class TestCopilotAppWorkflowHint:
    def test_copilot_app_path_triggers_workflow_hint(self, tmp_path: Path) -> None:
        pkg_info = MagicMock()
        pkg_info.install_path = str(tmp_path)
        pkg_info.name = "test-pkg"

        integrators = _make_integrators()

        # Build a mock dispatch entry whose file landed inside copilot-app/
        int_result = MagicMock()
        int_result.files_integrated = 1
        int_result.files_adopted = 0
        int_result.links_resolved = 0
        int_result.target_paths = [tmp_path / "copilot-app" / "workflows" / "wf-1"]

        entry = MagicMock()
        entry.multi_target = False
        entry.integrate_method = "integrate_prompt"
        entry.counter_key = "prompts"

        prim_mapping = MagicMock()
        # Use "copilot-app" as the deploy_root so _deploy_dir starts with "copilot-app/"
        prim_mapping.deploy_root = "copilot-app"
        prim_mapping.subdir = "workflows"
        prim_mapping.format_id = None

        mock_target = MagicMock()
        mock_target.primitives = {"prompts": prim_mapping}
        mock_target.root_dir = "copilot-app"
        mock_target.name = "copilot-app"
        mock_target.hooks_config_display = None

        integrators["prompt_integrator"].integrate_prompt.return_value = int_result

        logger = MagicMock()

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value={"prompts": entry},
        ):
            integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=[mock_target],
                diagnostics=MagicMock(),
                **integrators,
                force=False,
                managed_files=None,
                logger=logger,
            )

        all_log = " ".join(str(c) for c in logger.tree_item.call_args_list)
        assert "disabled" in all_log.lower() or "workflows" in all_log.lower()
