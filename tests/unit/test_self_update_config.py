"""Tests for self-update config-backed installer preferences."""

from urllib.parse import urlparse

from apm_cli.commands import self_update


def test_update_installer_env_reads_non_secret_config(monkeypatch, tmp_path):
    """Persisted non-secret installer prefs are passed to the installer."""
    install_dir = tmp_path / "apm-bin"
    monkeypatch.setattr("apm_cli.config.get_self_update_install_dir", lambda: str(install_dir))
    monkeypatch.setattr("apm_cli.config.get_self_update_channel", lambda: "prerelease")
    monkeypatch.delenv("APM_INSTALL_DIR", raising=False)
    monkeypatch.delenv("APM_SELF_UPDATE_CHANNEL", raising=False)
    monkeypatch.delenv("VERSION", raising=False)

    env = self_update._build_self_update_installer_env("1.2.3rc1")

    assert env["APM_INSTALL_DIR"] == str(install_dir)
    assert env["APM_SELF_UPDATE_CHANNEL"] == "prerelease"
    assert env["VERSION"] == "v1.2.3rc1"


def test_update_installer_env_preserves_invocation_env(monkeypatch, tmp_path):
    """Invocation-scoped env vars outrank persisted self-update prefs."""
    install_dir = tmp_path / "config-bin"
    monkeypatch.setattr("apm_cli.config.get_self_update_install_dir", lambda: str(install_dir))
    monkeypatch.setattr("apm_cli.config.get_self_update_channel", lambda: "prerelease")
    monkeypatch.setenv("APM_INSTALL_DIR", "/env/bin")
    monkeypatch.setenv("APM_SELF_UPDATE_CHANNEL", "stable")
    monkeypatch.setenv("VERSION", "v9.9.9")

    env = self_update._build_self_update_installer_env("1.2.3rc1")

    assert env["APM_INSTALL_DIR"] == "/env/bin"
    assert env["APM_SELF_UPDATE_CHANNEL"] == "stable"
    assert env["VERSION"] == "v9.9.9"


def test_prerelease_channel_uses_releases_endpoint(monkeypatch):
    """The prerelease channel reads release listing metadata instead of latest."""
    captured: dict[str, str] = {}

    class Response:
        status_code = 200

        def json(self):
            return [
                {"tag_name": "v2.0.0rc1", "prerelease": True},
                {"tag_name": "v1.0.0", "prerelease": False},
            ]

    def fake_get(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = str(timeout)
        return Response()

    monkeypatch.delenv("VERSION", raising=False)
    monkeypatch.delenv("APM_RELEASE_METADATA_URL", raising=False)
    monkeypatch.delenv("GITHUB_URL", raising=False)
    monkeypatch.delenv("APM_REPO", raising=False)
    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("apm_cli.utils.version_checker._get_github_token", lambda *_: None)

    version = self_update.get_latest_version_for_self_update("prerelease")

    parsed = urlparse(captured["url"])
    assert parsed.scheme == "https"
    assert parsed.hostname == "api.github.com"
    assert parsed.path == "/repos/microsoft/apm/releases"
    assert version == "2.0.0rc1"
