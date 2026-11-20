"""Installed-CLI contract: cleanup deletes only with deployment evidence (#3701).

A lockfile row whose hash matches a file proves the content, not that APM
deployed it. On a committed-output clone (fresh HOME and APM_HOME, so no
local deployment receipts) every lockfile-driven deletion -- install,
update, uninstall and prune -- needs byte equality with what the locked
package version renders. On the deploying machine the local receipts
prove it. Without evidence the file is kept and named, and uninstall stops
keeping the declaration.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.file_ops import robust_rmtree
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_URL = "https://github.com/test/prims.git"
_DEP = "test/prims"
_INSTRUCTION = ".github/instructions/style.instructions.md"
_PROMPT = ".github/prompts/hello.prompt.md"
_AGENT = ".github/agents/helper.agent.md"
_HOOK = ".github/hooks/prims-guard.json"
_DROPPED = ".github/instructions/dropped.instructions.md"
_EDITED = ".github/instructions/edited.instructions.md"
_FORGED = ".github/instructions/user.instructions.md"
_DEPLOYED = (_INSTRUCTION, _PROMPT, _AGENT, _HOOK)
_NO_EVIDENCE = "no local deployment receipt or render of the locked package version"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("ascii"))


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=60,
    ).stdout


def _package_v1(root: Path) -> None:
    root.mkdir(parents=True)
    dump_yaml({"name": "prims", "version": "0.1.0"}, root / "apm.yml")
    _write(root / ".apm/instructions/style.instructions.md", "---\napplyTo: '**'\n---\n# Style\n")
    _write(root / ".apm/instructions/dropped.instructions.md", "---\napplyTo: '**'\n---\n# Gone\n")
    _write(root / ".apm/instructions/edited.instructions.md", "---\napplyTo: '**'\n---\n# Edit\n")
    _write(root / ".apm/prompts/hello.prompt.md", "---\ndescription: Hello\n---\nHello.\n")
    _write(root / ".apm/agents/helper.agent.md", "---\ndescription: Helper\n---\nHelp.\n")
    hook = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "./scripts/g.sh"}]}
            ]
        }
    }
    _write(root / ".apm/hooks/guard.json", json.dumps(hook, indent=2) + "\n")
    _write(root / ".apm/hooks/scripts/g.sh", "#!/bin/sh\necho guard\n")


@dataclass
class _World:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner
    repositories: LocalGitRepositoryFactory
    package: LocalGitRepository
    project: Path

    def apm(self, cwd: Path, *args: str, expect: int = 0) -> str:
        result = self.runner.run(args, cwd=cwd, env=self.env)
        output = result.stdout + result.stderr
        assert result.returncode == expect, f"apm {' '.join(args)}\n{output}"
        return " ".join(output.split())

    @property
    def receipts(self) -> Path:
        return self.isolated.home / ".apm" / "deployment-receipts"

    def evict_machine_state(self) -> None:
        """Emulate a fresh machine: no receipts and no download cache."""
        if self.receipts.exists():
            robust_rmtree(self.receipts)
        if self.isolated.cache_root.exists():
            robust_rmtree(self.isolated.cache_root)
        self.isolated.cache_root.mkdir(parents=True)
        assert not self.receipts.exists()

    def make_source_unreachable(self) -> None:
        shutil.move(str(self.package.origin), str(self.package.origin) + ".gone")
        if self.isolated.cache_root.exists():
            robust_rmtree(self.isolated.cache_root)
        self.isolated.cache_root.mkdir(parents=True)

    def clone(self) -> Path:
        clone = self.isolated.work_root / "fresh-clone"
        _git(
            self.isolated.work_root,
            self.env,
            "clone",
            "--quiet",
            "-c",
            "core.autocrlf=false",
            str(self.project),
            str(clone),
        )
        self.evict_machine_state()
        return clone


def _world(tmp_path: Path, apm_binary_path: Path, *, dependency: str = f"{_DEP}#v0.1.0"):
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    source = isolated.package_root / "prims"
    _package_v1(source)
    package = repositories.create("prims", source_tree=source)
    first = repositories.commit(package, message="release v0.1.0")
    repositories.tag(package, "v0.1.0", first)
    env = repositories.url_rewrite_subprocess_env_many(((package, _URL),))
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [dependency], "mcp": []},
        },
        project / "apm.yml",
    )
    _write(project / ".gitignore", "apm_modules/\n")
    world = _World(
        isolated,
        env,
        ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180),
        repositories,
        package,
        project,
    )
    world.apm(project, "install", "--no-policy")
    for relative in (*_DEPLOYED, _DROPPED, _EDITED):
        assert (project / relative).is_file(), relative
    for args in (
        ("init", "--initial-branch=main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "APM Test"),
        ("config", "core.autocrlf", "false"),
        ("add", "--all"),
        ("commit", "-m", "commit installed output"),
    ):
        _git(project, env, *args)
    return world


def _forge_row(root: Path) -> bytes:
    """Claim a committed user file in the lock with the hash of its own bytes."""
    user = root / _FORGED
    _write(user, "---\napplyTo: '**'\n---\n# My own notes\n")
    lock_path = root / "apm.lock.yaml"
    lock = LockFile.read(lock_path)
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    dependency.deployed_files.append(_FORGED)
    dependency.deployed_file_hashes[_FORGED] = compute_file_hash(user)
    DeploymentLedgerCodec.refresh_from_legacy(lock)
    lock.write(lock_path)
    return user.read_bytes()


def _drop_dependency(root: Path) -> None:
    manifest = load_yaml(root / "apm.yml")
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, root / "apm.yml")


def test_update_on_receipt_less_clone_removes_only_locked_render_bytes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path, dependency=f"{_DEP}#>=0.1.0")
    for relative in ("dropped", "edited"):
        (world.package.worktree / f".apm/instructions/{relative}.instructions.md").unlink()
    manifest = load_yaml(world.package.worktree / "apm.yml")
    dump_yaml({**manifest, "version": "0.2.0"}, world.package.worktree / "apm.yml")
    second = world.repositories.commit(world.package, message="release v0.2.0")
    world.repositories.tag(world.package, "v0.2.0", second)
    clone = world.clone()
    user_bytes = _forge_row(clone)
    _write(clone / _EDITED, "---\napplyTo: '**'\n---\n# Locally edited\n")

    output = world.apm(clone, "update", "--yes")

    assert not (clone / _DROPPED).exists()
    assert (clone / _EDITED).read_bytes() == b"---\napplyTo: '**'\n---\n# Locally edited\n"
    assert (clone / _FORGED).read_bytes() == user_bytes
    assert f"Kept {_FORGED} (from {_DEP}): {_NO_EVIDENCE}" in output, output
    assert f"Kept user-edited file {_EDITED}" in output, output


def test_install_orphan_cleanup_on_receipt_less_clone(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path)
    clone = world.clone()
    user_bytes = _forge_row(clone)
    _drop_dependency(clone)

    output = world.apm(clone, "install", "--no-policy")

    for relative in _DEPLOYED:
        assert not (clone / relative).exists(), relative
    assert (clone / _FORGED).read_bytes() == user_bytes
    assert f"Kept {_FORGED}: {_NO_EVIDENCE}" in output, output


@pytest.mark.parametrize("source", ["modules-present", "source-only", "unavailable"])
def test_pre_receipt_uninstall(tmp_path: Path, apm_binary_path: Path, source: str) -> None:
    """An upgrade from a CLI without receipts uninstalls through the locked render."""
    world = _world(tmp_path, apm_binary_path)
    clone = world.clone()
    if source == "modules-present":
        world.apm(clone, "install", "--no-policy")
        assert (clone / "apm_modules").is_dir()
        world.evict_machine_state()
    if source != "source-only":
        world.make_source_unreachable()

    if source == "unavailable":
        output = world.apm(clone, "uninstall", _DEP, expect=1)
        assert "Resolve or remove the listed files, then retry uninstall." in output, output
        for relative in _DEPLOYED:
            assert (clone / relative).is_file(), relative
            assert relative in output, output
        assert _DEP in json.dumps(load_yaml(clone / "apm.yml"))
        return

    world.apm(clone, "uninstall", _DEP)
    for relative in (*_DEPLOYED, _DROPPED, _EDITED):
        assert not (clone / relative).exists(), relative


def test_prune_on_receipt_less_clone_renders_before_removing_sources(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path)
    clone = world.clone()
    world.apm(clone, "install", "--no-policy")
    world.evict_machine_state()
    world.make_source_unreachable()
    user_bytes = _forge_row(clone)
    _drop_dependency(clone)

    output = world.apm(clone, "prune")

    for relative in _DEPLOYED:
        assert not (clone / relative).exists(), relative
    assert (clone / _FORGED).read_bytes() == user_bytes
    assert f"Kept {_FORGED}: {_NO_EVIDENCE}" in output, output


def test_deploying_machine_uninstall_uses_receipts(tmp_path: Path, apm_binary_path: Path) -> None:
    """No render is possible, yet every primitive goes: the receipts prove it."""
    world = _world(tmp_path, apm_binary_path)
    assert world.receipts.is_dir()
    world.make_source_unreachable()

    world.apm(world.project, "uninstall", _DEP)

    for relative in (*_DEPLOYED, _DROPPED, _EDITED):
        assert not (world.project / relative).exists(), relative


@pytest.mark.parametrize("provenance", ["receipt", "receipt-less", "edited"])
def test_cowork_row_removed_only_with_valid_provenance(
    tmp_path: Path, apm_binary_path: Path, provenance: str
) -> None:
    """cowork:// rows carry hashes now and lose their cleanup exemption."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    onedrive = isolated.root / "onedrive"
    onedrive.mkdir()
    base_env = {**isolated.subprocess_env(), "APM_COPILOT_COWORK_SKILLS_DIR": str(onedrive)}
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=base_env)
    source = isolated.package_root / "cowork-kit"
    source.mkdir(parents=True)
    dump_yaml({"name": "cowork-kit", "version": "0.1.0"}, source / "apm.yml")
    _write(source / ".apm/skills/demo/SKILL.md", "---\nname: demo\ndescription: Demo\n---\nDemo.\n")
    package = repositories.create("cowork-kit", source_tree=source)
    repositories.tag(package, "v0.1.0", repositories.commit(package, message="v0.1.0"))
    env = repositories.url_rewrite_subprocess_env_many(
        ((package, "https://github.com/test/cowork-kit.git"),)
    )
    world = _World(
        isolated,
        env,
        ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180),
        repositories,
        package,
        isolated.work_root,
    )
    cwd = isolated.work_root
    world.apm(cwd, "experimental", "enable", "copilot-cowork")
    world.apm(
        cwd,
        "install",
        "-g",
        "test/cowork-kit#v0.1.0",
        "--target",
        "copilot-cowork",
        "--no-policy",
    )
    skill = onedrive / "demo" / "SKILL.md"
    assert skill.is_file()
    lock = LockFile.read(isolated.home / ".apm" / "apm.lock.yaml")
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    assert dependency.deployed_file_hashes["cowork://skills/demo/SKILL.md"] == (
        compute_file_hash(skill)
    )
    if provenance == "receipt-less":
        world.evict_machine_state()
    if provenance == "edited":
        skill.write_bytes(b"---\nname: demo\ndescription: Demo\n---\nMine now.\n")
    before = skill.read_bytes()

    output = world.apm(cwd, "uninstall", "-g", "test/cowork-kit")

    if provenance == "receipt":
        assert not skill.exists()
        return
    # The drift replay renders no experimental external target, so a cowork
    # row without a matching receipt has no evidence and is kept, named.
    assert skill.read_bytes() == before
    expected = (
        "file has been edited since APM deployed it" if provenance == "edited" else _NO_EVIDENCE
    )
    assert "cowork://skills/demo/SKILL.md" in output and expected in output, output
