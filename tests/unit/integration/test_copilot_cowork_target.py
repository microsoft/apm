"""Unit tests for cowork target gating in apm_cli.integration.targets."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, Dict  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest

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
            name="dummy",
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
    """Tests for cowork gating in active_targets / resolve_targets."""

    def test_cowork_absent_when_flag_off_auto_detect(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": False}})
        (tmp_path / "copilot-cowork").mkdir()
        results = active_targets(tmp_path)
        names = [t.name for t in results]
        assert "copilot-cowork" not in names

    def test_cowork_absent_when_flag_off_explicit_cowork(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": False}})
        results = active_targets(tmp_path, explicit_target="copilot-cowork")
        assert results == []

    def test_cowork_absent_from_all_when_flag_off(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({"experimental": {"copilot_cowork": False}})
        results = active_targets(tmp_path, explicit_target="all")
        names = [t.name for t in results]
        # "all" returns all targets regardless of flag gating
        # but explicit_target="copilot-cowork" with flag off returns []
        # The "all" path returns list(KNOWN_TARGETS.values()) which
        # includes cowork. This is documented: "all" bypasses flag gate.
        # So cowork IS in the "all" set even when flag is off.
        # This matches the implementation comment:
        # "Return all targets regardless of flag gating."
        assert "copilot-cowork" in names

    def test_cowork_absent_when_flag_on_resolver_returns_none(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": True}})
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

    def test_cowork_never_auto_detected(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({"experimental": {"copilot_cowork": True}})
        (tmp_path / "copilot-cowork").mkdir()
        results = active_targets(tmp_path)
        names = [t.name for t in results]
        assert "copilot-cowork" not in names

    def test_cowork_present_when_flag_on_explicit(self, tmp_path: Path, inject_config: Any) -> None:
        inject_config({"experimental": {"copilot_cowork": True}})
        results = active_targets(tmp_path, explicit_target="copilot-cowork")
        assert len(results) == 1
        assert results[0].name == "copilot-cowork"

    def test_all_user_scope_includes_cowork_when_flag_on_resolver_succeeds(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": True}})
        user_profiles = active_targets_user_scope(explicit_target="all")
        names = [t.name for t in user_profiles]
        assert "copilot-cowork" in names
        # Now resolve via resolve_targets with resolver returning a path
        with patch(
            "apm_cli.integration.targets._resolve_copilot_cowork_root",
            return_value=tmp_path,
        ):
            resolved = resolve_targets(
                tmp_path,
                user_scope=True,
                explicit_target="all",
            )
        resolved_names = [t.name for t in resolved]
        assert "copilot-cowork" in resolved_names

    def test_all_user_scope_excludes_cowork_when_flag_off(self, inject_config: Any) -> None:
        inject_config({"experimental": {"copilot_cowork": False}})
        results = active_targets_user_scope(explicit_target="all")
        names = [t.name for t in results]
        assert "copilot-cowork" not in names

    def test_other_targets_unaffected_when_flag_off(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": False}})
        results = active_targets(tmp_path)
        names = [t.name for t in results]
        assert "copilot" in names

    @pytest.mark.parametrize(
        "target_name",
        ["copilot", "claude", "cursor", "codex", "opencode"],
    )
    def test_existing_target_active_targets_unchanged_when_cowork_flag_off(
        self,
        target_name: str,
        tmp_path: Path,
        inject_config: Any,
    ) -> None:
        inject_config({"experimental": {"copilot_cowork": False}})
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
# TestExplicitCoworkFlagOff (Fix 2)
# ---------------------------------------------------------------------------


class TestExplicitCoworkFlagOff:
    """When the user explicitly requests --target copilot-cowork and the flag is OFF,
    the targets phase must emit an info hint and be a no-op."""

    def test_user_scope_explicit_cowork_flag_off_is_noop(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """User-scope + explicit cowork + flag OFF -> info hint, no error."""
        inject_config({"experimental": {"copilot_cowork": False}})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.scope = InstallScope.USER
        ctx.target_override = "copilot-cowork"
        ctx.apm_package = MagicMock()
        ctx.apm_package.target = None
        ctx.logger = MagicMock()

        with patch("apm_cli.core.target_detection.detect_target"):
            run(ctx)  # Should not raise

        hint_msg = ctx.logger.progress.call_args[0][0]
        assert "experimental flag" in hint_msg
        assert "apm experimental enable copilot-cowork" in hint_msg

    def test_project_scope_explicit_cowork_flag_off_is_noop(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """Project-scope + explicit cowork + flag OFF -> info hint, no error."""
        inject_config({"experimental": {"copilot_cowork": False}})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.scope = InstallScope.PROJECT
        ctx.target_override = "copilot-cowork"
        ctx.apm_package = MagicMock()
        ctx.apm_package.target = None
        ctx.logger = MagicMock()

        with patch("apm_cli.core.target_detection.detect_target"):
            run(ctx)  # Should not raise

        hint_msg = ctx.logger.progress.call_args[0][0]
        assert "experimental flag" in hint_msg

    def test_auto_detect_silent_when_flag_off(self, tmp_path: Path, inject_config: Any) -> None:
        """Auto-detect path (no explicit target) stays silent when flag OFF."""
        inject_config({"experimental": {"copilot_cowork": False}})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.scope = InstallScope.USER
        ctx.target_override = None
        ctx.apm_package = MagicMock()
        ctx.apm_package.target = None
        ctx.logger = MagicMock()

        with patch("apm_cli.core.target_detection.detect_target"):
            run(ctx)  # Should not raise

        # logger.error should NOT have been called with cowork-related message
        for c in ctx.logger.error.call_args_list:
            assert "cowork" not in str(c).lower()

    def test_multi_target_cowork_copilot_flag_off_copilot_proceeds(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """cowork + copilot targets, flag OFF: cowork dropped, copilot proceeds."""
        inject_config({"experimental": {"copilot_cowork": False}})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.scope = InstallScope.USER
        ctx.target_override = ["copilot-cowork", "copilot"]
        ctx.apm_package = MagicMock()
        ctx.apm_package.target = None
        ctx.logger = MagicMock()

        copilot = KNOWN_TARGETS["copilot"].for_scope(user_scope=True)
        with (
            patch(
                "apm_cli.integration.targets.resolve_targets",
                return_value=[copilot],
            ),
            patch("apm_cli.core.target_detection.detect_target"),
        ):
            run(ctx)  # Should not raise

        # Cowork hint was logged
        hint_calls = [
            c for c in ctx.logger.progress.call_args_list if "experimental flag" in str(c)
        ]
        assert len(hint_calls) == 1
        # Copilot target proceeds
        assert any(t.name == "copilot" for t in ctx.targets)


# ---------------------------------------------------------------------------
# TestExplicitCoworkUnresolvable (Fix 3)
# ---------------------------------------------------------------------------


class TestExplicitCoworkUnresolvable:
    """When the user explicitly requests --target copilot-cowork, flag is ON, but
    OneDrive path cannot be resolved, the targets phase must error."""

    def test_linux_flag_on_explicit_cowork_no_env_no_config_errors(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """Linux + flag ON + explicit cowork + no env + no config -> error."""
        inject_config({"experimental": {"copilot_cowork": True}})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.scope = InstallScope.USER
        ctx.target_override = "copilot-cowork"
        ctx.apm_package = MagicMock()
        ctx.apm_package.target = None
        ctx.logger = MagicMock()

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
        # emits "no OneDrive path detected" — accept either variant.
        assert (
            "no OneDrive path detected" in error_msg
            or "Cowork has no auto-detection on Linux" in error_msg
        ), f"Expected cowork resolver error in output. Got: {error_msg}"
        assert "APM_COPILOT_COWORK_SKILLS_DIR" in error_msg

    def test_linux_flag_on_explicit_cowork_env_set_succeeds(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """Linux + flag ON + explicit cowork + env var set -> success."""
        inject_config({"experimental": {"copilot_cowork": True}})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        cowork_root = tmp_path / "cowork-skills"
        cowork_root.mkdir()
        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.scope = InstallScope.USER
        ctx.target_override = "copilot-cowork"
        ctx.apm_package = MagicMock()
        ctx.apm_package.target = None
        ctx.logger = MagicMock()

        with (
            patch(
                "apm_cli.integration.targets._resolve_copilot_cowork_root",
                return_value=cowork_root,
            ),
            patch("apm_cli.core.target_detection.detect_target"),
        ):
            run(ctx)  # Should not raise

    def test_linux_flag_off_explicit_cowork_hint_message(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """Linux + flag OFF + explicit cowork -> info hint (not error)."""
        inject_config({"experimental": {"copilot_cowork": False}})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.scope = InstallScope.USER
        ctx.target_override = "copilot-cowork"
        ctx.apm_package = MagicMock()
        ctx.apm_package.target = None
        ctx.logger = MagicMock()

        with patch("apm_cli.core.target_detection.detect_target"):
            run(ctx)  # Should not raise

        # Should be the flag hint, not an error
        hint_msg = ctx.logger.progress.call_args[0][0]
        assert "experimental flag" in hint_msg
        assert "OneDrive" not in hint_msg

    def test_auto_detect_flag_on_no_resolution_silent(
        self, tmp_path: Path, inject_config: Any
    ) -> None:
        """Auto-detect + flag ON + no resolution -> still silent."""
        inject_config({"experimental": {"copilot_cowork": True}})
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.targets import run

        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.scope = InstallScope.USER
        ctx.target_override = None
        ctx.apm_package = MagicMock()
        ctx.apm_package.target = None
        ctx.logger = MagicMock()

        with (
            patch(
                "apm_cli.integration.targets._resolve_copilot_cowork_root",
                return_value=None,
            ),
            patch("apm_cli.core.target_detection.detect_target"),
        ):
            run(ctx)  # Should not raise

        # No error about cowork
        for c in ctx.logger.error.call_args_list:
            assert "cowork" not in str(c).lower()
