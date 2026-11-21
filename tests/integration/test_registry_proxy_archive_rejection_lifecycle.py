"""A rejected proxy archive stays the reported cause through the installed CLI (#3110).

Behind ``PROXY_REGISTRY_URL`` APM tries several archive URLs in order. When
the proxy answers one URL with an archive that extraction refuses (a symlink
member) and every later URL answers HTTP 404, ``apm install`` reports the
refused archive, its URL and the reason, never the last 404. The refusal is
still a refusal: nothing from the package is written, the lockfile keeps its
bytes, no link is created and the registry token stays out of the output and
on the proxy origin. A package whose first URL 404s and a later URL serves a
valid archive still installs.
"""

from __future__ import annotations

import io
import os
import stat
import sys
import zipfile
from collections.abc import Callable
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
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise(
        "P-LAW-outcome.status_matches_state",
        "P-LAW-transaction.failed_command_preserves_state",
        "P-OWN-archive-extraction-limits",
    ),
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
]

PROXY_HOST = "art.example.test"
PREFIX = "/artifactory/github"
TOKEN = "proxy-archive-sentinel-3110"
REF = "1.0.0"
_GUIDE = "---\napplyTo: '**'\ndescription: proxy fixture\n---\n# {name}\n"


def _archive(name: str, *, link: bool) -> bytes:
    """A GitHub-style archive of ``acme/<name>``, optionally with a symlink member."""
    root = f"{name}-{REF}/"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(root, "")
        archive.writestr(f"{root}apm.yml", f"name: {name}\nversion: {REF}\n")
        archive.writestr(
            f"{root}.apm/instructions/{name}.instructions.md", _GUIDE.format(name=name)
        )
        if link:
            member = zipfile.ZipInfo(f"{root}.apm/instructions/linked.instructions.md")
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(member, f"{name}.instructions.md")
    return buffer.getvalue()


def _url(name: str, tail: str) -> str:
    return f"https://{PROXY_HOST}{PREFIX}/acme/{name}/{tail}"


class _Proxy:
    """The registry proxy: exact archive paths answer 200, everything else 404."""

    def __init__(self) -> None:
        self.archives: dict[str, bytes] = {}

    def serve(self, name: str, tail: str, body: bytes) -> None:
        self.archives[f"{PREFIX}/acme/{name}/{tail}"] = body

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        body = self.archives.get(request.path)
        if body is None:
            return ScriptedResponse(404)
        return ScriptedResponse(200, {"Content-Type": "application/zip"}, body)


class _Scenario:
    def __init__(self, tmp_path: Path, binary: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(
            tmp_path / "scenario", base_env=dict(os.environ)
        )
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=180)
        self.proxy = _Proxy()
        self.project = self.isolated.work_root / "consumer"
        (self.project / ".github").mkdir(parents=True)
        (self.project / ".github" / "copilot-instructions.md").write_text("# c\n", encoding="utf-8")

    def write_manifest(self, *packages: str) -> None:
        deps = "".join(f"    - acme/{name}#{REF}\n" for name in packages)
        (self.project / "apm.yml").write_text(
            f"name: consumer\nversion: 1.0.0\ntargets: [copilot]\ndependencies:\n  apm:\n{deps}",
            encoding="utf-8",
        )

    def env(self, proxy: ScriptedHttpsProxy) -> dict[str, str]:
        env = proxy.child_environment(self.isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        env["PROXY_REGISTRY_URL"] = f"https://{PROXY_HOST}{PREFIX}"
        env["PROXY_REGISTRY_TOKEN"] = TOKEN
        env["PROXY_REGISTRY_ONLY"] = "1"
        return env

    def install(self, env: dict[str, str], step: str) -> CommandResult:
        return self.runner.run(
            ("install", "--target", "copilot", "--parallel-downloads", "0"),
            scenario_id=step,
            cwd=self.project,
            env=env,
        )

    def snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.project).as_posix(): path.read_bytes()
            for path in sorted(self.project.rglob("*"))
            if path.is_file() and path.name != "apm.yml"
        }


