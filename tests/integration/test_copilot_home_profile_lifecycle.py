"""Real CLI lifecycle for a Copilot CLI home selected by ``COPILOT_HOME`` (#3745).

``COPILOT_HOME`` replaces the whole ``~/.copilot`` directory for Copilot CLI
(https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference).
``apm install -g --target copilot`` must place every Copilot user primitive,
the MCP config and the LSP config under it; ``apm audit --ci`` and
``apm uninstall -g`` must use the same root; unsetting the variable leaves the
other profile's files in place; and a relative value fails clearly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_SKILL = "---\nname: copilot-home-skill\ndescription: Lifecycle skill.\n---\nUse it.\n"
_AGENT = "---\nname: copilot-home-agent\ndescription: Lifecycle agent.\n---\nAct.\n"
_INSTRUCTION = '---\napplyTo: "**/*.py"\n---\nPrefer explicit names.\n'
_HOOK = {
    "version": 1,
    "hooks": {"sessionStart": [{"type": "command", "bash": "echo copilot-home"}]},
}


class _CopilotHome:
    """One isolated user workspace with a package and a Copilot home outside HOME."""

    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(
            tmp_path / "copilot-home", base_env=dict(os.environ)
        )
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)
        self.cwd = self.isolated.work_root
        self.selected = (self.isolated.root / "profiles" / "copilot").resolve()
        self.default = self.isolated.home / ".copilot"
        factory = LocalPackageFactory(self.isolated.package_root)
        package = factory.create("copilot-home-pkg", targets=("copilot",))
        factory.add_skill(package, "copilot-home-skill", _SKILL)
        factory.add_agent(package, "copilot-home-agent", _AGENT)
        factory.add_instruction(package, "copilot-home-style", _INSTRUCTION)
        factory.add_hook(package, "copilot-home-hooks", _HOOK)
        self.package = package
        dump_yaml(
            {
                "name": "copilot-home-user",
                "version": "0.1.0",
                "dependencies": {
                    "mcp": [
                        {
                            "name": "copilot-home-mcp",
                            "registry": False,
                            "transport": "stdio",
                            "command": "python",
                            "args": ["-m", "fixture_server"],
                        }
                    ],
                    "lsp": [
                        {
                            "name": "copilot-home-lsp",
                            "command": "fixture-lsp",
                            "extensionToLanguage": {".fx": "fixture"},
                        }
                    ],
                },
            },
            self.isolated.config_root / "apm.yml",
        )

    def run(self, *args: str, copilot_home: str | None, cwd: Path | None = None) -> CommandResult:
        env = self.isolated.subprocess_env()
        if copilot_home is None:
            env.pop("COPILOT_HOME", None)
        else:
            env["COPILOT_HOME"] = copilot_home
        return self.runner.run(args, scenario_id="copilot-home", cwd=cwd or self.cwd, env=env)

    def install(self, *extra: str, copilot_home: str | None) -> CommandResult:
        return self.run(
            "install",
            "--global",
            str(self.package.root),
            "--target",
            "copilot",
            "--no-policy",
            *extra,
            copilot_home=copilot_home,
        )

    def user_primitives(self, root: Path) -> dict[str, bool]:
        """Return which Copilot user primitives and configs exist under *root*."""
        return {
            "agent": (root / "agents" / "copilot-home-agent.agent.md").is_file(),
            "instruction": (root / "instructions" / "copilot-home-style.instructions.md").is_file(),
            "hook": any((root / "hooks").glob("*.json")) if (root / "hooks").is_dir() else False,
            "mcp": "copilot-home-mcp" in _servers(root / "mcp-config.json", "mcpServers"),
            "lsp": "copilot-home-lsp" in _servers(root / "lsp-config.json", "lspServers"),
        }


def _servers(path: Path, key: str) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get(key, {})


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


_ALL = {"agent": True, "instruction": True, "hook": True, "mcp": True, "lsp": True}
_NONE = dict.fromkeys(_ALL, False)


@pytest.mark.parametrize("legacy_skill_paths", [False, True], ids=["default-skills", "legacy"])
def test_copilot_home_outside_home_owns_every_copilot_user_primitive(
    tmp_path: Path, apm_binary_path: Path, legacy_skill_paths: bool
) -> None:
    world = _CopilotHome(tmp_path, apm_binary_path)
    selected = str(world.selected)
    extra = ("--legacy-skill-paths",) if legacy_skill_paths else ()
    shared_skill = world.isolated.home / ".agents" / "skills" / "copilot-home-skill" / "SKILL.md"
    selected_skill = world.selected / "skills" / "copilot-home-skill" / "SKILL.md"

    installed = world.install(*extra, copilot_home=selected)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert world.user_primitives(world.selected) == _ALL
    assert world.user_primitives(world.default) == _NONE
    assert selected_skill.is_file() is legacy_skill_paths
    assert shared_skill.is_file() is not legacy_skill_paths
    assert not (world.default / "skills").exists()

    audited = world.run(
        "audit",
        "--ci",
        "--no-policy",
        copilot_home=selected,
        cwd=world.isolated.config_root,
    )
    assert audited.returncode == 0, audited.stdout + audited.stderr

    removed = world.run("uninstall", "--global", str(world.package.root), copilot_home=selected)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    primitives = world.user_primitives(world.selected)
    assert not primitives["agent"] and not primitives["instruction"] and not primitives["hook"]
    assert not selected_skill.exists()
    assert not shared_skill.exists()

    manifest = load_yaml(world.isolated.config_root / "apm.yml")
    manifest.pop("dependencies")
    dump_yaml(manifest, world.isolated.config_root / "apm.yml")
    pruned = world.run(
        "install", "--global", "--target", "copilot", "--no-policy", copilot_home=selected
    )
    assert pruned.returncode == 0, pruned.stdout + pruned.stderr
    assert world.user_primitives(world.selected) == _NONE


def test_unsetting_copilot_home_leaves_the_other_profile_in_place(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _CopilotHome(tmp_path, apm_binary_path)

    selected = world.install(copilot_home=str(world.selected))
    assert selected.returncode == 0, selected.stdout + selected.stderr
    before = {path: path.read_bytes() for path in world.selected.rglob("*") if path.is_file()}

    default = world.install(copilot_home=None)
    assert default.returncode == 0, default.stdout + default.stderr

    assert world.user_primitives(world.default) == _ALL
    assert world.user_primitives(world.selected) == _ALL
    assert {path: path.read_bytes() for path in before} == before


def test_relative_copilot_home_fails_clearly_without_writes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _CopilotHome(tmp_path, apm_binary_path)

    result = world.install(copilot_home="relative/copilot")

    assert result.returncode != 0
    assert "COPILOT_HOME is set to a non-absolute path" in _output(result)
    assert "must be an absolute path" in _output(result)
    assert not (world.cwd / "relative").exists()
    assert not world.default.exists()
