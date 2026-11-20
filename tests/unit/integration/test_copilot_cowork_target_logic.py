"""Unit tests for cowork target gating in apm_cli.integration.targets (pure logic cases)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict  # noqa: F401, UP035
from unittest.mock import patch

import pytest

from apm_cli.core.target_catalog import TARGET_CAPABILITIES
from apm_cli.integration.targets import (
    KNOWN_TARGETS,
    TargetProfile,
    active_targets_user_scope,
    get_integration_prefixes,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared fixtures (same pattern as test_experimental.py)
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


# ---------------------------------------------------------------------------
# TestTargetProfileForScope
# ---------------------------------------------------------------------------


class TestTargetProfileForScope:
    """Tests for TargetProfile.for_scope()."""

    def test_for_scope_false_returns_self(self) -> None:
        profile = KNOWN_TARGETS["copilot"]
        result = profile.for_scope(user_scope=False)
        assert result is profile

    def test_for_scope_user_scope_resolver_returns_none(self) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_cowork_root",
            return_value=None,
        ):
            result = KNOWN_TARGETS["copilot-cowork"].for_scope(user_scope=True)
        assert result is None

    def test_for_scope_non_resolver_user_supported_returns_profile(
        self,
    ) -> None:
        copilot = KNOWN_TARGETS["copilot"]
        result = copilot.for_scope(user_scope=True)
        assert result is not None
        assert result.name == "copilot"

    def test_for_scope_non_resolver_user_unsupported_returns_none(
        self,
    ) -> None:
        unsupported = TargetProfile(
            capability=replace(
                TARGET_CAPABILITIES["copilot"],
                name="dummy",
                aliases=(),
                runtimes=(),
            ),
            root_dir=".dummy",
            primitives={},
            user_supported=False,
        )
        result = unsupported.for_scope(user_scope=True)
        assert result is None


# ---------------------------------------------------------------------------
# TestDeployPath
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestActiveTargetsGating
# ---------------------------------------------------------------------------


class TestActiveTargetsGating:
    """Tests for cowork gating in active_targets / resolve_targets."""

    def test_all_user_scope_excludes_cowork_when_flag_off(self, inject_config: Any) -> None:
        inject_config({"experimental": {"copilot_cowork": False}})
        results = active_targets_user_scope(explicit_target="all")
        names = [t.name for t in results]
        assert "copilot-cowork" not in names


# ---------------------------------------------------------------------------
# TestGetIntegrationPrefixes
# ---------------------------------------------------------------------------


class TestGetIntegrationPrefixes:
    """Tests for get_integration_prefixes with cowork targets."""

    def test_cowork_prefix_absent_when_no_resolved_root(self) -> None:
        copilot = KNOWN_TARGETS["copilot"]
        prefixes = get_integration_prefixes([copilot])
        assert all(not p.startswith("cowork://") for p in prefixes)

    def test_standard_prefixes_unchanged_when_cowork_absent(self) -> None:
        copilot = KNOWN_TARGETS["copilot"]
        prefixes = get_integration_prefixes([copilot])
        assert ".github/" in prefixes

    # -- Regression tests for cleanup with targets=None (PR #926) ----------

    def test_get_integration_prefixes_includes_cowork_with_targets_none(
        self,
    ) -> None:
        """When targets=None, KNOWN_TARGETS is iterated. The static
        copilot-cowork entry has resolved_deploy_root=None but DOES have
        a user_root_resolver. The cowork prefix must be included so
        cleanup/uninstall can validate cowork:// lockfile entries.
        """
        prefixes = get_integration_prefixes(targets=None)
        assert "cowork://skills/" in prefixes

    def test_get_integration_prefixes_includes_cowork_with_explicit_static_targets(
        self,
    ) -> None:
        """Passing the static KNOWN_TARGETS['copilot-cowork'] instance
        (resolved_deploy_root=None, user_root_resolver is set) must
        include the cowork prefix -- same scenario as targets=None but
        with an explicit list containing only the static entry.
        """
        static_cowork = KNOWN_TARGETS["copilot-cowork"]
        # Confirm this is the unresolved static instance.
        assert static_cowork.resolved_deploy_root is None
        assert static_cowork.user_root_resolver is not None
        prefixes = get_integration_prefixes([static_cowork])
        assert "cowork://skills/" in prefixes


# ---------------------------------------------------------------------------
# TestExplicitCoworkFlagOff (Fix 2)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestExplicitCoworkUnresolvable (Fix 3)
# ---------------------------------------------------------------------------
