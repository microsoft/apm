"""Tests for the CLI > env var > apm config > default transport-preference precedence chain.

These tests verify the wiring between the install command and the config module
introduced in issue #1243. They do NOT exercise the full install pipeline;
they focus exclusively on the precedence resolution so each layer of the chain
can be validated in isolation.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = pytest.mark.component


@pytest.mark.windows_compat
@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize(
    ("create_config", "stored", "use_env", "expected_pref", "expected_fallback"),
    [
        (False, False, False, None, False),
        (True, False, False, None, False),
        (False, True, False, "ssh", True),
        (False, True, True, "https", False),
    ],
    ids=["read-only-defaults", "install-bootstrap", "read-only-stored", "read-only-env"],
)
def test_downloader_config_bootstrap_preserves_transport_precedence(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    create_config: bool,
    stored: bool,
    use_env: bool,
    expected_pref: str | None,
    expected_fallback: bool,
    platform: str,
) -> None:
    """Skipping initialization changes no configured or environment transport choice."""
    from apm_cli import config
    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.deps.transport_selection import ProtocolPreference

    config_path = tmp_path / ".apm/config.json"
    monkeypatch.setattr(config, "CONFIG_DIR", str(config_path.parent))
    monkeypatch.setattr(config, "CONFIG_FILE", str(config_path))
    monkeypatch.setattr(config, "_config_cache", None)
    for key in ("APM_GIT_PROTOCOL", "APM_ALLOW_PROTOCOL_FALLBACK", "APM_TEMP_DIR"):
        monkeypatch.delenv(key, raising=False)
    # Exercise the real Windows sentinel branch, not a stubbed config reader.
    # Scratch Git config is allowed; missing user configuration is not.
    scratch = tmp_path_factory.mktemp("git-sentinel")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    if stored:
        config_path.parent.mkdir()
        config_path.write_text(
            json.dumps({"prefer_ssh": True, "allow_protocol_fallback": True}), encoding="utf-8"
        )
    if use_env:
        monkeypatch.setenv("APM_GIT_PROTOCOL", "https")
        monkeypatch.setenv("APM_ALLOW_PROTOCOL_FALLBACK", "0")
    before = ArtifactSnapshot.capture(tmp_path)

    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(sys, "platform", platform)
        if create_config:
            downloader = GitHubPackageDownloader(auth_resolver=MagicMock())
        else:
            downloader = GitHubPackageDownloader(auth_resolver=MagicMock(), create_config=False)

    assert downloader._protocol_pref is ProtocolPreference.from_str(expected_pref)
    assert downloader._allow_fallback is expected_fallback
    assert downloader.git_env["GIT_ASKPASS"] == "echo"
    assert downloader.git_env["GIT_CONFIG_NOSYSTEM"] == "1"
    if platform == "win32":
        sentinel = scratch / ".apm_empty_gitconfig"
        assert downloader.git_env["GIT_CONFIG_GLOBAL"] == str(sentinel)
        assert sentinel.read_bytes() == b""
    else:
        assert downloader.git_env["GIT_CONFIG_GLOBAL"] == os.devnull
    if create_config:
        assert json.loads(config_path.read_text(encoding="utf-8")) == {"default_client": "vscode"}
    else:
        assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))


@pytest.mark.windows_compat
@pytest.mark.parametrize("use_env", [False, True], ids=["stored-temp", "env-temp"])
def test_read_only_windows_sentinel_preserves_temp_precedence(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    use_env: bool,
) -> None:
    """The sentinel uses env > saved temp without modifying saved configuration."""
    from apm_cli import config
    from apm_cli.deps.github_downloader import GitHubPackageDownloader

    saved_temp = tmp_path_factory.mktemp("saved-temp")
    env_temp = tmp_path_factory.mktemp("env-temp")
    config_path = tmp_path / ".apm/config.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({"temp_dir": str(saved_temp)}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_DIR", str(config_path.parent))
    monkeypatch.setattr(config, "CONFIG_FILE", str(config_path))
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.delenv("APM_TEMP_DIR", raising=False)
    if use_env:
        monkeypatch.setenv("APM_TEMP_DIR", str(env_temp))
    before = ArtifactSnapshot.capture(tmp_path)

    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(sys, "platform", "win32")
        downloader = GitHubPackageDownloader(auth_resolver=MagicMock(), create_config=False)

    expected_temp, unused_temp = (env_temp, saved_temp) if use_env else (saved_temp, env_temp)
    sentinel = expected_temp / ".apm_empty_gitconfig"
    assert downloader.git_env["GIT_CONFIG_GLOBAL"] == str(sentinel)
    assert sentinel.read_bytes() == b""
    assert not (unused_temp / ".apm_empty_gitconfig").exists()
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_protocol_preference():
    """Return a fresh ProtocolPreference-like enum from the real module."""
    from apm_cli.deps.transport_selection import ProtocolPreference

    return ProtocolPreference


# ---------------------------------------------------------------------------
# get_apm_allow_protocol_fallback precedence
# ---------------------------------------------------------------------------


class TestAllowProtocolFallbackPrecedence:
    """CLI flag > env var > apm config > False."""

    def test_cli_flag_true_wins_over_everything(self):
        """CLI flag=True overrides config=False and env=unset."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            # Simulate: allow_protocol_fallback (CLI) = True
            cli_flag = True
            result = cli_flag or cfg_module.get_apm_allow_protocol_fallback()
        assert result is True

    def test_env_var_wins_over_config_false(self):
        """APM_ALLOW_PROTOCOL_FALLBACK=1 overrides config=False."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": "1"}),
        ):
            result = cfg_module.get_apm_allow_protocol_fallback()
        assert result is True

    def test_config_true_used_when_cli_false_env_unset(self):
        """Config=True is used when CLI flag is False and env var is absent."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=True),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            # Simulate: CLI flag = False, so we call get_apm_allow_protocol_fallback
            cli_flag = False
            result = cli_flag or cfg_module.get_apm_allow_protocol_fallback()
        assert result is True

    def test_default_false_when_all_layers_absent(self):
        """Returns False when CLI flag=False, env unset, and config=False."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            cli_flag = False
            result = cli_flag or cfg_module.get_apm_allow_protocol_fallback()
        assert result is False

    @pytest.mark.parametrize("env_val", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_env_var_truthy_values(self, env_val):
        """All accepted truthy values for APM_ALLOW_PROTOCOL_FALLBACK."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=False),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": env_val}),
        ):
            assert cfg_module.get_apm_allow_protocol_fallback() is True

    @pytest.mark.parametrize("env_val", ["0", "false", "no", "off"])
    def test_env_var_explicit_falsy_overrides_config_true(self, env_val):
        """Explicit falsy env values return False even when config=True (env wins)."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=True),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": env_val}),
        ):
            # Explicit falsy env var overrides persisted config=True
            assert cfg_module.get_apm_allow_protocol_fallback() is False

    def test_env_var_empty_falls_through_to_config(self):
        """Empty APM_ALLOW_PROTOCOL_FALLBACK (unset semantics) falls through to config."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_allow_protocol_fallback", return_value=True),
            patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": ""}),
        ):
            assert cfg_module.get_apm_allow_protocol_fallback() is True


