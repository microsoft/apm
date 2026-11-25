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
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise(
        "P-INV-I7",
        "P-LAW-ownership.preserve_unowned",
        "P-OWN-file-level-deploy-sync-cleanup",
        "P-OWN-unmanaged-skill-preflight",
    ),
]

_REFUSAL = "no trustworthy local deployment baseline"
_MODIFIED = "it was modified since APM deployed it"
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


@pytest.mark.parametrize("apm_home", ["fresh", "shared"])
def test_new_worktree_adopts_committed_link_rewritten_skill(
    tmp_path: Path, apm_binary_path: Path, apm_home: str
) -> None:
    """A second worktree, with its own or the shared APM_HOME, installs over committed output.

    Receipts are keyed per checkout root, so the shared store never vouches across
    worktrees (negative control for #4230).
    """
    world = _new_world(tmp_path, apm_binary_path)
    v1, _v2, _v3 = _publish_kit(world)
    _committed_kit_project(world, v1)
    worktree = world.isolated.work_root / "consumer-b"
    world.git("worktree", "add", "-q", "-b", "b", str(worktree))
    if apm_home == "fresh":
        env = dict(world.env, APM_HOME=str(world.isolated.root / "apm-home-b"))
        Path(env["APM_HOME"]).mkdir()
        assert not list(Path(env["APM_HOME"]).rglob("deployment-receipts*/*.json"))
    else:
        env = world.env
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
    """Install keeps every member; a source edit after the round trip installs (#4428).

    The deployed bytes of the edited skill equal a render APM wrote earlier on
    this checkout, so the receipt history proves them and no recovery step is
    needed. A genuine user edit in another deployed skill is kept and named.
    """
    world = _new_world(tmp_path, apm_binary_path)
    _committed_local_project(world)
    _write(world.project / ".apm/skills/y/SKILL.md", _LOCAL_SKILL.replace("x", "y"))
    assert world.apm("install").returncode == 0
    _commit_all(world, "A: deploy skill y")
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
    other = world.project / ".agents/skills/y/SKILL.md"
    other.write_text(other.read_text(encoding="ascii") + _USER_EDIT, encoding="ascii")
    other_bytes = other.read_bytes()
    before = _deployed(world)

    installed = world.apm("install")
    output = _output(installed)
    _assert_nothing_deleted(before, world.project)
    assert installed.returncode == 0, output
    assert _REFUSAL not in output, output
    assert "--force" not in output, output
    assert "Cleaned" not in output, output
    assert (world.project / ".apm/skills/x/SKILL.md").read_text(encoding="ascii") == edited
    assert (world.project / _X / "SKILL.md").read_text(encoding="ascii") == edited
    assert (world.project / _X / "references/a.md").read_text(encoding="ascii") == (
        "reference a, version A\n"
    )
    # The genuine edit in another skill is kept and named, never overwritten.
    assert other.read_bytes() == other_bytes
    assert _MODIFIED in output, output
    # A further install changes nothing.
    converged = _tree(world.project, ".agents")
    lock_converged = (world.project / "apm.lock.yaml").read_bytes()
    again = world.apm("install")
    assert again.returncode == 0, _output(again)
    assert _tree(world.project, ".agents") == converged
    assert (world.project / "apm.lock.yaml").read_bytes() == lock_converged
    assert set(_deployed(world)) == members


def _discard(world: _World, how: str, root: Path | None = None) -> None:
    """Revert every tracked change the way a user would: checkout, stash or reset."""
    if how == "checkout":
        world.git("checkout", "--", ".", cwd=root)
    elif how == "stash":
        world.git("stash", "-q", cwd=root)
    else:
        world.git("reset", "-q", "--hard", cwd=root)


@pytest.mark.parametrize("discard", ["checkout", "stash", "reset"])
def test_discarded_deploy_then_source_edit_installs(
    tmp_path: Path, apm_binary_path: Path, discard: str
) -> None:
    """R5/R6, the primary #4230 trigger in one checkout (no branch switch, no #4229 receipt).

    Install, edit a.md, install, discard with git, edit SKILL.md, install: the
    git-restored a.md equals its render, so it is neither refused nor cleaned.
    """
    world = _new_world(tmp_path, apm_binary_path)
    _committed_local_project(world)
    _write(world.project / ".apm/skills/x/references/a.md", "reference a v2\n")
    assert world.apm("install").returncode == 0
    _discard(world, discard)
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


