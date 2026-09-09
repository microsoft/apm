"""Contracts for the opt-in Windows installer proof lane."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import load_workflow, workflow_job, workflow_step

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci-windows-installer-proof.yml"
PROOF_SCRIPT = ROOT / "scripts/windows/run-installer-proof.ps1"


def _workflow() -> dict:
    """Load the proof workflow."""
    return load_workflow(WORKFLOW)


def _script_text() -> str:
    """Read the proof script as text for static safety contracts."""
    return PROOF_SCRIPT.read_text(encoding="ascii")


def _assert_workflow(workflow: dict) -> None:
    """Require a read-only, opt-in diagnostic that builds one unsigned candidate."""
    assert workflow["on"] == {"pull_request": {"types": ["labeled"]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["env"]["BASELINE_VERSION"] == "v0.28.0"
    assert set(workflow["jobs"]) == {"windows-installer-proof"}

    job = workflow_job(workflow, "windows-installer-proof")
    assert job["if"] == "github.event.label.name == 'ci-windows-installer-proof'"
    assert job["runs-on"] == "windows-latest"
    assert job["timeout-minutes"] == 45
    assert "secrets" not in job
    assert "environment" not in job
    assert not job.get("continue-on-error", False)
    for step in job["steps"]:
        assert "secrets." not in str(step)
        assert "action-gh-release" not in step.get("uses", "")
        assert "deploy-pages" not in step.get("uses", "")
        assert "WINDOWS_CERT_PASSWORD" not in str(step)
        assert not step.get("continue-on-error", False)

    fresh = workflow_step(job, "Require fresh proof attempt")
    assert "GITHUB_RUN_ATTEMPT -ne '1'" in fresh["run"]

    checkout = job["steps"][1]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["persist-credentials"] is False

    build = workflow_step(job, "Build unsigned Windows candidate")
    assert build["env"] == {"WINDOWS_CERT_PFX": ""}
    assert "scripts/windows/build-binary.ps1" in build["run"]

    package = workflow_step(job, "Package exact Windows candidate archive")
    assert package["env"] == {
        "CANDIDATE_SHA": "${{ github.sha }}",
        "BINARY_NAME": "apm-windows-x86_64",
    }
    assert "scripts/package_release.py pack" in package["run"]

    candidate = workflow_step(job, "Upload standalone proof candidate archive")
    assert candidate["uses"].startswith("actions/upload-artifact@")
    assert candidate["with"]["name"] == (
        "windows-installer-proof-candidate-${{ github.run_attempt }}"
    )
    assert candidate["with"]["path"] == (
        "release-assets/apm-windows-x86_64.zip\n"
        "release-assets/apm-windows-x86_64.zip.sha256\n"
        "release-assets/apm-windows-x86_64.json\n"
    )
    assert candidate["with"]["if-no-files-found"] == "error"
    assert candidate["with"]["compression-level"] == 0
    assert candidate["with"]["retention-days"] == 30
    assert "qualified" not in candidate["with"]["name"]
    assert "release-evidence" not in candidate["with"]["name"]

    baseline = workflow_step(job, "Download historical upgrade source once")
    assert baseline["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "gh release download $env:BASELINE_VERSION" in baseline["run"]
    assert "--repo microsoft/apm" in baseline["run"]

    proof = workflow_step(job, "Diagnose and prove Windows installer root behavior")
    assert "scripts/windows/run-installer-proof.ps1" in proof["run"]
    assert "-CandidateArchive release-assets/apm-windows-x86_64.zip" in proof["run"]
    assert "-CandidateMetadata release-assets/apm-windows-x86_64.json" in proof["run"]
    assert "-BaselineArchive installer-baseline/apm-windows-x86_64.zip" in proof["run"]
    assert "-BaselineChecksum installer-baseline/apm-windows-x86_64.zip.sha256" in proof["run"]

    upload = workflow_step(job, "Upload Windows installer proof diagnostics")
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == "windows-installer-proof-${{ github.run_attempt }}"
    assert upload["with"]["path"] == "windows-installer-proof/"
    assert upload["with"]["if-no-files-found"] == "error"


def test_windows_installer_proof_is_read_only_and_opt_in() -> None:
    """The focused lane must not publish, sign, or run on every PR update."""
    _assert_workflow(_workflow())


@pytest.mark.parametrize(
    "fault",
    ["secret", "write-token", "eager-trigger", "signed", "waived-failure", "missing-upload"],
)
def test_windows_installer_proof_rejects_scope_creep(fault: str) -> None:
    """Guard against accidentally turning the diagnostic into release machinery."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, "windows-installer-proof")
    if fault == "secret":
        job["secrets"] = "inherit"
    elif fault == "write-token":
        workflow["permissions"]["contents"] = "write"
    elif fault == "eager-trigger":
        workflow["on"]["pull_request"]["types"].append("synchronize")
    elif fault == "signed":
        workflow_step(job, "Build unsigned Windows candidate")["env"] = {
            "WINDOWS_CERT_PFX": "${{ secrets.WINDOWS_CERT_PFX }}"
        }
    elif fault == "waived-failure":
        job["continue-on-error"] = True
    else:
        workflow_step(job, "Upload Windows installer proof diagnostics")["with"][
            "if-no-files-found"
        ] = "ignore"
    with pytest.raises(AssertionError):
        _assert_workflow(workflow)


