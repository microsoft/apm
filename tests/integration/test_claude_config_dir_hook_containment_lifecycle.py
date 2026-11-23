"""Scripted user-root hooks deploy under a CLAUDE_CONFIG_DIR outside HOME (#4772).

PROMISE: routing.authorized_targets_only -- at user scope, Claude hook scripts are
contained by the resolved Claude configuration root (CLAUDE_CONFIG_DIR when set,
the default under HOME otherwise), so ``apm install -g`` deploys them there while
symlink, traversal and '..' escapes from that root are still refused
(source: #4772 scope record).
CELL: OP=install SRC=root-local PRIM=hook(scripted) SCOPE=user TGT=claude
      CLAUDE_CONFIG_DIR=absent|outside-home
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration._harness_output_kit import HarnessScene, describe
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise("P-INV-I5", "P-INV-I7"),
    pytest.mark.requires_apm_binary,
]

_SCRIPT = "#!/bin/sh\necho guard\n"


def _hook(command: str) -> str:
    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}
    return json.dumps({"hooks": {"PreToolUse": [entry]}}, indent=2) + "\n"


@pytest.fixture
def scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[HarnessScene]:
    result = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    root = result.isolated.config_root
    (root / "apm.yml").write_text(
        "name: user-profile\nversion: 1.0.0\ndependencies:\n  apm: []\n", encoding="utf-8"
    )
    (root / "hooks" / "scripts").mkdir(parents=True)
    (root / "hooks" / "scripts" / "g.sh").write_text(_SCRIPT, encoding="utf-8")
    try:
        yield result
    finally:
        result.close()


def _claude_root(scene: HarnessScene, *, external: bool) -> Path:
    root = scene.home / ".claude"
    if external:
        root = scene.isolated.root / "claude-profile"
        scene.env["CLAUDE_CONFIG_DIR"] = str(root)
    root.mkdir()
    return root


def _install(scene: HarnessScene) -> CommandResult:
    cwd = scene.isolated.work_root / "cwd"
    cwd.mkdir(exist_ok=True)
    result = scene.runner.run(("install", "-g", "--target", "claude"), cwd=cwd, env=scene.env)
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), describe(result)
    return result


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("external", [False, True], ids=["default-root", "outside-home"])
def test_install_g_deploys_a_scripted_hook_under_the_claude_config_root(
    scene: HarnessScene, external: bool
) -> None:
    """The script lands under the resolved Claude root and the command names it."""
    root = _claude_root(scene, external=external)
    (scene.isolated.config_root / "hooks" / "guard.json").write_text(
        _hook("./scripts/g.sh"), encoding="utf-8"
    )

    result = _install(scene)

    assert result.returncode == 0, describe(result)
    scripts = [path for path in root.rglob("g.sh") if path.is_file()]
    assert len(scripts) == 1, sorted(_files(root))
    assert scripts[0].read_text(encoding="utf-8") == _SCRIPT
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"] for entry in settings["hooks"]["PreToolUse"] for hook in entry["hooks"]
    ]
    spellings = {scripts[0].as_posix(), scripts[0].resolve().as_posix()}
    for home in (scene.home, scene.home.resolve()):
        if scripts[0].resolve().is_relative_to(home.resolve()):
            relative = scripts[0].resolve().relative_to(home.resolve()).as_posix()
            spellings |= {f"$HOME/{relative}", f"${{HOME}}/{relative}"}
    assert any(spelling in command for command in commands for spelling in spellings), (
        commands,
        spellings,
    )
    if external:
        assert not (scene.home / ".claude").exists(), sorted(_files(scene.home))


def test_a_symlinked_hooks_dir_under_the_claude_config_root_is_refused(
    scene: HarnessScene,
) -> None:
    """A link inside CLAUDE_CONFIG_DIR that leaves it is an escape, not a deploy root."""
    root = _claude_root(scene, external=True)
    outside = scene.isolated.root / "elsewhere"
    outside.mkdir()
    (root / "hooks").symlink_to(outside, target_is_directory=True)
    (scene.isolated.config_root / "hooks" / "guard.json").write_text(
        _hook("./scripts/g.sh"), encoding="utf-8"
    )

    result = _install(scene)

    assert result.returncode != 0, describe(result)
    assert _files(outside) == set(), describe(result)


def _hook_package(scene: HarnessScene) -> str:
    """Declare a local hook package in the user manifest; return its reference."""
    package = scene.isolated.root / "pkgs" / "hookpkg"
    (package / ".apm" / "hooks").mkdir(parents=True)
    (package / "apm.yml").write_text("name: hookpkg\nversion: 1.0.0\n", encoding="utf-8")
    (package / ".apm" / "hooks" / "guard.json").write_text(_hook("echo guard"), encoding="utf-8")
    (scene.isolated.config_root / "apm.yml").write_text(
        f"name: user-profile\nversion: 1.0.0\ndependencies:\n  apm:\n  - {package.as_posix()}\n",
        encoding="utf-8",
    )
    return package.as_posix()


@pytest.mark.parametrize("external", [False, True], ids=["default-root", "outside-home"])
@pytest.mark.parametrize("preexisting", [False, True], ids=["apm-created", "user-file"])
def test_retiring_the_last_hook_deletes_only_the_settings_file_apm_created(
    scene: HarnessScene, external: bool, preexisting: bool
) -> None:
    """Retiring the last hook deletes settings.json APM created; a user's file is kept (#4945)."""
    root = _claude_root(scene, external=external)
    settings = root / "settings.json"
    if preexisting:
        settings.write_text(json.dumps({"theme": "dark"}, indent=2) + "\n", encoding="utf-8")
    package = _hook_package(scene)
    installed = _install(scene)
    assert installed.returncode == 0, describe(installed)
    assert "PreToolUse" in json.loads(settings.read_text(encoding="utf-8"))["hooks"]

    cwd = scene.isolated.work_root / "cwd"
    retired = scene.runner.run(("uninstall", "-g", package), cwd=cwd, env=scene.env)

    assert retired.returncode == 0, describe(retired)
    for stream in (retired.stdout, retired.stderr):
        assert stream.isascii(), describe(retired)
    if preexisting:
        assert json.loads(settings.read_text(encoding="utf-8")) == {"theme": "dark"}
    else:
        assert not settings.exists(), settings.read_text(encoding="utf-8")


def test_uninstall_never_deletes_through_a_profile_swapped_for_a_symlink(
    scene: HarnessScene,
) -> None:
    """A CLAUDE_CONFIG_DIR replaced by a symlink after install is rewritten, not deleted from."""
    root = _claude_root(scene, external=True)
    package = _hook_package(scene)
    installed = _install(scene)
    assert installed.returncode == 0, describe(installed)
    elsewhere = scene.isolated.root / "elsewhere"
    root.rename(elsewhere)
    root.symlink_to(elsewhere, target_is_directory=True)

    cwd = scene.isolated.work_root / "cwd"
    retired = scene.runner.run(("uninstall", "-g", package), cwd=cwd, env=scene.env)

    assert retired.returncode == 0, describe(retired)
    assert json.loads((elsewhere / "settings.json").read_text(encoding="utf-8")) == {}
