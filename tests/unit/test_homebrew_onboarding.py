"""Exercise installer entrypoints without running a real package manager."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(sys.platform == "win32" or BASH is None, reason="Unix shell installers"),
]


@pytest.mark.parametrize(
    ("managers", "expected"),
    [
        (("brew", "uv", "pip"), "brew install apm"),
        (("uv", "pip"), "uv pip install apm-cli"),
        (("pip",), "pip install apm-cli"),
    ],
)
def test_package_manager_installer_selects_available_manager(
    tmp_path: Path, managers: tuple[str, ...], expected: str
) -> None:
    """Core is selected without a tap; non-Homebrew fallbacks stay available."""
    functions = "\n".join(f'{name}() {{ printf "{name} %s\\n" "$*"; }}' for name in managers)
    result = subprocess.run(
        [BASH, "-c", functions + '\nsource "$1"', "test", str(ROOT / "scripts/install.sh")],
        env={"HOME": str(tmp_path), "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    calls = [
        line for line in result.stdout.splitlines() if line.startswith(("brew ", "uv ", "pip "))
    ]
    assert calls == [expected]
    assert "APM installed successfully!" in result.stdout.splitlines()


def test_package_manager_failure_does_not_install_a_second_copy(tmp_path: Path) -> None:
    """A Homebrew failure remains a failure rather than falling through to pip."""
    result = subprocess.run(
        [
            BASH,
            "-c",
            'brew() { return 42; }; pip() { echo unexpected-pip; }; source "$1"',
            "test",
            str(ROOT / "scripts/install.sh"),
        ],
        env={"HOME": str(tmp_path), "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 42
    assert "unexpected-pip" not in result.stdout.splitlines()
    assert "APM installed successfully!" not in result.stdout.splitlines()


def test_no_package_manager_found_is_a_hard_failure(tmp_path: Path) -> None:
    """A missing package manager must not be reported as a successful install."""
    result = subprocess.run(
        [BASH, str(ROOT / "scripts/install.sh")],
        env={"HOME": str(tmp_path), "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 1
    assert (
        "Error: No supported package manager found (brew, uv, or pip)."
        in result.stdout.splitlines()
    )
    assert "APM installed successfully!" not in result.stdout.splitlines()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_standalone_failure_recommends_homebrew_core(tmp_path: Path, platform: str) -> None:
    """Run the actual binary-failure branch, with no downloader or installer."""
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    begin = list(re.finditer(r"^# INSTALL_BINARY_CHECK_BEGIN$", source, re.MULTILINE))
    end = list(re.finditer(r"^# INSTALL_BINARY_CHECK_END$", source, re.MULTILINE))
    assert len(begin) == len(end) == 1, "Expected exactly one pair of binary-check sentinels"
    assert begin[0].end() < end[0].start(), "Binary-check sentinels must be ordered"
    failure_branch = source[begin[0].end() : end[0].start()]
    binary = tmp_path / "apm"
    binary.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    binary.chmod(0o755)
    prelude = (
        "set -e\n"
        "apm_echo() { printf '%s\\n' \"$*\"; }\n"
        "try_pip_installation() { return 1; }\n"
        "print_pip_recovery_guidance() { echo selected-interpreter-recovery; }\n"
        "grep() { return 1; }\n"
    )
    result = subprocess.run(
        [BASH, "-c", prelude + failure_branch],
        env={
            "HOME": str(tmp_path),
            "PATH": str(tmp_path),
            "TMP_DIR": str(tmp_path),
            "EXTRACTED_DIR": ".",
            "BINARY_NAME": "apm",
            "PLATFORM": platform,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 1
    assert (
        "1. Homebrew (macOS/Linux): brew install apm (no tap needed)" in result.stdout.splitlines()
    )
    assert result.stdout.splitlines().count("selected-interpreter-recovery") == 1
