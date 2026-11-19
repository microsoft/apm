"""Registry redirect targets are normalized before the origin decision (#3726).

Hardening companion to ``test_registry_redirect_origin_lifecycle.py`` (T1
brief section 5.5 and supply-chain R1): a gateway ``Location`` that only
looks like the configured registry (scheme-relative host, userinfo, dot or
encoded-dot segments, doubled slashes, an HTTPS downgrade, an endless chain)
never reaches (with or without the registry credential) anything outside the
configured origin and base path. A case-only host difference stays in origin.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from apm_cli.utils.yaml_io import dump_yaml
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
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
]

REGISTRY_HOST = "registry.corp.example"
OTHER_HOST = "mirror.elsewhere.example"
BASE = "/apm"
TOKEN = "registry-hardening-sentinel-91fa"
_GUIDE = "---\napplyTo: '**'\ndescription: registry fixture\n---\n# Registry package\n"

_LOCATIONS = {
    "scheme-relative-host": "//" + OTHER_HOST + "{path}/",
    "userinfo-host": "https://" + REGISTRY_HOST + "@" + OTHER_HOST + "{path}/",
    "dot-segment-escape": BASE + "/../other-tenant{tail}/",
    "encoded-dot-escape": BASE + "/%2e%2e/other-tenant{tail}/",
    "double-slash-escape": BASE + "//..//other-tenant{tail}/",
    "https-downgrade": "http://" + REGISTRY_HOST + "{path}/",
}


def _archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, text in (
            ("apm.yml", "name: widget\nversion: 1.0.0\ndescription: fixture\n"),
            (".apm/instructions/guide.instructions.md", _GUIDE),
        ):
            package.writestr(zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0)), text)
    return buffer.getvalue()


class _Gateway:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.archive = _archive()

    def _payload(self, path: str) -> ScriptedResponse:
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

    def registry(self, request: ScriptedRequest) -> ScriptedResponse:
        path = request.path
        if self.mode == "endless-chain":
            hops = path.count("/hop")
            base_path = path.split("/hop", 1)[0]
            if hops < 6:
                return ScriptedResponse(307, {"Location": f"{path}/hop{hops}"})
            return self._payload(base_path)
        if self.mode == "case-only-host":
            if path.endswith("/"):
                return self._payload(path)
            return ScriptedResponse(307, {"Location": f"https://{REGISTRY_HOST.upper()}{path}/"})
        if path.startswith(BASE + "/v1/") and not path.endswith("/"):
            tail = path[len(BASE) :]
            location = _LOCATIONS[self.mode].format(path=path, tail=tail)
            return ScriptedResponse(307, {"Location": location})
        return self._payload(path)

    def other(self, request: ScriptedRequest) -> ScriptedResponse:
        return self._payload(request.path)


def _run(tmp_path: Path, binary: Path, mode: str) -> tuple[CommandResult, list[ScriptedRequest]]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / ".github").mkdir()
    (project / ".github" / "copilot-instructions.md").write_text("# c\n", encoding="utf-8")
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "registries": {
                "fixture": {"url": f"https://{REGISTRY_HOST}{BASE}"},
                "default": "fixture",
            },
            "dependencies": {"apm": ["acme/widget#1.0.0"]},
        },
        project / "apm.yml",
    )
    gateway = _Gateway(mode)
    handlers = {REGISTRY_HOST: gateway.registry, OTHER_HOST: gateway.other}
    with local_scripted_https_proxy(isolated.root / "tls", handlers) as proxy:
        env = proxy.child_environment(isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        env["APM_REGISTRY_TOKEN_FIXTURE"] = TOKEN
        enabled = runner.run(
            ("experimental", "enable", "registries"), cwd=project, env=env, scenario_id="enable"
        )
        assert enabled.returncode == 0, enabled.stdout + enabled.stderr
        bound = runner.run(
            ("config", "set", "registry.fixture.url", f"https://{REGISTRY_HOST}{BASE}"),
            cwd=project,
            env=env,
            scenario_id="bind",
        )
        assert bound.returncode == 0, bound.stdout + bound.stderr
        result = runner.run(
            ("install", "--target", "copilot", "--no-policy"),
            cwd=project,
            env=env,
            scenario_id=f"install-{mode}",
        )
        observations = [o for o in proxy.observations if o.method != "CONNECT"]
    return result, observations


def _in_base(target: str) -> bool:
    path = posixpath.normpath(unquote(urlsplit(target).path).replace("\\", "/"))
    return path == BASE or path.startswith(BASE + "/")


def _escaped(observations: list[ScriptedRequest]) -> list[tuple[str, str, bool]]:
    """Requests that left the configured origin or (normalized) base path."""
    return [
        (o.host, o.target, o.header("Authorization") is not None)
        for o in observations
        if o.host != REGISTRY_HOST or not _in_base(o.target)
    ]


@pytest.mark.parametrize("mode", sorted(_LOCATIONS))
def test_registry_refuses_lookalike_redirect_targets(
    tmp_path: Path, apm_binary_path: Path, mode: str
) -> None:
    result, observations = _run(tmp_path, apm_binary_path, mode)
    escaped = _escaped(observations)
    assert all(not authorized for _host, _target, authorized in escaped), escaped
    assert escaped == [], escaped
    assert any(o.header("Authorization") for o in observations), "credential never presented"
    assert TOKEN not in result.stdout + result.stderr


def test_registry_refuses_endless_same_origin_redirect_chain(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    result, observations = _run(tmp_path, apm_binary_path, "endless-chain")
    assert result.returncode != 0, result.stdout + result.stderr
    assert len(observations) <= 8, [o.target for o in observations]


def test_registry_follows_case_only_host_difference(tmp_path: Path, apm_binary_path: Path) -> None:
    result, observations = _run(tmp_path, apm_binary_path, "case-only-host")
    assert _escaped(observations) == []
    assert result.returncode == 0, result.stdout + result.stderr
