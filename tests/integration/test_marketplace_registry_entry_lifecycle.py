"""Registry-routed marketplace entries install and replay end to end (#4009).

A catalog entry with ``registry:`` resolves through the package Registry HTTP
API. Installing it by name must persist the registry form of the dependency
(never a Git entry) and lock registry evidence (exact version, resolved URL,
resolved hash, no commit). Both that manifest and an object-form
``{name, marketplace}`` declaration must then replay with ``--frozen`` from
the lock alone: no catalog, no Git, no GitHub, and the locked hash checked
before extraction.

The registry is the scripted gateway from
``tests/integration/test_registry_redirect_origin_lifecycle.py``, used as it
is on main (the shared Registry HTTP API fixture may move to ``tests/utils``).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
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
    ScriptedRequest,
    ScriptedResponse,
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
_DEPLOYED = Path(".github/instructions/guide.instructions.md")
_INSTALL = ("install", "--target", "copilot", "--no-policy")
_FROZEN = ("install", "--frozen", "--target", "copilot", "--no-policy")
# The frozen preflight refuses a declaration whose registry lock row is not
# locked evidence; the precise reason is pinned by the unit tests.
_OUT_OF_SYNC = (
    "--frozen: apm.lock.yaml is out of sync with apm.yml.",
    "- widget@corp: declared source, ref, host provider, or transport differs from apm.lock.yaml",
)


def _widget_archive(guide: str) -> bytes:
    return flat_registry_archive(
        [
            ("apm.yml", "name: widget\nversion: 1.0.0\ndescription: fixture\n"),
            (".apm/instructions/guide.instructions.md", guide),
        ]
    )


class _Scenario:
    """One consumer, one local catalog, one scripted registry."""

    def __init__(self, tmp_path: Path, binary: Path) -> None:
        self.policy: dict[str, object] | None = None
        self.isolated = IsolatedApmEnvironment.create(
            tmp_path / "scenario", base_env=dict(os.environ)
        )
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=180)
        self.gateway = RegistryGateway()
        self.gateway.registry.seed(_PACKAGE, "1.0.0", _widget_archive("# guide\n"))
        self.project = self.isolated.work_root / "consumer"
        self.project.mkdir()
        (self.project / ".github").mkdir()
        (self.project / ".github" / "copilot-instructions.md").write_text("# c\n", encoding="utf-8")
        self.catalog = self.isolated.root / "catalog"
        self.catalog.mkdir()
        entry = {
            "name": _PLUGIN,
            "description": "Registry-backed widget",
            "version": "^1.0.0",
            "registry": _REGISTRY,
            "source": {"source": "github", "repo": _PACKAGE},
        }
        (self.catalog / "marketplace.json").write_text(
            json.dumps({"name": _CATALOG, "plugins": [entry]}), encoding="utf-8"
        )

    def serve(self):
        return local_scripted_https_proxy(
            self.isolated.root / "tls",
            {REGISTRY_HOST: self.gateway.handle, "api.github.com": self._org_policy},
        )

    def _org_policy(self, request: ScriptedRequest) -> ScriptedResponse:
        """Serve ``contoso/.github`` ``apm-policy.yml`` through the Contents API."""
        if self.policy is None or request.path != "/repos/contoso/.github/contents/apm-policy.yml":
            return ScriptedResponse(404)
        content = base64.b64encode(json.dumps(self.policy).encode()).decode()
        body = json.dumps({"encoding": "base64", "content": content}).encode()
        return ScriptedResponse(200, {"Content-Type": "application/json"}, body)

    def adopt_org_policy(self, env: dict[str, str], policy: dict[str, object]) -> None:
        """Make the consumer a ``contoso`` repository governed by *policy*."""
        self.policy = policy
        git = ("git", "-C", str(self.project))
        subprocess.run((*git, "init", "-q"), check=True, env=env)
        subprocess.run(
            (*git, "remote", "add", "origin", "https://github.com/contoso/consumer.git"),
            check=True,
            env=env,
        )

    def env(self, proxy: ScriptedHttpsProxy) -> dict[str, str]:
        env = proxy.child_environment(self.isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        return env

    def run(self, env: dict[str, str], *args: str, step: str) -> CommandResult:
        return self.runner.run(args, scenario_id=step, cwd=self.project, env=env)

    def ok(self, env: dict[str, str], *args: str, step: str) -> None:
        result = self.run(env, *args, step=step)
        assert result.returncode == 0, _text(result)

    def setup(self, env: dict[str, str], dependencies: list[object]) -> None:
        # The first command may run the CLI's daily self-version check; later
        # connections must reach the registry only.
        self.ok(env, "experimental", "enable", "registries", step="enable")
        self.ok(env, "marketplace", "add", str(self.catalog), "--name", _CATALOG, step="catalog")
        self.write_manifest(dependencies)

    def write_manifest(self, dependencies: list[object], *, registries: bool = True) -> None:
        manifest: dict[str, object] = {
            "name": "consumer",
            "version": "1.0.0",
            "description": "registry marketplace fixture",
            "targets": ["copilot"],
            "dependencies": {"apm": dependencies},
        }
        if registries:
            manifest["registries"] = {_REGISTRY: {"url": _REGISTRY_URL}}
        dump_yaml(manifest, self.project / "apm.yml")

    def declared(self) -> list[object]:
        return load_yaml(self.project / "apm.yml")["dependencies"]["apm"]

    def locked(self):
        return LockFile.read(self.project / "apm.lock.yaml").get_dependency(_PACKAGE)

    def lock_bytes(self) -> bytes:
        return (self.project / "apm.lock.yaml").read_bytes()

    def package_bytes(self) -> dict[str, bytes]:
        root = self.project / "apm_modules" / _PACKAGE
        files = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }
        files["<deployed>"] = (self.project / _DEPLOYED).read_bytes()
        return files

    def go_cold(self) -> None:
        """Fresh checkout: no materialized packages and no catalog to read."""
        shutil.rmtree(self.project / "apm_modules", ignore_errors=True)
        shutil.rmtree(self.catalog, ignore_errors=True)


def _text(result: CommandResult) -> str:
    return f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"


def _registry_only(proxy: ScriptedHttpsProxy, since: int = 0) -> None:
    """Assert every connection after *since* went to the registry (no Git or GitHub)."""
    hosts = {o.host for o in proxy.observations[since:]}
    assert hosts <= {REGISTRY_HOST}, f"unexpected hosts contacted: {sorted(hosts)}"


def _assert_registry_row(locked) -> None:
    assert locked is not None
    assert locked.source == "registry"
    assert locked.version == "1.0.0"
    assert locked.resolved_url and locked.resolved_url.startswith(_REGISTRY_URL + "/")
    assert locked.resolved_hash and locked.resolved_hash.startswith("sha256:")
    assert not locked.resolved_commit
    assert locked.discovered_via == _CATALOG
    assert locked.marketplace_plugin_name == _PLUGIN


def test_positional_registry_marketplace_install_replays_frozen(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``apm install widget@corp`` writes the registry form and replays cold."""
    scenario = _Scenario(tmp_path, apm_binary_path)
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.setup(env, [])
        baseline = len(proxy.observations)
        result = scenario.run(env, *_INSTALL, f"{_PLUGIN}@{_CATALOG}#main", step="0-label")
        assert result.returncode != 0, _text(result)
        assert "version 'main' not found" in " ".join(_text(result).split()), _text(result)
        _registry_only(proxy, baseline)
        scenario.ok(env, *_INSTALL, f"{_PLUGIN}@{_CATALOG}", step="1-positional")
        assert scenario.declared() == [{"id": _PACKAGE, "registry": _REGISTRY, "version": "^1.0.0"}]
        _assert_registry_row(scenario.locked())
        lock_bytes = scenario.lock_bytes()
        installed = scenario.package_bytes()
        _registry_only(proxy, baseline)

        scenario.go_cold()
        scenario.ok(env, *_FROZEN, step="2-frozen-cold")
        assert scenario.lock_bytes() == lock_bytes
        assert scenario.package_bytes() == installed

        scenario.go_cold()
        scenario.ok(env, *_INSTALL, step="3-install-cold")
        assert scenario.lock_bytes() == lock_bytes
        assert scenario.package_bytes() == installed
        _registry_only(proxy, baseline)