def test_windows_installer_proof_script_runs_same_archive_through_both_roots() -> None:
    """The script must distinguish path roots without changing installer assertions."""
    text = _script_text()
    assert "tests/integration/test_windows_installer_launchers.py" in text
    assert text.count('"APM_CANDIDATE_ARCHIVE" = $CandidateArchive') == 1
    assert 'Invoke-InstallerPytest -Name "long-root"' in text
    assert 'Invoke-InstallerPytest -Name "short-root"' in text
    assert '"wp-" + [System.Guid]::NewGuid().ToString("N").Substring(0, 5)' in text
    assert 'Join-Path $resolvedTemp.stdout.Trim() ("pytest-of-" + $env:USERNAME)' in text
    assert 'Join-Path $env:RUNNER_TEMP "apm-windows-installer"' in text
    assert 'throw "RUNNER_TEMP is required; this proof is CI-only"' in text
    assert 'throw "TEMP is required to reproduce the Windows pytest default root"' in text
    assert "Refusing to reuse preexisting proof basetemp" in text
    assert "Remove-OwnedBaseTemp -Path $longBaseTemp" in text
    assert "Remove-OwnedBaseTemp -Path $shortBaseTemp" in text
    assert "--basetemp" in text
    assert "if (Test-Path -LiteralPath $BaseTemp)" in text
    assert "APM Install Test & Edge " in text
    assert "if ($shortResult.timed_out -or $shortResult.exit_code -ne 0)" in text
    assert "exit $shortResult.exit_code" in text
    assert '"APM_E2E_TESTS" = "1"' in text
    assert "$result.junit = Read-InstallerOutcome" in text


def test_windows_installer_proof_records_charset_paths_and_ps5_stderr_confound() -> None:
    """Diagnostics must capture the observed package and PowerShell behavior."""
    text = _script_text()
    assert "archive-path-inventory.json" in text
    assert "$allEntries = @($zip.Entries)" in text
    assert "member_count = $allEntries.Count" in text
    assert "file_count = $files.Count" in text
    assert "charset_normalizer|chardet" in text
    assert '".pyd"' in text
    assert "temp_length" in text
    assert "stage_length" in text
    assert "final_length" in text
    assert "powershell_host" in text
    assert "powershell.exe" in text
    assert "deterministic-native-stderr" in text
    assert "exit23-stderr" in text
    assert "Broken direct exit23 native-stderr control" in text
    assert "$caseNames.Count -ne 2" in text
    assert '-not $case.threw -and $case.name -eq "exit0-stderr"' in text
    assert '-not $case.threw -and $case.name -eq "exit23-stderr"' in text
    assert "Expected Windows PowerShell 5.1" in text
    assert "native-stderr-behavior.json" in text
    assert 'Invoke-DirectCandidateProbe -Name "long-root"' in text
    assert 'Invoke-DirectCandidateProbe -Name "short-root"' in text
    assert '"direct-$Name.json"' in text


def test_windows_installer_proof_process_capture_selftest(tmp_path: Path) -> None:
    """Exercise timeout, stderr, and nonzero capture through the real PowerShell helper."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    out_dir = tmp_path / "capture"
    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(PROOF_SCRIPT),
            "-SelfTestProcessCapture",
            "-PythonExecutable",
            sys.executable,
            "-OutputDir",
            str(out_dir),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads((out_dir / "process-capture-selftest.json").read_text())
    assert payload["exit0"]["exit_code"] == 0
    assert "selftest-exit0-stderr" in payload["exit0"]["stderr"]
    assert payload["exit23"]["exit_code"] == 23
    assert "selftest-exit23-stderr" in payload["exit23"]["stderr"]
    assert payload["timeout"]["timed_out"] is True


@pytest.mark.parametrize(
    ("cases", "exit_code", "accepted"),
    [
        ('<testcase name="installer"/>', 0, True),
        ('<testcase name="installer"><skipped/></testcase>', 0, False),
        ("", 0, False),
        ('<testcase name="installer"><failure/></testcase>', 0, False),
        ('<testcase name="installer"><failure/></testcase>', 1, True),
    ],
)
def test_installer_outcome_requires_executed_cases(
    tmp_path: Path, cases: str, exit_code: int, accepted: bool
) -> None:
    """A green pytest exit with skipped assertions is not an installer proof."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    report = tmp_path / "junit.xml"
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(PROOF_SCRIPT),
            "-ValidateInstallerJUnit",
            str(report),
            "-InstallerExitCode",
            str(exit_code),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (completed.returncode == 0) is accepted, completed.stdout + completed.stderr
    if accepted:
        assert json.loads(completed.stdout)["skipped"] == 0


