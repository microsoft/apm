"""Function-level checks for the Windows release-validation prerequisites."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]

ROOT = Path(__file__).resolve().parents[2]
PWSH = shutil.which("pwsh")

_TOKEN_ENV_NAMES = {
    "APM_RUN_INFERENCE_TESTS",
    "GH_TOKEN",
    "GITHUB_API_TOKEN",
    "GITHUB_APM_PAT",
    "GITHUB_CLI_PAT",
    "GITHUB_COPILOT_PAT",
    "GITHUB_MODELS_KEY",
    "GITHUB_TOKEN",
}


def _clean_env(overrides: dict[str, str]) -> dict[str, str]:
    """Keep process basics, but never inherit ambient credentials into the fixture."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _TOKEN_ENV_NAMES and not key.startswith("GITHUB_APM_PAT_")
    }
    env.update(overrides)
    return env


def _run_test_prerequisite(
    overrides: dict[str, str], *, public_api_only: bool = False
) -> tuple[dict[str, object], str, str]:
    command = r"""
$ErrorActionPreference = "Stop"

. (Join-Path $PWD "scripts/windows/github-token-helper.ps1")

$script:Messages = [System.Collections.Generic.List[string]]::new()
function Write-Info { param([string]$Message) $script:Messages.Add("INFO:$Message") }
function Write-Success { param([string]$Message) $script:Messages.Add("SUCCESS:$Message") }
function Write-ErrorText { param([string]$Message) $script:Messages.Add("ERROR:$Message") }
function Write-TestHeader { param([string]$Message) $script:Messages.Add("HEADER:$Message") }

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PWD "scripts/windows/test-release-validation.ps1"),
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count) { throw ($errors | Out-String) }

$function = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Test-Prerequisite"
}, $true)
if (-not $function) { throw "Test-Prerequisite function not found" }

. ([scriptblock]::Create($function.Extent.Text))

$expectedEnvironment = @{}
foreach ($name in @("GITHUB_API_TOKEN", "GITHUB_APM_PAT", "GITHUB_MODELS_KEY", "GITHUB_TOKEN")) {
    $expectedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

function Test-EnvPreserved {
    param([string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ($null -eq $value) { return $null }

    return [string]::Equals($value, $expectedEnvironment[$Name], [StringComparison]::Ordinal)
}

$result = Test-Prerequisite
[pscustomobject]@{
    Result = [bool]$result
    EnvPreserved = [pscustomobject]@{
        GITHUB_API_TOKEN = Test-EnvPreserved "GITHUB_API_TOKEN"
        GITHUB_APM_PAT = Test-EnvPreserved "GITHUB_APM_PAT"
        GITHUB_MODELS_KEY = Test-EnvPreserved "GITHUB_MODELS_KEY"
        GITHUB_TOKEN = Test-EnvPreserved "GITHUB_TOKEN"
    }
    Messages = @($script:Messages)
} | ConvertTo-Json -Compress -Depth 5
"""

    command = f"$PublicApiOnly = ${str(public_api_only).lower()}\n" + command
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        env=_clean_env(overrides),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    json_lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert json_lines, result.stdout + result.stderr
    return json.loads(json_lines[-1]), result.stdout, result.stderr


def _messages(record: dict[str, object]) -> list[str]:
    messages = record["Messages"]
    if isinstance(messages, str):
        return [messages]
    return list(messages)


def _assert_sentinels_not_printed(stdout: str, stderr: str, *sentinels: str) -> None:
    combined = stdout + stderr
    for sentinel in sentinels:
        assert sentinel not in combined


@pytest.mark.skipif(PWSH is None, reason="PowerShell parser is not installed")
@pytest.mark.parametrize("public_api_only", [False, True])
def test_public_release_api_token_satisfies_prerequisite_when_inference_is_off(
    public_api_only: bool,
) -> None:
    api_token = "readonly-api-token-sentinel"

    record, stdout, stderr = _run_test_prerequisite(
        {"GITHUB_API_TOKEN": api_token}, public_api_only=public_api_only
    )

    assert record["Result"] is True
    assert record["EnvPreserved"] == {
        "GITHUB_API_TOKEN": True,
        "GITHUB_APM_PAT": None,
        "GITHUB_MODELS_KEY": None,
        "GITHUB_TOKEN": None,
    }
    assert any("GITHUB_API_TOKEN is set" in message for message in _messages(record))
    _assert_sentinels_not_printed(stdout, stderr, api_token)