def _text(result: CommandResult) -> str:
    return f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"


def _flat(result: CommandResult) -> str:
    """Output without whitespace, so terminal wrapping cannot split a URL."""
    return "".join((result.stdout + result.stderr).split())


def _run(
    tmp_path: Path, binary: Path, arrange: Callable[[_Proxy], None], package: str
) -> tuple[_Scenario, bytes, dict[str, bytes], CommandResult, ScriptedHttpsProxy]:
    """Install ``acme/good``, then add *package* and install again."""
    scenario = _Scenario(tmp_path, binary)
    scenario.proxy.serve("good", f"archive/refs/heads/{REF}.zip", _archive("good", link=False))
    arrange(scenario.proxy)
    scenario.write_manifest("good")
    with local_scripted_https_proxy(
        scenario.isolated.root / "tls", {PROXY_HOST: scenario.proxy.handle}
    ) as proxy:
        env = scenario.env(proxy)
        first = scenario.install(env, "1-install-good")
        assert first.returncode == 0, _text(first)
        lock_bytes = (scenario.project / "apm.lock.yaml").read_bytes()
        before = scenario.snapshot()
        scenario.write_manifest("good", package)
        proxy.clear()
        second = scenario.install(env, f"2-install-{package}")
    return scenario, lock_bytes, before, second, proxy


def test_rejected_proxy_archive_is_reported_instead_of_later_404(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    rejected_url = _url("linked", f"archive/refs/heads/{REF}.zip")

    def arrange(proxy: _Proxy) -> None:
        proxy.serve("linked", f"archive/refs/heads/{REF}.zip", _archive("linked", link=True))

    scenario, lock_bytes, before, refused, proxy = _run(
        tmp_path, apm_binary_path, arrange, "linked"
    )

    flat = _flat(refused)
    assert refused.returncode != 0, _text(refused)
    # The cause is the refused archive, not the 404 of a later fallback URL.
    assert f"Archiveerror:Unsafeziparchivefrom{rejected_url}" in flat, _text(refused)
    assert f"Refusingtoextractsymlink:linked-{REF}/.apm/instructions/" in flat, _text(refused)
    assert "Lasterror:HTTP404" not in flat, _text(refused)
    # Every later fallback URL was tried and answered 404.
    statuses = {r.path: r.status for r in proxy.requests_to(PROXY_HOST)}
    later = [path for path in statuses if "/acme/linked/" in path and "refs/heads" not in path]
    assert later and all(statuses[path] == 404 for path in later), statuses
    # Containment holds and the failed install preserves state.
    assert (scenario.project / "apm.lock.yaml").read_bytes() == lock_bytes, _text(refused)
    assert scenario.snapshot() == before, _text(refused)
    assert not any(p.is_symlink() for p in scenario.project.rglob("*"))
    # The registry token never reaches the output or another origin.
    assert TOKEN not in refused.stdout + refused.stderr
    assert proxy.hosts_receiving(TOKEN) == {PROXY_HOST}


def test_fallback_to_a_valid_proxy_archive_still_installs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    def arrange(proxy: _Proxy) -> None:
        proxy.serve("tagged", f"archive/refs/tags/{REF}.zip", _archive("tagged", link=False))

    scenario, _lock, _before, second, proxy = _run(tmp_path, apm_binary_path, arrange, "tagged")

    assert second.returncode == 0, _text(second)
    deployed = scenario.project / ".github" / "instructions" / "tagged.instructions.md"
    assert deployed.read_text(encoding="utf-8") == _GUIDE.format(name="tagged")
    lock = (scenario.project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert "acme/tagged" in lock and "acme/good" in lock
    assert "Archiveerror" not in _flat(second), _text(second)
    assert TOKEN not in second.stdout + second.stderr
    assert proxy.hosts_receiving(TOKEN) == {PROXY_HOST}
