"""Locks written by APM 0.35.1 and 0.36.0 replay on the fixed build (#3752, #3726).

The static locks under ``tests/fixtures/upgrade_0361/t1/<version>/`` were
produced by the released CLIs (see that directory's README). Replaying them
must:

* reinstall an Azure DevOps dependency with ``install --frozen`` and
  ``update`` when only a Git credential helper holds the credential (the old
  builds wrote the lock with ``ADO_APM_PAT``), leaving the lock unchanged and
  free of credential material;
* reinstall a registry dependency from the recorded ``resolved_url`` when the
  registry now sits behind a same-origin redirecting gateway, without lock
  churn.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._ado_auth_kit import (
    HELPER_SECRET,
    HELPER_USER,
    INSTALL,
    AdoAuthScenario,
    describe,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import (
    ScriptedHttpsProxy,
    ScriptedRequest,
    ScriptedResponse,
    basic_authorization,
    local_scripted_https_proxy,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX credential helper launcher"),
]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upgrade_0361" / "t1"
VERSIONS = ("0.35.1", "0.36.0")
REGISTRY_HOST = "registry.corp.example"
BASE = "/apm"
REGISTRY_TOKEN = "registry-upgrade-sentinel-2b9c"
_PAT = "upgrade-fixture-pat-sentinel-0d1e"
_GUIDE = "---\napplyTo: '**'\ndescription: registry fixture\n---\n# Registry package\n"


def _deterministic_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, text in (
            ("apm.yml", "name: widget\nversion: 1.0.0\ndescription: fixture\n"),
            (".apm/instructions/guide.instructions.md", _GUIDE),
        ):
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            package.writestr(info, text)
    return buffer.getvalue()


class RegistryUpgradeScenario:
    """Registry consumer whose registry may sit behind a same-origin redirect."""

    def __init__(self, root: Path, binary: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)
        self.archive = _deterministic_archive()
        self.redirect = False
        self.project = self.isolated.work_root / "consumer"
        self.project.mkdir()
        (self.project / ".github").mkdir()
        (self.project / ".github" / "copilot-instructions.md").write_text("# c\n", encoding="utf-8")
        (self.project / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\ndescription: registry upgrade fixture\n"
            "targets: [copilot]\n"
            f"registries:\n  fixture:\n    url: https://{REGISTRY_HOST}{BASE}\n  default: fixture\n"
            "dependencies:\n  apm:\n    - acme/widget#1.0.0\n",
            encoding="utf-8",
        )

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        path = request.path
        if self.redirect and not path.endswith("/"):
            return ScriptedResponse(307, {"Location": f"https://{REGISTRY_HOST}{path}/"})
        tail = path.rstrip("/").split("/v1/packages/", 1)[-1]
        if tail == "acme/widget/versions":
            body = {
                "versions": [
                    {
                        "version": "1.0.0",
                        "digest": hashlib.sha256(self.archive).hexdigest(),
                        "published_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }
            return ScriptedResponse(
                200, {"Content-Type": "application/json"}, json.dumps(body).encode()
            )
        if tail == "acme/widget/versions/1.0.0/download":
            return ScriptedResponse(200, {"Content-Type": "application/zip"}, self.archive)
        return ScriptedResponse(404)

    def serve(self):
        return local_scripted_https_proxy(self.isolated.root / "tls", {REGISTRY_HOST: self.handle})

    def env(self, proxy: ScriptedHttpsProxy) -> dict[str, str]:
        env = proxy.child_environment(self.isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        env["APM_REGISTRY_TOKEN_FIXTURE"] = REGISTRY_TOKEN
        return env

    def enable(self, env: dict[str, str]) -> None:
        """Enable registries and bind the credential to the registry origin in user config."""
        result = self.run(env, "experimental", "enable", "registries", step="enable")
        assert result.returncode == 0, result.stdout + result.stderr
        url = f"https://{REGISTRY_HOST}{BASE}"
        bound = self.run(env, "config", "set", "registry.fixture.url", url, step="bind")
        assert bound.returncode == 0, bound.stdout + bound.stderr

    def run(self, env: dict[str, str], *args: str, step: str) -> CommandResult:
        return self.runner.run(args, scenario_id=step, cwd=self.project, env=env)


def _seed_lock(project: Path, source: Path) -> bytes:
    for name in ("apm.yml", "apm.lock.yaml"):
        shutil.copyfile(source / name, project / name)
    return (project / "apm.lock.yaml").read_bytes()


def _assert_secret_free(project: Path, *secrets: str) -> None:
    for name in ("apm.yml", "apm.lock.yaml"):
        text = (project / name).read_text(encoding="utf-8")
        assert all(secret not in text for secret in secrets), name


@pytest.mark.parametrize("version", VERSIONS)
def test_old_ado_lock_replays_frozen_and_updates_through_credential_helper(
    tmp_path: Path, apm_binary_path: Path, version: str
) -> None:
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    project = scenario.project()
    lock_bytes = _seed_lock(project, FIXTURES / version / "ado")
    locked = load_yaml(project / "apm.lock.yaml")["dependencies"][0]
    assert locked["resolved_commit"] == scenario.first_commit.sha, "fixture drifted from source"
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        frozen = scenario.run((*INSTALL, "--frozen"), cwd=project, env=env, step="frozen")
        assert frozen.returncode == 0, describe(frozen, proxy)
        assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
        update = scenario.run(
            ("update", "--yes", "--target", "copilot", "--parallel-downloads", "0"),
            cwd=project,
            env=env,
            step="update",
        )
        assert update.returncode == 0, describe(update, proxy)
    assert load_yaml(project / "apm.lock.yaml")["dependencies"][0]["resolved_commit"] == (
        scenario.first_commit.sha
    )
    _assert_secret_free(project, HELPER_SECRET, HELPER_USER, _PAT)


@pytest.mark.parametrize("version", VERSIONS)
def test_old_ado_lock_replays_frozen_with_pat(
    tmp_path: Path, apm_binary_path: Path, version: str
) -> None:
    """Regression lock: the PAT path that wrote these locks still replays them unchanged."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.ado.accepted_authorizations.add(basic_authorization("", _PAT))
    project = scenario.project()
    lock_bytes = _seed_lock(project, FIXTURES / version / "ado")
    with scenario.serve() as proxy:
        env = scenario.env(proxy, {"ADO_APM_PAT": _PAT})
        frozen = scenario.run((*INSTALL, "--frozen"), cwd=project, env=env, step="frozen-pat")
        assert frozen.returncode == 0, describe(frozen, proxy)
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    _assert_secret_free(project, _PAT)


@pytest.mark.parametrize("redirect", [False, True], ids=["direct", "same-origin-307"])
@pytest.mark.parametrize("version", VERSIONS)
def test_old_registry_lock_replays_resolved_url_without_churn(
    tmp_path: Path, apm_binary_path: Path, version: str, redirect: bool
) -> None:
    scenario = RegistryUpgradeScenario(tmp_path / "scenario", apm_binary_path)
    lock_bytes = _seed_lock(scenario.project, FIXTURES / version / "registry")
    resolved = load_yaml(scenario.project / "apm.lock.yaml")["dependencies"][0].get("resolved_url")
    assert resolved, "fixture lock carries no resolved_url"
    scenario.redirect = redirect
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        frozen = scenario.run(
            env, "install", "--frozen", "--target", "copilot", "--no-policy", step="frozen"
        )
        receiving = proxy.hosts_receiving(REGISTRY_TOKEN)
    assert receiving == {REGISTRY_HOST}, receiving
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    assert (scenario.project / "apm.lock.yaml").read_bytes() == lock_bytes
    _assert_secret_free(scenario.project, REGISTRY_TOKEN)
