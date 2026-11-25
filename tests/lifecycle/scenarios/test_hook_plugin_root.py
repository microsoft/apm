"""A plugin hook reads its plugin's files on every target that deploys it (#2824).

PROMISE: P-OWN-plugin-root-hook-command-vocabulary -- a hook that resolves its
plugin root from its own location reaches the plugin's bundled files (source:
issue #2824, Superpowers reproduction; Claude's native plugin root already does).

CELL: OP=install SCOPE=project SRC=native-plugin PRIM=hook TGT=multi (claude, codex).
Codex deploys the hook scripts alone under ``.codex/hooks/<pkg>/``, so a hook
that reads ``<plugin root>/skills/...`` fails there (strict known cell). The
native Claude plugin keeps its root, so its hook is the required control.
"""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.known import KnownFailure
from tests.lifecycle.scenarios._kit import serve_repository
from tests.lifecycle.world import World, use_policy
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-OWN-plugin-root-hook-command-vocabulary"),
]

_PLUGIN = "superpowers"
_SKILL_TEXT = "Use the superpowers skills."
_SESSION_START = """#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cat "${PLUGIN_ROOT}/skills/using-superpowers/SKILL.md"
"""

_CODEX_HOOK = KnownFailure(
    "#2824",
    "the codex hook copy cannot read the skill its plugin bundles",
    r"cannot read its plugin's bundled skill",
)


def _publish_plugin(world: World) -> str:
    """A Claude plugin shaped like obra/superpowers, served as lifecycle/superpowers."""
    root = world.root / "authored" / _PLUGIN
    (root / ".claude-plugin").mkdir(parents=True)
    manifest = {"name": _PLUGIN, "version": "1.0.0", "description": "Lifecycle hook plugin."}
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="ascii")
    hook = {"type": "command", "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/session-start"'}
    hooks = {"hooks": {"SessionStart": [{"hooks": [hook]}]}}
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(json.dumps(hooks, indent=2), encoding="ascii")
    script = root / "hooks" / "session-start"
    script.write_text(_SESSION_START, encoding="ascii")
    script.chmod(0o755)
    skill = root / "skills" / "using-superpowers" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: using-superpowers\ndescription: Lifecycle skill.\n---\n\n{_SKILL_TEXT}\n",
        encoding="ascii",
    )
    repos = LocalGitRepositoryFactory(world.root / "repos", env=world.env)
    repository = repos.create(_PLUGIN, source_tree=root)
    commit = repos.commit(repository, message=f"{_PLUGIN} 1.0.0")
    repos.tag(repository, "v1.0.0", commit)
    serve_repository(world, f"lifecycle/{_PLUGIN}", repository.origin)
    return f"lifecycle/{_PLUGIN}#v1.0.0"


def _install(world: World) -> str:
    result = driver.run(world, ("install",))
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return output


def _deploy(world: World) -> str:
    """Declare the plugin for claude and codex, approve its hooks, reinstall."""
    use_policy(world, "allow")
    spec = _publish_plugin(world)
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = ["claude", "codex"]
    data.setdefault("dependencies", {})["apm"] = [spec]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")

    output = _install(world)
    assert f"'apm approve lifecycle/{_PLUGIN}'" in output, "the hooks park until approved"
    approved = driver.run(world, ("approve", f"lifecycle/{_PLUGIN}"))
    assert approved.returncode == 0, approved.stdout + approved.stderr
    return output + _install(world)


def _run_hook(world: World, hook) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(hook)], cwd=world.project, capture_output=True, text=True, check=False
    )


def test_the_claude_native_hook_reads_the_skill_its_plugin_bundles(lifecycle_world) -> None:
    """Control: the whole native plugin keeps its root, so its hook reads the skill."""
    world = lifecycle_world
    output = _deploy(world)

    hook = world.project / ".agents" / "plugins" / _PLUGIN / "hooks" / "session-start"
    assert hook.is_file(), f"the native claude hook is not deployed\n{output}"
    ran = _run_hook(world, hook)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert _SKILL_TEXT in ran.stdout, ran.stdout + ran.stderr


@_CODEX_HOOK.mark()
def test_the_codex_hook_reads_the_skill_its_plugin_bundles(lifecycle_world) -> None:
    world = lifecycle_world
    output = _deploy(world)

    hook = world.project / ".codex" / "hooks" / _PLUGIN / "hooks" / "session-start"
    codex = sorted(
        p.relative_to(world.project).as_posix() for p in (world.project / ".codex").rglob("*")
    )
    assert hook.is_file(), f"codex hook not deployed; .codex holds {codex}\n{output}"
    ran = _run_hook(world, hook)
    if ran.returncode != 0:
        # #2824 is the copy's missing plugin root only; any other failure fails as itself.
        missing = "skills/using-superpowers/SKILL.md" in ran.stderr and "No such file" in ran.stderr
        assert missing, (
            f"the codex hook failed for another reason: rc={ran.returncode}\n{ran.stderr}"
        )
    else:
        # A hook that runs but prints no skill is another regression, not #2824.
        assert _SKILL_TEXT in ran.stdout, f"the codex hook ran but printed no skill:\n{ran.stdout}"
    assert ran.returncode == 0 and _SKILL_TEXT in ran.stdout, (
        f"the codex hook cannot read its plugin's bundled skill: rc={ran.returncode}\n"
        f"{ran.stdout}{ran.stderr}"
    )
