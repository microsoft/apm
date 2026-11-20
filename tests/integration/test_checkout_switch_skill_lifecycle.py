"""Installed-CLI contracts for deployed skills across checkouts (#4229, #4230).

Receipts are bound to one checkout, so a new worktree has none and a branch
switch leaves receipts written under another lock. In both cases ``apm install``
accepts deployed skill bytes equal to what it renders (link-rewritten), keeps
every lockfile entry, and never deletes a member whose source still exists.
When no trustworthy baseline exists for a locally edited skill, install
refuses without changing anything and prints the recovery, which works.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_REFUSAL = "no trustworthy local deployment baseline"
_MODIFIED = "modified since deployment"
_REMOTE = "https://github.com/test/kit.git"
_SKILL = "---\nname: alpha\ndescription: Alpha\n---\nSee [guide](../../../docs/guide.md).\n"
_LOCAL_SKILL = "---\nname: x\ndescription: Skill x.\n---\n# x\n"


@dataclass
class _World:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner
    project: Path

    def apm(self, *args: str, cwd: Path | None = None, env: dict | None = None) -> CommandResult:
        return self.runner.run(args, cwd=cwd or self.project, env=env or self.env)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ("git", *args),
            cwd=cwd or self.project,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


def _tree(root: Path, *parts: str) -> dict[str, bytes]:
    """Return every deployed file under the given roots, keyed by project-relative path."""
    files: dict[str, bytes] = {}
    for part in parts:
        base = root / part
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _locked_files(project: Path) -> set[str]:
    lock = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    files = set(lock.get("local_deployed_files") or ())
    for dependency in lock.get("dependencies") or ():
        files.update(dependency.get("deployed_files") or ())
    return {path.rstrip("/") for path in files}


def _assert_converged(world: _World, result: CommandResult, root: Path) -> None:
    """Exit 0, no refusal or false edit warning, and every locked file on disk (I1, I7)."""
    output = _output(result)
    assert result.returncode == 0, output
    assert _REFUSAL not in output, output
    assert _MODIFIED not in output, output
    assert "Cleaned" not in output, output
    missing = sorted(path for path in _locked_files(root) if not (root / path).exists())
    assert not missing, missing


def _assert_nothing_deleted(before: dict[str, bytes], root: Path) -> None:
    """I7: no deployed member disappears, whatever the command decided."""
    gone = sorted(path for path in before if not (root / path).exists())
    assert not gone, gone


def _new_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    project.mkdir()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    return _World(isolated, env, runner, project)


def _init_repo(world: _World, root: Path | None = None) -> None:
    root = root or world.project
    world.git("init", "-q", "--initial-branch=main", cwd=root)
    world.git("config", "user.email", "test@example.com", cwd=root)
    world.git("config", "user.name", "APM Test", cwd=root)
    world.git("config", "commit.gpgsign", "false", cwd=root)


def _commit_all(world: _World, message: str, root: Path | None = None) -> None:
    world.git("add", "--all", cwd=root)
    world.git("commit", "-q", "-m", message, cwd=root)


# ---------------------------------------------------------------------------
# #4229: a git dependency whose SKILL.md links are rewritten on deploy.
# ---------------------------------------------------------------------------


def _publish_kit(world: _World) -> tuple[str, str, str]:
    """Publish v1, v2 (notes changed) and v3 (SKILL.md changed from v1)."""
    repositories = LocalGitRepositoryFactory(world.isolated.repository_root, env=world.env)
    source = world.isolated.package_root / "kit"
    _write(source / "apm.yml", "name: kit\nversion: 1.0.0\n")
    _write(source / "docs/guide.md", "Guide.\n")
    _write(source / ".apm/skills/alpha/SKILL.md", _SKILL)
    _write(source / ".apm/skills/alpha/references/notes.md", "Notes v1.\n")
    _write(
        source / ".apm/instructions/style.instructions.md",
        '---\napplyTo: "**"\ndescription: Style\n---\nUse ASCII.\n',
    )
    repository = repositories.create("kit", source_tree=source)
    v1 = repositories.commit(repository, message="v1").sha
    _write(repository.worktree / ".apm/skills/alpha/references/notes.md", "Notes v2.\n")
    v2 = repositories.commit(repository, message="v2").sha
    _write(repository.worktree / ".apm/skills/alpha/references/notes.md", "Notes v1.\n")
    _write(repository.worktree / ".apm/skills/alpha/SKILL.md", _SKILL + "Version 3.\n")
    v3 = repositories.commit(repository, message="v3").sha
    world.env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    return v1, v2, v3


def _manifest(ref: str, *, name: str = "consumer") -> dict:
    return {
        "name": name,
        "version": "1.0.0",
        "targets": ["copilot"],
        "dependencies": {"apm": [{"git": _REMOTE, "ref": ref}]},
    }


def _committed_kit_project(world: _World, ref: str) -> None:
    dump_yaml(_manifest(ref), world.project / "apm.yml")
    _write(world.project / ".gitignore", "apm_modules/\n.apm-local/\n")
    first = world.apm("install")
    assert first.returncode == 0, _output(first)
    rendered = (world.project / ".agents/skills/alpha/SKILL.md").read_text(encoding="ascii")
    assert "](../../../docs/guide.md)" not in rendered, "fixture must exercise link rendering"
    assert (world.project / ".github/instructions/style.instructions.md").is_file()
    _init_repo(world)
    _commit_all(world, "deploy kit")


_KIT_ROOTS = (".agents/skills", ".github/instructions")


def test_new_worktree_adopts_committed_link_rewritten_skill(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A worktree with a fresh APM_HOME installs over committed output unchanged."""
    world = _new_world(tmp_path, apm_binary_path)
    v1, _v2, _v3 = _publish_kit(world)
    _committed_kit_project(world, v1)
    worktree = world.isolated.work_root / "consumer-b"
    world.git("worktree", "add", "-q", "-b", "b", str(worktree))
    env = dict(world.env, APM_HOME=str(world.isolated.root / "apm-home-b"))
    Path(env["APM_HOME"]).mkdir()
    assert not list(Path(env["APM_HOME"]).rglob("deployment-receipts*/*.json"))
    before = _tree(worktree, *_KIT_ROOTS)
    lock_before = (worktree / "apm.lock.yaml").read_bytes()

    for _ in range(2):
        result = world.apm("install", cwd=worktree, env=env)
        _assert_nothing_deleted(before, worktree)
        _assert_converged(world, result, worktree)
        assert _tree(worktree, *_KIT_ROOTS) == before
        assert (worktree / "apm.lock.yaml").read_bytes() == lock_before
        assert world.git("status", "--porcelain", cwd=worktree) == ""


