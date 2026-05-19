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
    def test_absent_when_flag_off_auto_detect(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({"experimental": {"copilot_app": False}})
        results = active_targets(tmp_path)
        names = [t.name for t in results]
        assert "copilot-app" not in names

    def test_absent_when_flag_off_explicit_target(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({"experimental": {"copilot_app": False}})
        results = active_targets(tmp_path, explicit_target="copilot-app")
        assert results == []

    def test_absent_from_all_when_flag_off(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({"experimental": {"copilot_app": False}})
        results = active_targets(tmp_path, explicit_target="all")
        names = [t.name for t in results]
        assert "copilot-app" not in names

    def test_absent_from_all_when_flag_on(self, tmp_path: Path, inject_config: Any) -> None:
        """``--target all`` honors EXPERIMENTAL_TARGETS exclusion regardless of flag."""
        inject_config({"experimental": {"copilot_app": True}})
        results = active_targets(tmp_path, explicit_target="all")
        names = [t.name for t in results]
        assert "copilot-app" not in names

    def test_absent_when_flag_on_resolver_returns_none(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_app": True}})
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

    def test_present_when_flag_on_and_resolver_returns_path(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_app": True}})
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
