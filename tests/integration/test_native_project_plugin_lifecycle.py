"""APMLifecycle: a multi-harness plugin installs whole at project scope (#3896, #3921).

PROMISE: P-OWN-claude-native-plugin-registration -- an approved plugin is
deployed intact to the committed .agents/plugins/<plugin>/ and declared in
each harness's project settings; it is never split into loose primitives
(source: issue #3921 accepted scope, docs consumer/install-plugins.md).
CELL: OP=install,approve,uninstall SRC=git PRIM=plugin(hooks,skills)
SCOPE=project TGT=claude,copilot GATE=on

The plugin is shaped like obra/superpowers v7.0.2: a Claude manifest using
the default hooks/hooks.json, a Cursor manifest naming hooks/hooks-cursor.json,
and a SessionStart hook that reads a skill through its own plugin root. Split
into primitives, the two hook files collide under the retired filename
routing guard (#3226) and the install exits 1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise(
        "P-OWN-claude-native-plugin-registration", "P-INV-I1", "P-INV-I2", "P-INV-I3"
    ),
]

_PLUGIN = "sp-like"
_DEPENDENCY = "fixture/sp-like"
_SKILL_TEXT = "# Using sp-like\nRead me at session start.\n"
_SESSION_START = (
    "#!/bin/sh\n"
    "# Reads a skill through the plugin root, as superpowers does.\n"
    'cat "$CLAUDE_PLUGIN_ROOT/skills/using-sp-like/SKILL.md"\n'
)


def _hooks(command: str) -> str:
    return json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}},
        indent=2,
    )


_FILES = {
    ".claude-plugin/plugin.json": json.dumps({"name": _PLUGIN, "version": "1.0.0"}),
    ".cursor-plugin/plugin.json": json.dumps(
        {"name": _PLUGIN, "version": "1.0.0", "hooks": "./hooks/hooks-cursor.json"}
    ),
    "hooks/hooks.json": _hooks('"${CLAUDE_PLUGIN_ROOT}/hooks/session-start"'),
    "hooks/hooks-cursor.json": _hooks("./hooks/session-start"),
    "hooks/session-start": _SESSION_START,
    "skills/using-sp-like/SKILL.md": (
        "---\nname: using-sp-like\ndescription: Session primer\n---\n" + _SKILL_TEXT
    ),
}
_SETTINGS = (".claude/settings.json", ".github/copilot/settings.json")


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII, and I9 no duplicate status lines on either stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


def _commit_and_clone_with_crlf(project: Path, env: dict[str, str]) -> Path:
    """Commit *project* and clone it with ``core.autocrlf=true``."""

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    git("init", "-q", cwd=project)
    git("add", "-A", cwd=project)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c", cwd=project)
    clone = project.parent / "crlf-clone"
    git("-c", "core.autocrlf=true", "clone", "-q", str(project), str(clone), cwd=project.parent)
    git("config", "core.autocrlf", "true", cwd=clone)
    return clone


def test_multi_harness_plugin_installs_whole_at_project_scope(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = factory.create("sp-like")
    for relative, text in _FILES.items():
        path = repository.worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
    (repository.worktree / "hooks/session-start").chmod(0o755)
    revision = factory.commit(repository, message="superpowers-shaped plugin").sha
    env = factory.url_rewrite_subprocess_env(repository, f"https://github.com/{_DEPENDENCY}.git")

    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "targets": ["claude", "copilot"],
                "dependencies": {"apm": [{"git": _DEPENDENCY, "ref": revision}]},
                # The approval gate is on and nothing is approved yet.
                "executables": {},
            }
        ),
        encoding="utf-8",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str, expect: int = 0) -> CommandResult:
        result = runner.run(args, cwd=project, env=env)
        _assert_output_oracles(result)
        assert result.returncode == expect, _evidence(result)
        return result

    def snap() -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(project, targets=("claude", "copilot"))

    root = project / ".agents/plugins" / _PLUGIN

    # Unapproved: parked, nothing deployed or declared, and the install succeeds.
    apm("install")
    assert not root.exists()
    assert not any((project / settings).exists() for settings in _SETTINGS)

    apm("approve", _DEPENDENCY)
    # Approved: the whole plugin deploys; before #3921 this exited 1 (#3896).
    apm("install")
    for relative, text in _FILES.items():
        assert (root / relative).read_text(encoding="ascii") == text, relative
    loose = (".claude/hooks", ".claude/skills", ".github/hooks", ".agents/skills")
    assert not any((project / path).exists() for path in loose)
    catalog = json.loads((project / ".agents/plugins/.claude-plugin/marketplace.json").read_text())
    key = f"{_PLUGIN}@{catalog['name']}"
    for settings in _SETTINGS:
        declared = json.loads((project / settings).read_text(encoding="utf-8"))
        assert declared["extraKnownMarketplaces"] == {
            catalog["name"]: {"source": {"source": "directory", "path": "./.agents/plugins"}}
        }, settings
        assert declared["enabledPlugins"] == {key: True}, settings
    (locked,) = load_yaml(project / "apm.lock.yaml")["dependencies"]
    assert locked["native_plugin"]["root"] == f".agents/plugins/{_PLUGIN}"

    # I1 and I3: the read-only verifiers agree with the deployed state.
    apm("audit", "--ci")
    apm("install", "--frozen")
    # I2: a converged install changes no bytes.
    converged = snap()
    apm("install")
    assert snap() == converged

    # The SessionStart hook finds its skill through the intact plugin root.
    if os.name != "nt" and shutil.which("sh"):
        hook = subprocess.run(
            [str(root / "hooks/session-start")],
            env=dict(env, CLAUDE_PLUGIN_ROOT=str(root)),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert hook.returncode == 0, hook.stderr
        assert _SKILL_TEXT in hook.stdout

    # A teammate's checkout that converts line endings keeps the pinned bytes:
    # APM's .agents/plugins/.gitattributes turns conversion off for the roots.
    assert (project / ".agents/plugins/.gitattributes").read_text().endswith("* -text\n")
    clone = _commit_and_clone_with_crlf(project, env)
    assert (clone / ".agents/plugins" / _PLUGIN / "hooks/hooks.json").read_bytes() == (
        root / "hooks/hooks.json"
    ).read_bytes()
    for args in (("install", "--frozen"), ("audit", "--ci")):
        result = runner.run(args, cwd=clone, env=env)
        _assert_output_oracles(result)
        assert result.returncode == 0, _evidence(result)

    # Uninstall removes the root and only the entries APM declared.
    copilot = project / _SETTINGS[1]
    user_entry = {"theme": "dark"}
    copilot.write_text(
        json.dumps({**json.loads(copilot.read_text(encoding="utf-8")), **user_entry}),
        encoding="utf-8",
    )
    apm("uninstall", _DEPENDENCY)
    assert not root.exists()
    assert not (project / ".agents/plugins").exists()
    for settings in _SETTINGS:
        declared = json.loads((project / settings).read_text(encoding="utf-8"))
        assert key not in declared.get("enabledPlugins", {}), settings
        assert catalog["name"] not in declared.get("extraKnownMarketplaces", {}), settings
    assert json.loads(copilot.read_text(encoding="utf-8"))["theme"] == "dark"
    apm("audit", "--ci")


def test_uninstalling_a_sibling_keeps_a_local_native_plugin_whole(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """`approve --all` pins the whole local plugin; removing a sibling never splits it.

    CELL: OP=approve,install,uninstall SRC=local PRIM=plugin(hooks) SCOPE=project
    TGT=claude,copilot. Uninstall used to restage the local plugin from its
    source and re-integrate its hooks loose beside the enabled native plugin,
    so the SessionStart hook ran twice and audit failed.
    """
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    plugin = project / "plugins" / _PLUGIN
    for relative, text in _FILES.items():
        path = plugin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
    (plugin / "hooks/session-start").chmod(0o755)
    sibling = project / "packages" / "plain"
    (sibling / "skills/plain").mkdir(parents=True)
    (sibling / "apm.yml").write_text("name: plain\nversion: 1.0.0\n", encoding="ascii")
    (sibling / "skills/plain/SKILL.md").write_text(
        "---\nname: plain\ndescription: plain\n---\n# plain\n", encoding="ascii"
    )
    plugin_dep, sibling_dep = f"./plugins/{_PLUGIN}", "./packages/plain"
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "targets": ["claude", "copilot"],
                "dependencies": {"apm": [plugin_dep, sibling_dep]},
                "executables": {},
            }
        ),
        encoding="utf-8",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str, expect: int = 0) -> CommandResult:
        result = runner.run(args, cwd=project, env=env)
        _assert_output_oracles(result)
        assert result.returncode == expect, _evidence(result)
        return result

    root = project / ".agents/plugins" / _PLUGIN
    apm("install")
    assert not root.exists()
    apm("approve", "--all")
    pin = load_yaml(project / "apm.yml")["executables"]["allow"][plugin_dep]["plugin"]
    assert pin.startswith("sha256:")
    apm("install")
    assert (root / "hooks/hooks.json").read_text(encoding="ascii") == _FILES["hooks/hooks.json"]

    def enabled() -> dict[str, dict[str, bool]]:
        return {
            settings: json.loads((project / settings).read_text(encoding="utf-8"))["enabledPlugins"]
            for settings in _SETTINGS
        }

    declared = enabled()
    plugin_bytes = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }

    apm("uninstall", sibling_dep)
    assert not (project / ".agents/skills/plain").exists()
    assert enabled() == declared
    assert {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == plugin_bytes
    # Nothing of the plugin is deployed loose beside the enabled native copy.
    assert "hooks" not in json.loads((project / ".claude/settings.json").read_text("utf-8"))
    for loose in (".claude/hooks", ".claude/apm-hooks.json", ".github/hooks", ".agents/skills"):
        assert not (project / loose).exists(), loose
    (locked,) = load_yaml(project / "apm.lock.yaml")["dependencies"]
    assert locked["native_plugin"]["root"] == f".agents/plugins/{_PLUGIN}"
    # No closing `audit --ci`: the survivor preflight still normalizes the local
    # source in place (apm.yml, .apm/), a pre-existing defect tracked in #4566.