def test_installer_diagnostics_are_persisted_before_junit_validation() -> None:
    """The uploadable raw result and visible summary precede any JUnit rejection."""
    body = _script_text().split("function Invoke-InstallerPytest {", 1)[1]
    body = body.split("function Invoke-ProcessCaptureSelfTest {", 1)[0]
    validation = body.index("$result.junit = Read-InstallerOutcome")
    for diagnostic in (
        '"$Name-stdout.log"',
        '"$Name-stderr.log"',
        'Write-JsonFile -Value $result -Path (Join-Path $OutDir "$Name-result.json")',
        'Write-Host "[$Name] exit $($result.exit_code) timed_out=$($result.timed_out)"',
    ):
        assert body.index(diagnostic) < validation


@pytest.mark.windows_compat
@pytest.mark.parametrize(
    ("junit", "exit_code", "timed_out"),
    [
        (None, 124, True),
        ("<testsuites><broken", 23, False),
        ("<testsuites><testsuite/></testsuites>", 0, False),
        (
            '<testsuites><testsuite><testcase name="installer">'
            "<skipped/></testcase></testsuite></testsuites>",
            0,
            False,
        ),
        (
            '<testsuites><testsuite><testcase name="installer">'
            "<failure/></testcase></testsuite></testsuites>",
            0,
            False,
        ),
    ],
    ids=["missing-after-timeout", "malformed", "empty", "skipped", "false-success"],
)
def test_installer_rejected_junit_retains_process_diagnostics(
    tmp_path: Path, junit: str | None, exit_code: int, timed_out: bool
) -> None:
    """Execute the real persistence/validation path with a captured-process stub."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    out_dir = tmp_path / "diagnostics"
    out_dir.mkdir()
    if junit is not None:
        (out_dir / "junit-fixture.xml").write_text(junit, encoding="ascii")
    driver = tmp_path / "invoke-installer.ps1"
    driver.write_text(
        r"""
param([string]$SourceScript, [string]$OutDir, [int]$FixtureExitCode, [string]$TimedOut)
$ErrorActionPreference = "Stop"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $SourceScript, [ref]$tokens, [ref]$errors
)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @("Write-JsonFile", "Read-InstallerOutcome", "Invoke-InstallerPytest")) {
    $definition = $ast.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true)
    if (-not $definition) { throw "Missing function: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
function Invoke-CapturedProcess {
    param($FilePath, $Arguments, $Name, $TimeoutSeconds, $Environment)
    return [ordered]@{
        name = $Name
        exit_code = $FixtureExitCode
        timed_out = $TimedOut -eq "true"
        stdout = "fixture captured stdout"
        stderr = "fixture captured stderr"
        timeout_seconds = $TimeoutSeconds
    }
}
Invoke-InstallerPytest -Name "fixture" -OutDir $OutDir `
    -BaseTemp (Join-Path $OutDir "fresh-basetemp") -InstallerEnvironment @{} |
    ConvertTo-Json -Depth 8
""",
        encoding="ascii",
    )
    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(driver),
            "-SourceScript",
            str(PROOF_SCRIPT),
            "-OutDir",
            str(out_dir),
            "-FixtureExitCode",
            str(exit_code),
            "-TimedOut",
            str(timed_out).lower(),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode != 0, completed.stdout + completed.stderr
    record = json.loads((out_dir / "fixture-result.json").read_text(encoding="utf-8-sig"))
    assert record["exit_code"] == exit_code
    assert record["timed_out"] is timed_out
    assert record["timeout_seconds"] == 900
    assert record["stdout"] == "fixture captured stdout"
    assert record["stderr"] == "fixture captured stderr"
    assert record["junit_validation_error"]
    assert "junit" not in record
    for stream in ("stdout", "stderr"):
        log = (out_dir / f"fixture-{stream}.log").read_text(encoding="utf-8-sig")
        assert log.strip() == f"fixture captured {stream}"
        assert f"[fixture {stream}] fixture captured {stream}" in completed.stdout
    assert f"[fixture] exit {exit_code} timed_out={timed_out}" in completed.stdout
