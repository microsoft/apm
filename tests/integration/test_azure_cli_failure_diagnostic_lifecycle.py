"""Azure CLI token failures are reported with their real cause (#3753).

When ``az account get-access-token`` fails (for example AADSTS50076, MFA
required) while ``az account show`` still reports a tenant, the install
error must surface the Azure CLI's own reason and the ``az login`` remedy.
It must not claim Azure DevOps rejected a bearer token that was never
issued, must keep the phrase "bearer token" readable, and must not re-probe
the Azure CLI for every diagnostic line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.integration._ado_auth_kit import (
    INSTALL,
    AdoAuthScenario,
    ado_dependency_entry,
    describe,
    flat,
    manifest,
)
from tests.utils.local_github_https_fixture import openssl_executable

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX az shim"),
]

_TENANT = "72f988bf-0000-4000-8000-0000000000aa"
_AADSTS = "AADSTS50076"
_JWT_LIKE = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJsZWFrIn0.c2lnbmF0dXJlLXNlbnRpbmVs"

_AZ_SHIM = f"""
import json
import sys

args = sys.argv[1:]
with open(AZ_LOG, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:2] == ["account", "show"]:
    print({_TENANT!r})
    raise SystemExit(0)
if args[:2] == ["account", "get-access-token"]:
    sys.stderr.write(
        "ERROR: {_AADSTS}: Due to a configuration change made by your administrator, "
        "or because you moved to a new location, you must use multi-factor "
        "authentication to access '499b84ac-1321-427f-aa17-267ca6975798'. "
        "Trace ID: 00000000-0000-0000-0000-000000000000 token={_JWT_LIKE}\\n"
        "Run the command below to authenticate interactively:\\n"
        "az login --tenant {_TENANT} --scope 499b84ac-1321-427f-aa17-267ca6975798/.default\\n"
    )
    raise SystemExit(1)
raise SystemExit(2)
"""


def _az_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def test_azure_cli_token_failure_reports_aadsts_cause_not_a_rejected_token(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper(respond=False)
    az_log = scenario.install_az_shim(_AZ_SHIM)
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        result = scenario.run(INSTALL, cwd=project, env=scenario.env(proxy), step="az-mfa")
    text = flat(result)
    calls = _az_calls(az_log)
    assert result.returncode != 0, describe(result)
    assert any(call[:2] == ["account", "get-access-token"] for call in calls), calls
    assert _AADSTS in text, describe(result)
    assert "rejected it (HTTP 401)" not in text, describe(result)
    assert "returned a bearer token" not in text, describe(result)
    assert "az login" in text, describe(result)
    assert _JWT_LIKE not in text and "c2lnbmF0dXJlLXNlbnRpbmVs" not in text
    assert sum(1 for call in calls if call[:2] == ["account", "show"]) <= 1, calls


def test_azure_cli_failure_verbose_keeps_bearer_token_phrase_readable(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The redactor hides token values, not the words describing the failed step."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper(respond=False)
    scenario.install_az_shim(_AZ_SHIM)
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        result = scenario.run(
            (*INSTALL, "--verbose"), cwd=project, env=scenario.env(proxy), step="az-verbose"
        )
    text = flat(result)
    assert result.returncode != 0, describe(result)
    assert "ADO [REDACTED] acquisition" not in text, describe(result)
    assert _JWT_LIKE not in text
    assert _AADSTS in text, describe(result)
