"""APMLifecycle: a project install lets Claude Code's first session load the plugin (#5176).

PROMISE: P-OWN-claude-native-plugin-registration -- a project-scope native
plugin is committed under the relative ``./.agents/plugins`` marketplace, and
``apm install`` also lists that marketplace in Claude Code's user-level
``plugins/known_marketplaces.json`` with the installing checkout's absolute
path, so a first session loads the plugin's skills and hooks. The entry is per
machine and idempotent, honours ``CLAUDE_CONFIG_DIR``, never touches another
entry, and ``apm uninstall`` removes exactly APM's entry. An install in a git
worktree warns that Claude Code may load the main checkout's copy (source:
issues #5176 and #5180, real-harness shape A, ruling PD-47).
CELL: OP=install,reinstall,uninstall SRC=local PRIM=plugin(skills) SCOPE=project
TGT=claude CLAUDE_CONFIG_DIR=unset|set KNOWN=absent|other-entry CHECKOUT=main|worktree
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-claude-native-plugin-registration", "P-INV-I2", "P-INV-I5"),
]

_OTHER = {
    "claude-plugins-official": {
        "source": {"source": "github", "repo": "anthropics/claude-plugins-official"},
        "installLocation": "/elsewhere/claude-plugins-official",
        "lastUpdated": "2026-01-01T00:00:00.000Z",
    }
}


class _World:
    project: Path

    def __init__(self, tmp_path: Path, binary: Path, *, claude_config_dir: bool = False) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
        overrides = {"NO_COLOR": "1"}
        self.claude_dir = self.isolated.home / ".claude"
        if claude_config_dir:
            self.claude_dir = self.isolated.work_root / "claude-config"
            overrides["CLAUDE_CONFIG_DIR"] = str(self.claude_dir)
        self.env = self.isolated.subprocess_env(overrides=overrides)
        self.runner = ApmLifecycleRunner((str(binary),))
        self.project = self.isolated.work_root / "project"
        (self.project / ".claude").mkdir(parents=True)
        plugin = self.isolated.work_root / "packages" / "kit"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin/plugin.json").write_text(
            json.dumps({"name": "kit", "version": "1.0.0"}), encoding="utf-8"
        )
        (plugin / "skills/alpha").mkdir(parents=True)
        (plugin / "skills/alpha/SKILL.md").write_text(
            "---\nname: alpha\ndescription: alpha\n---\nalpha body.\n", encoding="utf-8"
        )
        self.plugin = plugin
        self.manifest([plugin.as_posix()])

    def manifest(self, deps: list[str]) -> None:
        lines = ["name: consumer", "version: 0.0.1", "targets: [claude]", "dependencies:"]
        lines += ["  apm:", *(f"    - {dep}" for dep in deps)] if deps else ["  apm: []"]
        (self.project / "apm.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    @property
    def known(self) -> Path:
        return self.claude_dir / "plugins" / "known_marketplaces.json"

    def run(self, *args: str, cwd: Path | None = None) -> CommandResult:
        return self.runner.run(
            args, cwd=cwd or self.project, env=self.env, scenario_id=" ".join(args)
        )

    def ok(self, *args: str, cwd: Path | None = None) -> CommandResult:
        result = self.run(*args, cwd=cwd)
        assert result.returncode == 0, _evidence(result)
        return result

    def git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(
            ("git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args),
            cwd=cwd or self.project,
            env=self.env,
            check=True,
            capture_output=True,
        )

    def marketplace(self, checkout: Path | None = None) -> str:
        settings = json.loads(
            ((checkout or self.project) / ".claude/settings.json").read_text("utf-8")
        )
        (name,) = [k for k in settings["extraKnownMarketplaces"] if k.startswith("apm-p-")]
        assert settings["extraKnownMarketplaces"][name]["source"]["path"] == "./.agents/plugins"
        return name


def _evidence(result: CommandResult) -> str:
    return f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"


def _expected_entry(world: _World, checkout: Path | None = None) -> dict:
    location = str(((checkout or world.project) / ".agents/plugins").resolve())
    return {"source": {"source": "directory", "path": location}, "installLocation": location}


def _known(world: _World) -> dict:
    return json.loads(world.known.read_text("utf-8"))


@pytest.mark.parametrize("claude_config_dir", [False, True], ids=["home", "CLAUDE_CONFIG_DIR"])
def test_project_install_lists_the_marketplace_for_claude_with_its_absolute_path(
    tmp_path: Path, apm_binary_path: Path, claude_config_dir: bool
) -> None:
    """A fresh machine's install writes the absolute entry; a re-install changes no byte."""
    world = _World(tmp_path, apm_binary_path, claude_config_dir=claude_config_dir)
    assert not world.known.exists()

    world.ok("install", "--no-policy")

    name = world.marketplace()
    entry = _known(world)[name]
    assert {k: entry[k] for k in ("source", "installLocation")} == _expected_entry(world)
    if claude_config_dir:
        assert not (world.isolated.home / ".claude/plugins/known_marketplaces.json").exists()
    before = world.known.read_bytes()
    world.ok("install", "--no-policy")
    assert world.known.read_bytes() == before


def test_install_and_uninstall_touch_only_apms_entry(tmp_path: Path, apm_binary_path: Path) -> None:
    """Another marketplace survives install and uninstall; uninstall removes APM's entry."""
    world = _World(tmp_path, apm_binary_path)
    world.known.parent.mkdir(parents=True)
    world.known.write_text(json.dumps(_OTHER, indent=2) + "\n", encoding="utf-8")

    world.ok("install", "--no-policy")
    name = world.marketplace()
    assert set(_known(world)) == {name, *_OTHER}
    assert {k: _known(world)[k] for k in _OTHER} == _OTHER

    world.ok("uninstall", world.plugin.as_posix())

    assert _known(world) == _OTHER
    assert not (world.project / ".agents/plugins/kit").exists()


_WORKTREE_NOTICE = "This checkout is a git worktree: Claude Code may load the main checkout's copy"


@pytest.mark.parametrize("checkout", ["main", "worktree"])
def test_a_checkout_install_lists_its_own_plugin_root(
    tmp_path: Path, apm_binary_path: Path, checkout: str
) -> None:
    """#5180 outcome 1: an install in a worktree alone lists that worktree's own root,
    and warns that Claude Code may load the main checkout's copy; a main install does not."""
    world = _World(tmp_path, apm_binary_path)
    world.git("init", "-q", "--initial-branch=main")
    world.git("add", "-A")
    world.git("commit", "-q", "-m", "consumer")
    target = world.project
    if checkout == "worktree":
        target = world.isolated.work_root / "consumer-wt"
        world.git("worktree", "add", "-q", str(target), "-b", "wt")

    result = world.ok("install", "--no-policy", cwd=target)

    entry = _known(world)[world.marketplace(target)]
    assert {k: entry[k] for k in ("source", "installLocation")} == _expected_entry(world, target)
    text = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert (f"[!] {_WORKTREE_NOTICE}" in text) is (checkout == "worktree"), text
    assert text.count(_WORKTREE_NOTICE) <= 1, text


def test_a_dry_run_install_writes_no_registry_entry(tmp_path: Path, apm_binary_path: Path) -> None:
    """``apm install --dry-run`` previews; it writes neither the registry nor APM's record."""
    world = _World(tmp_path, apm_binary_path)

    world.ok("install", "--dry-run", "--no-policy")

    assert not world.known.exists()
    assert not (world.isolated.home / ".apm" / "claude-known-marketplaces.json").exists()
