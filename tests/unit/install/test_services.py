"""Unit tests for apm_cli.install.services (_deployed_path_entry and Amendment 6 warning)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict  # noqa: F401, UP035
from unittest.mock import MagicMock, call, patch  # noqa: F401

import pytest

from apm_cli.install.services import _deployed_path_entry
from apm_cli.integration.targets import KNOWN_TARGETS

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset the in-process config cache before and after every test."""
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


@pytest.fixture
def inject_config(monkeypatch: pytest.MonkeyPatch):
    """Directly inject a dict into the config cache -- no disk I/O."""
    import apm_cli.config as _conf

    def _set(cfg: dict[str, Any]) -> None:
        monkeypatch.setattr(_conf, "_config_cache", cfg)

    return _set


def _make_cowork_target(cowork_root: Path) -> Any:
    """Return a TargetProfile with resolved_deploy_root set for cowork.

    Args:
        cowork_root: The resolved cowork skills root directory.

    Returns:
        A frozen TargetProfile suitable for cowork tests.
    """
    return replace(KNOWN_TARGETS["copilot-cowork"], resolved_deploy_root=cowork_root)


# ---------------------------------------------------------------------------
# TestDeployedPathEntry
# ---------------------------------------------------------------------------


class TestDeployedPathEntry:
    """Tests for _deployed_path_entry lockfile path generation."""

    def test_relative_path_for_project_target(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        target_path = project_root / ".github" / "skills" / "foo" / "SKILL.md"
        result = _deployed_path_entry(target_path, project_root, targets=[])
        assert result == ".github/skills/foo/SKILL.md"

    def test_cowork_uri_for_out_of_tree_path(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork-skills"
        cowork_root.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        target_path = cowork_root / "my-skill" / "SKILL.md"
        cowork_target = _make_cowork_target(cowork_root)

        with patch(
            "apm_cli.integration.copilot_cowork_paths.to_lockfile_path",
            return_value="cowork://skills/my-skill/SKILL.md",
        ):
            result = _deployed_path_entry(target_path, project_root, targets=[cowork_target])
        assert result == "cowork://skills/my-skill/SKILL.md"

    def test_runtime_error_when_no_matching_target(self, tmp_path: Path) -> None:
        """Out-of-tree path with no dynamic-root target must raise, not silently store an absolute path."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        target_path = tmp_path / "outside" / "file.md"
        with pytest.raises(RuntimeError, match="This is a bug"):
            _deployed_path_entry(target_path, project_root, targets=[])

    def test_path_traversal_error_propagates_from_cowork_translation(self, tmp_path: Path) -> None:
        """PathTraversalError from to_lockfile_path must propagate, never be swallowed."""
        from apm_cli.utils.path_security import PathTraversalError

        cowork_root = tmp_path / "cowork-skills"
        cowork_root.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        # target_path is deliberately outside the cowork_root
        target_path = tmp_path / "evil" / "escape.md"
        cowork_target = _make_cowork_target(cowork_root)

        with pytest.raises(PathTraversalError):
            _deployed_path_entry(target_path, project_root, targets=[cowork_target])

    @pytest.mark.parametrize(
        "dir_prefix",
        [".github", ".claude", ".cursor", ".codex"],
    )
    def test_deployed_path_entry_non_cowork_lockfile_unchanged_parametrised(
        self, dir_prefix: str, tmp_path: Path
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        target_path = project_root / dir_prefix / "sub" / "file.md"
        result = _deployed_path_entry(target_path, project_root, targets=[])
        expected = f"{dir_prefix}/sub/file.md"
        assert result == expected


# ---------------------------------------------------------------------------
# TestAmendment6Warning
# ---------------------------------------------------------------------------


class TestAmendment6Warning:
    """Tests for the cowork non-skill primitive warning in integrate_package_primitives."""

    def _make_ctx(self, cowork_active: bool = True) -> MagicMock:
        """Build a minimal ctx mock for Amendment 6 testing.

        Args:
            cowork_active: Whether cowork_nonsupported_warned starts False.

        Returns:
            A MagicMock configured as an InstallContext.
        """
        ctx = MagicMock()
        ctx.cowork_nonsupported_warned = False
        return ctx

    def _make_pkg_info(self, tmp_path: Path, non_skill_dirs: list[str] | None = None) -> MagicMock:
        """Create a package info mock with optional non-skill directories.

        Args:
            tmp_path: Base temp directory.
            non_skill_dirs: Subdirectory names under .apm/ to create.

        Returns:
            A MagicMock configured as PackageInfo.
        """
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        apm_dir = pkg_dir / ".apm"
        apm_dir.mkdir(exist_ok=True)
        if non_skill_dirs:
            for d in non_skill_dirs:
                sub = apm_dir / d
                sub.mkdir(exist_ok=True)
                (sub / "placeholder.md").write_text("# content")
        pkg = MagicMock()
        pkg.install_path = pkg_dir
        pkg.name = "test-pkg"
        return pkg

    def test_warning_fires_once_per_run_with_non_skill_primitives(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": True}})
        from apm_cli.install.services import integrate_package_primitives

        cowork_target = _make_cowork_target(tmp_path / "cowork")
        copilot = KNOWN_TARGETS["copilot"]
        targets = [copilot, cowork_target]

        pkg_info = self._make_pkg_info(tmp_path, ["agents"])
        logger = MagicMock()
        ctx = self._make_ctx()

        # Mock all integrators to avoid real dispatch
        integrators = {
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
        # Make skill_integrator.integrate_package_skill return a result
        skill_result = MagicMock()
        skill_result.target_paths = []
        skill_result.skill_created = False
        skill_result.sub_skills_promoted = 0
        integrators["skill_integrator"].integrate_package_skill.return_value = skill_result

        # Mock dispatch table to skip integration loops
        mock_dispatch = {}
        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value=mock_dispatch,
        ):
            integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="test-pkg",
                logger=logger,
                ctx=ctx,
                **integrators,
                force=False,
                managed_files=None,
            )

        # Warning should have fired
        warning_calls = [c for c in logger.warning.call_args_list if "cowork" in str(c).lower()]
        assert len(warning_calls) == 1
        assert ctx.cowork_nonsupported_warned is True

        # Second call should NOT fire again
        pkg_info2 = self._make_pkg_info(tmp_path / "pkg2", ["prompts"])
        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value=mock_dispatch,
        ):
            integrate_package_primitives(
                pkg_info2,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="test-pkg2",
                logger=logger,
                ctx=ctx,
                **integrators,
                force=False,
                managed_files=None,
            )
        warning_calls_after = [
            c for c in logger.warning.call_args_list if "cowork" in str(c).lower()
        ]
        assert len(warning_calls_after) == 1  # still just 1

    def test_warning_does_not_fire_when_only_skills(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": True}})
        from apm_cli.install.services import integrate_package_primitives

        cowork_target = _make_cowork_target(tmp_path / "cowork")
        targets = [cowork_target]

        # Package has only skills dir (no non-skill dirs)
        pkg_info = self._make_pkg_info(tmp_path, ["skills"])
        logger = MagicMock()
        ctx = self._make_ctx()

        integrators = {
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
        skill_result = MagicMock()
        skill_result.target_paths = []
        skill_result.skill_created = False
        skill_result.sub_skills_promoted = 0
        integrators["skill_integrator"].integrate_package_skill.return_value = skill_result

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value={},
        ):
            integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                logger=logger,
                ctx=ctx,
                **integrators,
                force=False,
                managed_files=None,
            )
        warning_calls = [c for c in logger.warning.call_args_list if "cowork" in str(c).lower()]
        assert len(warning_calls) == 0

    def test_warning_does_not_fire_when_cowork_not_active(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({})
        from apm_cli.install.services import integrate_package_primitives

        copilot = KNOWN_TARGETS["copilot"]
        targets = [copilot]

        pkg_info = self._make_pkg_info(tmp_path, ["agents"])
        logger = MagicMock()
        ctx = self._make_ctx()

        integrators = {
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
        skill_result = MagicMock()
        skill_result.target_paths = []
        skill_result.skill_created = False
        skill_result.sub_skills_promoted = 0
        integrators["skill_integrator"].integrate_package_skill.return_value = skill_result

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value={},
        ):
            integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                logger=logger,
                ctx=ctx,
                **integrators,
                force=False,
                managed_files=None,
            )
        warning_calls = [c for c in logger.warning.call_args_list if "cowork" in str(c).lower()]
        assert len(warning_calls) == 0

    def test_warning_does_not_fire_when_ctx_is_none(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": True}})
        from apm_cli.install.services import integrate_package_primitives

        cowork_target = _make_cowork_target(tmp_path / "cowork")
        targets = [cowork_target]

        pkg_info = self._make_pkg_info(tmp_path, ["agents"])
        logger = MagicMock()

        integrators = {
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
        skill_result = MagicMock()
        skill_result.target_paths = []
        skill_result.skill_created = False
        skill_result.sub_skills_promoted = 0
        integrators["skill_integrator"].integrate_package_skill.return_value = skill_result

        # ctx=None should not raise
        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value={},
        ):
            integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                logger=logger,
                ctx=None,
                **integrators,
                force=False,
                managed_files=None,
            )
        # No exception is the assertion

    def test_warning_msg_text_includes_package_name_and_primitive_types(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": True}})
        from apm_cli.install.services import integrate_package_primitives

        cowork_target = _make_cowork_target(tmp_path / "cowork")
        targets = [cowork_target]

        pkg_info = self._make_pkg_info(tmp_path, ["agents"])
        logger = MagicMock()
        ctx = self._make_ctx()

        integrators = {
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
        skill_result = MagicMock()
        skill_result.target_paths = []
        skill_result.skill_created = False
        skill_result.sub_skills_promoted = 0
        integrators["skill_integrator"].integrate_package_skill.return_value = skill_result

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value={},
        ):
            integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="my-awesome-pkg",
                logger=logger,
                ctx=ctx,
                **integrators,
                force=False,
                managed_files=None,
            )
        warning_calls = [c for c in logger.warning.call_args_list if "cowork" in str(c).lower()]
        assert len(warning_calls) == 1
        msg = str(warning_calls[0])
        assert "my-awesome-pkg" in msg
        assert "agents" in msg

    def test_warning_also_emitted_to_diagnostics_warn(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": True}})
        from apm_cli.install.services import integrate_package_primitives

        cowork_target = _make_cowork_target(tmp_path / "cowork")
        targets = [cowork_target]

        pkg_info = self._make_pkg_info(tmp_path, ["agents"])
        logger = MagicMock()
        ctx = self._make_ctx()
        diagnostics = MagicMock()

        integrators = {
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
        skill_result = MagicMock()
        skill_result.target_paths = []
        skill_result.skill_created = False
        skill_result.sub_skills_promoted = 0
        integrators["skill_integrator"].integrate_package_skill.return_value = skill_result

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value={},
        ):
            integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=targets,
                diagnostics=diagnostics,
                package_name="diag-pkg",
                logger=logger,
                ctx=ctx,
                **integrators,
                force=False,
                managed_files=None,
            )

        # logger.warning should have been called once
        logger_warn_calls = [c for c in logger.warning.call_args_list if "cowork" in str(c).lower()]
        assert len(logger_warn_calls) == 1

        # diagnostics.warn should also have been called once with same message
        diagnostics.warn.assert_called_once()
        diag_msg = diagnostics.warn.call_args[0][0]
        assert "cowork" in diag_msg
        assert "diag-pkg" in diag_msg

    def test_warning_with_prompts_only_does_not_mention_commands(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """Package with only prompts/ dir: warning says 'prompts', not 'commands'."""
        inject_config({"experimental": {"copilot_cowork": True}})
        from apm_cli.install.services import integrate_package_primitives

        cowork_target = _make_cowork_target(tmp_path / "cowork")
        targets = [cowork_target]

        # Package has only prompts dir (no agents, instructions, hooks, etc.)
        pkg_info = self._make_pkg_info(tmp_path, ["prompts"])
        logger = MagicMock()
        ctx = self._make_ctx()

        integrators = {
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
        skill_result = MagicMock()
        skill_result.target_paths = []
        skill_result.skill_created = False
        skill_result.sub_skills_promoted = 0
        integrators["skill_integrator"].integrate_package_skill.return_value = skill_result

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value={},
        ):
            integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="prompts-only-pkg",
                logger=logger,
                ctx=ctx,
                **integrators,
                force=False,
                managed_files=None,
            )

        warning_calls = [c for c in logger.warning.call_args_list if "cowork" in str(c).lower()]
        assert len(warning_calls) == 1
        msg = str(warning_calls[0])
        assert "prompts" in msg
        assert "commands" not in msg