def test_branch_switch_back_adopts_committed_link_rewritten_skill(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Receipts written under another branch's lock never refuse or delete this branch's output."""
    world = _new_world(tmp_path, apm_binary_path)
    v1, v2, _v3 = _publish_kit(world)
    _committed_kit_project(world, v1)
    main_tree = _tree(world.project, *_KIT_ROOTS)
    main_lock = (world.project / "apm.lock.yaml").read_bytes()

    world.git("checkout", "-q", "-b", "other")
    dump_yaml(_manifest(v2), world.project / "apm.yml")
    other = world.apm("install")
    _assert_nothing_deleted(main_tree, world.project)
    _assert_converged(world, other, world.project)
    assert _tree(world.project, *_KIT_ROOTS) != main_tree
    _commit_all(world, "move kit to v2")

    world.git("checkout", "-q", "main")
    assert _tree(world.project, *_KIT_ROOTS) == main_tree
    for _ in range(2):
        result = world.apm("install")
        _assert_nothing_deleted(main_tree, world.project)
        _assert_converged(world, result, world.project)
        assert _tree(world.project, *_KIT_ROOTS) == main_tree
        assert (world.project / "apm.lock.yaml").read_bytes() == main_lock
        assert world.git("status", "--porcelain") == ""


def test_pulled_lock_over_gitignored_output_installs(tmp_path: Path, apm_binary_path: Path) -> None:
    """A receipt the file still holds stays trusted when a pull changes the lock."""
    world = _new_world(tmp_path, apm_binary_path)
    v1, v2, _v3 = _publish_kit(world)
    dump_yaml(_manifest(v1), world.project / "apm.yml")
    _write(world.project / ".gitignore", "apm_modules/\n.apm-local/\n.agents/\n.github/\n")
    assert world.apm("install").returncode == 0
    _init_repo(world)
    _commit_all(world, "declare kit v1")

    teammate = world.isolated.work_root / "teammate"
    world.git("clone", "-q", str(world.project), str(teammate))
    world.git("config", "user.email", "test@example.com", cwd=teammate)
    world.git("config", "user.name", "APM Test", cwd=teammate)
    world.git("config", "commit.gpgsign", "false", cwd=teammate)
    env = dict(world.env, APM_HOME=str(world.isolated.root / "apm-home-teammate"))
    Path(env["APM_HOME"]).mkdir()
    dump_yaml(_manifest(v2), teammate / "apm.yml")
    assert world.apm("install", cwd=teammate, env=env).returncode == 0
    _commit_all(world, "bump kit to v2", root=teammate)

    world.git("pull", "-q", "--ff-only", str(teammate), "main")
    notes = world.project / ".agents/skills/alpha/references/notes.md"
    assert notes.read_text(encoding="ascii") == "Notes v1.\n"
    before = _tree(world.project, *_KIT_ROOTS)
    result = world.apm("install")
    _assert_nothing_deleted(before, world.project)
    _assert_converged(world, result, world.project)
    assert notes.read_text(encoding="ascii") == "Notes v2.\n"
    assert world.git("status", "--porcelain") == ""


def test_user_scope_restored_member_adopts_its_render(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """-g: a member restored from backup under a newer receipt is replaced, never deleted."""
    world = _new_world(tmp_path, apm_binary_path)
    v1, v2, v3 = _publish_kit(world)
    apm_home = Path(world.env["APM_HOME"])
    manifest = apm_home / "apm.yml"
    dump_yaml(_manifest(v1, name="user"), manifest)
    first = world.apm("install", "-g", cwd=world.isolated.home)
    assert first.returncode == 0, _output(first)
    home = world.isolated.home
    skill = next(
        path.parent for path in home.rglob("alpha/SKILL.md") if "apm_modules" not in str(path)
    )
    backup = world.isolated.root / "backup"
    shutil.copytree(skill, backup / "skill")
    shutil.copy2(manifest, backup / "apm.yml")
    shutil.copy2(apm_home / "apm.lock.yaml", backup / "apm.lock.yaml")

    dump_yaml(_manifest(v2, name="user"), manifest)
    second = world.apm("install", "-g", cwd=home)
    assert second.returncode == 0, _output(second)
    assert (skill / "references/notes.md").read_text(encoding="ascii") == "Notes v2.\n"

    # Restore the earlier deployment without APM, as a dotfiles checkout would.
    shutil.rmtree(skill)
    shutil.copytree(backup / "skill", skill)
    shutil.copy2(backup / "apm.yml", manifest)
    shutil.copy2(backup / "apm.lock.yaml", apm_home / "apm.lock.yaml")
    dump_yaml(_manifest(v3, name="user"), manifest)
    before = _tree(skill.parent, skill.name)

    result = world.apm("install", "-g", cwd=home)
    _assert_nothing_deleted(before, skill.parent)
    output = _output(result)
    assert result.returncode == 0, output
    assert _REFUSAL not in output and _MODIFIED not in output, output
    assert (skill / "references/notes.md").read_text(encoding="ascii") == "Notes v1.\n"
    assert "Version 3." in (skill / "SKILL.md").read_text(encoding="ascii")


# ---------------------------------------------------------------------------
# #4230: a local .apm skill across branch switches and discarded edits.
# ---------------------------------------------------------------------------

_X = ".agents/skills/x"


def _committed_local_project(world: _World) -> None:
    _init_repo(world)
    init = world.apm("init", "-y", "--target", "copilot")
    assert init.returncode == 0, _output(init)
    _write(world.project / ".apm/skills/x/SKILL.md", _LOCAL_SKILL)
    _write(world.project / ".apm/skills/x/references/a.md", "reference a, version A\n")
    first = world.apm("install")
    assert first.returncode == 0, _output(first)
    _commit_all(world, "A: deploy skill x")


def _deployed(world: _World) -> dict[str, bytes]:
    return _tree(world.project, _X)


@pytest.mark.parametrize("variant", ["clean", "dirty"])
def test_branch_switch_then_source_edit_never_deletes_skill_members(
    tmp_path: Path, apm_binary_path: Path, variant: str
) -> None:
    """Install keeps every member and lock entry; an unprovable edit refuses with a recovery."""
    world = _new_world(tmp_path, apm_binary_path)
    _committed_local_project(world)
    members = set(_deployed(world))
    assert members == {f"{_X}/SKILL.md", f"{_X}/references/a.md"}

    world.git("checkout", "-q", "-b", "B")
    _write(world.project / ".apm/skills/x/SKILL.md", _LOCAL_SKILL + "Read a (B).\n")
    _write(world.project / ".apm/skills/x/references/a.md", "reference a, version B\n")
    assert world.apm("install").returncode == 0
    if variant == "clean":
        _commit_all(world, "B: edit and redeploy")
    else:
        world.git("add", ".apm")
        world.git("commit", "-q", "-m", "B: edit sources only")

    for branch in ("main", "B"):
        world.git("checkout", "-q", branch)
        before = _deployed(world)
        result = world.apm("install")
        _assert_nothing_deleted(before, world.project)
        _assert_converged(world, result, world.project)
        assert set(_deployed(world)) == members

    dirty = bool(world.git("status", "--porcelain"))
    if dirty:
        world.git("stash", "-q", "-u")
    world.git("checkout", "-q", "main")
    if dirty:
        world.git("stash", "drop", "-q")
    edited = _LOCAL_SKILL + "Read a (A2).\n"
    _write(world.project / ".apm/skills/x/SKILL.md", edited)
    before = _deployed(world)
    lock_before = (world.project / "apm.lock.yaml").read_bytes()

    refused = world.apm("install")
    output = _output(refused)
    _assert_nothing_deleted(before, world.project)
    assert refused.returncode == 1, output
    assert _REFUSAL in output, output
    assert f"move {_X}/ aside" in output, output
    assert "then run 'apm install'" in output, output
    assert "Cleaned" not in output, output
    assert _deployed(world) == before
    assert (world.project / "apm.lock.yaml").read_bytes() == lock_before
    assert (world.project / ".apm/skills/x/SKILL.md").read_text(encoding="ascii") == edited

    # Execute the printed recovery: move the deployed skill aside, then install.
    aside = world.isolated.root / "x-aside"
    shutil.move(str(world.project / _X), aside)
    recovered = world.apm("install")
    _assert_converged(world, recovered, world.project)
    assert (world.project / _X / "SKILL.md").read_text(encoding="ascii") == edited
    assert (world.project / _X / "references/a.md").read_text(encoding="ascii") == (
        "reference a, version A\n"
    )
    kept = {
        p.relative_to(aside).as_posix(): p.read_bytes() for p in aside.rglob("*") if p.is_file()
    }
    assert kept == {path[len(_X) + 1 :]: data for path, data in before.items()}


@pytest.mark.parametrize("discard", ["checkout", "stash"])
def test_discarded_deploy_then_source_edit_installs(
    tmp_path: Path, apm_binary_path: Path, discard: str
) -> None:
    """R5/R6: a member restored by git matches its render, so the next edit deploys."""
    world = _new_world(tmp_path, apm_binary_path)
    _committed_local_project(world)
    _write(world.project / ".apm/skills/x/references/a.md", "reference a v2\n")
    assert world.apm("install").returncode == 0
    if discard == "checkout":
        world.git("checkout", "--", ".")
    else:
        world.git("stash", "-q")
    assert world.git("status", "--porcelain") == ""
    edited = _LOCAL_SKILL + "source edit\n"
    _write(world.project / ".apm/skills/x/SKILL.md", edited)
    before = _deployed(world)

    result = world.apm("install")
    _assert_nothing_deleted(before, world.project)
    _assert_converged(world, result, world.project)
    assert (world.project / _X / "SKILL.md").read_text(encoding="ascii") == edited
    assert (world.project / _X / "references/a.md").read_text(encoding="ascii") == (
        "reference a, version A\n"
    )