@pytest.mark.skipif(PWSH is None, reason="PowerShell parser is not installed")
@pytest.mark.parametrize("public_api_only", [False, True])
def test_missing_release_and_pat_tokens_rejects_prerequisite(public_api_only: bool) -> None:
    record, _stdout, _stderr = _run_test_prerequisite({}, public_api_only=public_api_only)

    assert record["Result"] is False
    assert any("GitHub token setup failed" in message for message in _messages(record))
    assert record["EnvPreserved"] == {
        "GITHUB_API_TOKEN": None,
        "GITHUB_APM_PAT": None,
        "GITHUB_MODELS_KEY": None,
        "GITHUB_TOKEN": None,
    }


@pytest.mark.skipif(PWSH is None, reason="PowerShell parser is not installed")
@pytest.mark.parametrize("public_api_only", [False, True])
def test_api_token_does_not_satisfy_inference_prerequisite_or_seed_pat_aliases(
    public_api_only: bool,
) -> None:
    api_token = "readonly-api-token-sentinel"

    record, stdout, stderr = _run_test_prerequisite(
        {"APM_RUN_INFERENCE_TESTS": "1", "GITHUB_API_TOKEN": api_token},
        public_api_only=public_api_only,
    )

    assert record["Result"] is False
    assert record["EnvPreserved"] == {
        "GITHUB_API_TOKEN": True,
        "GITHUB_APM_PAT": None,
        "GITHUB_MODELS_KEY": None,
        "GITHUB_TOKEN": None,
    }
    assert any("Inference tests require" in message for message in _messages(record))
    _assert_sentinels_not_printed(stdout, stderr, api_token)


@pytest.mark.skipif(PWSH is None, reason="PowerShell parser is not installed")
def test_existing_pat_and_models_tokens_are_preserved_and_not_logged() -> None:
    api_token = "readonly-api-token-sentinel"
    apm_pat = "apm-pat-token-sentinel"
    github_token = "models-token-sentinel"
    models_key = "models-key-sentinel"

    record, stdout, stderr = _run_test_prerequisite(
        {
            "APM_RUN_INFERENCE_TESTS": "1",
            "GITHUB_API_TOKEN": api_token,
            "GITHUB_APM_PAT": apm_pat,
            "GITHUB_MODELS_KEY": models_key,
            "GITHUB_TOKEN": github_token,
        }
    )

    assert record["Result"] is True
    assert record["EnvPreserved"] == {
        "GITHUB_API_TOKEN": True,
        "GITHUB_APM_PAT": True,
        "GITHUB_MODELS_KEY": True,
        "GITHUB_TOKEN": True,
    }
    messages = _messages(record)
    assert any("GITHUB_APM_PAT is set" in message for message in messages)
    assert any("GITHUB_TOKEN is set" in message for message in messages)
    _assert_sentinels_not_printed(stdout, stderr, api_token, apm_pat, github_token, models_key)


