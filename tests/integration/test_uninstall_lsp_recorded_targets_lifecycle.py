"""Uninstall reconciles LSP servers within the targets APM recorded (#4059).

Uninstall-time LSP reconciliation re-selected targets the way a fresh install
does, from a saved ``apm config target`` default or harness autodetection, and
never read the ``lsp_target_servers`` the lockfile records. A project installed
with ``--target copilot`` and no ``targets:`` failed with "No harness detected",
and a saved default moved a surviving package's server to another harness.
These scenarios drive the installed CLI at project scope and at user scope
(with ``APM_HOME`` outside ``HOME``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.integration._harness_output_kit import HarnessScene, describe, flat
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_NO_HARNESS = "No harness detected"


def _lsp(name: str) -> dict:
    return {
        "name": name,
        "command": "inert-language-server",
        "extensionToLanguage": {f".{name}": name},
    }


@dataclass
class _Scope:
    scene: HarnessScene
    user: bool
    packages: dict[str, LocalPackage]
    apm_home: Path

    @property
    def manifest_root(self) -> Path:
        return self.apm_home if self.user else self.scene.project

    @property
    def lock_path(self) -> Path:
        return self.manifest_root / "apm.lock.yaml"

    @property
    def copilot_config(self) -> Path:
        if self.user:
            return self.scene.home / ".copilot" / "lsp-config.json"
        return self.scene.project / ".github" / "lsp.json"

    @property
    def claude_lsp_root(self) -> Path:
        base = self.scene.home if self.user else self.scene.project
        return base / ".claude" / "skills" / "apm-lsp"

    def declare(self, *names: str, targets: list[str] | None = None) -> None:
        manifest: dict = {"name": "lsp-consumer", "version": "1.0.0"}
        if targets is not None:
            manifest["targets"] = targets
        manifest["dependencies"] = {"apm": [str(self.packages[n].root) for n in names]}
        (self.manifest_root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    def run(self, *args: str):
        return self.scene.run(*args, *(("--global",) if self.user and args[0] != "config" else ()))

    def install_copilot(self) -> None:
        result = self.scene.install("copilot", user=self.user)
        assert result.returncode == 0, describe(result)

    def uninstall(self, name: str):
        return self.run("uninstall", str(self.packages[name].root))

    def configured(self) -> set[str]:
        if not self.copilot_config.exists():
            return set()
        return set(
            json.loads(self.copilot_config.read_text(encoding="utf-8")).get("lspServers", {})
        )

    def claude_configured(self) -> dict[str, dict]:
        plugin = self.claude_lsp_root / ".claude-plugin" / "plugin.json"
        if not plugin.exists():
            return {}
        return json.loads(plugin.read_text(encoding="utf-8")).get("lspServers", {})

    def lock(self) -> dict:
        if not self.lock_path.exists():
            return {}
        return yaml.safe_load(self.lock_path.read_text(encoding="utf-8")) or {}

    def locked_views(self) -> dict[str, set[str]]:
        lock = self.lock()
        return {
            "lsp_servers": set(lock.get("lsp_servers") or []),
            "lsp_configs": set(lock.get("lsp_configs") or {}),
            "lsp_target_servers": {
                name for names in (lock.get("lsp_target_servers") or {}).values() for name in names
            },
            "lsp_config_provenance": set(lock.get("lsp_config_provenance") or {}),
        }

    def recorded_targets(self) -> dict[str, list[str]]:
        return self.lock().get("lsp_target_servers") or {}


@pytest.fixture(params=[False, True], ids=["project", "user-apm-home-outside-home"])
def scope(
    request: pytest.FixtureRequest, tmp_path: Path, apm_binary_path: Path
) -> Iterator[_Scope]:
    scene = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    apm_home = scene.isolated.root / "apm-home"
    apm_home.mkdir()
    if request.param:
        scene.env["APM_HOME"] = str(apm_home)
        assert not apm_home.is_relative_to(scene.home)
    factory = LocalPackageFactory(scene.isolated.package_root)
    packages = {
        "lsp-kit": factory.create("lsp-kit", lsp_dependencies=(_lsp("kit-lsp"),)),
        "keep-kit": factory.create("keep-kit", lsp_dependencies=(_lsp("keep-lsp"),)),
    }
    try:
        yield _Scope(scene, request.param, packages, apm_home)
    finally:
        scene.close()


def _assert_only_survivor_in_copilot(scope: _Scope) -> None:
    assert scope.configured() == {"keep-lsp"}
    assert all(view == {"keep-lsp"} for view in scope.locked_views().values()), scope.locked_views()
    assert scope.recorded_targets() == {"copilot": ["keep-lsp"]}
    assert not scope.claude_lsp_root.exists()


def test_uninstall_without_targets_key_uses_the_recorded_target(scope: _Scope) -> None:
    """``--target copilot`` with no ``targets:`` and no harness marker uninstalls cleanly."""
    scope.declare("lsp-kit", "keep-kit")
    scope.install_copilot()
    assert scope.recorded_targets() == {"copilot": ["keep-lsp", "kit-lsp"]}

    result = scope.uninstall("lsp-kit")

    assert result.returncode == 0, describe(result)
    assert _NO_HARNESS not in flat(result.stdout + result.stderr)
    _assert_only_survivor_in_copilot(scope)

    result = scope.uninstall("keep-kit")

    assert result.returncode == 0, describe(result)
    assert scope.configured() == set()
    assert all(view == set() for view in scope.locked_views().values()), scope.locked_views()


def test_saved_default_target_never_moves_a_surviving_server(scope: _Scope) -> None:
    scope.declare("lsp-kit", "keep-kit")
    scope.install_copilot()
    saved = scope.run("config", "set", "target", "claude")
    assert saved.returncode == 0, describe(saved)

    result = scope.uninstall("lsp-kit")

    assert result.returncode == 0, describe(result)
    _assert_only_survivor_in_copilot(scope)


def test_manifest_targets_still_win_over_the_recorded_target(scope: _Scope) -> None:
    """Control: declared ``targets:`` keep today's behavior, as install does."""
    scope.declare("lsp-kit", "keep-kit", targets=["copilot"])
    scope.install_copilot()
    scope.declare("keep-kit", "lsp-kit", targets=["claude"])

    result = scope.uninstall("lsp-kit")

    assert result.returncode == 0, describe(result)
    assert scope.configured() == set()
    claude = scope.claude_configured()
    assert set(claude) == {"keep-lsp"}, claude
    assert claude["keep-lsp"]["command"] == "inert-language-server"
    assert scope.recorded_targets() == {"claude": ["keep-lsp"]}