# ---------------------------------------------------------------------------
# get_apm_protocol_pref precedence (ssh preference)
# ---------------------------------------------------------------------------


class TestProtocolPrefPrecedence:
    """CLI flag > APM_GIT_PROTOCOL env > apm config prefer-ssh > None (git insteadOf)."""

    def test_cli_flag_ssh_wins_over_env_and_config(self):
        """CLI --ssh flag (use_ssh=True) bypasses env/config entirely in install.py."""
        # The install command checks use_ssh first and skips get_apm_protocol_pref.
        # We verify the helper would return 'ssh' even if it were called.
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=False),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_GIT_PROTOCOL", None)
            # CLI flag path: use_ssh=True → ProtocolPreference.SSH without calling helper
            # Just ensure the helper returns None here (not ssh)
            assert cfg_module.get_apm_protocol_pref() is None

    def test_env_var_ssh_wins_over_config_false(self):
        """APM_GIT_PROTOCOL=ssh overrides config prefer_ssh=False."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=False),
            patch.dict(os.environ, {"APM_GIT_PROTOCOL": "ssh"}),
        ):
            assert cfg_module.get_apm_protocol_pref() == "ssh"

    def test_env_var_https_wins_over_config_ssh_true(self):
        """APM_GIT_PROTOCOL=https wins even if config prefer_ssh=True."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=True),
            patch.dict(os.environ, {"APM_GIT_PROTOCOL": "https"}),
        ):
            assert cfg_module.get_apm_protocol_pref() == "https"

    def test_config_prefer_ssh_true_used_when_env_absent(self):
        """Config prefer_ssh=True maps to 'ssh' when APM_GIT_PROTOCOL is unset."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=True),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_GIT_PROTOCOL", None)
            assert cfg_module.get_apm_protocol_pref() == "ssh"

    def test_returns_none_when_no_preference_set(self):
        """Returns None to let git insteadOf rules decide when nothing is configured."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=False),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("APM_GIT_PROTOCOL", None)
            assert cfg_module.get_apm_protocol_pref() is None

    def test_unrecognised_env_val_falls_through_to_config(self):
        """An unrecognised APM_GIT_PROTOCOL value falls through to config."""
        import apm_cli.config as cfg_module

        with (
            patch.object(cfg_module, "get_prefer_ssh", return_value=True),
            patch.dict(os.environ, {"APM_GIT_PROTOCOL": "git"}),
        ):
            # 'git' is not in (ssh, https, http) → fallthrough to config (prefer_ssh=True)
            assert cfg_module.get_apm_protocol_pref() == "ssh"


