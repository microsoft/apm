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


def _run_test_prerequisite(overrides: dict[str, str]) -> tuple[dict[str, object], str, str]:
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
def test_public_release_api_token_satisfies_prerequisite_when_inference_is_off() -> None:
    api_token = "readonly-api-token-sentinel"

    record, stdout, stderr = _run_test_prerequisite({"GITHUB_API_TOKEN": api_token})

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
def test_missing_release_and_pat_tokens_rejects_prerequisite() -> None:
    record, _stdout, _stderr = _run_test_prerequisite({})

    assert record["Result"] is False
    assert any("GitHub token setup failed" in message for message in _messages(record))
    assert record["EnvPreserved"] == {
        "GITHUB_API_TOKEN": None,
        "GITHUB_APM_PAT": None,
        "GITHUB_MODELS_KEY": None,
        "GITHUB_TOKEN": None,
    }


@pytest.mark.skipif(PWSH is None, reason="PowerShell parser is not installed")
def test_api_token_does_not_satisfy_inference_prerequisite_or_seed_pat_aliases() -> None:
    api_token = "readonly-api-token-sentinel"

    record, stdout, stderr = _run_test_prerequisite(
        {"APM_RUN_INFERENCE_TESTS": "1", "GITHUB_API_TOKEN": api_token}
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
