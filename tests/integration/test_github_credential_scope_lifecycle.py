"""GitHub-host credentials follow the resolver's org scope.

Org policy discovery on GitHub uses the same per-org credential selection as
dependency installs: ``GITHUB_APM_PAT_<ORG>`` reads ``<org>/.github``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
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
ORG = "contoso-gh"
ORG_PAT = "github-org-scoped-sentinel-a41f"
GLOBAL_PAT = "github-global-sentinel-0b7e"
_POLICY = (
    "name: contoso-gh\nversion: '1.0'\nenforcement: warn\n"
    "dependencies:\n  deny: [untrusted/package]\n"
)


def _text(result: CommandResult) -> str:
    return f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"


def _isolated(tmp_path: Path) -> IsolatedApmEnvironment:
    return IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))


def _child_env(isolated: IsolatedApmEnvironment, proxy, extra: dict[str, str]) -> dict[str, str]:
    env = proxy.child_environment(isolated.subprocess_env())
    env["APM_NON_INTERACTIVE"] = "1"
    env["PATH"] = os.pathsep.join(p for p in ("/usr/bin", "/bin") if Path(p).is_dir())
    env.update(extra)
    return env


def _github_policy_api(accepted: str):
    def handle(request: ScriptedRequest) -> ScriptedResponse:
        authorization = request.header("Authorization") or ""
        if request.path == f"/repos/{ORG}/.github/contents/apm-policy.yml":
            if authorization.split(" ", 1)[-1] != accepted:
                return ScriptedResponse(
                    404, {"Content-Type": "application/json"}, b'{"message":"Not Found"}'
                )
            payload = {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(_POLICY.encode()).decode(),
            }
            return ScriptedResponse(
                200, {"Content-Type": "application/json"}, json.dumps(payload).encode()
            )
        return ScriptedResponse(
            404, {"Content-Type": "application/json"}, b'{"message":"Not Found"}'
        )

    return handle


def test_github_policy_discovery_uses_org_scoped_credential(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``policy status`` finds a private ``<org>/.github`` policy with ``GITHUB_APM_PAT_<ORG>``."""
    isolated = _isolated(tmp_path)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    git_env = isolated.subprocess_env()
    for args in (
        ("git", "init", "--quiet"),
        ("git", "remote", "add", "origin", f"https://github.com/{ORG}/service.git"),
    ):
        subprocess.run(args, cwd=project, env=git_env, check=True, capture_output=True, timeout=30)
    handlers = {API: _github_policy_api(ORG_PAT), RAW: _github_policy_api(ORG_PAT)}
    with local_scripted_https_proxy(isolated.root / "tls", handlers) as proxy:
        env = _child_env(
            isolated,
            proxy,
            {"GITHUB_APM_PAT_CONTOSO_GH": ORG_PAT, "GITHUB_APM_PAT": GLOBAL_PAT},
        )
        result = runner.run(
            ("policy", "status", "--no-cache", "--json"),
            cwd=project,
            env=env,
            scenario_id="gh-policy",
        )
        presented = sorted({o.header("Authorization") or "-" for o in proxy.requests_to(API)})
    assert result.returncode == 0, _text(result)
    report = json.loads(result.stdout[result.stdout.find("{") : result.stdout.rfind("}") + 1])
    assert report["outcome"] == "found", (report, presented)
    assert GLOBAL_PAT not in " ".join(presented), presented
