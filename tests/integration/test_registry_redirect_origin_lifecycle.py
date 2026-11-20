"""Registry redirects stay inside the configured registry origin (#3726).

A registry behind a gateway may answer with a same-origin redirect (path
normalization, for example Envoy ``UNESCAPE_AND_REDIRECT``). The client must
follow a redirect only to the same scheme, host and port under the
configured base path, keep the canonical (pre-redirect) URL in the lockfile,
and refuse anything else without sending the registry credential or the
archive body to another origin or path. The same rules hold for the lockfile
replay fetch and for ``apm publish``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import (
    ScriptedHttpsProxy,
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

REGISTRY_HOST = "registry.corp.example"
OTHER_HOST = "mirror.elsewhere.example"
BASE = "/apm"
TOKEN = "registry-bearer-sentinel-6d02"
_GUIDE = "---\napplyTo: '**'\ndescription: registry fixture\n---\n# Registry package\n"


def _archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("apm.yml", "name: widget\nversion: 1.0.0\ndescription: fixture\n")
        package.writestr(".apm/instructions/guide.instructions.md", _GUIDE)
    return buffer.getvalue()


@dataclass
class RegistryGateway:
    """Registry origin behind a configurable redirecting gateway."""

    mode: str = "direct"
    archive: bytes = field(default_factory=_archive)
    published: list[bytes] = field(default_factory=list)

    def _canonical(self, path: str) -> str:
        return path.rstrip("/")

    def _redirect(self, request: ScriptedRequest) -> ScriptedResponse | None:
        path = request.path
        if self.mode == "direct" or path.endswith("/"):
            return None
        code = 302 if self.mode == "same-origin-302" else 307
        if self.mode in ("same-origin-307", "same-origin-302"):
            location = f"https://{REGISTRY_HOST}{path}/"
        elif self.mode == "cross-origin-307":
            location = f"https://{OTHER_HOST}{path}/"
        elif self.mode == "outside-base-307":
            location = f"https://{REGISTRY_HOST}/other-tenant{path[len(BASE) :]}/"
        else:
            raise AssertionError(self.mode)
        return ScriptedResponse(code, {"Location": location})

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        redirect = self._redirect(request)
        if redirect is not None:
            return redirect
        return self.serve(request)

    def serve(self, request: ScriptedRequest) -> ScriptedResponse:
        path = self._canonical(request.path)
        if not path.startswith(BASE + "/") and not path.startswith("/other-tenant/"):
            return ScriptedResponse(404)
        tail = path.split("/v1/packages/", 1)[-1]
        if request.method == "PUT":
            self.published.append(request.body)
            payload = {
                "package": "acme/widget",
                "version": "1.0.0",
                "digest": "sha256:" + hashlib.sha256(request.body).hexdigest(),
                "published_at": "2026-01-01T00:00:00Z",
            }
            return ScriptedResponse(
                201, {"Content-Type": "application/json"}, json.dumps(payload).encode()
            )
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


class _Scenario:
    def __init__(self, tmp_path: Path, binary: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(
            tmp_path / "scenario", base_env=dict(os.environ)
        )
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)
        self.gateway = RegistryGateway()
        self.other = RegistryGateway()
        self.project = self.isolated.work_root / "consumer"
        self.project.mkdir()
        (self.project / ".github").mkdir()
        (self.project / ".github" / "copilot-instructions.md").write_text("# c\n", encoding="utf-8")

    def env(self, proxy: ScriptedHttpsProxy) -> dict[str, str]:
        env = proxy.child_environment(self.isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        env["APM_REGISTRY_TOKEN_FIXTURE"] = TOKEN
        return env

    def serve(self):
        return local_scripted_https_proxy(
            self.isolated.root / "tls",
            {REGISTRY_HOST: self.gateway.handle, OTHER_HOST: self.other.serve},
        )

    def run(self, env: dict[str, str], *args: str, step: str) -> CommandResult:
        return self.runner.run(args, scenario_id=step, cwd=self.project, env=env)

    def enable(self, env: dict[str, str]) -> None:
        """Enable registries and bind the credential to the registry origin in user config."""
        result = self.run(env, "experimental", "enable", "registries", step="enable")
        assert result.returncode == 0, result.stdout + result.stderr
        bound = self.run(
            env,
            "config",
            "set",
            "registry.fixture.url",
            f"https://{REGISTRY_HOST}{BASE}",
            step="bind-credential",
        )
        assert bound.returncode == 0, bound.stdout + bound.stderr

    def write_manifest(self, *, consumer: bool = True) -> None:
        manifest: dict[str, object] = {
            "name": "consumer" if consumer else "widget",
            "version": "1.0.0",
            "description": "registry redirect fixture",
            "registries": {
                "fixture": {"url": f"https://{REGISTRY_HOST}{BASE}"},
                "default": "fixture",
            },
        }
        if consumer:
            manifest["targets"] = ["copilot"]
            manifest["dependencies"] = {"apm": ["acme/widget#1.0.0"]}
        dump_yaml(manifest, self.project / "apm.yml")


def _text(result: CommandResult) -> str:
    return f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"


def _off_origin(proxy: ScriptedHttpsProxy) -> list[ScriptedRequest]:
    return [
        o
        for o in proxy.observations
        if o.method != "CONNECT"
        and (o.host != REGISTRY_HOST or not (o.path == BASE or o.path.startswith(BASE + "/")))
    ]


_INSTALL = ("install", "--target", "copilot", "--no-policy")


def test_registry_install_follows_same_origin_gateway_redirect(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path)
    scenario.gateway.mode = "same-origin-307"
    scenario.write_manifest()
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        result = scenario.run(env, *_INSTALL, step="install-307")
        off_origin = _off_origin(proxy)
        assert REGISTRY_HOST in proxy.hosts_receiving(TOKEN), "credential never presented"
    assert off_origin == []
    assert result.returncode == 0, _text(result)
    locked = load_yaml(scenario.project / "apm.lock.yaml")["dependencies"][0]
    resolved = str(locked.get("resolved_url") or "")
    assert not resolved.endswith("/"), (
        "lock recorded the post-redirect URL instead of the canonical one"
    )
    assert urlsplit(resolved).hostname in (None, "", REGISTRY_HOST), resolved


@pytest.mark.parametrize("mode", ["cross-origin-307", "outside-base-307"])
def test_registry_install_refuses_redirect_outside_configured_registry(
    tmp_path: Path, apm_binary_path: Path, mode: str
) -> None:
    """Regression lock: no request (and no credential) reaches another origin or base path."""
    scenario = _Scenario(tmp_path, apm_binary_path)
    scenario.gateway.mode = mode
    scenario.write_manifest()
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        result = scenario.run(env, *_INSTALL, step=f"install-{mode}")
        receiving = proxy.hosts_receiving(TOKEN)
        assert REGISTRY_HOST in receiving, "credential never presented"
        off_origin = _off_origin(proxy)
    assert result.returncode != 0, _text(result)
    assert receiving <= {REGISTRY_HOST}
    assert off_origin == [], [(o.host, o.path) for o in off_origin]
    assert TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize("replay_mode", ["direct", "same-origin-307"])
def test_registry_lock_replay_follows_same_origin_redirect_without_lock_churn(
    tmp_path: Path, apm_binary_path: Path, replay_mode: str
) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path)
    scenario.write_manifest()
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        first = scenario.run(env, *_INSTALL, step="install-direct")
        assert first.returncode == 0, _text(first)
        lock_bytes = (scenario.project / "apm.lock.yaml").read_bytes()
        clean = scenario.run(env, "cache", "clean", "--yes", step="cache-clean")
        assert clean.returncode == 0, _text(clean)
        for path in sorted((scenario.project / "apm_modules").rglob("*"), reverse=True):
            path.unlink() if path.is_file() or path.is_symlink() else path.rmdir()
        scenario.gateway.mode = replay_mode
        frozen = scenario.run(env, *_INSTALL, "--frozen", step=f"frozen-{replay_mode}")
        off_origin = _off_origin(proxy)
    assert off_origin == []
    assert frozen.returncode == 0, _text(frozen)
    assert (scenario.project / "apm.lock.yaml").read_bytes() == lock_bytes


def _publish_project(scenario: _Scenario) -> None:
    scenario.write_manifest(consumer=False)
    instructions = scenario.project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "guide.instructions.md").write_text(_GUIDE, encoding="utf-8")
    (scenario.project / "LICENSE").write_text("MIT\n", encoding="utf-8")


def test_publish_upload_never_leaves_registry_origin(tmp_path: Path, apm_binary_path: Path) -> None:
    """A redirected publish sends neither the archive nor the credential to another origin."""
    scenario = _Scenario(tmp_path, apm_binary_path)
    scenario.gateway.mode = "cross-origin-307"
    _publish_project(scenario)
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        result = scenario.run(
            env, "publish", "--package", "acme/widget", "--registry", "fixture", step="publish-x"
        )
        off_origin = _off_origin(proxy)
        assert proxy.hosts_receiving(TOKEN) == {REGISTRY_HOST}, "credential not presented on origin"
    assert off_origin == [], [(o.host, o.method, o.path, len(o.body)) for o in off_origin]
    assert scenario.other.published == []
    assert result.returncode != 0, _text(result)
    assert "Published" not in result.stdout


def test_publish_upload_follows_same_origin_307_with_body(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Regression lock: a same-origin 307 keeps the PUT method and the archive body."""
    scenario = _Scenario(tmp_path, apm_binary_path)
    scenario.gateway.mode = "same-origin-307"
    _publish_project(scenario)
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        result = scenario.run(
            env, "publish", "--package", "acme/widget", "--registry", "fixture", step="publish-307"
        )
        followed = [o for o in proxy.requests_to(REGISTRY_HOST) if o.path.endswith("/")]
    assert result.returncode == 0, _text(result)
    assert followed and all(o.header("Authorization") == f"Bearer {TOKEN}" for o in followed)
    assert len(scenario.gateway.published) == 1 and scenario.gateway.published[0]


def test_publish_upload_is_not_downgraded_by_302(tmp_path: Path, apm_binary_path: Path) -> None:
    """A 302 on PUT is not silently replayed as a body-less GET reported as success."""
    scenario = _Scenario(tmp_path, apm_binary_path)
    scenario.gateway.mode = "same-origin-302"
    _publish_project(scenario)
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        result = scenario.run(
            env, "publish", "--package", "acme/widget", "--registry", "fixture", step="publish-302"
        )
        downgraded = [o for o in proxy.requests_to(REGISTRY_HOST) if o.method == "GET"]
    assert downgraded == [], [(o.method, o.path) for o in downgraded]
    assert result.returncode != 0, _text(result)
