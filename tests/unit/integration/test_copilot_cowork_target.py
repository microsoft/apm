"""Unit tests for cowork target gating in apm_cli.integration.targets."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, Dict  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.target_catalog import TARGET_CAPABILITIES
from apm_cli.integration.targets import (
    KNOWN_TARGETS,
    TargetProfile,
    active_targets,
    active_targets_user_scope,
    get_integration_prefixes,
    resolve_targets,
)

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

    def test_for_scope_user_scope_resolver_returns_path(self, tmp_path: Path) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_cowork_root",
            return_value=tmp_path,
        ):
            result = KNOWN_TARGETS["copilot-cowork"].for_scope(user_scope=True)
        assert result is not None
        assert result.resolved_deploy_root == tmp_path

    def test_for_scope_user_scope_resolver_returns_none(self) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_cowork_root",
            return_value=None,
        ):
            result = KNOWN_TARGETS["copilot-cowork"].for_scope(user_scope=True)
        assert result is None

    def test_for_scope_result_is_frozen(self, tmp_path: Path) -> None:
        with patch(
            "apm_cli.integration.targets._resolve_copilot_cowork_root",
            return_value=tmp_path,
        ):
            result = KNOWN_TARGETS["copilot-cowork"].for_scope(user_scope=True)
        assert result is not None
        with pytest.raises(FrozenInstanceError):
            result.name = "changed"  # type: ignore[misc]

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


class TestDeployPath:
    """Tests for TargetProfile.deploy_path()."""

    def test_deploy_path_with_resolved_root_and_parts(self, tmp_path: Path) -> None:
        cowork = replace(
            KNOWN_TARGETS["copilot-cowork"],
            resolved_deploy_root=tmp_path,
        )
        result = cowork.deploy_path(Path("/unused"), "sub", "file.md")
        assert result == tmp_path / "sub" / "file.md"

    def test_deploy_path_with_resolved_root_no_parts(self, tmp_path: Path) -> None:
        cowork = replace(
            KNOWN_TARGETS["copilot-cowork"],
            resolved_deploy_root=tmp_path,
        )
        result = cowork.deploy_path(Path("/unused"))
        assert result == tmp_path

    def test_deploy_path_without_resolved_root_uses_project(self, tmp_path: Path) -> None:
        copilot = KNOWN_TARGETS["copilot"]
        result = copilot.deploy_path(tmp_path)
        assert result == tmp_path / ".github"


# ---------------------------------------------------------------------------
# TestActiveTargetsGating
# ---------------------------------------------------------------------------


class TestActiveTargetsGating:
    """Tests for cowork selection in active_targets / resolve_targets.

    ``copilot-cowork`` is GA and explicit-only: never auto-detected, never
    part of ``all``, always available under an explicit ``--target``.
    """

    def test_cowork_not_auto_detected_by_directory(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({})
        (tmp_path / "copilot-cowork").mkdir()
        results = active_targets(tmp_path)
        names = [t.name for t in results]
        assert "copilot-cowork" not in names

    def test_cowork_present_when_explicit(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({})
        results = active_targets(tmp_path, explicit_target="copilot-cowork")
        assert len(results) == 1
        assert results[0].name == "copilot-cowork"

    def test_cowork_absent_from_all_project_scope(self, tmp_path: Path, inject_config: Any) -> None:
        """`--target all` (project scope) must NOT include cowork.

        cowork is explicit-only: opt-in via ``--target copilot-cowork``
        and never resolved through ``all``.  It also deploys at user
        scope only, so folding it into a project-scope ``all`` would only
        get it dropped again with a warning.
        """
        inject_config({})
        results = active_targets(tmp_path, explicit_target="all")
        names = [t.name for t in results]
        assert "copilot-cowork" not in names

    def test_cowork_absent_from_all_user_scope(self, inject_config: Any) -> None:
        """`--target all --global` must NOT include cowork either."""
        inject_config({})
        results = active_targets_user_scope(explicit_target="all")
        names = [t.name for t in results]
        assert "copilot-cowork" not in names

    def test_cowork_absent_when_resolver_returns_none(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({})
        with patch(
            "apm_cli.integration.targets._resolve_copilot_cowork_root",
            return_value=None,
        ):
            results = resolve_targets(
                tmp_path,
                user_scope=True,
                explicit_target="copilot-cowork",
            )
        names = [t.name for t in results]
        assert "copilot-cowork" not in names

    def test_cowork_resolved_user_scope_when_resolver_succeeds(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({})
        with patch(
            "apm_cli.integration.targets._resolve_copilot_cowork_root",
            return_value=tmp_path,
        ):
            resolved = resolve_targets(
                tmp_path,
                user_scope=True,
                explicit_target="copilot-cowork",
            )
        assert [t.name for t in resolved] == ["copilot-cowork"]

    def test_other_targets_unaffected(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({})
        results = active_targets(tmp_path)
        names = [t.name for t in results]
        assert "copilot" in names

    @pytest.mark.parametrize(
        "target_name",
        ["copilot", "claude", "cursor", "codex", "opencode"],
    )
    def test_existing_targets_still_registered(
        self,
        target_name: str,
        inject_config: Any,
    ) -> None:
        inject_config({})
        assert target_name in KNOWN_TARGETS


# ---------------------------------------------------------------------------
# TestGetIntegrationPrefixes
# ---------------------------------------------------------------------------


class TestGetIntegrationPrefixes:
    """Tests for get_integration_prefixes with cowork targets."""

    def test_cowork_prefix_present_when_resolved_root_set(self, tmp_path: Path) -> None:
        cowork = replace(
            KNOWN_TARGETS["copilot-cowork"],
            resolved_deploy_root=tmp_path,
        )
        prefixes = get_integration_prefixes([cowork])
        assert "cowork://skills/" in prefixes

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

    def test_get_integration_prefixes_resolved_target_still_works(self, tmp_path: Path) -> None:
        """A fully-resolved per-install target (resolved_deploy_root set)
        must still produce the cowork prefix -- regression guard for the
        normal install path.
        """
        resolved_cowork = replace(
            KNOWN_TARGETS["copilot-cowork"],
            resolved_deploy_root=tmp_path,
        )
        prefixes = get_integration_prefixes([resolved_cowork])
        assert "cowork://skills/" in prefixes


# ---------------------------------------------------------------------------
# TestExplicitCoworkScopeRules
# ---------------------------------------------------------------------------


def _cowork_ctx(tmp_path: Path, scope: Any, target_override: Any) -> MagicMock:
    """Build a minimal ctx mock for the targets phase."""
    ctx = MagicMock()
    ctx.project_root = tmp_path
    ctx.scope = scope
    ctx.target_override = target_override
    ctx.target_override_source = None
    ctx.target_decision = None
    ctx.apm_package = MagicMock()
    ctx.apm_package.target = None
    ctx.logger = MagicMock()
    ctx.targets = []
    return ctx


class TestExplicitCoworkScopeRules:
    """``--target copilot-cowork`` is GA: available at user scope, refused
    at project scope with an actionable ``--global`` hint."""

    def test_user_scope_explicit_cowork_resolves(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        cowork_root = tmp_path / "cowork-skills"
        cowork_root.mkdir()
        ctx = _cowork_ctx(tmp_path, InstallScope.USER, "copilot-cowork")

        with (
            patch(
                "apm_cli.integration.targets._resolve_copilot_cowork_root",
                return_value=cowork_root,
            ),
            patch("apm_cli.core.target_detection.detect_target"),
        ):
            run(ctx)  # Should not raise

        assert any(t.name == "copilot-cowork" for t in ctx.targets)
        # No experimental-flag hint is emitted any more.
        for call in ctx.logger.progress.call_args_list:
            assert "experimental flag" not in str(call)

    def test_project_scope_explicit_cli_cowork_errors_with_global_hint(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        cowork_root = tmp_path / "cowork-skills"
        cowork_root.mkdir()
        ctx = _cowork_ctx(tmp_path, InstallScope.PROJECT, "copilot-cowork")

        with (
            patch(
                "apm_cli.integration.targets._resolve_copilot_cowork_root",
                return_value=cowork_root,
            ),
            patch("apm_cli.core.target_detection.detect_target"),
            pytest.raises(SystemExit) as exc_info,
        ):
            run(ctx)

        assert exc_info.value.code == 1
        assert "--global" in str(ctx.logger.error.call_args_list[0])

    def test_auto_detect_silent_when_cowork_not_requested(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """Auto-detect path (no explicit target) never mentions cowork."""
        inject_config({})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = _cowork_ctx(tmp_path, InstallScope.USER, None)

        with patch("apm_cli.core.target_detection.detect_target"):
            run(ctx)  # Should not raise

        for c in ctx.logger.error.call_args_list:
            assert "cowork" not in str(c).lower()

    def test_multi_target_cowork_copilot_both_proceed(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """cowork + copilot at user scope: both targets deploy."""
        inject_config({})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        cowork_root = tmp_path / "cowork-skills"
        cowork_root.mkdir()
        ctx = _cowork_ctx(tmp_path, InstallScope.USER, ["copilot-cowork", "copilot"])

        with (
            patch(
                "apm_cli.integration.targets._resolve_copilot_cowork_root",
                return_value=cowork_root,
            ),
            patch("apm_cli.core.target_detection.detect_target"),
        ):
            run(ctx)  # Should not raise

        names = [t.name for t in ctx.targets]
        assert "copilot" in names
        assert "copilot-cowork" in names


# ---------------------------------------------------------------------------
# TestExplicitCoworkUnresolvable
# ---------------------------------------------------------------------------


class TestExplicitCoworkUnresolvable:
    """When the user explicitly requests --target copilot-cowork but the
    OneDrive path cannot be resolved, the targets phase must error."""

    def test_explicit_cowork_no_env_no_config_errors(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = _cowork_ctx(tmp_path, InstallScope.USER, "copilot-cowork")

        with (
            patch(
                "apm_cli.integration.targets._resolve_copilot_cowork_root",
                return_value=None,
            ),
            patch("apm_cli.core.target_detection.detect_target"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run(ctx)
            assert exc_info.value.code == 1

        error_msg = ctx.logger.error.call_args[0][0]
        # Linux emits "Cowork has no auto-detection on Linux." while macOS
        # emits "no OneDrive path detected" -- accept either variant.
        assert (
            "no OneDrive path detected" in error_msg
            or "Cowork has no auto-detection on Linux" in error_msg
        ), f"Expected cowork resolver error in output. Got: {error_msg}"
        assert "APM_COPILOT_COWORK_SKILLS_DIR" in error_msg

    def test_explicit_cowork_env_set_succeeds(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        cowork_root = tmp_path / "cowork-skills"
        cowork_root.mkdir()
        ctx = _cowork_ctx(tmp_path, InstallScope.USER, "copilot-cowork")

        with (
            patch(
                "apm_cli.integration.targets._resolve_copilot_cowork_root",
                return_value=cowork_root,
            ),
            patch("apm_cli.core.target_detection.detect_target"),
        ):
            run(ctx)  # Should not raise

    def test_auto_detect_no_resolution_silent(self, tmp_path: Path, inject_config: Any) -> None:
        """Auto-detect + no resolution -> still silent."""
        inject_config({})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = _cowork_ctx(tmp_path, InstallScope.USER, None)

        with (
            patch(
                "apm_cli.integration.targets._resolve_copilot_cowork_root",
                return_value=None,
            ),
            patch("apm_cli.core.target_detection.detect_target"),
        ):
            run(ctx)  # Should not raise

        for c in ctx.logger.error.call_args_list:
            assert "cowork" not in str(c).lower()
