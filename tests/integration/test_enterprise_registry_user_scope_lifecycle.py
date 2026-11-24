"""APMLifecycle: the enterprise registry marketplace at user scope (#3299, ``-g`` stage).

The user-scope variant of the enterprise scenario: an employee installs a
registry-backed company catalog entry into their user scope (``install -g``),
a teammate's cold HOME replays it from the committed user manifest and lock
alone (``install -g --frozen``), and ``update -g`` takes a newer registry version
the catalog's range allows. The registry is the scripted gateway the
catalog-identity tests use (#4009); the catalog is a local file.

Every step asserts the exit code, the declared form, the lock row (source,
version, resolved URL and hash), the deployed bytes under HOME, which hosts
were contacted, and that nothing was written to the working directory (user
scope never writes project files).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import load_yaml
from tests.integration.test_registry_redirect_origin_lifecycle import (
    BASE,
    REGISTRY_HOST,
    RegistryGateway,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_package_registry import flat_registry_archive
from tests.utils.local_scripted_https_proxy import (
    ScriptedHttpsProxy,
    local_scripted_https_proxy,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise("P-OWN-marketplace-locked-selection", "req-lk-003", "req-lk-019"),
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
]

_CATALOG = "corp"
_PLUGIN = "widget"
_PACKAGE = "acme/widget"
_REGISTRY = "fixture"
_REGISTRY_URL = f"https://{REGISTRY_HOST}{BASE}"
# Copilot's user-scope skill surface (tests/lifecycle/machines/_kit.py USER_SURFACE_GLOBS).
_SKILL = Path(".agents/skills/widget/SKILL.md")
_INSTALL = ("install", "-g", "--target", "copilot", "--no-policy")


def _skill(version: str) -> str:
    return f"---\nname: widget\ndescription: Widget skill {version}\n---\n\n# Widget {version}\n"


def _archive(version: str) -> bytes:
    return flat_registry_archive(
        [
            ("apm.yml", f"name: widget\nversion: {version}\ndescription: fixture\n"),
            (".apm/skills/widget/SKILL.md", _skill(version)),
        ]
    )


def _text(result: CommandResult) -> str:
    return (
        f"command={result.command[1:]!r} rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    )


class _Home:
    """One person's isolated HOME and an empty working directory."""

    def __init__(self, tmp_path: Path, label: str, binary: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / label, base_env=dict(os.environ))
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=180)
        self.cwd = self.isolated.work_root / "anywhere"
        self.cwd.mkdir(parents=True)

    @property
    def manifest(self) -> Path:
        return self.isolated.config_root / "apm.yml"

    @property
    def lock(self) -> Path:
        return self.isolated.config_root / "apm.lock.yaml"

    @property
    def skill(self) -> Path:
        return self.isolated.home / _SKILL

    def env(self, proxy: ScriptedHttpsProxy) -> dict[str, str]:
        env = proxy.child_environment(self.isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        env["APM_E2E_TESTS"] = "1"
        return env

    def run(self, env: dict[str, str], *args: str, step: str) -> CommandResult:
        return self.runner.run(args, scenario_id=step, cwd=self.cwd, env=env)

    def ok(self, env: dict[str, str], *args: str, step: str) -> CommandResult:
        result = self.run(env, *args, step=step)
        assert result.returncode == 0, f"step={step}\n{_text(result)}"
        return result

    def provision(self, env: dict[str, str]) -> None:
        """Enable registries and map the company registry in user config."""
        self.ok(env, "experimental", "enable", "registries", step="enable-registries")
        self.ok(env, "config", "set", f"registry.{_REGISTRY}.url", _REGISTRY_URL, step="registry")

    def locked(self):
        return LockFile.read(self.lock).get_dependency(_PACKAGE)

    def assert_cwd_untouched(self) -> None:
        assert sorted(path.name for path in self.cwd.iterdir()) == [], (
            "a user-scope command wrote into the working directory"
        )


def _assert_registry_row(locked, gateway: RegistryGateway, version: str) -> None:
    """The row pins the selected version's download endpoint and the seeded archive digest."""
    assert locked is not None
    assert locked.source == "registry"
    assert locked.version == version
    url = urlparse(locked.resolved_url or "")
    assert (url.scheme, url.hostname, url.path) == (
        "https",
        REGISTRY_HOST,
        gateway.registry.download_path(_PACKAGE, version),
    ), locked.resolved_url
    assert locked.resolved_hash == gateway.registry.artifact(_PACKAGE, version).digest
    assert not locked.resolved_commit
    assert locked.discovered_via == _CATALOG
    assert locked.marketplace_plugin_name == _PLUGIN


def _registry_only(proxy: ScriptedHttpsProxy, since: int) -> None:
    hosts = {o.host for o in proxy.observations[since:]}
    assert hosts <= {REGISTRY_HOST}, f"unexpected hosts contacted: {sorted(hosts)}"


def _catalog(root: Path) -> Path:
    entry = {
        "name": _PLUGIN,
        "description": "Registry-backed widget",
        "version": "^1.0.0",
        "registry": _REGISTRY,
        "source": {"source": "github", "repo": _PACKAGE},
    }
    root.mkdir(parents=True)
    (root / "marketplace.json").write_text(
        json.dumps({"name": _CATALOG, "plugins": [entry]}), encoding="utf-8"
    )
    return root


def test_user_scope_catalog_registry_install_replays_cold_and_updates(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    gateway = RegistryGateway()
    gateway.registry.seed(_PACKAGE, "1.0.0", _archive("1.0.0"))
    employee = _Home(tmp_path, "employee", apm_binary_path)
    teammate = _Home(tmp_path, "teammate", apm_binary_path)
    catalog = _catalog(tmp_path / "catalog")
    with local_scripted_https_proxy(tmp_path / "tls", {REGISTRY_HOST: gateway.handle}) as proxy:
        # Employee: install the catalog entry into the user scope.
        env = employee.env(proxy)
        employee.provision(env)
        employee.ok(env, "marketplace", "add", str(catalog), "--name", _CATALOG, step="catalog")
        baseline = len(proxy.observations)
        employee.ok(env, *_INSTALL, f"{_PLUGIN}@{_CATALOG}", step="1-install-g")
        _registry_only(proxy, baseline)
        declared = load_yaml(employee.manifest)["dependencies"]["apm"]
        assert declared == [{"id": _PACKAGE, "registry": _REGISTRY, "version": "^1.0.0"}]
        _assert_registry_row(employee.locked(), gateway, "1.0.0")
        assert employee.skill.read_text(encoding="utf-8") == _skill("1.0.0")
        employee.assert_cwd_untouched()

        # Teammate: a cold HOME with only the user manifest and lock; no catalog.
        mates = teammate.env(proxy)
        teammate.provision(mates)
        teammate.isolated.config_root.mkdir(parents=True, exist_ok=True)
        for source, target in (
            (employee.manifest, teammate.manifest),
            (employee.lock, teammate.lock),
        ):
            shutil.copyfile(source, target)
        lock_bytes = teammate.lock.read_bytes()
        shutil.rmtree(catalog)
        baseline = len(proxy.observations)
        teammate.ok(mates, *_INSTALL, "--frozen", step="2-frozen-cold-g")
        _registry_only(proxy, baseline)
        assert teammate.lock.read_bytes() == lock_bytes
        assert teammate.skill.read_text(encoding="utf-8") == _skill("1.0.0")
        teammate.assert_cwd_untouched()

        # A newer version the catalog's range allows: update -g takes it.
        gateway.registry.seed(_PACKAGE, "1.1.0", _archive("1.1.0"))
        _catalog(catalog)
        baseline = len(proxy.observations)
        employee.ok(env, "update", "-g", "--yes", step="3-update-g")
        _registry_only(proxy, baseline)
        _assert_registry_row(employee.locked(), gateway, "1.1.0")
        assert employee.skill.read_text(encoding="utf-8") == _skill("1.1.0")
        assert load_yaml(employee.manifest)["dependencies"]["apm"] == declared
        employee.assert_cwd_untouched()