def test_member_removed_from_source_is_still_cleaned(tmp_path: Path, apm_binary_path: Path) -> None:
    """Positive control: retention covers kept members only; a dropped member goes."""
    world = _new_world(tmp_path, apm_binary_path)
    _committed_local_project(world)
    (world.project / ".apm/skills/x/references/a.md").unlink()
    _write(world.project / ".apm/skills/x/SKILL.md", _LOCAL_SKILL + "No references.\n")

    result = world.apm("install")
    output = _output(result)
    assert result.returncode == 0, output
    assert _REFUSAL not in output and _MODIFIED not in output, output
    assert not (world.project / _X / "references/a.md").exists()
    assert f"{_X}/references/a.md" not in _locked_files(world.project)
    assert "No references." in (world.project / _X / "SKILL.md").read_text(encoding="ascii")


# ---------------------------------------------------------------------------
# #4230 at user scope, and a trigger that needs no receipt from another lock.
# ---------------------------------------------------------------------------

_USER_GITIGNORE = (
    "/*\n!/.gitignore\n!/pkgs/\n!/.agents/\n/.agents/*\n!/.agents/skills/\n"
    "!/.apm/\n/.apm/*\n!/.apm/apm.yml\n!/.apm/apm.lock.yaml\n"
)


def _user_world(tmp_path: Path, apm_binary_path: Path) -> tuple[_World, Path]:
    """A HOME under git with a local skill package installed with -g."""
    world = _new_world(tmp_path, apm_binary_path)
    home = world.isolated.home
    package = home / "pkgs/x"
    _write(package / "apm.yml", "name: x\nversion: 1.0.0\n")
    _write(package / ".apm/skills/x/SKILL.md", _LOCAL_SKILL)
    _write(package / ".apm/skills/x/references/a.md", "reference a, version A\n")
    manifest = Path(world.env["APM_HOME"]) / "apm.yml"
    _write(
        manifest,
        f"name: user\nversion: 1.0.0\ntargets: [copilot]\n"
        f"dependencies:\n  apm:\n    - {package.as_posix()}\n",
    )
    _write(home / ".gitignore", _USER_GITIGNORE)
    first = world.apm("install", "-g", cwd=home)
    assert first.returncode == 0, _output(first)
    _init_repo(world, home)
    _commit_all(world, "A: deploy skill x", root=home)
    return world, package


@pytest.mark.parametrize("variant", ["clean", "dirty"])
def test_user_scope_branch_switch_then_source_edit_never_deletes_skill_members(
    tmp_path: Path, apm_binary_path: Path, variant: str
) -> None:
    """-g: the #4230 sequence on a HOME under git keeps every member and lock row."""
    world, package = _user_world(tmp_path, apm_binary_path)
    home = world.isolated.home
    lock = Path(world.env["APM_HOME"]) / "apm.lock.yaml"
    members = set(_tree(home, _X))
    assert members == {f"{_X}/SKILL.md", f"{_X}/references/a.md"}

    world.git("checkout", "-q", "-b", "B", cwd=home)
    _write(package / ".apm/skills/x/SKILL.md", _LOCAL_SKILL + "Read a (B).\n")
    _write(package / ".apm/skills/x/references/a.md", "reference a, version B\n")
    assert world.apm("install", "-g", cwd=home).returncode == 0
    if variant == "clean":
        _commit_all(world, "B: edit and redeploy", root=home)
    else:
        world.git("add", "pkgs", cwd=home)
        world.git("commit", "-q", "-m", "B: edit sources only", cwd=home)
    for branch in ("main", "B", "main"):
        if branch == "main" and world.git("status", "--porcelain", cwd=home):
            world.git("stash", "-q", "-u", cwd=home)
            world.git("checkout", "-q", branch, cwd=home)
            world.git("stash", "drop", "-q", cwd=home)
        else:
            world.git("checkout", "-q", branch, cwd=home)
        before = _tree(home, _X)
        result = world.apm("install", "-g", cwd=home)
        _assert_nothing_deleted(before, home)
        assert result.returncode == 0, _output(result)
        assert "Cleaned" not in _output(result), _output(result)
        assert set(_tree(home, _X)) == members

    _write(package / ".apm/skills/x/SKILL.md", _LOCAL_SKILL + "Read a (A2).\n")
    before = _tree(home, _X)
    lock_before = lock.read_bytes()
    result = world.apm("install", "-g", cwd=home)
    output = _output(result)
    _assert_nothing_deleted(before, home)
    assert "Cleaned" not in output, output
    assert set(_tree(home, _X)) == members
    if result.returncode != 0:
        assert lock.read_bytes() == lock_before
    assert set(_locked_files_at(lock)) >= members


