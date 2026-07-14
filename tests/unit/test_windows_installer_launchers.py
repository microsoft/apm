"""Regression coverage for Windows installer launcher resolution."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
    test_script = (ROOT / "scripts/windows/test-install-script.ps1").read_text(
        encoding="utf-8"
    )

    helper_name = "Assert-MissingStableExecutableFailsForNativeProcess"
    assert f"function {helper_name}" in test_script

    helper_start = test_script.index(f"function {helper_name}")
    helper_end = test_script.index(
        "# ---------------------------------------------------------------------------",
        helper_start,
    )
    helper = test_script[helper_start:helper_end]
    assert 'Join-Path $BinDir "apm.cmd"' in helper
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
