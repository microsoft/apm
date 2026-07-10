"""Regression coverage for Windows installer launcher resolution."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_windows_installer_exposes_stable_executable_on_path() -> None:
    """Bare CreateProcess callers must resolve apm.exe without PATHEXT."""
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")

    current_dir = '$currentDir = Join-Path $installRoot "current"'
    current_exe = '$currentExe = Join-Path $currentDir "apm.exe"'
    create_junction = "New-Item -ItemType Junction"
    add_current_to_path = "Add-ToUserPath -PathEntry $currentDir"

    assert current_dir in installer
    assert current_exe in installer
    assert create_junction in installer
    assert add_current_to_path in installer
    assert "Refusing to replace non-junction path" in installer
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