def _locked_files_at(lock: Path) -> set[str]:
    data = yaml.safe_load(lock.read_text(encoding="utf-8"))
    files = set(data.get("local_deployed_files") or ())
    for dependency in data.get("dependencies") or ():
        files.update(dependency.get("deployed_files") or ())
    return {path.rstrip("/") for path in files}


@pytest.mark.parametrize("scope", ["project", "user"])
def test_member_skipped_as_modified_is_never_stale(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    """A user-edited member skipped as modified keeps its file and lock row; no cleanup runs."""
    if scope == "project":
        world = _new_world(tmp_path, apm_binary_path)
        _committed_local_project(world)
        root, args = world.project, ("install",)
        lock = world.project / "apm.lock.yaml"
    else:
        world, _package = _user_world(tmp_path, apm_binary_path)
        root, args = world.isolated.home, ("install", "-g")
        lock = Path(world.env["APM_HOME"]) / "apm.lock.yaml"
    edited = root / _X / "SKILL.md"
    edited.write_text(edited.read_text(encoding="ascii") + "USER EDIT\n", encoding="ascii")
    sibling = (root / _X / "references/a.md").read_bytes()
    lock_before = lock.read_bytes()
    before = _tree(root, _X)

    for _ in range(2):
        result = world.apm(*args, cwd=root)
        output = _output(result)
        _assert_nothing_deleted(before, root)
        assert result.returncode == 0, output
        assert _MODIFIED in output, output
        for cleanup_line in ("Kept user-edited file", "Skipped removing", "Cleaned"):
            assert cleanup_line not in output, output
        assert edited.read_text(encoding="ascii").endswith("USER EDIT\n")
        assert (root / _X / "references/a.md").read_bytes() == sibling
        assert lock.read_bytes() == lock_before


_USER_EDIT = "USER EDIT\n"


@pytest.mark.parametrize("case", ["R1", "R2", "R3", "R4"])
def test_user_edit_variants_never_lose_bytes(
    tmp_path: Path, apm_binary_path: Path, case: str
) -> None:
    """No-loss controls beside the R5 trigger: a deployed user edit is kept, nothing is cleaned."""
    world = _new_world(tmp_path, apm_binary_path)
    _committed_local_project(world)
    deployed = world.project / _X / "SKILL.md"
    deployed.write_text(deployed.read_text(encoding="ascii") + _USER_EDIT, encoding="ascii")
    if case in ("R1", "R4"):
        _write(world.project / ".apm/skills/x/references/a.md", "reference a v2\n")
    if case in ("R2", "R4"):
        _write(world.project / ".apm/skills/x/SKILL.md", _LOCAL_SKILL + "source edit\n")
    before = _tree(world.project, _X)

    # R2/R4 bring new bytes for the edited file: the kept edit blocks the
    # requested state, so install exits 1; otherwise it only warns.
    result = world.apm("install")
    output = _output(result)
    _assert_nothing_deleted(before, world.project)
    assert result.returncode == (1 if case in ("R2", "R4") else 0), output
    assert _MODIFIED in output, output
    assert "Cleaned" not in output and "Kept user-edited file" not in output, output
    assert deployed.read_text(encoding="ascii").endswith(_USER_EDIT)
    # R4 fails before its lockfile commits, so it restores what it wrote (#4282).
    expected_a = "reference a v2\n" if case == "R1" else "reference a, version A\n"
    assert (world.project / _X / "references/a.md").read_text(encoding="ascii") == expected_a
    missing = sorted(p for p in _locked_files(world.project) if not (world.project / p).exists())
    assert not missing, missing


@pytest.mark.parametrize("discard", ["checkout", "stash", "reset"])
def test_user_scope_discarded_deploy_then_source_edit_installs(
    tmp_path: Path, apm_binary_path: Path, discard: str
) -> None:
    """-g revert variant of #4230: install, revert deployed files, edit a source, install."""
    world, package = _user_world(tmp_path, apm_binary_path)
    home = world.isolated.home
    lock = Path(world.env["APM_HOME"]) / "apm.lock.yaml"
    _write(package / ".apm/skills/x/references/a.md", "reference a v2\n")
    assert world.apm("install", "-g", cwd=home).returncode == 0
    _discard(world, discard, home)
    assert world.git("status", "--porcelain", cwd=home) == ""
    edited = _LOCAL_SKILL + "source edit\n"
    _write(package / ".apm/skills/x/SKILL.md", edited)
    before = _tree(home, _X)

    result = world.apm("install", "-g", cwd=home)
    output = _output(result)
    _assert_nothing_deleted(before, home)
    assert result.returncode == 0, output
    assert _REFUSAL not in output and _MODIFIED not in output, output
    assert "Cleaned" not in output, output
    assert (home / _X / "SKILL.md").read_text(encoding="ascii") == edited
    assert (home / _X / "references/a.md").read_text(encoding="ascii") == (
        "reference a, version A\n"
    )
    missing = sorted(p for p in _locked_files_at(lock) if not (home / p).exists())
    assert not missing, missing


def _branch_round_trip(world: _World) -> None:
    """Deploy on main and B, round-trip both, then return to main without an install.

    The receipts then hold branch B's bytes while main's checked-out deploys
    hold bytes APM wrote earlier in this installation (#4428).
    """
    _committed_local_project(world)
    world.git("checkout", "-q", "-b", "B")
    _write(world.project / ".apm/skills/x/SKILL.md", _LOCAL_SKILL + "Read a (B).\n")
    _write(world.project / ".apm/skills/x/references/a.md", "reference a, version B\n")
    assert world.apm("install").returncode == 0
    _commit_all(world, "B: edit and redeploy")
    for branch in ("main", "B"):
        world.git("checkout", "-q", branch)
        assert world.apm("install").returncode == 0
    world.git("checkout", "-q", "main")


def test_source_edit_after_branch_round_trip_installs_without_recovery(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Step 5 of the #4230 repro installs the edit directly (#4428).

    The deployed bytes match a render APM wrote earlier in this installation;
    the receipt history proves them, so no manual recovery step is needed.
    """
    world = _new_world(tmp_path, apm_binary_path)
    _branch_round_trip(world)
    edited = _LOCAL_SKILL + "Read a (A2).\n"
    _write(world.project / ".apm/skills/x/SKILL.md", edited)
    before = _deployed(world)

    result = world.apm("install")
    output = _output(result)
    _assert_nothing_deleted(before, world.project)
    assert result.returncode == 0, output
    assert _REFUSAL not in output and _MODIFIED not in output, output
    assert (world.project / _X / "SKILL.md").read_text(encoding="ascii") == edited
    assert (world.project / _X / "references/a.md").read_text(encoding="ascii") == (
        "reference a, version A\n"
    )


def test_user_edit_after_branch_round_trip_is_still_refused(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """History proves only bytes this installation wrote: a user edit stays refused (#4428)."""
    world = _new_world(tmp_path, apm_binary_path)
    _branch_round_trip(world)
    _write(world.project / ".apm/skills/x/SKILL.md", _LOCAL_SKILL + "Read a (A2).\n")
    member = world.project / _X / "references/a.md"
    member.write_text(member.read_text(encoding="ascii") + _USER_EDIT, encoding="ascii")
    before = _deployed(world)
    lock_before = (world.project / "apm.lock.yaml").read_bytes()

    refused = world.apm("install")
    output = _output(refused)
    _assert_nothing_deleted(before, world.project)
    assert refused.returncode == 1, output
    assert _REFUSAL in output, output
    assert f"move {_X}/ aside" in output, output
    assert _deployed(world) == before
    assert (world.project / "apm.lock.yaml").read_bytes() == lock_before


@pytest.mark.parametrize("removed", ["member", "skill"])
def test_removal_after_branch_round_trip_needs_todays_deletion_evidence(
    tmp_path: Path, apm_binary_path: Path, removed: str
) -> None:
    """History is never deletion evidence: removed content is kept without it (#4428).

    After the round trip the receipt holds branch B's bytes and the deployed
    files hold main's bytes; only the history proves those, and cleanup does
    not consult it. Removing one member, or the whole local skill (the
    uninstall of local content), leaves the deployed bytes on disk.
    """
    world = _new_world(tmp_path, apm_binary_path)
    _branch_round_trip(world)
    if removed == "member":
        (world.project / ".apm/skills/x/references/a.md").unlink()
        _write(world.project / ".apm/skills/x/SKILL.md", _LOCAL_SKILL + "No references.\n")
        kept = {"references/a.md": (world.project / _X / "references/a.md").read_bytes()}
    else:
        shutil.rmtree(world.project / ".apm/skills/x")
        kept = {path[len(_X) + 1 :]: data for path, data in _deployed(world).items()}

    result = world.apm("install")
    output = _output(result)
    assert result.returncode == 0, output
    assert {name: (world.project / _X / name).read_bytes() for name in kept} == kept, output
    assert "Cleaned" not in output, output


# ---------------------------------------------------------------------------
# #5399: an older lock taken back (a merge-conflict resolution) while SKILL.md
# changes. The skill's own file that a later install deployed is still this
# installation's write: one install records it and --frozen passes. A file APM
# never wrote at that path stays unmanaged.
# ---------------------------------------------------------------------------

_DEMO = "---\nname: demo\ndescription: Demo skill for a lockfile repro. Use when testing.\n---\n\n# Demo\n"
_DEMO_DIR = ".claude/skills/demo"
_HELPER = f"{_DEMO_DIR}/scripts/helper.py"
_UNMANAGED = "not managed by APM"


def _restored_lock_project(world: _World, *, apm_wrote_helper: bool) -> None:
    init = world.apm("init", "-y", "--target", "claude")
    assert init.returncode == 0, _output(init)
    _write(world.project / ".apm/skills/demo/SKILL.md", _DEMO)
    first = world.apm("install")
    assert first.returncode == 0, _output(first)
    older_lock = (world.project / "apm.lock.yaml").read_bytes()
    _write(world.project / ".apm/skills/demo/scripts/helper.py", "print(1)\n")
    if apm_wrote_helper:
        added = world.apm("install")
        assert added.returncode == 0, _output(added)
        assert _HELPER in _locked_files(world.project)
    else:
        _write(world.project / _HELPER, "print(1)\n")  # hand-made, the same bytes
    (world.project / "apm.lock.yaml").write_bytes(older_lock)
    source = world.project / ".apm/skills/demo/SKILL.md"
    source.write_text(_DEMO + "\nEdited.\n", encoding="ascii")


@pytest.mark.parametrize("sibling", [False, True])
def test_restored_older_lock_records_the_skills_own_file_in_one_install(
    tmp_path: Path, apm_binary_path: Path, sibling: bool
) -> None:
    """A hand-made sibling in the skill directory never blocks reclaiming APM's own file."""
    world = _new_world(tmp_path, apm_binary_path)
    _restored_lock_project(world, apm_wrote_helper=True)
    notes = world.project / _DEMO_DIR / "personal-notes.txt"
    if sibling:
        notes.write_text("my notes\n", encoding="ascii")
    before = _tree(world.project, ".claude")

    installed = world.apm("install")

    output = _output(installed)
    _assert_nothing_deleted(before, world.project)
    _assert_converged(world, installed, world.project)
    assert _UNMANAGED not in output, output
    assert "replaced" not in output, output
    assert _HELPER in _locked_files(world.project), output
    assert (world.project / _HELPER).read_text(encoding="ascii") == "print(1)\n"
    assert (
        (world.project / _DEMO_DIR / "SKILL.md").read_text(encoding="ascii").endswith("Edited.\n")
    )
    lock = (world.project / "apm.lock.yaml").read_bytes()
    converged = _tree(world.project, ".claude")
    frozen = world.apm("install", "--frozen")
    assert frozen.returncode == 0, _output(frozen)
    again = world.apm("install")
    assert again.returncode == 0, _output(again)
    assert (world.project / "apm.lock.yaml").read_bytes() == lock
    assert _tree(world.project, ".claude") == converged
    if sibling:
        assert notes.read_text(encoding="ascii") == "my notes\n"
        assert f"{_DEMO_DIR}/personal-notes.txt" not in _locked_files(world.project)


def test_restored_older_lock_keeps_a_hand_made_file_unmanaged(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """No receipt proves APM wrote the file, even with the source's own bytes."""
    world = _new_world(tmp_path, apm_binary_path)
    _restored_lock_project(world, apm_wrote_helper=False)
    hand_made = (world.project / _HELPER).read_bytes()

    installed = world.apm("install")

    output = _output(installed)
    assert _UNMANAGED in output, output
    assert _HELPER not in _locked_files(world.project), output
    assert (world.project / _HELPER).read_bytes() == hand_made
