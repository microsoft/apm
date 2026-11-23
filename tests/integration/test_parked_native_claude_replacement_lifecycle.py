"""APMLifecycle: a parked native Claude replacement keeps the loose install (#5036).

PROMISE: P-INV-I7 -- nothing is removed silently: when an install would replace
a target's loose deployment with a native plugin that is then parked, the loose
files stay usable, the output names the parked plugin and the kept files, and
the exit status is the documented one (source: issue #5036 scope record).
CELL: OP=install(upgrade) SRC=git PRIM=skill,native-plugin(claude) SCOPE=user
DEPLOYED=loose(claude,codex) GATE=native-parked|native-admitted
TGT=claude(default|CLAUDE_CONFIG_DIR outside HOME|CLAUDE_CONFIG_DIR=HOME, #5092)
SCOPE=project LAYOUT=legacy-skill-paths -> flagless (parked for claude+copilot, #5092)
owner: install/deployment_plan.py PackageDeploymentPlan (loose projection and
the values a parked native plan keeps)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7", "P-OWN-claude-native-plugin-registration"),
]

_REMOTE = "https://github.com/fixture/marketplace"
_SUBDIR = "plugins/demo"
_CLAUDE_SKILL = ".claude/skills/demo/SKILL.md"
_CODEX_SKILL = ".agents/skills/demo/SKILL.md"
_INSTALL = ("install", "--global", "--no-policy", "--no-audit")


def _skill(body: str) -> str:
    return f"---\nname: demo\ndescription: Demo\n---\n{body}\n"


def _evidence(result: CommandResult) -> str:
    return f"argv={result.command!r} exit={result.returncode}\n{result.stdout}\n{result.stderr}"


def _text(result: CommandResult) -> str:
    return " ".join(f"{result.stdout}\n{result.stderr}".split())


class _World:
    """A git package installed loose for its targets, at user or project scope."""

    def __init__(
        self,
        tmp_path: Path,
        apm_binary_path: Path,
        *,
        claude: str = "default",
        project: bool = False,
        targets: tuple[str, ...] = ("codex", "claude"),
    ) -> None:
        isolated = IsolatedApmEnvironment.create(
            tmp_path.resolve() / "world", base_env=dict(os.environ)
        )
        self.home = isolated.home
        self.cwd = isolated.work_root
        self.project = project
        self.targets = list(targets)
        # The Claude user root: ~/.claude, a CLAUDE_CONFIG_DIR outside HOME, or HOME itself.
        self.claude_root = {
            "default": self.home / ".claude",
            "external": isolated.root / "claude-config",
            "home": self.home,
        }[claude]
        self.claude_root.mkdir(exist_ok=True)
        (self.home / ".codex").mkdir(exist_ok=True)
        self.manifest = (self.cwd if project else isolated.config_root) / "apm.yml"
        repositories = LocalGitRepositoryFactory(
            isolated.repository_root, env=isolated.subprocess_env()
        )
        self.repositories = repositories
        self.repository = repositories.create("marketplace")
        self.package = self.repository.worktree / _SUBDIR
        (self.package / "skills" / "demo").mkdir(parents=True)
        (self.package / "apm.yml").write_text(
            f"name: demo\nversion: 1.0.0\ntargets: [{', '.join(targets)}]\n"
            "dependencies:\n  apm: []\n  mcp: []\nincludes:\n  - skills\n",
            encoding="ascii",
        )
        self.env = repositories.url_rewrite_subprocess_env(self.repository, _REMOTE)
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME", "APM_LEGACY_SKILL_PATHS"):
            self.env.pop(name, None)
        if claude != "default":
            self.env["CLAUDE_CONFIG_DIR"] = str(self.claude_root)
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def publish(self, body: str, descriptor: dict | None = None) -> str:
        (self.package / "skills" / "demo" / "SKILL.md").write_text(_skill(body), encoding="ascii")
        if descriptor is not None:
            text = json.dumps(descriptor) + "\n"
            (self.package / ".claude-plugin").mkdir(exist_ok=True)
            (self.package / ".claude-plugin" / "plugin.json").write_text(text, encoding="ascii")
            (self.package / "plugin.json").write_text(text, encoding="ascii")
        return self.repositories.commit(self.repository, message=f"publish {body}").sha

    def pin(self, commit: str) -> None:
        dependency = {
            "git": "fixture/marketplace",
            "path": _SUBDIR,
            "ref": commit,
            "targets": self.targets,
        }
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(
            json.dumps(
                {
                    "name": "consumer",
                    "version": "1.0.0",
                    **({"targets": self.targets} if self.project else {}),
                    "dependencies": {"apm": [dependency], "mcp": []},
                }
            ),
            encoding="ascii",
        )

    def install(self, *extra: str) -> CommandResult:
        argv = tuple(a for a in _INSTALL if not (self.project and a == "--global"))
        return self.runner.run(
            (*argv, *extra),
            scenario_id="parked-native-replacement",
            cwd=self.cwd,
            env=self.env,
        )

    def audit(self) -> CommandResult:
        return self.runner.run(
            ("audit", "--ci", "--no-policy"),
            scenario_id="parked-native-replacement-audit",
            cwd=self.manifest.parent,
            env=self.env,
        )

    def deployed(self) -> list[str]:
        lock = load_yaml(self.manifest.parent / "apm.lock.yaml")
        (row,) = lock["dependencies"]
        return list(row["deployed_files"])


def _installed_loose(world: _World) -> None:
    world.pin(world.publish("Initial skill."))
    first = world.install()
    assert first.returncode == 0, _evidence(first)
    assert (world.home / _CLAUDE_SKILL).read_text(encoding="ascii") == _skill("Initial skill.")
    assert _CLAUDE_SKILL in world.deployed()


def test_a_parked_native_replacement_keeps_the_loose_claude_skill(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path)
    _installed_loose(world)
    descriptor = {
        "name": "demo",
        "version": "1.0.0",
        "skills": "./skills/",
        "interface": {"displayName": "Demo"},
    }
    world.pin(world.publish("Updated skill.", descriptor))

    upgraded = world.install("--no-frozen")

    text = _text(upgraded)
    assert upgraded.returncode == 0, _evidence(upgraded)
    # The loose Claude copy stays usable with its previous bytes and its lock claim.
    assert (world.home / _CLAUDE_SKILL).read_text(encoding="ascii") == _skill("Initial skill.")
    assert _CLAUDE_SKILL in world.deployed(), _evidence(upgraded)
    assert (world.home / _CODEX_SKILL).read_text(encoding="ascii") == _skill("Updated skill.")
    assert "Parked native plugin fixture/marketplace/plugins/demo" in text, _evidence(upgraded)
    assert "Kept ~/.claude/skills/demo" in text, _evidence(upgraded)
    assert "native plugin that replaces it was parked" in text, _evidence(upgraded)
    assert "~/.claude/skills/demo/" not in text.split("Cleaned", 1)[-1].split("[", 1)[0]
    assert "is not part of this install" not in text, _evidence(upgraded)

    # I3: frozen replay and audit agree on the kept state.
    frozen = world.install("--frozen")
    assert frozen.returncode == 0, _evidence(frozen)
    audit = world.runner.run(
        ("audit", "--ci", "--no-policy"),
        scenario_id="parked-native-replacement-audit",
        cwd=world.manifest.parent,
        env=world.env,
    )
    assert audit.returncode == 0, _evidence(audit)

    # I2: the converged state repeats with no change.
    lock_before = (world.manifest.parent / "apm.lock.yaml").read_bytes()
    again = world.install("--no-frozen")
    assert again.returncode == 0, _evidence(again)
    assert (world.manifest.parent / "apm.lock.yaml").read_bytes() == lock_before
    assert (world.home / _CLAUDE_SKILL).read_text(encoding="ascii") == _skill("Initial skill.")

    # The kept copy is still bound by its recorded hash: an edit fails the audit.
    (world.home / _CLAUDE_SKILL).write_text(_skill("Tampered."), encoding="ascii")
    tampered = world.runner.run(
        ("audit", "--ci", "--no-policy"),
        scenario_id="parked-native-replacement-tampered",
        cwd=world.manifest.parent,
        env=world.env,
    )
    assert tampered.returncode == 1, _evidence(tampered)
    assert f"hash-drift: {_CLAUDE_SKILL}" in _text(tampered), _evidence(tampered)


def test_an_admitted_native_replacement_still_retires_the_loose_claude_skill(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path)
    _installed_loose(world)
    descriptor = {"name": "demo", "version": "1.0.0", "skills": "./skills/"}
    world.pin(world.publish("Updated skill.", descriptor))

    upgraded = world.install("--no-frozen")

    text = _text(upgraded)
    assert upgraded.returncode == 0, _evidence(upgraded)
    assert not (world.home / _CLAUDE_SKILL).exists(), _evidence(upgraded)
    assert _CLAUDE_SKILL not in world.deployed(), _evidence(upgraded)
    assert "native plugin(s) deployed" in text, _evidence(upgraded)
    assert "Parked native plugin" not in text, _evidence(upgraded)
    # The retired copy is not also reported as kept.
    assert "is not part of this install" not in text, _evidence(upgraded)


_PARKED = {
    "name": "demo",
    "version": "1.0.0",
    "skills": "./skills/",
    "interface": {"displayName": "Demo"},
}


def _upgrade_to_parked(world: _World, *first_flags: str) -> tuple[CommandResult, list[str]]:
    """Install loose, then upgrade flaglessly to a parked native Claude replacement."""
    world.pin(world.publish("Initial skill."))
    first = world.install(*first_flags)
    assert first.returncode == 0, _evidence(first)
    claims = world.deployed()
    world.pin(world.publish("Updated skill.", _PARKED))
    upgraded = world.install("--no-frozen")
    assert upgraded.returncode == 0, _evidence(upgraded)
    assert "Parked native plugin fixture/marketplace/plugins/demo" in _text(upgraded)
    return upgraded, claims


def _assert_kept_and_bound(world: _World, kept: dict[str, Path], step: CommandResult) -> None:
    """Kept loose skills stay byte-identical and claimed; frozen and audit agree (I3).

    A tampered kept skill still fails the audit with hash-drift.
    """
    for claim, skill in kept.items():
        assert skill.read_text(encoding="ascii") == _skill("Initial skill."), _evidence(step)
        assert claim in world.deployed(), _evidence(step)
    frozen = world.install("--frozen")
    assert frozen.returncode == 0, _evidence(frozen)
    audit = world.audit()
    assert audit.returncode == 0, _evidence(audit)
    for skill in kept.values():
        assert skill.read_text(encoding="ascii") == _skill("Initial skill."), _evidence(audit)
    claim, skill = next(iter(kept.items()))
    skill.write_text(_skill("Tampered."), encoding="ascii")
    tampered = world.audit()
    assert tampered.returncode == 1, _evidence(tampered)
    assert f"hash-drift: {claim}" in _text(tampered), _evidence(tampered)


@pytest.mark.parametrize("claude", ["external"], ids=["outside-home"])
def test_a_parked_native_replacement_keeps_the_loose_skill_under_a_relocated_claude_root(
    tmp_path: Path, apm_binary_path: Path, claude: str
) -> None:
    """CLAUDE_CONFIG_DIR outside HOME (absolute claims) or at HOME (root ``.``) (#5092)."""
    world = _World(tmp_path, apm_binary_path, claude=claude)
    skill = world.claude_root / "skills" / "demo" / "SKILL.md"
    claim = skill.as_posix() if claude == "external" else "skills/demo/SKILL.md"

    upgraded, before = _upgrade_to_parked(world)

    assert claim in before, before
    text = _text(upgraded)
    assert (world.home / _CODEX_SKILL).read_text(encoding="ascii") == _skill("Updated skill.")
    assert "native plugin that replaces it was parked" in text, _evidence(upgraded)
    shown = claim.rsplit("/", 1)[0] if claude == "external" else "~/skills/demo"
    assert f"Kept {shown}" in text, _evidence(upgraded)
    assert "~//" not in text, _evidence(upgraded)
    _assert_kept_and_bound(world, {claim: skill}, upgraded)


def test_a_parked_native_replacement_keeps_legacy_layout_skills_in_a_project(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """--legacy-skill-paths install, then a flagless parked upgrade (#5092).

    At project scope the Claude-format plugin serves Claude and Copilot, so
    both loose copies are kept, including Copilot's under .github/skills/.
    """
    world = _World(tmp_path, apm_binary_path, project=True, targets=("copilot", "claude"))
    for harness in (".github", ".claude"):
        (world.cwd / harness).mkdir(exist_ok=True)
    kept = {
        claim: world.cwd / claim
        for claim in (".github/skills/demo/SKILL.md", ".claude/skills/demo/SKILL.md")
    }

    upgraded, before = _upgrade_to_parked(world, "--legacy-skill-paths")

    assert set(kept) <= set(before), before
    assert not (world.cwd / ".agents" / "skills" / "demo").exists(), _evidence(upgraded)
    assert "Kept .claude/skills/demo, .github/skills/demo:" in _text(upgraded), _evidence(upgraded)
    _assert_kept_and_bound(world, kept, upgraded)
