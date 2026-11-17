"""Harness user-root variables relocate every user-scope write (#3745, 0.36.1).

``COPILOT_HOME`` replaces the whole ``~/.copilot`` tree (skills, agents,
instructions, hooks, ``mcp-config.json``, ``lsp-config.json``). With it set to
an absolute directory outside ``$HOME``, ``apm install -g --target copilot``
must write nothing under ``~/.copilot``; ``audit --ci`` and cleanup must use
the same root; unsetting it later leaves the old profile's files in place.
``CLAUDE_CONFIG_DIR`` likewise owns the Claude LSP plugin. A relative value
for any user-root variable (``COPILOT_HOME``, ``CLAUDE_CONFIG_DIR``,
``CODEX_HOME``, ``HERMES_HOME``) fails with an error naming the variable
(approved decision E1) and creates nothing.
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

_MCP = {
    "name": "kit-mcp",
    "registry": False,
    "transport": "http",
    "url": "https://kit.example.invalid/mcp",
}
_LSP = {
    "name": "kit-lsp",
    "command": "inert-language-server",
    "extensionToLanguage": {".fixture": "fixture"},
}


@dataclass
class _UserScene:
    scene: HarnessScene
    package: LocalPackage
    cwd: Path

    @property
    def home(self) -> Path:
        return self.scene.home

    def write_user_manifest(self, *, mcp: bool = True, lsp: bool = True) -> None:
        dependencies: dict = {"apm": [str(self.package.root)]}
        if mcp:
            dependencies["mcp"] = [_MCP]
        if lsp:
            dependencies["lsp"] = [_LSP]
        manifest = {"name": "user-profile", "version": "1.0.0", "dependencies": dependencies}
        path = self.scene.isolated.config_root / "apm.yml"
        path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    def apm(self, *args: str, env: dict[str, str] | None = None):
        return self.scene.runner.run(
            args, cwd=self.cwd, env=env or self.scene.env, scenario_id=" ".join(args)
        )

    def audit(self, env: dict[str, str] | None = None):
        return self.scene.runner.run(
            ("audit", "--ci", "--no-policy"),
            cwd=self.scene.isolated.config_root,
            env=env or self.scene.env,
            scenario_id="audit",
        )


@pytest.fixture
def user_scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[_UserScene]:
    scene = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    factory = LocalPackageFactory(scene.isolated.package_root)
    package = factory.create("copilot-kit")
    factory.add_skill(package, "kit-skill", "---\nname: kit-skill\ndescription: kit\n---\nBody.\n")
    factory.add_agent(package, "kit-agent", "---\nname: kit-agent\ndescription: kit\n---\nAgent.\n")
    factory.add_instruction(
        package, "kit-rule", "---\napplyTo: '**'\ndescription: kit\n---\nRule.\n"
    )
    factory.add_hook(
        package,
        "kit-hook",
        {"version": 1, "hooks": {"sessionStart": [{"type": "command", "bash": "echo kit"}]}},
    )
    cwd = scene.isolated.work_root / "cwd"
    cwd.mkdir()
    try:
        yield _UserScene(scene, package, cwd)
    finally:
        scene.close()


def _files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _servers(path: Path) -> dict:
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    return document.get("mcpServers") or document.get("lspServers") or document.get("servers") or {}


@pytest.mark.parametrize("legacy", [False, True], ids=["default-skills", "legacy-skill-paths"])
def test_copilot_home_receives_every_copilot_user_primitive(
    user_scene: _UserScene, legacy: bool
) -> None:
    copilot_home = user_scene.scene.isolated.root / "profiles" / "copilot-home"
    env = {**user_scene.scene.env, "COPILOT_HOME": str(copilot_home)}
    user_scene.write_user_manifest()
    install = ("install", "-g", "--no-policy", "--target", "copilot")
    install += ("--legacy-skill-paths",) if legacy else ()

    result = user_scene.apm(*install, env=env)

    assert result.returncode == 0, describe(result)
    assert _files(user_scene.home / ".copilot") == set(), "wrote under ~/.copilot"
    files = _files(copilot_home)
    assert "agents/kit-agent.agent.md" in files, files
    assert "instructions/kit-rule.instructions.md" in files, files
    assert any(name.startswith("hooks/") and "kit-hook" in name for name in files), files
    if legacy:
        assert "skills/kit-skill/SKILL.md" in files, files
    assert "kit-mcp" in _servers(copilot_home / "mcp-config.json")
    assert "kit-lsp" in json.dumps(json.loads((copilot_home / "lsp-config.json").read_text()))

    audit = user_scene.audit(env)
    assert audit.returncode == 0, describe(audit)
    snapshot = {name: (copilot_home / name).read_bytes() for name in files}
    user_scene.apm(*install, env=env)
    assert {name: (copilot_home / name).read_bytes() for name in files} == snapshot

    user_scene.write_user_manifest(mcp=False, lsp=False)
    removed = user_scene.apm(*install, env=env)
    assert removed.returncode == 0, describe(removed)
    assert "kit-mcp" not in _servers(copilot_home / "mcp-config.json")
    lsp_path = copilot_home / "lsp-config.json"
    assert not lsp_path.exists() or "kit-lsp" not in lsp_path.read_text(encoding="utf-8")

    uninstall = user_scene.apm("uninstall", "-g", str(user_scene.package.root), env=env)
    assert uninstall.returncode == 0, describe(uninstall)
    leftovers = {
        name
        for name in _files(copilot_home)
        if name.startswith(("agents/", "instructions/", "hooks/", "skills/"))
    }
    assert leftovers == set(), leftovers
    assert _files(user_scene.home / ".copilot") == set()


def test_unsetting_copilot_home_keeps_the_old_profile_files(user_scene: _UserScene) -> None:
    copilot_home = user_scene.scene.isolated.root / "profiles" / "copilot-home"
    moved = {**user_scene.scene.env, "COPILOT_HOME": str(copilot_home)}
    user_scene.write_user_manifest(lsp=False)
    install = ("install", "-g", "--no-policy", "--target", "copilot")
    first = user_scene.apm(*install, env=moved)
    assert first.returncode == 0, describe(first)
    profile_files = {
        name: (copilot_home / name).read_bytes()
        for name in _files(copilot_home)
        if name.startswith(("agents/", "instructions/"))
    }
    assert profile_files, _files(copilot_home)

    default = user_scene.apm(*install)

    assert default.returncode == 0, describe(default)
    assert {name: (copilot_home / name).read_bytes() for name in profile_files} == profile_files
    assert "agents/kit-agent.agent.md" in _files(user_scene.home / ".copilot")


def test_claude_config_dir_owns_the_user_lsp_plugin(user_scene: _UserScene) -> None:
    claude_home = user_scene.scene.isolated.root / "profiles" / "claude-home"
    env = {**user_scene.scene.env, "CLAUDE_CONFIG_DIR": str(claude_home)}
    user_scene.write_user_manifest(mcp=False)

    result = user_scene.apm("install", "-g", "--no-policy", "--target", "claude", env=env)

    assert result.returncode == 0, describe(result)
    plugin = claude_home / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json"
    assert plugin.is_file(), _files(claude_home)
    assert "kit-lsp" in plugin.read_text(encoding="utf-8")
    assert not (user_scene.home / ".claude" / "skills" / "apm-lsp").exists()


@pytest.mark.parametrize(
    ("variable", "target"),
    [
        ("COPILOT_HOME", "copilot"),
        ("CLAUDE_CONFIG_DIR", "claude"),
        ("CODEX_HOME", "codex"),
        ("HERMES_HOME", "hermes"),
    ],
)
def test_relative_user_root_is_rejected_before_any_write(
    user_scene: _UserScene, variable: str, target: str
) -> None:
    env = {**user_scene.scene.env, variable: "relative-profile"}
    user_scene.write_user_manifest(lsp=False)
    before_home = _files(user_scene.home)

    result = user_scene.apm("install", "-g", "--no-policy", "--target", target, env=env)

    assert result.returncode != 0, describe(result)
    assert variable in flat(result.stdout + result.stderr), describe(result)
    assert not (user_scene.cwd / "relative-profile").exists()
    assert not (user_scene.scene.isolated.config_root / "relative-profile").exists()
    native_writes = {
        name
        for name in _files(user_scene.home) - before_home
        if not name.startswith((".apm/", ".apm-local/", ".cache/"))
    }
    assert native_writes == set(), native_writes
