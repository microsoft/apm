"""OpenCode receives LSP servers; unsupported targets fail before writing (#3727, 0.36.1).

``dependencies.lsp`` lands in the ``lsp`` key of ``opencode.json`` (project)
or ``~/.config/opencode/opencode.json`` (user), keeping unrelated keys, and a
user's ``"lsp": false`` is refused without rewriting the file. A target set
with no LSP-capable target (``--target cursor``) fails BEFORE any target write:
no deployed skills, no lockfile, no ``.gitignore`` change (approved decision F1).
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

_LSP = {
    "name": "kit-lsp",
    "command": "inert-language-server",
    "args": ["--stdio"],
    "extensionToLanguage": {".fixture": "fixture"},
}
_EXPECTED_ENTRY = {"command": ["inert-language-server", "--stdio"], "extensions": [".fixture"]}


@dataclass
class _LspScene:
    scene: HarnessScene
    package: LocalPackage

    @property
    def project(self) -> Path:
        return self.scene.project

    def declare(self, *, user: bool = False) -> None:
        root = self.scene.manifest_root(user)
        manifest = {
            "name": "lsp-consumer",
            "version": "1.0.0",
            "dependencies": {"apm": [str(self.package.root)]},
        }
        (root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


@pytest.fixture
def lsp_scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[_LspScene]:
    scene = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    factory = LocalPackageFactory(scene.isolated.package_root)
    package = factory.create("lsp-kit", lsp_dependencies=(_LSP,))
    factory.add_skill(package, "kit-skill", "---\nname: kit-skill\ndescription: kit\n---\nBody.\n")
    try:
        yield _LspScene(scene, package)
    finally:
        scene.close()


def _opencode(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fresh_project_writes_lsp_to_opencode_json(lsp_scene: _LspScene) -> None:
    lsp_scene.declare()

    result = lsp_scene.scene.install("opencode", user=False)

    assert result.returncode == 0, describe(result)
    config = _opencode(lsp_scene.project / "opencode.json")
    assert config["lsp"] == {"kit-lsp": _EXPECTED_ENTRY}
    assert "built-in" in flat(result.stdout).lower(), "no note that lsp enables built-ins"
    lock = lsp_scene.scene.lockfile(user=False)
    assert lock.get("lsp_target_servers", {}).get("opencode") == ["kit-lsp"]

    before = (lsp_scene.project / "opencode.json").read_bytes()
    lsp_scene.scene.ok(*result.command[1:])
    assert (lsp_scene.project / "opencode.json").read_bytes() == before


def test_existing_opencode_json_keeps_schema_and_mcp(lsp_scene: _LspScene) -> None:
    existing = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"mine": {"type": "local", "command": ["mine-mcp"]}},
        "theme": "fixture",
    }
    (lsp_scene.project / "opencode.json").write_text(json.dumps(existing), encoding="utf-8")
    lsp_scene.declare()

    result = lsp_scene.scene.install("opencode", user=False)

    assert result.returncode == 0, describe(result)
    config = _opencode(lsp_scene.project / "opencode.json")
    assert {key: config[key] for key in existing} == existing
    assert config["lsp"] == {"kit-lsp": _EXPECTED_ENTRY}


def test_lsp_false_in_opencode_json_is_refused_untouched(lsp_scene: _LspScene) -> None:
    content = json.dumps({"lsp": False, "theme": "fixture"}, indent=2) + "\n"
    (lsp_scene.project / "opencode.json").write_text(content, encoding="utf-8")
    lsp_scene.declare()

    result = lsp_scene.scene.install("opencode", user=False)

    assert result.returncode != 0, describe(result)
    assert "opencode.json" in flat(result.stdout + result.stderr)
    assert (lsp_scene.project / "opencode.json").read_text(encoding="utf-8") == content


def test_user_scope_writes_lsp_to_user_opencode_json(lsp_scene: _LspScene) -> None:
    lsp_scene.declare(user=True)

    result = lsp_scene.scene.install("opencode", user=True)

    assert result.returncode == 0, describe(result)
    config = _opencode(lsp_scene.scene.home / ".config" / "opencode" / "opencode.json")
    assert config["lsp"] == {"kit-lsp": _EXPECTED_ENTRY}
    assert not (lsp_scene.project / "opencode.json").exists()


@pytest.mark.xfail(
    strict=True,
    reason="#3768: uninstall leaves LSP entries behind for every target (pre-existing)",
)
def test_uninstall_removes_the_opencode_lsp_entry(lsp_scene: _LspScene) -> None:
    (lsp_scene.project / "opencode.json").write_text(
        json.dumps({"theme": "fixture"}), encoding="utf-8"
    )
    lsp_scene.declare()
    lsp_scene.scene.ok("install", "--no-policy", "--target", "opencode")

    result = lsp_scene.scene.run("uninstall", str(lsp_scene.package.root))

    assert result.returncode == 0, describe(result)
    config = _opencode(lsp_scene.project / "opencode.json")
    assert "kit-lsp" not in (config.get("lsp") or {}), config
    assert config["theme"] == "fixture"


def _project_files(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file() and "apm_modules" not in path.relative_to(project).parts
    }


def test_target_without_lsp_support_fails_before_any_write(lsp_scene: _LspScene) -> None:
    (lsp_scene.project / ".cursor").mkdir()
    (lsp_scene.project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    lsp_scene.declare()
    before = _project_files(lsp_scene.project)
    home_before = {p for p in lsp_scene.scene.home.rglob("*") if p.is_file()}

    result = lsp_scene.scene.install("cursor", user=False)

    assert result.returncode != 0, describe(result)
    assert "LSP" in result.stdout + result.stderr
    assert _project_files(lsp_scene.project) == before, "target files written before LSP gate"
    new_home = {
        p.relative_to(lsp_scene.scene.home).as_posix()
        for p in lsp_scene.scene.home.rglob("*")
        if p.is_file() and p not in home_before
    }
    assert {p for p in new_home if "skills" in p} == set(), new_home
