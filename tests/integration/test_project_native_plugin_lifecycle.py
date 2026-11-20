"""Installed-CLI lifecycle for project-scope native plugins (#3921).

A project that targets Claude and Copilot installs a Claude plugin intact
under the committed ``.agents/plugins/<plugin>/``, declares it in the
committed ``.claude/settings.json`` and ``.github/copilot/settings.json``
with the relative path ``./.agents/plugins``, and uninstall removes only the
entries its lock produced. When a ``copilot`` CLI is on PATH, a fresh clone
without APM or ``apm_modules`` is checked against the live CLI, including the
file precedence the native route relies on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

_CLAUDE = ".claude/settings.json"
_COPILOT = ".github/copilot/settings.json"


def _plugin(root: Path) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name": "demo", "version": "1.0.0"}')
    (root / "skills/hello").mkdir(parents=True)
    (root / "skills/hello/SKILL.md").write_text("---\nname: hello\ndescription: hi\n---\nHi\n")
    (root / "commands").mkdir()
    (root / "commands/status.md").write_text("Report status.\n")
    return root


def _copilot_plugins(repo: Path, home: Path) -> str:
    """``copilot plugin list`` for *repo* in an isolated Copilot home that trusts it."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"trustedFolders": [str(repo)]}))
    env = {**os.environ, "COPILOT_HOME": str(home)}
    result = subprocess.run(
        ["copilot", "plugin", "list"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result.stdout + result.stderr


def test_project_native_plugin_commits_declares_and_uninstalls(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    plugin = _plugin(tmp_path / "plugin")
    project = isolated.work_root
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    manifest = {
        "name": "consumer",
        "version": "1.0.0",
        "targets": ["claude", "copilot"],
        "dependencies": {"apm": [str(plugin)]},
    }
    (project / "apm.yml").write_text(json.dumps(manifest), encoding="utf-8")
    user = {"theme": "dark", "enabledPlugins": {"mine@elsewhere": True}}
    (project / ".claude").mkdir()
    (project / _CLAUDE).write_text(json.dumps(user), encoding="utf-8")
    runner = ApmLifecycleRunner(apm_engine_command)

    def run(*args: str) -> str:
        result = runner.run(args, scenario_id="project-native-plugin", cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout + result.stderr

    run("install")
    root = project / ".agents/plugins"
    assert (root / "demo/skills/hello/SKILL.md").is_file()
    assert not (root / "demo/apm.yml").exists()
    catalog = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    name = catalog["name"]
    assert name.startswith("apm-p-")
    marketplace = {name: {"source": {"source": "directory", "path": "./.agents/plugins"}}}
    claude = json.loads((project / _CLAUDE).read_text())
    copilot = json.loads((project / _COPILOT).read_text())
    assert claude == {
        **user,
        "extraKnownMarketplaces": marketplace,
        "enabledPlugins": {"mine@elsewhere": True, f"demo@{name}": True},
    }
    assert copilot == {
        "extraKnownMarketplaces": marketplace,
        "enabledPlugins": {f"demo@{name}": True},
    }
    # Split install is gone: no loose copy of the plugin's skill or command.
    assert not (project / ".claude/skills/hello").exists()
    assert not (project / ".github/skills/hello").exists()
    # The committed root is never ignored; only apm_modules is.
    ignored = (project / ".gitignore").read_text()
    assert ".agents" not in ignored

    if shutil.which("copilot"):
        _check_fresh_clone_in_copilot(project, tmp_path, name)

    run("uninstall", str(plugin))
    # Only APM's entries are gone; an emptied section stays as an empty object.
    assert json.loads((project / _CLAUDE).read_text()) == {**user, "extraKnownMarketplaces": {}}
    assert json.loads((project / _COPILOT).read_text()) == {
        "extraKnownMarketplaces": {},
        "enabledPlugins": {},
    }
    assert not root.exists()


def _check_fresh_clone_in_copilot(project: Path, tmp_path: Path, name: str) -> None:
    """A clone without APM loads the plugin; Copilot's own file wins over .claude/."""
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    git = ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@e"]
    subprocess.run([*git, "-c", "commit.gpgsign=false", "commit", "-qm", "x"], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(project), str(clone)], check=True)
    assert not (clone / "apm_modules").exists()
    listing = _copilot_plugins(clone, tmp_path / "copilot-home")
    assert f"demo@{name}" in listing and "(enabled)" in listing, listing
    assert str(clone / ".agents/plugins") in listing, listing

    # Both files name the plugin; .github/copilot/settings.json takes precedence.
    copilot = json.loads((clone / _COPILOT).read_text())
    copilot["enabledPlugins"][f"demo@{name}"] = False
    (clone / _COPILOT).write_text(json.dumps(copilot))
    listing = _copilot_plugins(clone, tmp_path / "copilot-home")
    assert "(disabled)" in listing, listing
