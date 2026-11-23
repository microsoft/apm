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

    def declare(self, *names: str, targets: tuple[str, ...] = ("copilot",)) -> None:
        manifest = {
            "name": "lsp-consumer",
            "version": "1.0.0",
            "targets": list(targets),
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

    def harness_servers(self) -> dict[str, set[str]]:
        """Return the LSP server names each LSP-capable harness config holds."""
        base = self.scene.home if self.user else self.scene.project
        configs = {
            "copilot": (self.lsp_config, "lspServers"),
            "claude": (
                base / ".claude" / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json",
                "lspServers",
            ),
            "opencode": (
                self.scene.home / ".config" / "opencode" / "opencode.json"
                if self.user
                else self.scene.project / "opencode.json",
                "lsp",
            ),
        }
        servers: dict[str, set[str]] = {}
        for harness, (path, key) in configs.items():
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            servers[harness] = set(data.get(key) or {})
        return servers

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


def test_lsp_only_uninstall_clears_every_harness_it_wrote(scope: _Scope) -> None:
    """After uninstall, no LSP-capable harness config keeps the server (#4045)."""
    harnesses = ("copilot", "claude", "opencode")
    scope.declare("lsp-kit", targets=harnesses)
    installed = scope.scene.install(",".join(harnesses), user=scope.user)
    assert installed.returncode == 0, describe(installed)
    assert scope.harness_servers() == {harness: {"kit-lsp"} for harness in harnesses}

    result = scope.uninstall("lsp-kit")

    assert result.returncode == 0, describe(result)
    assert _MANIFEST_ERROR not in flat(result.stdout + result.stderr)
    assert scope.harness_servers() == {harness: set() for harness in harnesses}
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


@pytest.mark.parametrize(
    "cell",
    ["gate-on-approved", "lsp-only-survivor", "denied-lsp"],
    ids=["4161-gate-on", "4214-lsp-only-survivor", "4215-denied-lsp"],
)
def test_uninstalling_another_package_reconciles_lsp_and_exits_zero(
    tmp_path: Path, apm_binary_path: Path, cell: str
) -> None:
    """#4161, #4214, #4215: removing a skill package next to an LSP package exits 0."""
    scene = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    try:
        factory = LocalPackageFactory(scene.isolated.package_root)
        skill = factory.create("skill-kit", targets=("copilot",))
        factory.add_skill(skill, "s", "---\nname: s\ndescription: d\n---\n# s\n")
        lsp = factory.create("lsp-kit", lsp_dependencies=(_lsp("kit-lsp"),))
        manifest: dict = {
            "name": "lsp-consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [str(skill.root), str(lsp.root)]},
        }
        if cell != "lsp-only-survivor":
            manifest["executables"] = {"allow": {}}
        (scene.project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        lsp_config = scene.project / ".github" / "lsp.json"

        assert scene.install("copilot", user=False).returncode == 0
        if cell != "lsp-only-survivor":
            scene.ok("approve", str(lsp.root))
            assert scene.install("copilot", user=False).returncode == 0
        assert "kit-lsp" in json.loads(lsp_config.read_text(encoding="utf-8"))["lspServers"]
        if cell == "denied-lsp":
            scene.ok("deny", str(lsp.root))

        result = scene.run("uninstall", str(skill.root))

        text = flat(result.stdout + result.stderr)
        assert result.returncode == 0, describe(result)
        assert "cleanup is incomplete" not in text and "did not finish" not in text, text
        assert not (scene.project / ".github" / "skills" / "s").exists()
        assert not (scene.project / ".agents" / "skills" / "s").exists()
        lock = yaml.safe_load((scene.project / "apm.lock.yaml").read_text(encoding="utf-8"))
        configured = (
            set(json.loads(lsp_config.read_text(encoding="utf-8")).get("lspServers", {}))
            if lsp_config.exists()
            else set()
        )
        if cell == "denied-lsp":
            assert configured == set(), text
            assert "kit-lsp" in text, text  # the retirement is announced
            assert "kit-lsp" not in set(lock.get("lsp_servers") or []), lock
        else:
            assert configured == {"kit-lsp"}, text
            assert set(lock.get("lsp_servers") or []) == {"kit-lsp"}, lock
    finally:
        scene.close()


def test_lsp_only_package_installs_from_git_like_a_local_path(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4257: one acceptance rule admits an LSP-only package from every source kind."""
    from tests.utils.local_git_repository import LocalGitRepositoryFactory

    scene = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    try:
        source = LocalPackageFactory(scene.isolated.package_root).create(
            "lsp-kit", lsp_dependencies=(_lsp("kit-lsp"),)
        )
        repositories = LocalGitRepositoryFactory(scene.isolated.repository_root, env=scene.env)
        repository = repositories.create("lsp-kit", source_tree=source.root)
        repositories.tag(repository, "v1.0.0", repositories.commit(repository, message="1.0.0"))
        env = repositories.url_rewrite_subprocess_env(
            repository, "https://github.com/acme/lsp-kit.git"
        )
        manifest = {
            "name": "lsp-consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": ["acme/lsp-kit#v1.0.0"]},
        }
        (scene.project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

        result = scene.run("install", "--no-policy", "--target", "copilot", env=env)

        assert result.returncode == 0, describe(result)
        lsp_config = scene.project / ".github" / "lsp.json"
        assert set(json.loads(lsp_config.read_text(encoding="utf-8"))["lspServers"]) == {"kit-lsp"}
        assert scene.run("audit", "--ci", "--no-policy", env=env).returncode == 0
    finally:
        scene.close()
