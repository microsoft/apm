"""Registry archive limits apply to every archive format through the installed CLI (#4235).

A registry may serve a package as a zip or a gzip tarball. Whichever format
arrives, an archive over the shared limits (entry count, duplicate entries)
is refused: the install exits non-zero, no package file is written, the
lockfile keeps its bytes and the project's deployed files are unchanged.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

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
BASE = "/apm"
TOKEN = "registry-bearer-sentinel-4235"
_GUIDE = "---\napplyTo: '**'\ndescription: registry fixture\n---\n# Registry package\n"
_MANIFEST = "name: {name}\nversion: 1.0.0\ndescription: fixture\n"
_ENTRY_LIMIT = 10_000


def _entries(name: str, shape: str) -> list[tuple[str, bytes]]:
    base = [
        ("apm.yml", _MANIFEST.format(name=name).encode()),
        (".apm/instructions/guide.instructions.md", _GUIDE.encode()),
    ]
    if shape == "valid":
        return base
    if shape == "duplicate-entry":
        return [*base, ("APM.yml", _MANIFEST.format(name="other").encode())]
    if shape == "entry-count":
        return base + [(f"filler/{i}.txt", b"") for i in range(_ENTRY_LIMIT)]
    raise AssertionError(shape)


def _archive(fmt: str, entries: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(buffer, "w") as package:
            for name, content in entries:
                package.writestr(name, content)
        return buffer.getvalue(), "application/zip"
    with tarfile.open(fileobj=buffer, mode="w:gz") as package:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            package.addfile(info, io.BytesIO(content))
    return buffer.getvalue(), "application/gzip"


@dataclass
class Registry:
    """A registry serving one archive per package."""

    archives: dict[str, tuple[bytes, str]] = field(default_factory=dict)

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        path = request.path.rstrip("/")
        if not path.startswith(BASE + "/v1/packages/"):
            return ScriptedResponse(404)
        tail = path.split("/v1/packages/", 1)[1]
        for package, (body, content_type) in self.archives.items():
            if tail == f"{package}/versions":
                payload = {
                    "versions": [
                        {
                            "version": "1.0.0",
                            "digest": hashlib.sha256(body).hexdigest(),
                            "published_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                }
                return ScriptedResponse(
                    200, {"Content-Type": "application/json"}, json.dumps(payload).encode()
                )
            if tail == f"{package}/versions/1.0.0/download":
                return ScriptedResponse(200, {"Content-Type": content_type}, body)
        return ScriptedResponse(404)


class _Scenario:
    def __init__(self, tmp_path: Path, binary: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(
            tmp_path / "scenario", base_env=dict(os.environ)
        )
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=180)
        self.registry = Registry()
        self.project = self.isolated.work_root / "consumer"
        self.project.mkdir()
        (self.project / ".github").mkdir()
        (self.project / ".github" / "copilot-instructions.md").write_text("# c\n", encoding="utf-8")

    def serve(self):
        return local_scripted_https_proxy(
            self.isolated.root / "tls", {REGISTRY_HOST: self.registry.handle}
        )

    def env(self, proxy: ScriptedHttpsProxy) -> dict[str, str]:
        env = proxy.child_environment(self.isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        env["APM_REGISTRY_TOKEN_FIXTURE"] = TOKEN
        return env

    def run(self, env: dict[str, str], *args: str, step: str) -> CommandResult:
        return self.runner.run(args, scenario_id=step, cwd=self.project, env=env)

    def enable(self, env: dict[str, str]) -> None:
        for args, step in (
            (("experimental", "enable", "registries"), "enable"),
            (("config", "set", "registry.fixture.url", f"https://{REGISTRY_HOST}{BASE}"), "bind"),
        ):
            result = self.run(env, *args, step=step)
            assert result.returncode == 0, _text(result)

    def write_manifest(self, dependencies: list[str]) -> None:
        dump_yaml(
            {
                "name": "consumer",
                "version": "1.0.0",
                "description": "registry archive limits fixture",
                "registries": {
                    "fixture": {"url": f"https://{REGISTRY_HOST}{BASE}"},
                    "default": "fixture",
                },
                "targets": ["copilot"],
                "dependencies": {"apm": dependencies},
            },
            self.project / "apm.yml",
        )


def _text(result: CommandResult) -> str:
    return f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "apm.yml"
    }


_INSTALL = ("install", "--target", "copilot", "--no-policy")


@pytest.mark.parametrize("fmt", ["zip", "tar.gz"])
@pytest.mark.parametrize("shape", ["duplicate-entry", "entry-count"])
def test_registry_archive_over_the_limits_is_refused_without_writing(
    tmp_path: Path, apm_binary_path: Path, fmt: str, shape: str
) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path)
    scenario.registry.archives["acme/widget"] = _archive(fmt, _entries("widget", "valid"))
    scenario.registry.archives["acme/bundle"] = _archive(fmt, _entries("bundle", shape))
    scenario.write_manifest(["acme/widget#1.0.0"])
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        first = scenario.run(env, *_INSTALL, step="install-valid")
        assert first.returncode == 0, _text(first)
        lock_bytes = (scenario.project / "apm.lock.yaml").read_bytes()
        before = _snapshot(scenario.project)

        scenario.write_manifest(["acme/widget#1.0.0", "acme/bundle#1.0.0"])
        refused = scenario.run(env, *_INSTALL, step=f"install-{shape}-{fmt}")

    assert refused.returncode != 0, _text(refused)
    assert (scenario.project / "apm.lock.yaml").read_bytes() == lock_bytes, _text(refused)
    assert _snapshot(scenario.project) == before, _text(refused)
    bundle_root = scenario.project / "apm_modules" / "acme" / "bundle"
    assert not [p for p in bundle_root.rglob("*") if p.is_file()], _text(refused)
    locked = {
        row["repo_url"] for row in load_yaml(scenario.project / "apm.lock.yaml")["dependencies"]
    }
    assert locked == {"acme/widget"}, locked