def test_object_form_registry_marketplace_declaration_replays_frozen(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A ``{name, marketplace}`` declaration replays its registry row cold."""
    scenario = _Scenario(tmp_path, apm_binary_path)
    declaration = {"name": _PLUGIN, "marketplace": _CATALOG}
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.setup(env, [declaration])
        baseline = len(proxy.observations)
        scenario.ok(env, *_INSTALL, step="1-install")
        _assert_registry_row(scenario.locked())
        lock_bytes = scenario.lock_bytes()
        installed = scenario.package_bytes()

        scenario.go_cold()
        scenario.ok(env, *_FROZEN, step="2-frozen-cold")
        assert scenario.lock_bytes() == lock_bytes
        assert scenario.package_bytes() == installed

        scenario.go_cold()
        scenario.ok(env, *_INSTALL, step="3-install-cold")
        assert scenario.lock_bytes() == lock_bytes
        assert scenario.package_bytes() == installed
        _registry_only(proxy, baseline)

        # A selector the locked version still satisfies keeps replaying.
        scenario.write_manifest([{**declaration, "version": "^1.0.0"}])
        scenario.go_cold()
        scenario.ok(env, *_FROZEN, step="4-frozen-compatible-selector")
        assert scenario.lock_bytes() == lock_bytes


def test_registry_marketplace_frozen_replay_fails_closed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Changed selectors, incomplete pins, changed bytes and missing config refuse."""
    scenario = _Scenario(tmp_path, apm_binary_path)
    declaration = {"name": _PLUGIN, "marketplace": _CATALOG}
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.setup(env, [declaration])
        baseline = len(proxy.observations)
        scenario.ok(env, *_INSTALL, step="1-install")
        lock_path = scenario.project / "apm.lock.yaml"
        lock_bytes = scenario.lock_bytes()
        scenario.go_cold()

        scenario.write_manifest([{**declaration, "version": "^2.0.0"}])
        result = scenario.run(env, *_FROZEN, step="2-incompatible-selector")
        assert result.returncode != 0, _text(result)
        assert all(part in " ".join(_text(result).split()) for part in _OUT_OF_SYNC), _text(result)
        scenario.write_manifest([declaration])

        lock = load_yaml(lock_path)
        for row in lock["dependencies"]:
            if row.get("repo_url") == _PACKAGE:
                row.pop("resolved_hash")
        dump_yaml(lock, lock_path)
        result = scenario.run(env, *_FROZEN, step="3-incomplete-pin")
        assert result.returncode != 0, _text(result)
        assert all(part in " ".join(_text(result).split()) for part in _OUT_OF_SYNC), _text(result)
        lock_path.write_bytes(lock_bytes)

        scenario.gateway.registry.download_fault = "wrong-bytes"
        scenario.gateway.registry.substitute_archive = _widget_archive("# tampered\n")
        result = scenario.run(env, *_FROZEN, step="4-changed-bytes-frozen")
        assert result.returncode != 0, _text(result)
        assert not (scenario.project / "apm_modules" / _PACKAGE).exists()
        result = scenario.run(env, *_INSTALL, step="5-changed-bytes")
        assert result.returncode != 0, _text(result)
        assert "sha256 mismatch" in " ".join(_text(result).split()), _text(result)
        assert not (scenario.project / "apm_modules" / _PACKAGE).exists()
        assert lock_path.read_bytes() == lock_bytes

        scenario.write_manifest([declaration], registries=False)
        result = scenario.run(env, *_FROZEN, step="6-missing-registry-frozen")
        assert result.returncode != 0, _text(result)
        result = scenario.run(env, *_INSTALL, step="7-missing-registry")
        assert result.returncode != 0, _text(result)
        assert "registries:" in _text(result), _text(result)
        assert lock_path.read_bytes() == lock_bytes
        _registry_only(proxy, baseline)


def _registry_policy(required: str) -> dict[str, object]:
    return {
        "name": "contoso-policy",
        "version": "1.0.0",
        "enforcement": "block",
        "registry_source": {"require": [required]},
    }


def test_required_registry_policy_applies_to_registry_marketplace_replay(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A required-registry policy judges a replayed row by the registry that serves it."""
    scenario = _Scenario(tmp_path, apm_binary_path)
    declaration = {"name": _PLUGIN, "marketplace": _CATALOG}
    governed = ("install", "--target", "copilot")
    frozen = ("install", "--frozen", "--target", "copilot")
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.setup(env, [declaration])
        scenario.adopt_org_policy(env, _registry_policy(_REGISTRY))
        scenario.ok(env, *governed, step="1-install-governed")
        lock_bytes = scenario.lock_bytes()

        scenario.go_cold()
        scenario.ok(env, *frozen, step="2-frozen-governed")
        assert scenario.lock_bytes() == lock_bytes

        scenario.policy = _registry_policy("other")
        shutil.rmtree(Path(env["APM_CACHE_DIR"]) / "policy_v1")
        scenario.go_cold()
        result = scenario.run(env, *frozen, step="3-frozen-other-registry")
        text = " ".join(_text(result).split())
        assert result.returncode != 0, text
        assert f"sourced from registry '{_REGISTRY}'" in text, text
        assert not (scenario.project / "apm_modules" / _PACKAGE).exists()
