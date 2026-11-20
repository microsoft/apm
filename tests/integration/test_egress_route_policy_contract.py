"""Installed-CLI contract: under PROXY_REGISTRY_ONLY each egress takes its documented route.

Issue #4088. The ``egress-route-policy`` owner (``deps/egress_route.py``)
answers the route of every outbound surface. With ``PROXY_REGISTRY_ONLY=1``
the documented routes (docs ``enterprise/registry-proxy``) are:

* package archives go through ``PROXY_REGISTRY_URL``;
* probes and enumeration against the source host are not sent;
* the ``apm-policy.yml`` fetch goes direct: it is the governance channel;
* the automatic version check is not sent; ``apm self-update`` goes direct.

Each scenario runs the real CLI with the observation seams of
``test_registry_only_enumeration_contract``: a ``git`` shim that records and
fails network subcommands, and a loopback forward proxy that serves the
registry-proxy archive and records every other request (including
``CONNECT`` tunnels). The recorded egress is the snapshot each test asserts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.integration.test_registry_only_enumeration_contract import (
    _PROXY_HOST,
    _PROXY_URL,
    _install_git_shim,
    _recording_proxy,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.promise("P-LAW-auth.credentials_isolated", "P-LAW-outcome.status_matches_state"),
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX git shim"),
]

_UPDATE = ("install", "--update", "--target", "copilot")
_RELEASE_HOST = "releases.example.invalid"
_RELEASE_ENV = {"APM_RELEASE_METADATA_URL": f"https://{_RELEASE_HOST}/apm/latest.json"}

# Non-github.com hosts take the update auth preflight; each still downloads
# through the registry proxy in registry-only mode.
_PREFLIGHT_HOSTS = {
    "gitlab": "gitlab.com/acme/pkg#1.0.0",
    "generic": "git.example.com/acme/pkg#1.0.0",
    "azure-devops": "dev.azure.com/org/proj/_git/pkg#1.0.0",
}


@dataclass(frozen=True)
class _EgressSnapshot:
    """Every network contact one CLI run made, by route."""

    result: CommandResult
    project: Path
    git_network: tuple[str, ...]
    proxied: tuple[str, ...]
    upstream: tuple[str, ...]

    @property
    def output(self) -> str:
        return " ".join((self.result.stdout + self.result.stderr).split())


def _run(
    tmp_path: Path,
    apm_binary_path: Path,
    args: tuple[str, ...],
    dependency: str,
    *,
    registry_only: bool = True,
    github_origin: bool = False,
    extra_env: dict[str, str] | None = None,
) -> _EgressSnapshot:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text(
        f"name: consumer\nversion: 0.0.1\ndependencies:\n  apm:\n    - {dependency}\n",
        encoding="utf-8",
    )
    if github_origin:
        # Policy discovery derives the org from the project's git remote.
        for command in (
            ("git", "init", "-q"),
            ("git", "remote", "add", "origin", "https://github.com/acme/consumer.git"),
        ):
            subprocess.run(command, cwd=project, check=True)
    git_log = isolated.root / "git-network.log"
    bin_dir = isolated.root / "shim-bin"
    _install_git_shim(bin_dir, git_log)
    # A host-installed Azure CLI sends its own telemetry; credential lookup
    # must see "no az" so the snapshot records only APM's egress.
    az = bin_dir / "az"
    az.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    az.chmod(0o755)

    with _recording_proxy() as proxy:
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        overrides = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PROXY_REGISTRY_URL": _PROXY_URL,
            "PROXY_REGISTRY_ALLOW_HTTP": "1",
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "NO_PROXY": "",
            "APM_E2E_TESTS": "1",
            **(extra_env or {}),
        }
        if registry_only:
            overrides["PROXY_REGISTRY_ONLY"] = "1"
        environment = isolated.subprocess_env(overrides=overrides)
        if not registry_only:
            environment.pop("PROXY_REGISTRY_ONLY", None)
            environment.pop("ARTIFACTORY_ONLY", None)
        if not environment.get("APM_E2E_TESTS"):
            environment.pop("APM_E2E_TESTS", None)
        environment["APM_TEST_LOOPBACK_PORTS"] = str(proxy.server_port)
        result = ApmLifecycleRunner((str(apm_binary_path),)).run(
            args, scenario_id="egress-route-policy", cwd=project, env=environment
        )
    git_network = (
        tuple(git_log.read_text(encoding="utf-8").splitlines()) if git_log.exists() else ()
    )
    proxied = tuple(
        target for _method, target in proxy.requests if urlparse(target).hostname == _PROXY_HOST
    )
    upstream = tuple(
        f"{method} {target}"
        for method, target in proxy.requests
        if urlparse(target).hostname != _PROXY_HOST
    )
    return _EgressSnapshot(result, project, git_network, proxied, upstream)


@pytest.mark.parametrize("host_kind", sorted(_PREFLIGHT_HOSTS))
def test_update_preflight_sends_no_source_host_probe(
    tmp_path: Path, apm_binary_path: Path, host_kind: str
) -> None:
    """``apm install --update`` probes no source host; the archive comes from the proxy."""
    snapshot = _run(tmp_path, apm_binary_path, _UPDATE, _PREFLIGHT_HOSTS[host_kind])

    assert snapshot.result.returncode == 0, snapshot.output
    assert snapshot.git_network == (), snapshot.output
    assert snapshot.upstream == (), snapshot.output
    assert snapshot.proxied, "the archive must be fetched through the registry proxy"
    lockfile = LockFile.read(snapshot.project / "apm.lock.yaml")
    assert lockfile is not None
    assert {dep.host for dep in lockfile.dependencies.values()} == {_PROXY_HOST}


def test_update_preflight_probes_source_host_when_mode_is_off(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Sentinel: without the mode the preflight probe is observed by the git shim."""
    snapshot = _run(
        tmp_path, apm_binary_path, _UPDATE, _PREFLIGHT_HOSTS["gitlab"], registry_only=False
    )

    assert any("ls-remote" in call for call in snapshot.git_network), snapshot.output


