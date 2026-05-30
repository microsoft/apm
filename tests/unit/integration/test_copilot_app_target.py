"""Unit tests for copilot-app target gating in apm_cli.integration.targets.

Mirrors test_copilot_cowork_target.py; covers the same dimensions:

  * for_scope() with resolver returning a path (success) and None (skip)
  * gating by experimental flag in active_targets / resolve_targets
  * exclusion from --target all (EXPERIMENTAL_TARGETS contract)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from apm_cli.integration.targets import (
    KNOWN_TARGETS,
    active_targets,
    resolve_targets,
)


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


class TestForScope:
    def test_for_scope_resolver_returns_path(self, tmp_path: Path) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_app_root",
            return_value=tmp_path,
        ):
            result = KNOWN_TARGETS["copilot-app"].for_scope(user_scope=True)
        assert result is not None
        assert result.resolved_deploy_root == tmp_path

    def test_for_scope_resolver_returns_none(self) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_app_root",
            return_value=None,
        ):
            result = KNOWN_TARGETS["copilot-app"].for_scope(user_scope=True)
        assert result is None


class TestActiveTargetsGating:
    def test_project_scope_auto_detect_still_ignores_copilot_app(self, tmp_path: Path) -> None:
        results = active_targets(tmp_path)
        names = [t.name for t in results]
        assert "copilot-app" not in names

    def test_explicit_target_available_without_experimental_flag(self, tmp_path: Path) -> None:
        results = active_targets(tmp_path, explicit_target="copilot-app")
        names = [t.name for t in results]
        assert "copilot-app" in names

    def test_absent_from_all_by_default(self, tmp_path: Path) -> None:
        results = active_targets(tmp_path, explicit_target="all")
        names = [t.name for t in results]
        assert "copilot-app" not in names

    def test_absent_when_resolver_returns_none(self, tmp_path: Path) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_app_root",
            return_value=None,
        ):
            results = resolve_targets(
                tmp_path,
                user_scope=True,
                explicit_target="copilot-app",
            )
        names = [t.name for t in results]
        assert "copilot-app" not in names

    def test_present_when_resolver_returns_path(self, tmp_path: Path) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_app_root",
            return_value=tmp_path,
        ):
            results = resolve_targets(
                tmp_path,
                user_scope=True,
                explicit_target="copilot-app",
            )
        names = [t.name for t in results]
        assert "copilot-app" in names

    def test_user_scope_fallback_includes_copilot_app_when_app_is_present(
        self, tmp_path: Path
    ) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_app_root",
            return_value=tmp_path,
        ):
            results = resolve_targets(tmp_path, user_scope=True)
        names = [t.name for t in results]
        assert names[:2] == ["copilot-app", "copilot"]
