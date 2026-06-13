"""Static coverage for enterprise bootstrap mirror support in installers."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIRROR_ENV_VARS = {
    "APM_RELEASE_BASE_URL",
    "APM_RELEASE_METADATA_URL",
    "APM_INSTALLER_BASE_URL",
    "APM_PYPI_INDEX_URL",
    "APM_NO_DIRECT_FALLBACK",
}


def _read_repo_file(name: str) -> str:
    """Read an installer script from the repository root."""
    return (ROOT / name).read_text(encoding="utf-8")


def test_unix_installer_exposes_enterprise_bootstrap_env_vars() -> None:
    """install.sh documents and wires every v0 enterprise bootstrap env var."""
    text = _read_repo_file("install.sh")

    missing = {name for name in MIRROR_ENV_VARS if name not in text}
    assert missing == set()
    assert "release_metadata_url" in text
    assert "release_asset_url" in text
    assert "pip_index_args" in text


def test_windows_installer_exposes_enterprise_bootstrap_env_vars() -> None:
    """install.ps1 documents and wires every v0 enterprise bootstrap env var."""
    text = _read_repo_file("install.ps1")

    missing = {name for name in MIRROR_ENV_VARS if name not in text}
    assert missing == set()
    assert "Get-ReleaseMetadataUri" in text
    assert "Get-ReleaseAssetUri" in text
    assert "Get-PipIndexArgs" in text


def test_unix_installer_redacts_printed_mirror_urls() -> None:
    """install.sh must redact credentials before printing mirror URLs."""
    text = _read_repo_file("install.sh")

    assert "redact_url_credentials()" in text
    assert 'Download URL: $(redact_url_credentials "$DOWNLOAD_URL")' in text
    assert 'Direct URL: $(redact_url_credentials "$DOWNLOAD_URL")' in text
    assert text.count('Mirror URL: $(redact_url_credentials "$APM_RELEASE_METADATA_URL")') == 2
    assert text.count('Mirror URL: $(redact_url_credentials "$DOWNLOAD_URL")') == 2


def test_windows_installer_redacts_printed_mirror_urls() -> None:
    """install.ps1 must redact credentials before printing mirror URLs."""
    text = _read_repo_file("install.ps1")

    assert "function Redact-UrlCredentials" in text
    assert "Mirror URL: $(Redact-UrlCredentials -Url $releaseMetadataUrl)" in text
    assert "Mirror URL was: $(Redact-UrlCredentials -Url $directUrl)" in text
    assert "Direct URL was: $(Redact-UrlCredentials -Url $directUrl)" in text


def test_unix_installer_does_not_send_token_to_mirror_metadata() -> None:
    """install.sh must not attach the GitHub token to a mirror metadata host.

    Regression trap for the cross-host token-transmission fix: the metadata
    fetch attaches Authorization only when APM_RELEASE_METADATA_URL is unset
    (canonical GitHub / GHES host), staying symmetric with install.ps1.
    """
    text = _read_repo_file("install.sh")

    assert 'if [ -n "$AUTH_HEADER_VALUE" ] && [ -z "$APM_RELEASE_METADATA_URL" ]; then' in text


def test_unix_installer_does_not_send_token_to_mirror_asset() -> None:
    """install.sh must not attach the GitHub token to a mirror asset host."""
    text = _read_repo_file("install.sh")

    assert 'if [ -n "$AUTH_HEADER_VALUE" ] && [ -z "$APM_RELEASE_BASE_URL" ]; then' in text


def test_windows_installer_does_not_send_token_to_mirror() -> None:
    """install.ps1 must gate every auth retry on mirror-env absence.

    Both the asset final-fallback and the checksum retry now require
    -not $releaseBaseUrl, so the GitHub token never reaches an operator
    mirror host (symmetric with install.sh).
    """
    text = _read_repo_file("install.ps1")

    assert "if (-not $downloadOk -and -not $releaseBaseUrl) {" in text
    assert "if ($headers.Count -gt 0 -and -not $releaseBaseUrl) {" in text


def _run_unix_installer(extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    """Execute install.sh with a sanitized env and no mirror coverage.

    All mirror env vars and any resolved token are stripped so the script
    reaches its fail-closed guards before any network call.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "APM_RELEASE_BASE_URL",
            "APM_RELEASE_METADATA_URL",
            "APM_INSTALLER_BASE_URL",
            "APM_PYPI_INDEX_URL",
            "APM_NO_DIRECT_FALLBACK",
            "VERSION",
            "GITHUB_URL",
            "GITHUB_APM_PAT",
            "GITHUB_TOKEN",
            "GH_TOKEN",
        }
    }
    env.update(extra_env)
    return subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="install.sh is the Unix installer; its OS guard rejects MINGW/Windows before env-var checks",
)
def test_unix_installer_fail_closed_metadata_exit_code() -> None:
    """Fail-closed metadata path exits non-zero with actionable guidance.

    Executable regression trap (no network): APM_NO_DIRECT_FALLBACK without a
    metadata mirror must exit non-zero before contacting any public host.
    """
    if shutil.which("sh") is None:
        pytest.skip("POSIX sh not available")

    result = _run_unix_installer({"APM_NO_DIRECT_FALLBACK": "1"})

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "APM_NO_DIRECT_FALLBACK is set" in combined
    assert "APM_RELEASE_METADATA_URL is not configured" in combined


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="install.sh is the Unix installer; its OS guard rejects MINGW/Windows before env-var checks",
)
def test_unix_installer_fail_closed_asset_exit_code() -> None:
    """Fail-closed asset path (pinned VERSION) exits non-zero, no public fallback."""
    if shutil.which("sh") is None:
        pytest.skip("POSIX sh not available")

    result = _run_unix_installer({"APM_NO_DIRECT_FALLBACK": "1", "VERSION": "v9.9.9"})

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "APM_NO_DIRECT_FALLBACK is set" in combined
    assert "APM_RELEASE_BASE_URL is not configured" in combined


def test_windows_installer_uses_auth_on_first_ghes_metadata_fetch() -> None:
    """install.ps1 should not make an unauthenticated GHES metadata request first."""
    text = _read_repo_file("install.ps1")

    assert "$headers = if ($releaseMetadataUrl) { @{} } else { Get-AuthHeader }" in text
    assert "$release = Invoke-GitHubJson -Uri $latestUri -Headers $headers" in text
    assert "$release = Invoke-RestMethod -Uri $latestUri" not in text


def test_unix_installer_centralizes_fail_closed_error_style() -> None:
    """install.sh fail-closed guards should share one actionable error helper."""
    text = _read_repo_file("install.sh")

    assert "fail_closed_error()" in text
    assert text.count("fail_closed_error APM_RELEASE_BASE_URL") == 2
    assert text.count("fail_closed_error APM_RELEASE_METADATA_URL") == 1
    assert text.count("fail_closed_error APM_PYPI_INDEX_URL") == 1
