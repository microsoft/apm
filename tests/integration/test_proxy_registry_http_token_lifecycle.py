"""The proxy registry token travels over http only to private hosts or with an opt-in (#4314).

Promise (docs/enterprise/registry-proxy.md): over ``http://``, the installed CLI
sends ``PROXY_REGISTRY_TOKEN`` only to a loopback or RFC 1918 private host, or
when ``PROXY_REGISTRY_ALLOW_HTTP=1`` authorizes it; otherwise it sends no token
and prints a ``[!]`` line that names the URL and the opt-in. Requests reach a
recording proxy, so each origin's headers are observed directly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_header_capture_proxy import local_header_capture_proxy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
]

PUBLIC_HOST = "art.example.test"
PRIVATE_HOST = "10.20.30.40"
SECRET = "proxy-http-token-sentinel-4314"


def _install(
    tmp_path: Path, binary: Path, host: str, *, opt_in: bool
) -> tuple[CommandResult, set[str], int]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    (project / ".claude").mkdir(parents=True)
    (project / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n    - octo/repo\n",
        encoding="utf-8",
    )
    runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)
    with local_header_capture_proxy(tmp_path / "tls", (PUBLIC_HOST, PRIVATE_HOST)) as proxy:
        env = proxy.child_environment(isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        env["PROXY_REGISTRY_URL"] = f"http://{host}/artifactory/github"
        env["PROXY_REGISTRY_TOKEN"] = SECRET
        env["PROXY_REGISTRY_ONLY"] = "1"
        if opt_in:
            env["PROXY_REGISTRY_ALLOW_HTTP"] = "1"
        result = runner.run(
            ("install", "--no-policy", "--parallel-downloads", "0"),
            cwd=project,
            env=env,
            scenario_id=f"proxy-http-{host}-{opt_in}",
        )
        reached = sum(1 for o in proxy.observations if o.host == host)
        return result, proxy.hosts_receiving(SECRET), reached


def test_public_http_proxy_gets_no_token_and_the_cli_says_why(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    result, receiving, reached = _install(tmp_path, apm_binary_path, PUBLIC_HOST, opt_in=False)
    output = result.stdout + result.stderr
    assert reached, output
    assert receiving == set(), output
    assert f"http://{PUBLIC_HOST}/artifactory/github" in output, output
    assert "PROXY_REGISTRY_ALLOW_HTTP=1" in output, output
    assert SECRET not in output


@pytest.mark.parametrize(
    ("host", "opt_in"), [(PRIVATE_HOST, False), (PUBLIC_HOST, True)], ids=["private", "opt-in"]
)
def test_private_or_opted_in_http_proxy_gets_the_token(
    tmp_path: Path, apm_binary_path: Path, host: str, opt_in: bool
) -> None:
    result, receiving, reached = _install(tmp_path, apm_binary_path, host, opt_in=opt_in)
    output = result.stdout + result.stderr
    assert reached, output
    assert receiving == {host}, output
    assert "PROXY_REGISTRY_ALLOW_HTTP=1" not in output, output
