"""Registry-only mode installs exact pins idempotently through the proxy (#3471, #3649).

CHANGELOG promise: under ``PROXY_REGISTRY_ONLY`` "exact tags and locked
resolutions still install through the proxy". Chain: install an exact pin
through the proxy, reinstall without lockfile churn, and ``apm view
versions`` / ``outdated`` keep refusing enumeration -- all without
contacting the source host.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.integration.test_registry_only_enumeration_contract import (
    _PACKAGE,
    _PROXY_HOST,
    _PROXY_URL,
    _install_git_shim,
    _recording_proxy,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX git shim"),
]


class _RegistryOnlyChain:
    """One consumer installing ``acme/pkg`` through the recording proxy."""

    def __init__(self, tmp_path: Path, apm_binary_path: Path, ref: str = "1.0.0") -> None:
        self.isolated = IsolatedApmEnvironment.create(
            tmp_path / "scenario", base_env=dict(os.environ)
        )
        self.project = self.isolated.work_root / "consumer"
        self.project.mkdir()
        self.manifest = self.project / "apm.yml"
        self.manifest.write_text(
            f"name: consumer\nversion: 0.0.1\ndependencies:\n  apm:\n    - {_PACKAGE}#{ref}\n",
            encoding="utf-8",
        )
        self.git_log = self.isolated.root / "git-network.log"
        self.bin_dir = self.isolated.root / "shim-bin"
        _install_git_shim(self.bin_dir, self.git_log)
        self.runner = ApmLifecycleRunner((str(apm_binary_path),))

    def env(self, proxy) -> dict[str, str]:
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        env = self.isolated.subprocess_env(
            overrides={
                "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "PROXY_REGISTRY_URL": _PROXY_URL,
                "PROXY_REGISTRY_ALLOW_HTTP": "1",
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "NO_PROXY": "",
                "APM_E2E_TESTS": "1",
                "PROXY_REGISTRY_ONLY": "1",
            }
        )
        env["APM_TEST_LOOPBACK_PORTS"] = str(proxy.server_port)
        return env

    def run(self, env, *args: str, step: str) -> tuple[int, str]:
        result = self.runner.run(args, scenario_id=step, cwd=self.project, env=env)
        return result.returncode, " ".join((result.stdout + result.stderr).split())

    def no_upstream(self, proxy) -> None:
        assert not self.git_log.exists() or self.git_log.read_text(encoding="utf-8") == ""
        upstream = [
            (method, target)
            for method, target in proxy.requests
            if urlparse(target).hostname != _PROXY_HOST
        ]
        assert upstream == []


_INSTALL = ("install", "--target", "copilot", "--no-policy")


def _changed_archive() -> bytes:
    """The same package path served with different instruction bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pkg-1.0.0/", "")
        archive.writestr("pkg-1.0.0/apm.yml", "name: pkg\nversion: 1.0.0\n")
        archive.writestr(
            "pkg-1.0.0/.apm/instructions/guide.instructions.md",
            "---\napplyTo: '**'\n---\nChanged behind the same ref.\n",
        )
    return buffer.getvalue()


def _deployed_bytes(project: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in project.joinpath(".github").rglob("*.md")}