@pytest.mark.skipif(PWSH is None, reason="PowerShell parser is not installed")
@pytest.mark.parametrize(
    ("public_api_only", "fail_update", "expected_exit"),
    [(True, False, 0), (True, True, 1), (False, False, 0)],
)
def test_release_caller_runs_public_dependency_helper_without_api_token_aliases(
    tmp_path: Path, public_api_only: bool, fail_update: bool, expected_exit: int
) -> None:
    """Run the real caller and helper gates; stub only binary/network leaf checks."""
    scripts = ROOT / "scripts/windows"
    caller = (scripts / "test-release-validation.ps1").read_text(encoding="utf-8")
    entrypoint = "# Run main function\nMain\n"
    assert caller.endswith(entrypoint)
    # Inject hermetic leaves immediately before the real Main invocation. Keep
    # parameter binding, helper loading, prerequisites and both selection gates.
    leaves = r"""
function Test-BasicCommand { return $true }
function Test-RuntimeSetup { return $true }
function Test-HeroZeroConfig { return $true }
function Test-HeroGuardrailing { return $true }
function Test-RealDependencyInstallation {
    param([string]$TestDir, [string]$ApmBinary)
    $global:SelectedChecks.Add("installation")
    return $true
}
function Test-MultiDependencyScenario {
    param([string]$TestDir, [string]$ApmBinary)
    $global:SelectedChecks.Add("multi-dependency")
    return $true
}
function Test-DependencyUpdate {
    param([string]$TestDir, [string]$ApmBinary)
    $global:SelectedChecks.Add("update")
    return $env:FIXTURE_FAIL_UPDATE -ne "1"
}
function Test-DependencyCleanup {
    param([string]$TestDir, [string]$ApmBinary)
    $global:SelectedChecks.Add("cleanup")
    return $true
}
"""
    (tmp_path / "caller.ps1").write_text(
        caller.removesuffix(entrypoint) + leaves + entrypoint, encoding="utf-8"
    )
    for helper in ("github-token-helper.ps1", "test-dependency-integration.ps1"):
        shutil.copyfile(scripts / helper, tmp_path / helper)
    (tmp_path / "candidate.exe").touch()
    command = r"""
$ErrorActionPreference = "Stop"
$global:SelectedChecks = [System.Collections.Generic.List[string]]::new()
$expectedEnvironment = @{}
foreach ($name in @("GITHUB_API_TOKEN", "GITHUB_APM_PAT", "GITHUB_MODELS_KEY", "GITHUB_TOKEN")) {
    $expectedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
& (Join-Path $PWD "caller.ps1") -BinaryPath (Join-Path $PWD "candidate.exe") `
    -PublicApiOnly:($env:FIXTURE_PUBLIC_API_ONLY -eq "1")
$callerExit = $LASTEXITCODE
$preserved = @{}
foreach ($name in $expectedEnvironment.Keys) {
    $value = [Environment]::GetEnvironmentVariable($name, "Process")
    $preserved[$name] = if ($null -eq $value) { $null } else {
        [string]::Equals($value, $expectedEnvironment[$name], [StringComparison]::Ordinal)
    }
}
[pscustomobject]@{
    ExitCode = $callerExit
    SelectedChecks = @($global:SelectedChecks)
    EnvPreserved = $preserved
} | ConvertTo-Json -Compress -Depth 5
exit $callerExit
"""
    api_token = "caller-readonly-api-sentinel"
    pat = "caller-pat-sentinel"
    models_token = "caller-models-token-sentinel"
    models_key = "caller-models-key-sentinel"
    overrides = {
        "GITHUB_API_TOKEN": api_token,
        "TEMP": str(tmp_path),
        "FIXTURE_PUBLIC_API_ONLY": "1" if public_api_only else "0",
        "FIXTURE_FAIL_UPDATE": "1" if fail_update else "0",
    }
    if not public_api_only:
        overrides.update(
            GITHUB_APM_PAT=pat, GITHUB_TOKEN=models_token, GITHUB_MODELS_KEY=models_key
        )
    completed = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=tmp_path,
        env=_clean_env(overrides),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == expected_exit, completed.stdout + completed.stderr
    records = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    assert records, completed.stdout + completed.stderr
    record = json.loads(records[-1])
    assert record["ExitCode"] == expected_exit
    expected_checks = ["installation", "multi-dependency", "update"]
    if not fail_update:
        expected_checks.append("cleanup")
    assert record["SelectedChecks"] == expected_checks
    assert record["EnvPreserved"] == {
        "GITHUB_API_TOKEN": True,
        "GITHUB_APM_PAT": None if public_api_only else True,
        "GITHUB_MODELS_KEY": None if public_api_only else True,
        "GITHUB_TOKEN": None if public_api_only else True,
    }
    assert "Dependency integration tests will be included" in completed.stdout
    assert f"Results: {4 if fail_update else 5}/5 tests passed" in completed.stdout
    _assert_sentinels_not_printed(
        completed.stdout, completed.stderr, api_token, pat, models_token, models_key
    )
