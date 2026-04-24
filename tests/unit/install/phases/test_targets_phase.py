"""Tests for apm_cli.install.phases.targets (project-scope gate, auto-create)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.integration.cowork_paths import CoworkResolutionError
from apm_cli.integration.targets import KNOWN_TARGETS, TargetProfile


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

    def _set(cfg: Dict[str, Any]) -> None:
        monkeypatch.setattr(_conf, "_config_cache", cfg)

    return _set


def _make_cowork_target(cowork_root: Path) -> TargetProfile:
    """Return a frozen TargetProfile with resolved_deploy_root for cowork.

    Args:
        cowork_root: The resolved cowork skills root directory.

    Returns:
        A frozen TargetProfile suitable for cowork tests.
    """
    return replace(KNOWN_TARGETS["cowork"], resolved_deploy_root=cowork_root)


def _make_ctx(
    tmp_path: Path,
    scope: InstallScope = InstallScope.PROJECT,
    target_override: Optional[str] = None,
) -> MagicMock:
    """Build a minimal ctx mock for phase tests.

    Args:
        tmp_path: Base temp directory for project_root.
        scope: Install scope (PROJECT or USER).
        target_override: CLI --target value.

    Returns:
        A MagicMock configured as an InstallContext.
    """
    ctx = MagicMock()
    ctx.project_root = tmp_path / "project"
    ctx.project_root.mkdir(parents=True, exist_ok=True)
    ctx.scope = scope
    ctx.target_override = target_override
    ctx.apm_package = MagicMock()
    ctx.apm_package.target = None
    ctx.logger = MagicMock()
    ctx.targets = []
    ctx.integrators = {}
    return ctx


# ---------------------------------------------------------------------------
# TestProjectScopeGateForCowork
# ---------------------------------------------------------------------------


class TestProjectScopeGateForCowork:
    """Tests for the project-scope cowork gate in phases/targets.py."""

    def test_project_scope_with_cowork_raises_system_exit(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"cowork": True}})
        cowork_target = _make_cowork_target(tmp_path / "cowork")
        ctx = _make_ctx(tmp_path, scope=InstallScope.PROJECT)

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            return_value=[cowork_target],
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            with pytest.raises(SystemExit):
                from apm_cli.install.phases.targets import run
                run(ctx)

    def test_project_scope_with_cowork_logs_error_before_exit(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"cowork": True}})
        cowork_target = _make_cowork_target(tmp_path / "cowork")
        ctx = _make_ctx(tmp_path, scope=InstallScope.PROJECT)

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            return_value=[cowork_target],
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            with pytest.raises(SystemExit):
                from apm_cli.install.phases.targets import run
                run(ctx)
        # Check that the error was logged with --global hint
        error_calls = ctx.logger.error.call_args_list
        assert len(error_calls) >= 1
        msg = str(error_calls[0])
        assert "--global" in msg

    def test_project_scope_with_cowork_no_mkdir_before_exit(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"cowork": True}})
        cowork_target = _make_cowork_target(tmp_path / "cowork")
        ctx = _make_ctx(tmp_path, scope=InstallScope.PROJECT)

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            return_value=[cowork_target],
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            with pytest.raises(SystemExit):
                from apm_cli.install.phases.targets import run
                run(ctx)
        assert not (ctx.project_root / "cowork").exists()

    def test_user_scope_with_cowork_does_not_raise(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"cowork": True}})
        cowork_target = _make_cowork_target(tmp_path / "cowork")
        ctx = _make_ctx(tmp_path, scope=InstallScope.USER)

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            return_value=[cowork_target],
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            from apm_cli.install.phases.targets import run
            run(ctx)  # Should not raise

    def test_project_scope_non_cowork_target_unaffected(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({})
        copilot = KNOWN_TARGETS["copilot"]
        ctx = _make_ctx(tmp_path, scope=InstallScope.PROJECT)

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            return_value=[copilot],
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            from apm_cli.install.phases.targets import run
            run(ctx)  # Should not raise


# ---------------------------------------------------------------------------
# TestAutoCreateSkipForDynamicRoot
# ---------------------------------------------------------------------------


class TestAutoCreateSkipForDynamicRoot:
    """Tests for auto-create directory skipping with dynamic-root targets."""

    def test_dynamic_root_target_skips_mkdir(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"cowork": True}})
        cowork_target = _make_cowork_target(tmp_path / "cowork")
        ctx = _make_ctx(tmp_path, scope=InstallScope.USER)
        ctx.target_override = "cowork"

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            return_value=[cowork_target],
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            from apm_cli.install.phases.targets import run
            run(ctx)
        assert not (ctx.project_root / "cowork").exists()

    def test_static_root_target_does_mkdir(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({})
        copilot = KNOWN_TARGETS["copilot"]
        ctx = _make_ctx(tmp_path, scope=InstallScope.PROJECT)
        ctx.target_override = "copilot"

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            return_value=[copilot],
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            from apm_cli.install.phases.targets import run
            run(ctx)
        assert (ctx.project_root / ".github").exists()


# ---------------------------------------------------------------------------
# TestCoworkResolutionErrorHandling
# ---------------------------------------------------------------------------


class TestCoworkResolutionErrorHandling:
    """Tests for CoworkResolutionError catch in phases/targets.py run()."""

    def test_resolution_error_raises_system_exit(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"cowork": True}})
        ctx = _make_ctx(tmp_path, scope=InstallScope.USER, target_override="cowork")

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            side_effect=CoworkResolutionError("Multiple OneDrive mounts detected"),
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            with pytest.raises(SystemExit) as exc_info:
                from apm_cli.install.phases.targets import run
                run(ctx)
            assert exc_info.value.code == 1

    def test_resolution_error_logs_message_no_traceback(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"cowork": True}})
        ctx = _make_ctx(tmp_path, scope=InstallScope.USER, target_override="cowork")
        error_msg = "Multiple OneDrive mounts detected:\n  - /a\n  - /b"

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            side_effect=CoworkResolutionError(error_msg),
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            with pytest.raises(SystemExit):
                from apm_cli.install.phases.targets import run
                run(ctx)

        ctx.logger.error.assert_called_once_with(error_msg, symbol="cross")

    def test_resolution_error_no_logger_still_exits(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"cowork": True}})
        ctx = _make_ctx(tmp_path, scope=InstallScope.USER, target_override="cowork")
        ctx.logger = None

        with patch(
            "apm_cli.integration.targets.resolve_targets",
            side_effect=CoworkResolutionError("test"),
        ), patch(
            "apm_cli.core.target_detection.detect_target",
        ):
            with pytest.raises(SystemExit) as exc_info:
                from apm_cli.install.phases.targets import run
                run(ctx)
            assert exc_info.value.code == 1
