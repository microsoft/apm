"""Tests for self-update config-backed installer preferences."""

from urllib.parse import urlparse

import pytest

from apm_cli.commands import self_update

pytestmark = pytest.mark.unit


def test_stable_channel_sets_resolved_installer_version(monkeypatch):
    """Stable release discovery must pin the same version for installation."""
    monkeypatch.setattr("apm_cli.config.get_self_update_install_dir", lambda: None)
    monkeypatch.setattr("apm_cli.config.get_self_update_channel", lambda: "stable")
    monkeypatch.delenv("APM_SELF_UPDATE_CHANNEL", raising=False)
    monkeypatch.delenv("VERSION", raising=False)

    release = self_update._resolve_self_update_release("1.5.0")
    env = self_update._build_self_update_installer_env(release)

    assert env["APM_SELF_UPDATE_CHANNEL"] == "stable"
    assert env["VERSION"] == "v1.5.0"


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

    def fake_get(url, headers, timeout, **kwargs):
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
    assert parsed.query == "per_page=5"
    assert version == "2.0.0rc1"


@pytest.mark.windows_compat
def test_windows_installer_env_drops_inherited_psmodulepath(monkeypatch):
    """A PowerShell 7 parent's module path must not leak into the installer host (#3926)."""
    monkeypatch.setattr(self_update, "_is_windows_platform", lambda: True)
    monkeypatch.setattr("apm_cli.config.get_self_update_install_dir", lambda: None)
    monkeypatch.setattr("apm_cli.config.get_self_update_channel", lambda: "stable")
    monkeypatch.setattr(
        self_update,
        "external_process_env",
        lambda: {
            "PSMODULEPATH": r"C:\Program Files\PowerShell\7\Modules",
            "PSModulePath": r"C:\Users\me\Documents\PowerShell\Modules",
            "PATH": r"C:\Windows\System32",
        },
    )

    env = self_update._build_self_update_installer_env(
        self_update._resolve_self_update_release("1.2.3")
    )

    assert not [key for key in env if key.upper() == "PSMODULEPATH"]
    assert env["PATH"] == r"C:\Windows\System32"


@pytest.mark.windows_compat
def test_unix_installer_env_keeps_psmodulepath(monkeypatch):
    """Only the Windows PowerShell installer host needs its own module path."""
    monkeypatch.setattr(self_update, "_is_windows_platform", lambda: False)
    monkeypatch.setattr("apm_cli.config.get_self_update_install_dir", lambda: None)
    monkeypatch.setattr("apm_cli.config.get_self_update_channel", lambda: "stable")
    # Inject the env: Windows os.environ upper-cases keys, so a real setenv
    # would surface as PSMODULEPATH on Windows runners.
    monkeypatch.setattr(
        self_update,
        "external_process_env",
        lambda: {"PSModulePath": "/opt/microsoft/powershell/7/Modules"},
    )

    env = self_update._build_self_update_installer_env(
        self_update._resolve_self_update_release("1.2.3")
    )

    assert env["PSModulePath"] == "/opt/microsoft/powershell/7/Modules"
