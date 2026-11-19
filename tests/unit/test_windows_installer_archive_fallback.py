"""Behavioral coverage for install.ps1's release-archive extraction (#3926).

Windows PowerShell 5.1 started below a PowerShell 7 parent can load the
PowerShell 7 Archive module, whose Expand-Archive calls a ZipFile overload
.NET Framework lacks. ``Expand-ReleaseArchive`` must then extract with the
2-argument ``ZipFile.ExtractToDirectory`` overload instead of failing.

The extracted function runs under every available PowerShell host; the
failure is simulated with an ``Expand-Archive`` function, which PowerShell
resolves before the cmdlet.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.windows_compat

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL_HOSTS = ("powershell", "pwsh")
BROKEN_EXPAND_ARCHIVE = (
    "function Expand-Archive { throw [System.Management.Automation.MethodException]::new("
    '\'Cannot find an overload for "ExtractToDirectory" and the argument count: "3".\') }'
)


def _extract_function(name: str) -> str:
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^function {re.escape(name)} \{{.*?^\}}", text)
    assert match is not None, f"{name} not found in install.ps1"
    return match.group(0)


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _expand(host: str, tmp_path: Path, *, broken: bool) -> tuple[Path, str]:
    executable = shutil.which(host)
    if executable is None:
        pytest.skip(f"{host} is not available")
    temp_dir = tmp_path / "apm-install"
    temp_dir.mkdir()
    zip_path = temp_dir / "apm-windows-x86_64.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("apm-windows-x86_64/apm.exe", "binary")
    script = tmp_path / "expand.ps1"
    lines = [
        '$ErrorActionPreference = "Stop"',
        _extract_function("Write-WarningText"),
        _extract_function("Expand-ReleaseArchive"),
    ]
    if broken:
        lines.append(BROKEN_EXPAND_ARCHIVE)
    lines += [
        f"$root = Expand-ReleaseArchive -ZipPath {_quote(zip_path)} -DestinationPath {_quote(temp_dir)}",
        'Write-Output "ROOT=$root"',
    ]
    script.write_text("\n".join(lines) + "\n", encoding="ascii")
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    roots = [line[5:].strip() for line in result.stdout.splitlines() if line.startswith("ROOT=")]
    assert len(roots) == 1, result.stdout
    return Path(roots[0]), result.stdout


@pytest.mark.parametrize("host", POWERSHELL_HOSTS)
def test_release_archive_extracts_with_expand_archive(host: str, tmp_path: Path):
    root, output = _expand(host, tmp_path, broken=False)

    assert root == tmp_path / "apm-install"
    assert (root / "apm-windows-x86_64" / "apm.exe").read_text(encoding="utf-8") == "binary"
    assert "retrying" not in output


@pytest.mark.parametrize("host", POWERSHELL_HOSTS)
def test_release_archive_falls_back_to_zipfile_when_expand_archive_fails(host: str, tmp_path: Path):
    root, output = _expand(host, tmp_path, broken=True)

    assert root.parent == tmp_path / "apm-install"
    assert root.name.startswith("extract-")
    assert (root / "apm-windows-x86_64" / "apm.exe").read_text(encoding="utf-8") == "binary"
    assert "Expand-Archive failed" in output
    assert "ExtractToDirectory" in output


def test_installer_stages_package_from_extraction_root() -> None:
    """The install flow must use the helper's root, not assume $tempDir."""
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")
    body = _extract_function("Expand-ReleaseArchive")

    assert "Add-Type -AssemblyName System.IO.Compression.FileSystem" in body
    assert "[System.IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $fallbackDir)" in body
    assert "-ErrorAction Stop" in body
    call = "$extractRoot = Expand-ReleaseArchive -ZipPath $zipPath -DestinationPath $tempDir"
    assert call in text
    assert '$packageDir = Join-Path $extractRoot "apm-windows-x86_64"' in text
    assert text.count("Expand-Archive -Path") == 1