def test_policy_fetch_goes_direct_in_registry_only_mode(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The documented exception: the org policy fetch goes direct, and says so once."""
    snapshot = _run(
        tmp_path,
        apm_binary_path,
        ("install", "--target", "copilot"),
        "acme/pkg#1.0.0",
        github_origin=True,
    )

    assert snapshot.result.returncode == 0, snapshot.output
    assert snapshot.output.count("org policy is fetched directly from https://api.github.com") == 1
    assert "Could not fetch org policy" in snapshot.output
    assert snapshot.git_network == (), snapshot.output
    assert set(snapshot.upstream) == {"CONNECT api.github.com:443"}, snapshot.output
    assert snapshot.proxied, "package archives still go through the registry proxy"


def _release_requests(snapshot: _EgressSnapshot) -> list[str]:
    return [request for request in snapshot.upstream if _RELEASE_HOST in request]


@pytest.mark.parametrize("registry_only", [True, False], ids=["registry-only", "mode-off"])
def test_automatic_version_check_is_not_sent_in_registry_only_mode(
    tmp_path: Path, apm_binary_path: Path, registry_only: bool
) -> None:
    """The automatic version check is skipped (verbose note); mode off is the sentinel."""
    snapshot = _run(
        tmp_path,
        apm_binary_path,
        ("-v", "install", "--target", "copilot"),
        "acme/pkg#1.0.0",
        registry_only=registry_only,
        github_origin=True,
        extra_env={**_RELEASE_ENV, "APM_E2E_TESTS": ""},
    )

    if registry_only:
        assert snapshot.result.returncode == 0, snapshot.output
        assert snapshot.git_network == (), snapshot.output
        assert _release_requests(snapshot) == [], snapshot.output
        assert "Update check skipped: PROXY_REGISTRY_ONLY is set" in snapshot.output
    else:
        # Sentinel: without the mode the check reaches the release host.
        assert _release_requests(snapshot) == [f"CONNECT {_RELEASE_HOST}:443"], snapshot.output
        assert "Update check skipped" not in snapshot.output


def test_version_check_note_is_verbose_only(tmp_path: Path, apm_binary_path: Path) -> None:
    snapshot = _run(
        tmp_path,
        apm_binary_path,
        ("install", "--target", "copilot"),
        "acme/pkg#1.0.0",
        github_origin=True,
        extra_env={**_RELEASE_ENV, "APM_E2E_TESTS": ""},
    )

    assert snapshot.result.returncode == 0, snapshot.output
    assert _release_requests(snapshot) == [], snapshot.output
    assert "Update check skipped" not in snapshot.output


def test_explicit_self_update_check_goes_direct_in_registry_only_mode(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``apm self-update`` is user-initiated: its release lookup goes direct."""
    snapshot = _run(
        tmp_path,
        apm_binary_path,
        ("self-update", "--check"),
        "acme/pkg#1.0.0",
        github_origin=True,
        extra_env=_RELEASE_ENV,
    )

    assert snapshot.git_network == (), snapshot.output
    assert _release_requests(snapshot) == [f"CONNECT {_RELEASE_HOST}:443"], snapshot.output
    assert snapshot.proxied == (), snapshot.output