def test_lock_without_recorded_targets_keeps_the_fallback(scope: _Scope) -> None:
    """Control: a lock with no ``lsp_target_servers`` still uses today's selection."""
    scope.declare("lsp-kit", "keep-kit")
    scope.install_copilot()
    lock = scope.lock()
    lock.pop("lsp_target_servers")
    scope.lock_path.write_text(yaml.safe_dump(lock), encoding="utf-8")
    saved = scope.run("config", "set", "target", "copilot")
    assert saved.returncode == 0, describe(saved)

    result = scope.uninstall("lsp-kit")

    assert result.returncode == 0, describe(result)
    assert "keep-lsp" in scope.configured()
    assert scope.recorded_targets() == {"copilot": ["keep-lsp"]}


def test_malformed_recorded_target_config_still_fails_closed(scope: _Scope) -> None:
    """Malformed JSON in the recorded target's config fails closed with "Uninstall incomplete".

    A control for the fail-closed path, and partly a regression test: on the
    base, the project case failed earlier with "No harness detected".
    """
    scope.declare("lsp-kit")
    scope.install_copilot()
    foreign = b'{"lspServers": {"kit-lsp": {"command": "hand-edited"'
    scope.copilot_config.write_bytes(foreign)

    result = scope.uninstall("lsp-kit")

    assert result.returncode != 0, describe(result)
    output = flat(result.stdout + result.stderr)
    assert "Uninstall incomplete" in output, describe(result)
    assert _NO_HARNESS not in output, describe(result)
    assert scope.copilot_config.read_bytes() == foreign
