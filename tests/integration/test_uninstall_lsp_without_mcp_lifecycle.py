"""Uninstall removes LSP servers when the scope has no MCP state (#4045, #3768).

``apm uninstall`` loaded the post-removal manifest only for MCP cleanup, so a
scope with recorded LSP servers but no MCP state reached the LSP reconciler
without a manifest: the command exited 1 and left the server configured. These
scenarios drive the installed CLI at project scope and at user scope (with
``APM_HOME`` outside ``HOME``) and pin that the server leaves the target config
and every lockfile LSP view, while a surviving package keeps its server.
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

_MANIFEST_ERROR = "without a valid project manifest"


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
    def lsp_config(self) -> Path:
        if self.user:
            return self.scene.home / ".copilot" / "lsp-config.json"
        return self.scene.project / ".github" / "lsp.json"

    def declare(self, *names: str) -> None:
        manifest = {
            "name": "lsp-consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [str(self.packages[n].root) for n in names]},
        }
        (self.manifest_root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    def install(self, *extra: str) -> None:
        result = self.scene.install("copilot", user=self.user, extra=extra)
        assert result.returncode == 0, describe(result)

    def uninstall(self, name: str):
        args = ["uninstall", str(self.packages[name].root)]
        if self.user:
            args.append("--global")
        return self.scene.run(*args)

    def configured(self) -> set[str]:
        if not self.lsp_config.exists():
            return set()
        return set(json.loads(self.lsp_config.read_text(encoding="utf-8")).get("lspServers", {}))

    def locked_views(self) -> dict[str, set[str]]:
        lock_path = self.manifest_root / "apm.lock.yaml"
        lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {}
        lock = lock or {}
        return {
            "lsp_servers": set(lock.get("lsp_servers") or []),
            "lsp_configs": set(lock.get("lsp_configs") or {}),
            "lsp_target_servers": {
                name for names in (lock.get("lsp_target_servers") or {}).values() for name in names
            },
            "lsp_config_provenance": set(lock.get("lsp_config_provenance") or {}),
            "deployments": {
                str(row.get("value"))
                for row in lock.get("deployments") or []
                if row.get("target") == "lsp"
            },
        }


@pytest.fixture(params=[False, True], ids=["project", "user-apm-home-outside-home"])
def scope(
    request: pytest.FixtureRequest, tmp_path: Path, apm_binary_path: Path
) -> Iterator[_Scope]:
    scene = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    apm_home = scene.isolated.root / "apm-home"
    apm_home.mkdir()
    if request.param:
        # User scope with APM_HOME outside HOME (#4045).
        scene.env["APM_HOME"] = str(apm_home)
        assert not apm_home.is_relative_to(scene.home)
    factory = LocalPackageFactory(scene.isolated.package_root)
    packages = {
        "lsp-kit": factory.create("lsp-kit", lsp_dependencies=(_lsp("kit-lsp"),)),
        "keep-kit": factory.create("keep-kit", lsp_dependencies=(_lsp("keep-lsp"),)),
        "mixed-kit": factory.create(
            "mixed-kit",
            lsp_dependencies=(_lsp("mixed-lsp"),),
            mcp_dependencies=(
                {
                    "name": "mixed-mcp",
                    "registry": False,
                    "transport": "http",
                    "url": "https://example.invalid/mixed",
                },
            ),
        ),
    }
    try:
        yield _Scope(scene, request.param, packages, apm_home)
    finally:
        scene.close()


def test_lsp_only_uninstall_removes_the_server_and_keeps_survivors(scope: _Scope) -> None:
    scope.declare("lsp-kit", "keep-kit")
    scope.install()
    assert scope.configured() == {"kit-lsp", "keep-lsp"}
    assert all(view == {"kit-lsp", "keep-lsp"} for view in scope.locked_views().values()), (
        scope.locked_views()
    )

    result = scope.uninstall("lsp-kit")

    assert result.returncode == 0, describe(result)
    assert _MANIFEST_ERROR not in flat(result.stdout + result.stderr)
    assert scope.configured() == {"keep-lsp"}
    assert all(view == {"keep-lsp"} for view in scope.locked_views().values()), scope.locked_views()

    result = scope.uninstall("keep-kit")

    assert result.returncode == 0, describe(result)
    assert scope.configured() == set()
    assert all(view == set() for view in scope.locked_views().values()), scope.locked_views()


def test_mixed_mcp_and_lsp_package_still_uninstalls_cleanly(scope: _Scope) -> None:
    scope.declare("mixed-kit", "keep-kit")
    scope.install("--trust-transitive-mcp")
    assert scope.configured() == {"mixed-lsp", "keep-lsp"}

    result = scope.uninstall("mixed-kit")

    assert result.returncode == 0, describe(result)
    assert scope.configured() == {"keep-lsp"}
    assert all(view == {"keep-lsp"} for view in scope.locked_views().values()), scope.locked_views()


def test_unreadable_target_config_still_fails_closed(scope: _Scope) -> None:
    scope.declare("lsp-kit")
    scope.install()
    foreign = b'{"lspServers": {"kit-lsp": {"command": "hand-edited"'
    scope.lsp_config.write_bytes(foreign)

    result = scope.uninstall("lsp-kit")

    assert result.returncode != 0, describe(result)
    output = flat(result.stdout + result.stderr)
    assert "LSP cleanup failed" in output, describe(result)
    assert _MANIFEST_ERROR not in output, describe(result)
    assert scope.lsp_config.read_bytes() == foreign
