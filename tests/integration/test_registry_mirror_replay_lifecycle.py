"""Lockfile replay tolerates a moved or mirrored registry endpoint (#4013, req-rs-009).

A registry dependency is installed from endpoint A, then the same registry
name is reconfigured to endpoint B. Replay fetches from the configured
endpoint B (never from the locked URL on A), verifies the locked hash before
extraction, and leaves the lockfile untouched. B serving different bytes
fails closed before anything is extracted (req-lk-013).
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
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
    pytest.mark.e2e,
    pytest.mark.promise("req-rs-009"),
]

HOST_A = "registry-a.corp.example"
HOST_B = "registry-b.corp.example"
BASE = "/apm"
DOWNLOAD = "/v1/packages/acme/widget/versions/1.0.0/download"
DEPLOYED = Path(".github") / "instructions" / "guide.instructions.md"


def _archive(body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, text in (
            ("apm.yml", "name: widget\nversion: 1.0.0\ndescription: fixture\n"),
            (
                ".apm/instructions/guide.instructions.md",
                f"---\napplyTo: '**'\ndescription: registry fixture\n---\n# {body}\n",
            ),
        ):
            package.writestr(zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0)), text)
    return buffer.getvalue()


class _Endpoint:
    """One registry endpoint serving ``acme/widget`` 1.0.0."""

    def __init__(self, archive: bytes) -> None:
        self.archive = archive

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        tail = request.path.rstrip("/").split("/v1/packages/", 1)[-1]
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


class MirrorScenario:
    def __init__(self, root: Path, binary: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)
        self.archive = _archive("Registry package")
        self.a = _Endpoint(self.archive)
        self.b = _Endpoint(self.archive)
        self.project = self.isolated.work_root / "consumer"
        self.project.mkdir()
        (self.project / ".github").mkdir()
        (self.project / ".github" / "copilot-instructions.md").write_text("# c\n", encoding="utf-8")
        self.configure(HOST_A)

    def configure(self, host: str) -> None:
        (self.project / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\ndescription: registry mirror fixture\n"
            "targets: [copilot]\n"
            f"registries:\n  fixture:\n    url: https://{host}{BASE}\n  default: fixture\n"
            "dependencies:\n  apm:\n    - acme/widget#1.0.0\n",
            encoding="utf-8",
        )

    def serve(self):
        return local_scripted_https_proxy(
            self.isolated.root / "tls", {HOST_A: self.a.handle, HOST_B: self.b.handle}
        )

    def env(self, proxy: ScriptedHttpsProxy) -> dict[str, str]:
        env = proxy.child_environment(self.isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        return env

    def run(self, env: dict[str, str], *args: str, step: str) -> CommandResult:
        return self.runner.run(args, scenario_id=step, cwd=self.project, env=env)

    def install(self, env: dict[str, str], *extra: str, step: str) -> CommandResult:
        return self.run(env, "install", "--target", "copilot", *extra, step=step)

    def drop_modules(self) -> None:
        shutil.rmtree(self.project / "apm_modules", ignore_errors=True)

    def snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.project).as_posix(): path.read_bytes()
            for path in sorted(self.project.rglob("*"))
            if path.is_file() and "apm_modules" not in path.relative_to(self.project).parts
        }


def _out(result: CommandResult) -> str:
    return result.stdout + result.stderr


def _flat(result: CommandResult) -> str:
    """Output with all whitespace removed, so terminal wrapping cannot split a URL."""
    return "".join(_out(result).split())


def test_replay_from_reconfigured_endpoint_verifies_hash_and_never_contacts_locked_url(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = MirrorScenario(tmp_path / "scenario", apm_binary_path)
    url_a = f"https://{HOST_A}{BASE}{DOWNLOAD}"
    url_b = f"https://{HOST_B}{BASE}{DOWNLOAD}"
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        enabled = scenario.run(env, "experimental", "enable", "registries", step="enable")
        assert enabled.returncode == 0, _out(enabled)

        first = scenario.install(env, step="1-install-from-a")
        assert first.returncode == 0, _out(first)
        lock = (scenario.project / "apm.lock.yaml").read_bytes()
        assert url_a.encode() in lock
        deployed = (scenario.project / DEPLOYED).read_bytes()

        # Equal-URL replay is unchanged: no mirror line.
        scenario.drop_modules()
        same = scenario.install(env, step="2-replay-same-url")
        assert same.returncode == 0, _out(same)
        assert url_b not in _flat(same)

        # Reconfigure the same registry name to endpoint B.
        scenario.configure(HOST_B)
        for step, extra in (("3-replay-mirror", ()), ("4-replay-mirror-frozen", ("--frozen",))):
            scenario.drop_modules()
            proxy.clear()
            result = scenario.install(env, *extra, step=step)
            assert result.returncode == 0, _out(result)
            assert "[i]" in _out(result)
            assert url_a in _flat(result) and url_b in _flat(result)
            assert proxy.requests_to(HOST_A) == (), "replay contacted the locked URL"
            assert any(r.path == BASE + DOWNLOAD for r in proxy.requests_to(HOST_B))
            assert (scenario.project / "apm.lock.yaml").read_bytes() == lock
            assert (scenario.project / DEPLOYED).read_bytes() == deployed

        # B serves different bytes: fail closed before extraction.
        scenario.b.archive = _archive("Tampered package")
        expected = hashlib.sha256(scenario.archive).hexdigest()
        actual = hashlib.sha256(scenario.b.archive).hexdigest()
        for step, extra in (("5-mirror-mismatch", ()), ("6-mirror-mismatch-frozen", ("--frozen",))):
            scenario.drop_modules()
            before = scenario.snapshot()
            proxy.clear()
            failed = scenario.install(env, *extra, step=step)
            assert failed.returncode != 0, _out(failed)
            for needle in ("acme/widget", url_a, url_b, expected, actual):
                assert needle in _flat(failed), f"{needle!r} missing from:\n{_out(failed)}"
            assert proxy.requests_to(HOST_A) == ()
            assert scenario.snapshot() == before, "failed install wrote files"
            widget = scenario.project / "apm_modules" / "acme" / "widget"
            assert not widget.exists() or not any(widget.iterdir()), "partial extraction"
