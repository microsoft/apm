"""A narrowed ``apm install -g`` names the stale skill copy it keeps (#4692).

PROMISE: P-INV-I7 -- nothing is skipped silently: when an install refreshes a
skill in its active targets but keeps an APM-managed copy in a target outside
this run, it names that copy and how to refresh it (source: #4692 scope record).
CELL: OP=install SRC=local PRIM=skill SCOPE=user UPDATE
      TGT=claude,agent-skills -> agent-skills (narrowed) -> hint (widened back)
OWNER: unmanaged-skill-preflight (integration/skill_adoption.py renders the current
       source for the kept destination; skill-layout recognises legacy roots)
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from pathlib import Path

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet, assert_snapshot_set_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
    pytest.mark.promise("P-INV-I1"),
    pytest.mark.promise("P-OWN-skill-layout"),
]

_SKILL = "kit"
_V1 = "---\nname: kit\ndescription: Kit skill\n---\nVersion one.\n"
_V2 = "---\nname: kit\ndescription: Kit skill\n---\nVersion two.\n"
_CLAUDE_SKILL = f".claude/skills/{_SKILL}/SKILL.md"
_AGENTS_SKILL = f".agents/skills/{_SKILL}/SKILL.md"
_KEPT_LINE = f"~/.claude/skills/{_SKILL}"
_HINT = re.compile(r"Run '(apm install [^']+)' to refresh it\.")


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


def _output(result: CommandResult) -> str:
    return result.stdout + result.stderr


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _recorded_hashes(lock_path: Path) -> dict[str, str]:
    data = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    dependencies = data.get("dependencies") or []
    if isinstance(dependencies, dict):
        dependencies = list(dependencies.values())
    hashes: dict[str, str] = {}
    for dependency in dependencies:
        hashes.update(dependency.get("deployed_file_hashes") or {})
    return hashes


def _kept_copy_hint(result: CommandResult, kept: str, target: str) -> list[str]:
    """Assert *kept* is named exactly once as stale and return the printed refresh command."""
    shown = " ".join(_output(result).split())
    assert shown.count(kept) == 1, _evidence(result)
    assert f"Kept stale skill copy {kept}: target '{target}'" in shown, _evidence(result)
    hint = _HINT.search(shown)
    assert hint is not None, _evidence(result)
    argv = shlex.split(hint.group(1))
    assert argv[:2] == ["apm", "install"] and "-g" in argv, hint.group(1)
    return argv


class _World:
    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
        self.env = self.isolated.subprocess_env()
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME"):
            self.env.pop(name, None)
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
        self.home = self.isolated.home
        (self.home / ".claude").mkdir()
        packages = LocalPackageFactory(self.isolated.package_root)
        package = packages.create("kit-pkg")
        self.skill_source = packages.add_skill(package, _SKILL, _V1)
        self.lock_path = self.isolated.config_root / "apm.lock.yaml"
        (self.isolated.config_root / "apm.yml").write_text(
            f"name: user\nversion: 1.0.0\ndependencies:\n  apm:\n    - {package.root.as_posix()}\n",
            encoding="utf-8",
        )

    def apm(self, *args: str) -> CommandResult:
        result = self.runner.run(args, cwd=self.isolated.work_root, env=self.env)
        _assert_output_oracles(result)
        assert result.returncode == 0, _evidence(result)
        return result

    def snapshot(self) -> ArtifactSnapshotSet:
        return ArtifactSnapshotSet.capture(
            {
                "claude": self.home / ".claude" / "skills",
                "agents": self.home / ".agents",
            }
        )

    def body(self, rel: str) -> str:
        return (self.home / rel).read_text(encoding="utf-8")

    def assert_lock_matches_disk(self, *copies: str) -> None:
        """I1: every APM-managed copy is recorded with its on-disk bytes."""
        recorded = _recorded_hashes(self.lock_path)
        for rel in copies or (_CLAUDE_SKILL, _AGENTS_SKILL):
            assert rel in recorded, (rel, sorted(recorded))
            assert recorded[rel] == _sha256(self.home / rel), rel


def test_narrowed_user_install_names_the_stale_skill_copy_it_keeps(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path)

    # GIVEN: one skill deployed to both user-scope targets.
    first = world.apm("install", "-g", "--target", "claude,agent-skills")
    assert world.body(_CLAUDE_SKILL) == _V1, _evidence(first)
    assert world.body(_AGENTS_SKILL) == _V1, _evidence(first)
    world.assert_lock_matches_disk()

    # The package ships a new version of the skill.
    world.skill_source.write_text(_V2, encoding="utf-8")

    # WHEN: the next install narrows to agent-skills only.
    narrowed = world.apm("install", "-g", "--target", "agent-skills")

    # THEN: the active copy is refreshed, the claude copy is kept untouched
    # (I5: the narrowed run never writes outside its targets), the lock still
    # records both copies with their real bytes, and the kept stale copy is
    # named together with a refresh command (I7: not silent).
    assert world.body(_AGENTS_SKILL) == _V2, _evidence(narrowed)
    assert world.body(_CLAUDE_SKILL) == _V1, _evidence(narrowed)
    world.assert_lock_matches_disk()
    argv = _kept_copy_hint(narrowed, _KEPT_LINE, "claude")

    # Every printed hint is executed: it refreshes the kept copy.
    refreshed = world.apm(*argv[1:])
    assert world.body(_CLAUDE_SKILL) == _V2, _evidence(refreshed)
    assert world.body(_AGENTS_SKILL) == _V2, _evidence(refreshed)
    world.assert_lock_matches_disk()
    assert _KEPT_LINE not in _output(refreshed), _evidence(refreshed)

    # Negative control and I2: once every copy is current, the same narrowed
    # install keeps the claude copy without naming it and changes no bytes.
    before, lock_before = world.snapshot(), world.lock_path.read_bytes()
    quiet = world.apm("install", "-g", "--target", "agent-skills")
    assert _KEPT_LINE not in _output(quiet), _evidence(quiet)
    assert_snapshot_set_unchanged(before, world.snapshot())
    assert world.lock_path.read_bytes() == lock_before, _evidence(quiet)

    # An add-only update leaves the kept copy incomplete: it is named again,
    # and stays untouched by the narrowed run.
    notes = world.skill_source.parent / "notes.md"
    notes.write_text("Extra notes.\n", encoding="utf-8")
    added = world.apm("install", "-g", "--target", "agent-skills")
    assert (world.home / ".agents/skills/kit/notes.md").is_file(), _evidence(added)
    assert not (world.home / ".claude/skills/kit/notes.md").exists(), _evidence(added)
    _kept_copy_hint(added, _KEPT_LINE, "claude")


def test_narrowed_legacy_layout_install_names_the_stale_skill_copy_it_keeps(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A copy kept in a per-client legacy skill root is recognised and refreshed in that layout."""
    world = _World(tmp_path, apm_binary_path)
    cursor_skill = f".cursor/skills/{_SKILL}/SKILL.md"
    (world.home / ".cursor").mkdir()

    first = world.apm("install", "-g", "--target", "claude,cursor", "--legacy-skill-paths")
    assert world.body(cursor_skill) == _V1, _evidence(first)
    world.assert_lock_matches_disk(_CLAUDE_SKILL, cursor_skill)

    world.skill_source.write_text(_V2, encoding="utf-8")
    narrowed = world.apm("install", "-g", "--target", "claude", "--legacy-skill-paths")
    assert world.body(_CLAUDE_SKILL) == _V2, _evidence(narrowed)
    assert world.body(cursor_skill) == _V1, _evidence(narrowed)
    world.assert_lock_matches_disk(_CLAUDE_SKILL, cursor_skill)
    argv = _kept_copy_hint(narrowed, f"~/.cursor/skills/{_SKILL}", "cursor")
    assert "--legacy-skill-paths" in argv, argv

    refreshed = world.apm(*argv[1:])
    assert world.body(cursor_skill) == _V2, _evidence(refreshed)
    assert world.body(_CLAUDE_SKILL) == _V2, _evidence(refreshed)
    world.assert_lock_matches_disk(_CLAUDE_SKILL, cursor_skill)
    assert "Kept stale skill copy" not in _output(refreshed), _evidence(refreshed)
