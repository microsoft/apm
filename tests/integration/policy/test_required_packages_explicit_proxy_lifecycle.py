"""Lifecycle: ``dependencies.require`` on an explicit proxy-URL dependency (#4037).

A consumer declares ``<proxy-host>/artifactory/github/acme/tools`` with no
``PROXY_REGISTRY_URL``. The lockfile writer keys that row by the proxy host
(``<proxy-host>/acme/tools``) while the reference key stays bare, so the
required-package checks must read the row the writer created. The org policy
(``enforcement: block``) is discovered from the project's ``origin`` remote;
the archive and the policy are served by a loopback HTTPS proxy, so nothing
leaves the machine. Controls: an undeclared package and another proxy host
fail both presence checks; an env-proxy install (``PROXY_REGISTRY_URL``,
shorthand dependency, hostless entry) keeps passing.
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import (
    ScriptedRequest,
    ScriptedResponse,
    local_scripted_https_proxy,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
    pytest.mark.e2e,
    pytest.mark.promise("req-pl-018"),
]

PROXY_HOST = "art.example.test"
PREFIX = "artifactory/github"
PACKAGE = "acme/tools"
EXPLICIT = f"{PROXY_HOST}/{PREFIX}/{PACKAGE}"
QUALIFIED = f"{PROXY_HOST}/{PACKAGE}"
POLICY_PATH = "/repos/contoso/.github/contents/apm-policy.yml"
PRESENCE = ("required-packages", "required-packages-deployed")


def _archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("tools-v1.0.0/", "")
        package.writestr("tools-v1.0.0/apm.yml", "name: tools\nversion: 1.0.0\n")
        package.writestr(
            "tools-v1.0.0/.apm/instructions/guide.instructions.md",
            "---\napplyTo: '**'\ndescription: proxy fixture\n---\n# Proxy package\n",
        )
    return buffer.getvalue()


def _policy(entry: str) -> dict:
    return {
        "name": "require-explicit-proxy",
        "version": "1.0.0",
        "enforcement": "block",
        "dependencies": {"require": [entry]},
    }


@dataclass
class _World:
    project: Path
    runner: ApmLifecycleRunner
    env: dict[str, str]

    def run(self, *args: str) -> CommandResult:
        return self.runner.run(args, scenario_id="-".join(args[:2]), cwd=self.project, env=self.env)

    def audit(self, entry: str) -> tuple[CommandResult, dict[str, dict]]:
        policy = self.project.parent / "audit-policy.yml"
        dump_yaml(_policy(entry), policy)
        result = self.run(
            "audit", "--ci", "--no-fail-fast", "--policy", str(policy), "--format", "json"
        )
        checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
        return result, checks


@pytest.fixture
def world(tmp_path: Path, apm_binary_path: Path, request: pytest.FixtureRequest):
    env_proxy = getattr(request, "param", False)
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env={})
    project = isolated.work_root / "consumer"
    project.mkdir()
    dependency = f"{PACKAGE}#v1.0.0" if env_proxy else f"{EXPLICIT}#v1.0.0"
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [dependency]},
        },
        project / "apm.yml",
    )
    git_env = isolated.subprocess_env()
    for args in (
        ("init", "-q"),
        ("remote", "add", "origin", "https://github.com/contoso/consumer.git"),
    ):
        subprocess.run(("git", *args), cwd=project, env=git_env, check=True)

    archive = _archive()
    org_policy = json.dumps(
        {
            "encoding": "base64",
            "content": base64.b64encode(
                json.dumps(_policy(PACKAGE if env_proxy else QUALIFIED)).encode()
            ).decode(),
        }
    ).encode()

    def proxy(request: ScriptedRequest) -> ScriptedResponse:
        if request.method == "GET" and request.path.startswith(f"/{PREFIX}/{PACKAGE}/"):
            return ScriptedResponse(200, {"Content-Type": "application/zip"}, archive)
        return ScriptedResponse(404)

    def github_api(request: ScriptedRequest) -> ScriptedResponse:
        if request.path == POLICY_PATH:
            return ScriptedResponse(200, {"Content-Type": "application/json"}, org_policy)
        return ScriptedResponse(404)

    handlers = {PROXY_HOST: proxy, "api.github.com": github_api}
    with local_scripted_https_proxy(isolated.root / "tls", handlers) as scripted:
        env = scripted.child_environment(isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        if env_proxy:
            env["PROXY_REGISTRY_URL"] = f"https://{PROXY_HOST}/{PREFIX}"
            env["PROXY_REGISTRY_ONLY"] = "1"
        current = _World(project, ApmLifecycleRunner((str(apm_binary_path),)), env)
        installed = current.run("install", "--target", "copilot")
        assert installed.returncode == 0, installed.stdout + installed.stderr
        assert scripted.requests_to("api.github.com"), "org policy was not discovered"
        assert scripted.requests_to(PROXY_HOST), "package was not fetched through the proxy"
        yield current


def test_explicit_proxy_require_passes_install_and_audit(world: _World) -> None:
    rows = (load_yaml(world.project / "apm.lock.yaml") or {}).get("dependencies", [])
    assert [(row["repo_url"], row.get("host"), row.get("registry_prefix")) for row in rows] == [
        (PACKAGE, PROXY_HOST, None)
    ]

    _, checks = world.audit(QUALIFIED)

    for name in PRESENCE:
        assert checks[name]["passed"], checks[name]

    reinstalled = world.run("install", "--target", "copilot")
    assert reinstalled.returncode == 0, reinstalled.stdout + reinstalled.stderr


def test_other_proxy_host_fails_both_presence_checks(world: _World) -> None:
    entry = f"other-proxy.example.test/{PACKAGE}"

    result, checks = world.audit(entry)

    assert result.returncode != 0
    for name in PRESENCE:
        assert not checks[name]["passed"], checks[name]
        assert checks[name]["details"] == [entry]


def test_undeclared_package_fails_both_presence_checks(world: _World) -> None:
    manifest = load_yaml(world.project / "apm.yml")
    manifest["dependencies"] = {"apm": []}
    dump_yaml(manifest, world.project / "apm.yml")

    result, checks = world.audit(QUALIFIED)

    assert result.returncode != 0
    for name in PRESENCE:
        assert not checks[name]["passed"], checks[name]
        assert checks[name]["details"] == [QUALIFIED]


@pytest.mark.parametrize("world", [True], indirect=True, ids=["env-proxy"])
def test_env_proxy_hostless_require_keeps_passing(world: _World) -> None:
    rows = (load_yaml(world.project / "apm.lock.yaml") or {}).get("dependencies", [])
    assert [(row["repo_url"], row.get("registry_prefix")) for row in rows] == [(PACKAGE, PREFIX)]

    result, checks = world.audit(PACKAGE)

    assert result.returncode == 0, result.stdout
    for name in PRESENCE:
        assert checks[name]["passed"], checks[name]
