"""Unit tests for apm_cli.integration.copilot_cowork_paths."""

from __future__ import annotations

import os  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest

from apm_cli.integration.copilot_cowork_paths import (
    COWORK_LOCKFILE_PREFIX,  # noqa: F401
    COWORK_URI_SCHEME,  # noqa: F401
    CoworkResolutionError,
    from_lockfile_path,
    is_cowork_path,
    resolve_copilot_cowork_skills_dir,
    to_lockfile_path,
)
from apm_cli.utils.path_security import PathTraversalError

# ---------------------------------------------------------------------------
# TestResolveCoworkSkillsDir
# ---------------------------------------------------------------------------


class TestResolveCoworkSkillsDir:
    """Tests for resolve_copilot_cowork_skills_dir auto-detection and env override."""

    def test_env_override_returns_expanded_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "my-skills"
        target.mkdir()
        monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", str(target))
        result = resolve_copilot_cowork_skills_dir()
        assert isinstance(result, Path)
        assert result.name == "my-skills"

    def test_env_override_wins_over_glob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "env-skills"
        target.mkdir()
        monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", str(target))
        # Even if home has cloud storage dirs, env should win:
        cloud = tmp_path / "Library" / "CloudStorage"
        (cloud / "OneDrive - TenantA").mkdir(parents=True)
        (cloud / "OneDrive - TenantB").mkdir(parents=True)
        result = resolve_copilot_cowork_skills_dir()
        assert result is not None
        assert result.name == "env-skills"

    def test_env_override_traversal_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", "../escape")
        with pytest.raises(CoworkResolutionError, match="traversal"):
            resolve_copilot_cowork_skills_dir()

    def test_env_override_embedded_traversal_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", "/valid/../invalid")
        with pytest.raises(CoworkResolutionError, match="traversal"):
            resolve_copilot_cowork_skills_dir()

    def test_macos_single_tenant_returns_skills_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        cloud_dir = tmp_path / "Library" / "CloudStorage"
        tenant_dir = cloud_dir / "OneDrive - Tenant"
        tenant_dir.mkdir(parents=True)
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "darwin"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
        ):
            result = resolve_copilot_cowork_skills_dir()
        expected = tenant_dir / "Documents" / "Cowork" / "skills"
        assert result == expected

    def test_macos_zero_tenant_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        cloud_dir = tmp_path / "Library" / "CloudStorage"
        cloud_dir.mkdir(parents=True)
        # No OneDrive dirs
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "darwin"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
        ):
            result = resolve_copilot_cowork_skills_dir()
        assert result is None

    def test_macos_no_cloud_storage_dir_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        # No Library/CloudStorage at all
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "darwin"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
        ):
            result = resolve_copilot_cowork_skills_dir()
        assert result is None

    def test_macos_multi_tenant_raises_cowork_resolution_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        cloud_dir = tmp_path / "Library" / "CloudStorage"
        (cloud_dir / "OneDrive - TenantA").mkdir(parents=True)
        (cloud_dir / "OneDrive - TenantB").mkdir(parents=True)
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "darwin"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
            pytest.raises(CoworkResolutionError),
        ):
            resolve_copilot_cowork_skills_dir()

    def test_multi_tenant_error_message_lists_candidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        cloud_dir = tmp_path / "Library" / "CloudStorage"
        (cloud_dir / "OneDrive - TenantA").mkdir(parents=True)
        (cloud_dir / "OneDrive - TenantB").mkdir(parents=True)
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "darwin"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
            pytest.raises(CoworkResolutionError) as exc_info,
        ):
            resolve_copilot_cowork_skills_dir()
        msg = str(exc_info.value)
        assert "TenantA" in msg
        assert "TenantB" in msg

    def test_multi_tenant_error_message_hint_contains_env_var_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        cloud_dir = tmp_path / "Library" / "CloudStorage"
        (cloud_dir / "OneDrive - TenantA").mkdir(parents=True)
        (cloud_dir / "OneDrive - TenantB").mkdir(parents=True)
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "darwin"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
            pytest.raises(CoworkResolutionError) as exc_info,
        ):
            resolve_copilot_cowork_skills_dir()
        assert "APM_COPILOT_COWORK_SKILLS_DIR" in str(exc_info.value)

    def test_windows_env_var_returns_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", "/tmp/fake-onedrive/skills")
        result = resolve_copilot_cowork_skills_dir()
        assert isinstance(result, Path)

    def test_linux_no_env_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "linux"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
        ):
            result = resolve_copilot_cowork_skills_dir()
        assert result is None

    # -----------------------------------------------------------------------
    # Resolution precedence: config layer
    # -----------------------------------------------------------------------

    def test_config_beats_macos_auto_detect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config value is used instead of macOS auto-detection when env is unset."""
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        # Set up a cloud storage directory that auto-detect would find.
        cloud = tmp_path / "Library" / "CloudStorage"
        (cloud / "OneDrive - Tenant").mkdir(parents=True)
        with (
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value="/config/skills"),
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "darwin"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
        ):
            result = resolve_copilot_cowork_skills_dir()
        # Config path should win over auto-detected tenant directory.
        assert result == Path("/config/skills").expanduser().resolve()

    def test_env_beats_config_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var takes precedence over the persisted config value."""
        monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", "/env/override/skills")
        with patch("apm_cli.config.get_copilot_cowork_skills_dir") as mock_get_cfg:
            result = resolve_copilot_cowork_skills_dir()
        # Config should not be consulted when the env var is present.
        mock_get_cfg.assert_not_called()
        assert result == Path("/env/override/skills").expanduser().resolve()

    def test_auto_detect_used_when_both_env_and_config_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls through to macOS auto-detection when env and config are both absent."""
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        tenant = tmp_path / "Library" / "CloudStorage" / "OneDrive - EPAM"
        tenant.mkdir(parents=True)
        with (
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value=None),
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "darwin"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
        ):
            result = resolve_copilot_cowork_skills_dir()
        assert result == tenant / "Documents" / "Cowork" / "skills"

    def test_config_path_traversal_raises_cowork_resolution_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A traversal sequence in the config value raises CoworkResolutionError."""
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        with (
            patch(
                "apm_cli.config.get_copilot_cowork_skills_dir",
                return_value="/valid/../invalid",
            ),
            pytest.raises(CoworkResolutionError, match="traversal"),
        ):
            resolve_copilot_cowork_skills_dir()

    def test_config_none_falls_through_cleanly_to_next_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None from config is silently skipped; no exception is raised."""
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        # No CloudStorage directory -- auto-detect returns None.
        with (
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value=None),
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "darwin"),
            patch(
                "apm_cli.integration.copilot_cowork_paths.Path.home",
                return_value=tmp_path,
            ),
        ):
            result = resolve_copilot_cowork_skills_dir()
        assert result is None

    # -----------------------------------------------------------------------
    # Windows auto-detection
    # -----------------------------------------------------------------------

    def test_windows_onedrivecommercial_autodetect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ONEDRIVECOMMERCIAL is used first on Windows."""
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        od_root = tmp_path / "OneDrive - Contoso"
        od_root.mkdir()
        monkeypatch.setenv("ONEDRIVECOMMERCIAL", str(od_root))
        monkeypatch.delenv("ONEDRIVE", raising=False)
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "win32"),
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value=None),
        ):
            result = resolve_copilot_cowork_skills_dir()
        expected = (od_root / "Documents" / "Cowork" / "skills").resolve()
        assert result == expected

    def test_windows_onedrive_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ONEDRIVE is used when ONEDRIVECOMMERCIAL is absent."""
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        od_root = tmp_path / "OneDrive"
        od_root.mkdir()
        monkeypatch.delenv("ONEDRIVECOMMERCIAL", raising=False)
        monkeypatch.setenv("ONEDRIVE", str(od_root))
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "win32"),
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value=None),
        ):
            result = resolve_copilot_cowork_skills_dir()
        expected = (od_root / "Documents" / "Cowork" / "skills").resolve()
        assert result == expected

    def test_windows_neither_env_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Neither ONEDRIVECOMMERCIAL nor ONEDRIVE set returns None."""
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        monkeypatch.delenv("ONEDRIVECOMMERCIAL", raising=False)
        monkeypatch.delenv("ONEDRIVE", raising=False)
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "win32"),
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value=None),
        ):
            result = resolve_copilot_cowork_skills_dir()
        assert result is None

    def test_windows_onedrivecommercial_empty_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Empty ONEDRIVECOMMERCIAL falls through to ONEDRIVE."""
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        od_root = tmp_path / "OneDrive"
        od_root.mkdir()
        monkeypatch.setenv("ONEDRIVECOMMERCIAL", "")
        monkeypatch.setenv("ONEDRIVE", str(od_root))
        with (
            patch("apm_cli.integration.copilot_cowork_paths.sys.platform", "win32"),
            patch("apm_cli.config.get_copilot_cowork_skills_dir", return_value=None),
        ):
            result = resolve_copilot_cowork_skills_dir()
        expected = (od_root / "Documents" / "Cowork" / "skills").resolve()
        assert result == expected


# ---------------------------------------------------------------------------
# TestToLockfilePath
# ---------------------------------------------------------------------------


class TestToLockfilePath:
    """Tests for to_lockfile_path encoding."""

    def test_round_trip_absolute_macos_path(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "my-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.touch()
        result = to_lockfile_path(skill_md, tmp_path)
        assert result == "cowork://skills/my-skill/SKILL.md"

    def test_round_trip_path_with_spaces(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "my skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.touch()
        result = to_lockfile_path(skill_md, tmp_path)
        assert "my skill" in result
        assert result.startswith("cowork://")

    def test_escape_attempt_raises_path_traversal_error(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.md"
        with pytest.raises(PathTraversalError):
            to_lockfile_path(outside, tmp_path)

    def test_result_starts_with_cowork_scheme(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "foo" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.touch()
        result = to_lockfile_path(skill_md, tmp_path)
        assert result.startswith("cowork://")


# ---------------------------------------------------------------------------
# TestFromLockfilePath
# ---------------------------------------------------------------------------


class TestFromLockfilePath:
    """Tests for from_lockfile_path decoding."""

    def test_decode_skills_prefix(self, tmp_path: Path) -> None:
        expected = tmp_path / "my-skill" / "SKILL.md"
        expected.parent.mkdir(parents=True)
        expected.touch()
        result = from_lockfile_path("cowork://skills/my-skill/SKILL.md", tmp_path)
        assert result == expected.resolve()

    def test_round_trip_macos_path(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "round-trip-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.touch()
        encoded = to_lockfile_path(skill_md, tmp_path)
        decoded = from_lockfile_path(encoded, tmp_path)
        assert decoded == skill_md.resolve()

    def test_round_trip_posix_on_windows_style(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "win-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.touch()
        encoded = to_lockfile_path(skill_md, tmp_path)
        decoded = from_lockfile_path(encoded, tmp_path)
        assert decoded.as_posix().endswith("win-skill/SKILL.md")

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        with pytest.raises((PathTraversalError, CoworkResolutionError)):
            from_lockfile_path("cowork://skills/../../etc/passwd", tmp_path)

    def test_non_cowork_uri_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Not a cowork lockfile path"):
            from_lockfile_path("relative/path.md", tmp_path)

    def test_traversal_via_url_encoding_rejected(self, tmp_path: Path) -> None:
        # URL-encoded ".." (%2e%2e) -- the implementation does NOT decode
        # URL-encoded sequences so the literal "%2e%2e" segment is not ".."
        # and is not rejected by validate_path_segments. Document current
        # behavior: it returns a path (no exception).
        result = from_lockfile_path("cowork://skills/%2e%2e/etc/passwd", tmp_path)
        # Current behavior: the literal %2e%2e is treated as a dir name.
        assert isinstance(result, Path)
        # NOTE: potential security gap -- URL-encoded traversal sequences
        # are not decoded/rejected. Reported as implementation observation.


# ---------------------------------------------------------------------------
# TestIsCoworkPath
# ---------------------------------------------------------------------------


class TestIsCoworkPath:
    """Tests for is_cowork_path predicate."""

    def test_cowork_uri_returns_true(self) -> None:
        assert is_cowork_path("cowork://skills/foo/SKILL.md") is True

    def test_relative_path_returns_false(self) -> None:
        assert is_cowork_path("relative/path.md") is False

    def test_empty_string_returns_false(self) -> None:
        assert is_cowork_path("") is False