def test_registry_only_exact_pin_chain_is_idempotent(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = _RegistryOnlyChain(tmp_path, apm_binary_path)
    with _recording_proxy() as proxy:
        env = chain.env(proxy)
        rc, text = chain.run(env, *_INSTALL, step="1-exact")
        assert rc == 0, text
        locked = LockFile.read(chain.project / "apm.lock.yaml").get_dependency(_PACKAGE)
        assert locked is not None and locked.resolved_ref == "1.0.0"
        assert locked.host == _PROXY_HOST
        lock_bytes = (chain.project / "apm.lock.yaml").read_bytes()

        rc, text = chain.run(env, *_INSTALL, step="2-reinstall")
        assert rc == 0, text
        assert (chain.project / "apm.lock.yaml").read_bytes() == lock_bytes, "lock churn"

        rc, text = chain.run(env, "view", _PACKAGE, "versions", step="3-view")
        assert rc == 1 and "registry-only mode" in text, text

        # Proxy-routed locks (registry_prefix) are skipped by outdated by design.
        rc, text = chain.run(env, "outdated", "-j", "0", step="4-outdated")
        assert rc == 0, text
        chain.no_upstream(proxy)


@pytest.mark.lifecycle_smoke
@pytest.mark.parametrize("ref", ["1.0.0", "v1.0.0"])
def test_registry_only_frozen_replays_lock_written_through_proxy(
    tmp_path: Path, apm_binary_path: Path, ref: str
) -> None:
    """--frozen replays the proxy-routed lock the same proxy just wrote (#3657, #4172)."""
    chain = _RegistryOnlyChain(tmp_path, apm_binary_path, ref)
    frozen = ("install", "--frozen", "--target", "copilot", "--no-policy")
    with _recording_proxy() as proxy:
        env = chain.env(proxy)
        rc, text = chain.run(env, *_INSTALL, step="1-exact")
        assert rc == 0, text
        lock_bytes = (chain.project / "apm.lock.yaml").read_bytes()

        rc, text = chain.run(env, *frozen, step="2-frozen")
        assert rc == 0, text
        assert (chain.project / "apm.lock.yaml").read_bytes() == lock_bytes

        # Fresh CI runner: no materialized packages, same lock.
        rc, text = chain.run(env, "cache", "clean", "--yes", step="3-cache-clean")
        assert rc == 0, text
        for path in sorted((chain.project / "apm_modules").rglob("*"), reverse=True):
            path.unlink() if path.is_file() or path.is_symlink() else path.rmdir()
        rc, text = chain.run(env, *frozen, step="4-frozen-cold")
        assert rc == 0, text
        assert (chain.project / "apm.lock.yaml").read_bytes() == lock_bytes
        chain.no_upstream(proxy)

        # A different proxy route is a different source: --frozen still refuses.
        moved = dict(env)
        moved["PROXY_REGISTRY_URL"] = _PROXY_URL.replace(_PROXY_HOST, "other-proxy.example.invalid")
        rc, text = chain.run(moved, *frozen, step="5-frozen-other-proxy")
        assert rc == 1, text
        assert "out of sync" in text, text
        assert (chain.project / "apm.lock.yaml").read_bytes() == lock_bytes


def test_registry_only_frozen_refuses_proxy_archive_that_changed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A proxy lock row without a commit is still verified by its content hash (#4172)."""
    chain = _RegistryOnlyChain(tmp_path, apm_binary_path, "v1.0.0")
    with _recording_proxy() as proxy:
        env = chain.env(proxy)
        rc, text = chain.run(env, "install", "--target", "copilot", step="1-tag")
        assert rc == 0, text
        locked = LockFile.read(chain.project / "apm.lock.yaml").get_dependency(_PACKAGE)
        assert locked is not None and locked.resolved_commit is None
        assert locked.registry_prefix == "artifactory/github" and locked.content_hash
        lock_bytes = (chain.project / "apm.lock.yaml").read_bytes()
        deployed = _deployed_bytes(chain.project)

        proxy.archive = _changed_archive()
        rc, text = chain.run(env, "cache", "clean", "--yes", step="2-cache-clean")
        assert rc == 0, text
        shutil.rmtree(chain.project / "apm_modules")
        rc, text = chain.run(env, "install", "--frozen", "--target", "copilot", step="3-frozen")
        assert rc == 1, text
        assert f"Content hash mismatch for {_PACKAGE}: expected {locked.content_hash}" in text, text
        assert (chain.project / "apm.lock.yaml").read_bytes() == lock_bytes
        assert _deployed_bytes(chain.project) == deployed
        chain.no_upstream(proxy)
