"""Marketplace metadata enrichment keeps the GitHub credential on its origin.

``apm pack`` reads remote package metadata through the shared credential-scoped
HTTP owner: a redirect to another host is not followed with the GitHub
credential attached.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import (
    ScriptedRequest,
    ScriptedResponse,
    local_scripted_https_proxy,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
]

API = "api.github.com"
RAW = "raw.githubusercontent.com"
ELSEWHERE = "objects.elsewhere.example"
ORG = "contoso-gh"
GLOBAL_PAT = "github-global-sentinel-0b7e"
_SHA = "1111111111111111111111111111111111111111"


def _text(result: CommandResult) -> str:
    return f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"


def _marketplace_handler(record: list[str]):
    def handle(request: ScriptedRequest) -> ScriptedResponse:
        if request.header("Authorization"):
            record.append(request.host)
            return ScriptedResponse(302, {"Location": f"https://{ELSEWHERE}/blob{request.path}"})
        return ScriptedResponse(
            404, {"Content-Type": "application/json"}, b'{"message":"Not Found"}'
        )

    return handle


def _elsewhere(request: ScriptedRequest) -> ScriptedResponse:
    return ScriptedResponse(404)


def test_marketplace_metadata_credential_stays_on_github_origin(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A redirected metadata fetch never carries the GitHub credential to the redirect target."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    project = isolated.work_root / "market"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: market\ndescription: fixture\nversion: 1.0.0\ndependencies: {}\n"
        "marketplace:\n  owner:\n    name: Fixture\n  outputs: [claude]\n  packages:\n"
        f"    - name: private-pkg\n      source: {ORG}/private-pkg\n      ref: {_SHA}\n",
        encoding="utf-8",
    )
    (project / "apm.lock.yaml").write_text("dependencies: []\n", encoding="utf-8")
    authenticated: list[str] = []
    handlers = {
        API: _marketplace_handler(authenticated),
        RAW: _marketplace_handler(authenticated),
        ELSEWHERE: _elsewhere,
    }
    with local_scripted_https_proxy(isolated.root / "tls", handlers) as proxy:
        env = proxy.child_environment(isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        env["PATH"] = os.pathsep.join(p for p in ("/usr/bin", "/bin") if Path(p).is_dir())
        env["GITHUB_APM_PAT"] = GLOBAL_PAT
        result = runner.run(("pack", "--dry-run"), cwd=project, env=env, scenario_id="pack-meta")
        receiving = proxy.hosts_receiving(GLOBAL_PAT)
        reached = [o for o in proxy.observations if o.host == ELSEWHERE and o.method != "CONNECT"]
    assert authenticated, f"scenario never reached an authenticated metadata fetch\n{_text(result)}"
    assert receiving <= {API, RAW}, receiving
    assert all(o.header("Authorization") is None for o in reached), [o.path for o in reached]