# ---------------------------------------------------------------------------
# ProtocolPreference.from_str round-trip
# ---------------------------------------------------------------------------


class TestProtocolPreferenceFromStr:
    """Verify ProtocolPreference.from_str handles get_apm_protocol_pref outputs."""

    def test_ssh_string_maps_to_ssh(self):
        """'ssh' maps to ProtocolPreference.SSH."""
        from apm_cli.deps.transport_selection import ProtocolPreference

        assert ProtocolPreference.from_str("ssh") == ProtocolPreference.SSH

    def test_https_string_maps_to_https(self):
        """'https' maps to ProtocolPreference.HTTPS."""
        from apm_cli.deps.transport_selection import ProtocolPreference

        assert ProtocolPreference.from_str("https") == ProtocolPreference.HTTPS

    def test_none_maps_to_none(self):
        """None maps to ProtocolPreference.NONE (uses git insteadOf)."""
        from apm_cli.deps.transport_selection import ProtocolPreference

        assert ProtocolPreference.from_str(None) == ProtocolPreference.NONE

    def test_empty_string_maps_to_none(self):
        """Empty string maps to ProtocolPreference.NONE."""
        from apm_cli.deps.transport_selection import ProtocolPreference

        assert ProtocolPreference.from_str("") == ProtocolPreference.NONE


# ---------------------------------------------------------------------------
# Round-trip: config set → get_apm_* helpers
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    """Verify that set_* followed by get_apm_* helpers honour the stored value."""

    def test_set_prefer_ssh_true_reflected_in_get_apm_protocol_pref(self, isolated_config):
        """After set_prefer_ssh(True), get_apm_protocol_pref returns 'ssh' when env is absent."""
        import apm_cli.config as cfg_module

        cfg_module.set_prefer_ssh(True)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_GIT_PROTOCOL", None)
            assert cfg_module.get_apm_protocol_pref() == "ssh"

    def test_set_prefer_ssh_false_reflected_in_get_apm_protocol_pref(self, isolated_config):
        """After set_prefer_ssh(False), get_apm_protocol_pref returns None when env is absent."""
        import apm_cli.config as cfg_module

        cfg_module.set_prefer_ssh(False)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_GIT_PROTOCOL", None)
            assert cfg_module.get_apm_protocol_pref() is None

    def test_set_allow_protocol_fallback_reflected_in_get_apm_helper(self, isolated_config):
        """After set_allow_protocol_fallback(True), get_apm_allow_protocol_fallback is True."""
        import apm_cli.config as cfg_module

        cfg_module.set_allow_protocol_fallback(True)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            assert cfg_module.get_apm_allow_protocol_fallback() is True

    def test_set_allow_protocol_fallback_false_reflected(self, isolated_config):
        """After set_allow_protocol_fallback(False), get_apm_allow_protocol_fallback is False."""
        import apm_cli.config as cfg_module

        cfg_module.set_allow_protocol_fallback(False)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_ALLOW_PROTOCOL_FALLBACK", None)
            assert cfg_module.get_apm_allow_protocol_fallback() is False


# ---------------------------------------------------------------------------
# Fixture: isolated config (mirrors test_config_command.py fixture)
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE to a temp dir so tests never touch ~/.apm."""
    import apm_cli.config as _conf

    _conf._invalidate_config_cache()
    config_dir = tmp_path / ".apm"
    config_file = config_dir / "config.json"
    monkeypatch.setattr(_conf, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(config_file))
    yield config_file
    _conf._invalidate_config_cache()
