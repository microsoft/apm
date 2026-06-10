"""Static coverage for enterprise bootstrap mirror support in installers."""

from pathlib import Path

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
