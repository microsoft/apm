"""Automatic pip fallbacks must preserve foreign launchers in pip's user scheme."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.component, pytest.mark.windows_compat]


def _probe(path: Path) -> str:
    """Extract the exact standalone ownership probe, without its shell wrapper."""
    return (
        path.read_text(encoding="utf-8")
        .split("# APM_PIP_COMPANION_GUARD_BEGIN\n", 1)[1]
        .split("# APM_PIP_COMPANION_GUARD_END", 1)[0]
    )


def _fixture(tmp_path: Path, state: str, name: str = "apmx") -> tuple[dict[str, str], Path]:
    """Provide a private user scheme and optional, RECORD-owned companion."""
    env = {
        **os.environ,
        "PYTHONUSERBASE": str(tmp_path / "python user & base"),
        "PIP_CONFIG_FILE": os.devnull,
    }
    for key in ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT"):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from pip._internal.locations import get_scheme; "
            's=get_scheme("apm-cli", user=True); print(json.dumps([s.scripts,s.purelib]))',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    scripts, site = map(Path, json.loads(result.stdout))
    launcher = scripts / (name + ".exe" if os.name == "nt" else name)
    if state != "absent":
        scripts.mkdir(parents=True)
        launcher.write_bytes(b"original companion bytes")
    if state in ("owned", "modified", "wrong-project"):
        dist = site / "apm_cli-0.0.1.dist-info"
        dist.mkdir(parents=True)
        name = "foreign-project" if state == "wrong-project" else "apm-cli"
        (dist / "METADATA").write_text(f"Name: {name}\nVersion: 0.0.1\n", encoding="ascii")
        (dist / "entry_points.txt").write_text(
            "[console_scripts]\napm = apm_cli.cli:cli\napmx = apm_cli.apmx:main\n",
            encoding="ascii",
        )
        content = launcher.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        with (dist / "RECORD").open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(
                [os.path.relpath(launcher, site), f"sha256={digest}", len(content)]
            )
        if state == "modified":
            launcher.write_bytes(b"foreign replacement")
    return env, launcher


def test_standalone_pip_probes_cannot_drift() -> None:
    """Both downloaded installers carry the same platform-aware ownership policy."""
    assert _probe(ROOT / "install.sh") == _probe(ROOT / "install.ps1")


@pytest.mark.parametrize("state", ["foreign", "owned", "absent", "modified", "wrong-project"])
@pytest.mark.parametrize("launcher_name", ["apm", "apmx"])
def test_pip_guard_checks_actual_user_scheme_and_record(
    tmp_path: Path, state: str, launcher_name: str
) -> None:
    """An unrelated native bin directory cannot authorize pip's user-script writes."""
    env, launcher = _fixture(tmp_path, state, launcher_name)
    before = launcher.read_bytes() if launcher.exists() else None
    result = subprocess.run(
        [sys.executable, "-c", _probe(ROOT / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if state in ("owned", "absent") else 1), result.stderr
    if result.returncode == 0:
        assert Path(result.stdout.strip()) == launcher.parent
    else:
        assert f"Refusing to replace unrelated {launcher_name} launcher" in result.stderr
    assert (launcher.read_bytes() if launcher.exists() else None) == before


@pytest.mark.parametrize("setting", ["PIP_ROOT", "PIP_PREFIX", "PIP_TARGET"])
def test_redirected_pip_destinations_fail_closed(tmp_path: Path, setting: str) -> None:
    """Ambient pip destination overrides must not bypass the verified user scheme."""
    env, _ = _fixture(tmp_path, "absent")
    env[setting] = str(tmp_path / "redirected")
    result = subprocess.run(
        [sys.executable, "-c", _probe(ROOT / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "redirected pip destinations" in result.stderr
    assert not (tmp_path / "redirected").exists()


@pytest.mark.parametrize("installer", ["unix", "windows", "windows-legacy", "windows-5.1"])
@pytest.mark.parametrize("state", ["foreign", "owned", "absent"])
def test_automatic_fallback_checks_before_pip_install(
    tmp_path: Path, installer: str, state: str
) -> None:
    """Execute the production fallback; a foreign launcher must prevent pip writes."""
    if installer == "unix" and os.name == "nt":
        pytest.skip("POSIX shell caller is exercised on Unix")
    powershell = (
        shutil.which("powershell.exe") if installer == "windows-5.1" else shutil.which("pwsh")
    )
    if installer != "unix" and not powershell:
        pytest.skip("PowerShell is needed to execute the Windows fallback")
    env, launcher = _fixture(tmp_path, state)
    modules = tmp_path / "modules"
    fake_pip = modules / "pip"
    fake_pip.mkdir(parents=True)
    (fake_pip / "__init__.py").write_text(
        "from pkgutil import extend_path\nfrom importlib.metadata import version\n"
        '__path__ = extend_path(__path__, __name__)\n__version__ = version("pip")\n',
        encoding="ascii",
    )
    (fake_pip / "__main__.py").write_text(
        "import os,sys\nfrom pathlib import Path\n"
        'if sys.argv[1:] != ["--version"]:\n'
        '    Path(os.environ["PIP_TEST_LOG"]).write_text("\\n".join(sys.argv[1:]))\n',
        encoding="ascii",
    )
    log = tmp_path / "pip.log"
    env.update(
        PYTHONPATH=str(modules),
        PIP_TEST_LOG=str(log),
        APM_TEST_PYTHON=sys.executable,
        APM_TEST_INSTALLER=str(ROOT / "install.ps1"),
    )
    before = launcher.read_bytes() if launcher.exists() else None
    if installer == "unix":
        source = (ROOT / "install.sh").read_text(encoding="ascii")
        body = source.split("try_pip_installation() {", 1)[1].split("# Reject invalid requests", 1)[
            0
        ]
        script = (
            "apm_resolve_install_paths() { :; }\n"
            'check_python_requirements() { PYTHON_CMD="$APM_TEST_PYTHON"; }\n'
            "id() { echo 1000; }\n"
            "is_truthy() { return 1; }\n"
            "apm_echo() { :; }\n"
            "apm_print_path_guidance() { :; }\n"
            "apm_install_error() { printf '%s\\n' \"$1\" >&2; exit 1; }\n"
            "try_pip_installation() {" + body + "\ntry_pip_installation\n"
        )
        command = ["/bin/sh", "-c", script]
    else:
        script = r"""
$ErrorActionPreference = "Stop"
if ($env:APM_TEST_LEGACY_ARGUMENTS -eq "1") {
    $PSNativeCommandArgumentPassing = "Legacy"
}
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:APM_TEST_INSTALLER, [ref]$tokens, [ref]$errors)
$definition = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq "Install-ViaPip"
}, $true)[0]
. ([scriptblock]::Create($definition.Extent.Text))
function Test-PythonRequirement { return $env:APM_TEST_PYTHON }
function Write-Info { param($Text) }
function Write-Success { param($Text) }
function Write-WarningText { param($Text) }
function Write-ErrorText { param($Text) Write-Host $Text }
function Get-PipIndexArgs { return @() }
function Get-Command { return $null }
if (Install-ViaPip) { exit 0 } else { exit 1 }
"""
        env["APM_TEST_LEGACY_ARGUMENTS"] = "1" if installer == "windows-legacy" else ""
        script_path = tmp_path / "pip-fallback.ps1"
        script_path.write_text(script, encoding="ascii")
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    allowed = state in ("owned", "absent")
    assert result.returncode == (0 if allowed else 1), result.stdout + result.stderr
    assert log.exists() == allowed
    if allowed:
        assert log.read_text().splitlines() == ["install", "--user", "apm-cli"]
    assert (launcher.read_bytes() if launcher.exists() else None) == before
