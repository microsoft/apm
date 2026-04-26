"""Tests for installation scope resolution."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.scope import (
    InstallScope,
    USER_APM_DIR,
    ensure_user_dirs,
    get_apm_dir,
    get_deploy_root,
    get_lockfile_dir,
    get_manifest_path,
    get_modules_dir,
    get_unsupported_targets,
    warn_unsupported_user_scope,
)
from apm_cli.integration.targets import KNOWN_TARGETS


# ---------------------------------------------------------------------------
# InstallScope enum
# ---------------------------------------------------------------------------


class TestInstallScope:
    """Basic enum sanity checks."""

    def test_values(self):
        assert InstallScope.PROJECT.value == "project"
        assert InstallScope.USER.value == "user"

    def test_from_string(self):
        assert InstallScope("project") is InstallScope.PROJECT
        assert InstallScope("user") is InstallScope.USER

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            InstallScope("global")


# ---------------------------------------------------------------------------
# get_deploy_root
# ---------------------------------------------------------------------------


class TestGetDeployRoot:
    """Tests for get_deploy_root."""

    def test_project_returns_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_deploy_root(InstallScope.PROJECT) == tmp_path

    def test_user_returns_home(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            assert get_deploy_root(InstallScope.USER) == tmp_path


# ---------------------------------------------------------------------------
# get_apm_dir
# ---------------------------------------------------------------------------


class TestGetApmDir:
    """Tests for get_apm_dir."""

    def test_project_is_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_apm_dir(InstallScope.PROJECT) == tmp_path

    def test_user_is_home_dot_apm(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            assert get_apm_dir(InstallScope.USER) == tmp_path / USER_APM_DIR


# ---------------------------------------------------------------------------
# get_modules_dir
# ---------------------------------------------------------------------------


class TestGetModulesDir:
    """Tests for get_modules_dir."""

    def test_project_modules(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_modules_dir(InstallScope.PROJECT) == tmp_path / "apm_modules"

    def test_user_modules(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            assert get_modules_dir(InstallScope.USER) == tmp_path / ".apm" / "apm_modules"


# ---------------------------------------------------------------------------
# get_manifest_path
# ---------------------------------------------------------------------------


class TestGetManifestPath:
    """Tests for get_manifest_path."""

    def test_project_manifest(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_manifest_path(InstallScope.PROJECT) == tmp_path / "apm.yml"

    def test_user_manifest(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            assert get_manifest_path(InstallScope.USER) == tmp_path / ".apm" / "apm.yml"


# ---------------------------------------------------------------------------
# get_lockfile_dir
# ---------------------------------------------------------------------------


class TestGetLockfileDir:
    """Tests for get_lockfile_dir."""

    def test_project_lockfile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_lockfile_dir(InstallScope.PROJECT) == tmp_path

    def test_user_lockfile(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            assert get_lockfile_dir(InstallScope.USER) == tmp_path / ".apm"


# ---------------------------------------------------------------------------
# ensure_user_dirs
# ---------------------------------------------------------------------------


class TestEnsureUserDirs:
    """Tests for ensure_user_dirs."""

    def test_creates_dirs(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            result = ensure_user_dirs()
            assert result == tmp_path / ".apm"
            assert result.is_dir()
            assert (result / "apm_modules").is_dir()

    def test_idempotent(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            ensure_user_dirs()
            ensure_user_dirs()  # Should not raise
            assert (tmp_path / ".apm").is_dir()


# ---------------------------------------------------------------------------
# TargetProfile user-scope metadata (replaces old USER_SCOPE_TARGETS tests)
# ---------------------------------------------------------------------------


class TestTargetProfileUserScope:
    """Validate user-scope metadata on TargetProfile in KNOWN_TARGETS."""

    def test_all_known_targets_present(self):
        expected = {"copilot", "claude", "cursor", "opencode", "codex", "gemini"}
        assert set(KNOWN_TARGETS.keys()) == expected

    def test_each_target_has_user_supported(self):
        for name, profile in KNOWN_TARGETS.items():
            assert profile.user_supported in (True, False, "partial"), (
                f"{name} has unexpected user_supported value"
            )

    def test_claude_is_supported(self):
        assert KNOWN_TARGETS["claude"].user_supported is True

    def test_copilot_is_partially_supported(self):
        assert KNOWN_TARGETS["copilot"].user_supported == "partial"

    def test_cursor_is_partially_supported(self):
        assert KNOWN_TARGETS["cursor"].user_supported == "partial"
        assert KNOWN_TARGETS["cursor"].user_root_dir == ".cursor"
        assert "instructions" in KNOWN_TARGETS["cursor"].unsupported_user_primitives

    def test_opencode_is_partially_supported(self):
        assert KNOWN_TARGETS["opencode"].user_supported == "partial"
        assert KNOWN_TARGETS["opencode"].user_root_dir == ".config/opencode"
        assert "hooks" in KNOWN_TARGETS["opencode"].unsupported_user_primitives

    def test_copilot_user_root_dir(self):
        assert KNOWN_TARGETS["copilot"].user_root_dir == ".copilot"

    def test_claude_uses_default_root_at_user_scope(self):
        # Claude uses .claude at both project and user scope
        assert KNOWN_TARGETS["claude"].user_root_dir is None

    def test_copilot_unsupported_user_primitives(self):
        assert "prompts" in KNOWN_TARGETS["copilot"].unsupported_user_primitives
        assert "instructions" in KNOWN_TARGETS["copilot"].unsupported_user_primitives

    def test_effective_root_project_scope(self):
        assert KNOWN_TARGETS["copilot"].effective_root(user_scope=False) == ".github"

    def test_effective_root_user_scope_with_override(self):
        assert KNOWN_TARGETS["copilot"].effective_root(user_scope=True) == ".copilot"

    def test_effective_root_user_scope_no_override(self):
        assert KNOWN_TARGETS["claude"].effective_root(user_scope=True) == ".claude"

    def test_supports_at_user_scope_true(self):
        assert KNOWN_TARGETS["claude"].supports_at_user_scope("agents") is True
        assert KNOWN_TARGETS["claude"].supports_at_user_scope("commands") is True

    def test_supports_at_user_scope_partial(self):
        # Copilot supports agents at user scope but not prompts or instructions
        assert KNOWN_TARGETS["copilot"].supports_at_user_scope("agents") is True
        assert KNOWN_TARGETS["copilot"].supports_at_user_scope("prompts") is False
        assert KNOWN_TARGETS["copilot"].supports_at_user_scope("instructions") is False

    def test_supports_at_user_scope_cursor_partial(self):
        # Cursor supports agents at user scope but not instructions
        assert KNOWN_TARGETS["cursor"].supports_at_user_scope("agents") is True
        assert KNOWN_TARGETS["cursor"].supports_at_user_scope("skills") is True
        assert KNOWN_TARGETS["cursor"].supports_at_user_scope("hooks") is True
        assert KNOWN_TARGETS["cursor"].supports_at_user_scope("instructions") is False

    def test_supports_at_user_scope_opencode_partial(self):
        # OpenCode supports agents at user scope but not hooks
        assert KNOWN_TARGETS["opencode"].supports_at_user_scope("agents") is True
        assert KNOWN_TARGETS["opencode"].supports_at_user_scope("skills") is True
        assert KNOWN_TARGETS["opencode"].supports_at_user_scope("commands") is True
        assert KNOWN_TARGETS["opencode"].supports_at_user_scope("hooks") is False

    def test_unsupported_targets_have_no_user_root(self):
        for name, profile in KNOWN_TARGETS.items():
            if profile.user_supported is False:
                assert profile.user_root_dir is None, (
                    f"{name} is unsupported but has user_root_dir set"
                )


# ---------------------------------------------------------------------------
# get_unsupported_targets / warn_unsupported_user_scope
# ---------------------------------------------------------------------------


class TestScopeWarnings:
    """Tests for unsupported-target warnings."""

    def test_get_unsupported_targets(self):
        unsupported = get_unsupported_targets()
        # All targets now support user scope (fully or partially)
        assert "cursor" not in unsupported
        assert "opencode" not in unsupported
        assert "copilot" not in unsupported
        assert "claude" not in unsupported

    def test_warn_message_includes_partial_targets(self):
        msg = warn_unsupported_user_scope()
        assert msg  # non-empty because there are partially supported targets
        # All four targets now support user scope
        assert "cursor" in msg
        assert "opencode" in msg
        assert "copilot" in msg
        # Claude is fully supported
        assert "claude" in msg
        assert "fully supported" in msg.lower()
        # Copilot, cursor, opencode are partially supported
        assert "partially supported" in msg.lower()

    def test_warn_message_includes_unsupported_primitives(self):
        msg = warn_unsupported_user_scope()
        # Copilot excludes prompts and instructions
        assert "copilot (prompts, instructions)" in msg
        # Cursor excludes instructions
        assert "cursor (instructions)" in msg
        # OpenCode excludes hooks
        assert "opencode (hooks)" in msg


# ---------------------------------------------------------------------------
# active_targets_user_scope
# ---------------------------------------------------------------------------


class TestActiveTargetsUserScope:
    """Tests for active_targets_user_scope() in targets.py."""

    def test_explicit_copilot(self):
        from apm_cli.integration.targets import active_targets_user_scope
        result = active_targets_user_scope(explicit_target="copilot")
        assert len(result) == 1
        assert result[0].name == "copilot"

    def test_explicit_all_returns_all_user_capable(self):
        from apm_cli.integration.targets import active_targets_user_scope
        result = active_targets_user_scope(explicit_target="all")
        names = {t.name for t in result}
        assert "copilot" in names
        assert "claude" in names
        assert "cursor" in names
        assert "opencode" in names

    def test_explicit_unknown_returns_empty(self):
        from apm_cli.integration.targets import active_targets_user_scope
        result = active_targets_user_scope(explicit_target="nonexistent")
        assert result == []

    def test_explicit_vscode_alias(self):
        from apm_cli.integration.targets import active_targets_user_scope
        result = active_targets_user_scope(explicit_target="vscode")
        assert len(result) == 1
        assert result[0].name == "copilot"

    def test_auto_detect_by_dir_presence(self, tmp_path):
        """When cursor dir exists at ~/, it should be detected."""
        from apm_cli.integration.targets import active_targets_user_scope
        (tmp_path / ".cursor").mkdir()
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = active_targets_user_scope()
        names = {t.name for t in result}
        assert "cursor" in names

    def test_auto_detect_multiple_dirs(self, tmp_path):
        """Detects all targets with existing home dirs."""
        from apm_cli.integration.targets import active_targets_user_scope
        (tmp_path / ".cursor").mkdir()
        (tmp_path / ".claude").mkdir()
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = active_targets_user_scope()
        names = {t.name for t in result}
        assert "cursor" in names
        assert "claude" in names

    def test_fallback_to_copilot(self, tmp_path):
        """When no target dirs exist, falls back to copilot."""
        from apm_cli.integration.targets import active_targets_user_scope
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = active_targets_user_scope()
        assert len(result) == 1
        assert result[0].name == "copilot"

    def test_opencode_nested_dir(self, tmp_path):
        """OpenCode uses ~/.config/opencode/ which is nested."""
        from apm_cli.integration.targets import active_targets_user_scope
        (tmp_path / ".config" / "opencode").mkdir(parents=True)
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = active_targets_user_scope()
        names = {t.name for t in result}
        assert "opencode" in names
