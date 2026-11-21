"""Tests for installation scope resolution."""

import os  # noqa: F401
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.scope import (
    USER_APM_DIR,
    InstallScope,
    ensure_user_dirs,
    get_apm_dir,
    get_apm_home,
    get_deploy_root,
    get_lockfile_dir,
    get_manifest_path,
    get_modules_dir,
    get_unsupported_targets,
    is_user_scope,
    warn_unsupported_user_scope,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.policy.discovery import PolicyFetchResult

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

    def test_user_scope_identity_is_fail_closed(self):
        assert is_user_scope(InstallScope.USER)
        assert not is_user_scope(InstallScope.PROJECT)
        assert not is_user_scope("user")
        assert not is_user_scope(None)


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

    def test_user_honors_apm_home(self, tmp_path, monkeypatch):
        apm_home = tmp_path / "isolated-apm"
        monkeypatch.setenv("APM_HOME", str(apm_home))

        assert get_apm_home() == apm_home
        assert get_apm_dir(InstallScope.USER) == apm_home

    def test_empty_apm_home_uses_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APM_HOME", "")
        with patch.object(Path, "home", return_value=tmp_path):
            assert get_apm_home() == tmp_path / USER_APM_DIR

    def test_relative_apm_home_is_anchored_to_startup_cwd(self, tmp_path, monkeypatch):
        from apm_cli.core import scope

        monkeypatch.setattr(scope, "_STARTUP_CWD", tmp_path)
        monkeypatch.setenv("APM_HOME", "metadata")
        later = tmp_path / "metadata"
        later.mkdir()
        monkeypatch.chdir(later)

        assert get_apm_home() == tmp_path / "metadata"
        assert get_manifest_path(InstallScope.USER) == tmp_path / "metadata" / "apm.yml"

    def test_relative_apm_home_fails_closed_without_startup_cwd(self, monkeypatch):
        from apm_cli.core import scope

        monkeypatch.setattr(scope, "_STARTUP_CWD", None)
        monkeypatch.setenv("APM_HOME", "metadata")

        with pytest.raises(ValueError, match="absolute APM_HOME"):
            get_apm_home()

    def test_apm_home_expands_leading_tilde(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APM_HOME", "~/isolated-apm")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        assert get_apm_home() == tmp_path / "isolated-apm"


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

    def test_user_modules_honor_apm_home(self, tmp_path, monkeypatch):
        apm_home = tmp_path / "metadata"
        monkeypatch.setenv("APM_HOME", str(apm_home))

        assert get_modules_dir(InstallScope.USER) == apm_home / "apm_modules"


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

    def test_user_manifest_honors_apm_home(self, tmp_path, monkeypatch):
        apm_home = tmp_path / "metadata"
        monkeypatch.setenv("APM_HOME", str(apm_home))

        assert get_manifest_path(InstallScope.USER) == apm_home / "apm.yml"


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

    def test_user_lockfile_honors_apm_home(self, tmp_path, monkeypatch):
        apm_home = tmp_path / "metadata"
        monkeypatch.setenv("APM_HOME", str(apm_home))

        assert get_lockfile_dir(InstallScope.USER) == apm_home


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

    def test_honors_apm_home(self, tmp_path, monkeypatch):
        apm_home = tmp_path / "metadata"
        monkeypatch.setenv("APM_HOME", str(apm_home))

        assert ensure_user_dirs() == apm_home
        assert (apm_home / "apm_modules").is_dir()


def test_apm_home_does_not_redirect_target_deployment(tmp_path, monkeypatch):
    """APM_HOME isolates metadata while target deployment stays under HOME."""
    apm_home = tmp_path / "metadata"
    os_home = tmp_path / "home"
    monkeypatch.setenv("APM_HOME", str(apm_home))

    with patch.object(Path, "home", return_value=os_home):
        assert get_apm_dir(InstallScope.USER) == apm_home
        assert get_deploy_root(InstallScope.USER) == os_home


# ---------------------------------------------------------------------------
# TargetProfile user-scope metadata (replaces old USER_SCOPE_TARGETS tests)
# ---------------------------------------------------------------------------


class TestTargetProfileUserScope:
    """Validate user-scope metadata on TargetProfile in KNOWN_TARGETS."""

    def test_all_known_targets_present(self):
        expected = {
            "copilot",
            "claude",
            "cursor",
            "opencode",
            "codex",
            "gemini",
            "grok-build",
            "grok-cloud",
            "antigravity",
            "windsurf",
            "kiro",
            "copilot-cowork",
            "copilot-app",
            "agent-skills",
            "openclaw",
            "hermes",
            "grok-bot",
        }
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

    def test_cursor_is_supported_at_user_scope(self):
        assert KNOWN_TARGETS["cursor"].user_supported is True
        assert KNOWN_TARGETS["cursor"].user_root_dir == ".cursor"
        assert KNOWN_TARGETS["cursor"].unsupported_user_primitives == ()

    def test_opencode_is_partially_supported(self):
        assert KNOWN_TARGETS["opencode"].user_supported == "partial"
        assert KNOWN_TARGETS["opencode"].user_root_dir == ".config/opencode"
        assert "hooks" in KNOWN_TARGETS["opencode"].unsupported_user_primitives

    def test_kiro_is_supported_at_user_scope(self):
        assert KNOWN_TARGETS["kiro"].user_supported is True
        assert KNOWN_TARGETS["kiro"].user_root_dir == ".kiro"

    def test_copilot_user_root_dir(self):
        assert KNOWN_TARGETS["copilot"].user_root_dir == ".copilot"

    def test_claude_uses_default_root_at_user_scope(self):
        # Claude uses .claude at both project and user scope
        assert KNOWN_TARGETS["claude"].user_root_dir is None

    def test_copilot_unsupported_user_primitives(self):
        # Instructions are now supported at user scope via concat (#650).
        assert "instructions" not in KNOWN_TARGETS["copilot"].unsupported_user_primitives

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
        # Copilot supports agents, prompts, and instructions at user scope.
        # Instructions are concatenated into copilot-instructions.md (#650).
        assert KNOWN_TARGETS["copilot"].supports_at_user_scope("agents") is True
        assert KNOWN_TARGETS["copilot"].supports_at_user_scope("prompts") is True
        assert KNOWN_TARGETS["copilot"].supports_at_user_scope("instructions") is True

    def test_supports_at_user_scope_cursor(self):
        # Cursor fully supports instructions, agents, skills, and hooks at user scope.
        assert KNOWN_TARGETS["cursor"].supports_at_user_scope("agents") is True
        assert KNOWN_TARGETS["cursor"].supports_at_user_scope("skills") is True
        assert KNOWN_TARGETS["cursor"].supports_at_user_scope("hooks") is True
        assert KNOWN_TARGETS["cursor"].supports_at_user_scope("instructions") is True

    def test_supports_at_user_scope_opencode_partial(self):
        # OpenCode supports agents at user scope but not hooks
        assert KNOWN_TARGETS["opencode"].supports_at_user_scope("agents") is True
        assert KNOWN_TARGETS["opencode"].supports_at_user_scope("skills") is True
        assert KNOWN_TARGETS["opencode"].supports_at_user_scope("commands") is True
        assert KNOWN_TARGETS["opencode"].supports_at_user_scope("hooks") is False

    def test_windsurf_is_partially_supported(self):
        assert KNOWN_TARGETS["windsurf"].user_supported == "partial"
        assert KNOWN_TARGETS["windsurf"].user_root_dir == ".codeium/windsurf"
        assert "instructions" in KNOWN_TARGETS["windsurf"].unsupported_user_primitives

    def test_supports_at_user_scope_windsurf_partial(self):
        # Windsurf supports skills, commands, hooks at user scope; agents (no native primitive) and instructions are unsupported
        assert KNOWN_TARGETS["windsurf"].supports_at_user_scope("skills") is True
        assert KNOWN_TARGETS["windsurf"].supports_at_user_scope("commands") is True
        assert KNOWN_TARGETS["windsurf"].supports_at_user_scope("hooks") is True
        assert KNOWN_TARGETS["windsurf"].supports_at_user_scope("agents") is False
        assert KNOWN_TARGETS["windsurf"].supports_at_user_scope("instructions") is False

    def test_windsurf_effective_root_project_scope(self):
        assert KNOWN_TARGETS["windsurf"].effective_root(user_scope=False) == ".windsurf"

    def test_windsurf_effective_root_user_scope(self):
        assert KNOWN_TARGETS["windsurf"].effective_root(user_scope=True) == ".codeium/windsurf"

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

    def test_warning_names_only_selected_targets(self):
        """A Claude-only global install is not warned about other targets (#3994)."""
        assert warn_unsupported_user_scope(["claude"]) == ""
        msg = warn_unsupported_user_scope(["claude", "windsurf", "opencode"])
        assert "windsurf (instructions)" in msg
        assert "opencode (hooks)" in msg
        assert "claude" not in msg
        assert "antigravity" not in msg


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

    def test_unknown_target_raises_at_parse_time(self):
        """Unknown tokens fail at the parser, not silently in the
        user-scope resolver.  Mirrors the project-scope contract change
        from #820 -- both entry points share one validator
        (:func:`apm_cli.core.target_detection.parse_target_field`)."""
        import pytest

        from apm_cli.core.target_detection import parse_target_field

        with pytest.raises(ValueError, match="not a valid target"):
            parse_target_field("nonexistent")

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


def test_user_scope_grants_and_policy_root_are_apm_home(tmp_path, monkeypatch) -> None:
    """Grants and the policy root are $APM_HOME at user scope, never the deploy root (#4087)."""
    from types import SimpleNamespace

    from apm_cli.security import executables

    home, apm_home = tmp_path / "home", tmp_path / "elsewhere/apm-home"
    home.mkdir()
    apm_home.mkdir(parents=True)
    (apm_home / "apm.yml").write_text(
        "name: u\nversion: 1.0.0\nexecutables:\n  allow:\n    owner/pkg:\n      hooks: true\n"
    )
    (home / "apm.yml").write_text(
        "name: d\nversion: 1.0.0\nexecutables:\n  allow:\n    owner/decoy:\n      hooks: true\n"
    )
    monkeypatch.setenv("APM_HOME", str(apm_home))
    discovered: list = []
    monkeypatch.setattr(
        "apm_cli.policy.discovery.discover_policy_with_chain",
        lambda root: discovered.append(root) or PolicyFetchResult(outcome="absent"),
    )
    allow = executables.resolve_project_exec_allow_map(
        home,
        SimpleNamespace(allow_executables=None),
        user_scope=True,
    )
    assert discovered == [apm_home]
    assert allow is not None and allow["owner/pkg"]["hooks"] is True
    assert "owner/decoy" not in allow
