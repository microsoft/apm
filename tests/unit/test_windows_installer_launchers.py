"""Regression coverage for Windows installer launcher resolution."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.windows_compat


def test_windows_installer_exposes_stable_executable_on_path() -> None:
    """Bare CreateProcess callers must resolve apm.exe without PATHEXT."""
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")

    current_dir = '$currentDir = Join-Path $installRoot "current"'
    current_exe = '$currentExe = Join-Path $currentDir "apm.exe"'
    create_junction = "New-Item -ItemType Junction"
    add_current_to_path = "Add-ToUserPath -PathEntry $currentDir"
    remove_old_junction = "[System.IO.Directory]::Delete($oldCurrentDir)"

    assert current_dir in installer
    assert current_exe in installer
    assert create_junction in installer
    assert add_current_to_path in installer
    assert "Refusing to replace non-junction path" in installer
    assert remove_old_junction in installer
    assert "Remove-Item -Force $oldCurrentDir" not in installer
    assert installer.index(create_junction) < installer.index(add_current_to_path)


def test_windows_installer_e2e_covers_bare_subprocess_resolution() -> None:
    """The Windows release gate must exercise the reporter's failing call."""
    test_script = (ROOT / "scripts/windows/test-install-script.ps1").read_text(encoding="utf-8")

    assert '["apm", "--version"],' in test_script
    # Spaces and a cmd metacharacter in the prefix defend launcher quoting.
    assert "APM Install Test & Edge" in test_script
    assert 'cwd=os.environ["APM_LAUNCH_TEST_CWD"]' in test_script
    assert 'PATH="$2:$PATH"' in test_script
    assert "command -v apm && apm --version" in test_script
    assert "cmd.exe /d /c" in test_script
    assert "Stable executable directory precedes command shim directory in user PATH" in test_script


def test_windows_installer_e2e_covers_missing_stable_executable_negative_twin() -> None:
    """The native-process proof must fail when only the cmd shim remains."""
    test_script = (ROOT / "scripts/windows/test-install-script.ps1").read_text(encoding="utf-8")

    helper_name = "Assert-MissingStableExecutableFailsForNativeProcess"
    assert f"function {helper_name}" in test_script

    helper_start = test_script.index(f"function {helper_name}")
    helper_end = test_script.index(
        "# ---------------------------------------------------------------------------",
        helper_start,
    )
    helper = test_script[helper_start:helper_end]
    assert 'Join-Path $BinDir "$CommandName.cmd"' in helper
    assert '["apm", "--version"],' in helper
    assert 'cwd=os.environ["APM_LAUNCH_TEST_CWD"]' in helper
    assert "except FileNotFoundError:" in helper
    assert '$env:Path = "$CurrentDir;$BinDir"' in helper
    assert 'throw "Python subprocess unexpectedly resolved' in helper

    e2e_start = test_script.index("function Test-EndToEndInstall")
    e2e_end = test_script.index("function Test-NonJunctionCollision")
    assert helper_name in test_script[e2e_start:e2e_end]


def test_windows_installer_e2e_covers_non_junction_collision() -> None:
    """The Windows release gate must prove collision failures preserve data."""
    test_script = (ROOT / "scripts/windows/test-install-script.ps1").read_text(encoding="utf-8")

    assert "function Test-NonJunctionCollision" in test_script
    assert "Installer refuses a non-junction current path" in test_script
    assert "Non-junction current path preserves its canary file" in test_script
    assert "Test-NonJunctionCollision" in test_script[test_script.index("# Runner") :]


def test_companion_is_native_owned_and_transactional() -> None:
    """The companion cannot be a shim-only command or steal another apmx path."""
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert '$stagedCompanion = Join-Path $stagingDir "apmx.exe"' in installer
    assert (
        "Test-OwnedCompanionShim -Path $companionShim -ExpectedContent $companionContent"
        in installer
    )
    assert "[System.IO.File]::ReadAllText($Path) -ceq $ExpectedContent" in installer
    assert "Refusing to replace unrelated apmx.cmd" in installer
    assert "Unrelated apmx.exe exists" in installer
    assert "} elseif ($ownedCompanion) {" in installer
    assert "Remove-Item -LiteralPath $companionShim -Force" in installer
    assert installer.index("& $stagedCompanion $option") < installer.index("$promoted = $true")
    assert installer.index("& $currentCompanion $option") < installer.rindex(
        "Remove-Item -Recurse -Force $backupDir"
    )
    assert "[System.IO.File]::WriteAllBytes($shimPath, [byte[]]$oldShimBytes)" in installer


def test_current_companion_gate_has_negative_twins_and_legacy_downgrade() -> None:
    """The gate uses new build bytes, not historical releases without apmx."""
    test_script = (ROOT / "scripts/windows/test-install-script.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/build-release.yml").read_text(encoding="utf-8")
    assert "-LocalBundle dist/apm-windows-x86_64" in workflow
    assert "function Test-LocalCompanionBundle" in test_script
    assert 'subprocess.run(["apmx", flag]' in test_script
    assert "-CommandName apmx" in test_script
    assert "Companion activation failure rejects the upgrade" in test_script
    assert "Installer refuses unrelated apmx.cmd" in test_script
    assert "Legacy downgrade removes only the owned apmx shim" in test_script


def test_companion_shim_ownership_executes_exact_content_contract(tmp_path: Path) -> None:
    """Run the production PowerShell owner on real files without installing anything."""
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is needed to execute the Windows installer owner")
    command = r"""
$ErrorActionPreference = "Stop"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:APM_TEST_INSTALLER, [ref]$tokens, [ref]$errors)
if ($errors) { throw ($errors | Out-String) }
$functions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @("Get-CompanionShimContent", "Test-OwnedCompanionShim")
}, $true)
foreach ($function in $functions) { . ([scriptblock]::Create($function.Extent.Text)) }
$current = Join-Path $env:APM_TEST_ROOT "space & percent%/current"
$shim = Join-Path $env:APM_TEST_ROOT "apmx.cmd"
$content = Get-CompanionShimContent -CurrentDir $current
if (-not $content.Contains('percent%%')) { throw "Literal percent was not escaped" }
if (-not $content.Contains('" %*')) { throw "Launcher target is not quoted" }
Set-Content -LiteralPath $shim -Value $content -Encoding ASCII -NoNewline
if (-not (Test-OwnedCompanionShim $shim $content)) { throw "Owned shim rejected" }
Set-Content -LiteralPath $shim -Value ($content + "REM user edit") -Encoding ASCII -NoNewline
if (Test-OwnedCompanionShim $shim $content) { throw "Modified shim accepted" }
Remove-Item -LiteralPath $shim
New-Item -ItemType Directory -Path $shim | Out-Null
if (Test-OwnedCompanionShim $shim $content) { throw "Directory accepted as owned shim" }
Write-Output "companion ownership verified"
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        env={
            **os.environ,
            "APM_TEST_INSTALLER": str(ROOT / "install.ps1"),
            "APM_TEST_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "companion ownership verified" in result.stdout


def test_windows_e2e_verifies_junction_resolution_after_upgrade() -> None:
    """Upgrade/reinstall gates must confirm the junction re-points at the new release."""
    test_script = (ROOT / "scripts/windows/test-install-script.ps1").read_text(encoding="utf-8")

    # A wrong-target junction survives the installer's own Test-Path guard, so
    # the upgrade and reinstall gates must resolve current\apm.exe and assert
    # its reported version, not merely that the file exists.
    assert "$stableExe --version" in test_script
    assert "junction temps at install root" in test_script
    assert "current.new-*" in test_script
    assert "current.old-*" in test_script
