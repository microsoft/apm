"""Cold updates track transitive graph changes and re-stamp plugins (#3649).

* A cold ``apm update`` where the new kit release moves one transitive
  dependency, drops another and adds a third (#3571 x #3638).
* A Claude plugin without its own version (#2627): a corrupted module cache
  is re-downloaded and re-stamped in the same process, ``apm install
  --update`` records a hash that later fresh ``--frozen`` installs verify.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.file_ops import robust_rmtree
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._lockfile_chain_kit import (
    KIT_URL,
    Kit,
    evict_machine_state,
    git,
    kit_v100,
    locked_tag,
    output,
    publish_v110,
    skill,
    status,
    tree,
    write,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _url(name: str) -> str:
    return f"https://github.com/test/{name}.git"


def _commit_project(project: Path, env: dict[str, str]) -> None:
    git(project, env, "init", "--initial-branch=main")
    git(project, env, "config", "user.email", "test@example.com")
    git(project, env, "config", "user.name", "APM Test")
    git(project, env, "add", "--all")
    git(project, env, "commit", "-m", "deployed output")


def _fresh_clone(isolated: IsolatedApmEnvironment, env, project: Path, name: str) -> Path:
    clone = isolated.work_root / name
    git(isolated.work_root, env, "clone", "--quiet", str(project), str(clone))
    for receipts in isolated.config_root.glob("deployment-receipts*"):
        robust_rmtree(receipts)
    robust_rmtree(isolated.cache_root)
    isolated.cache_root.mkdir(parents=True)
    return clone


def _locked(project: Path) -> dict[str, str]:
    lock = load_yaml(project / "apm.lock.yaml")
    return {dep["repo_url"]: dep["resolved_commit"] for dep in lock["dependencies"]}


def test_cold_update_moves_drops_and_adds_transitive_dependencies(
    tmp_path, apm_binary_path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())

    source = isolated.package_root / "t1"
    write(source / "SKILL.md", skill("t1", "T1 a."))
    write(source / "ref.md", "t1 ref a\n")
    t1 = factory.create("t1", source_tree=source)
    t1a = factory.commit(t1, message="a")
    write(t1.worktree / "SKILL.md", skill("t1", "T1 b."))
    (t1.worktree / "ref.md").unlink()
    t1b = factory.commit(t1, message="b")

    source = isolated.package_root / "t2"
    write(source / "SKILL.md", skill("t2", "T2."))
    t2 = factory.create("t2", source_tree=source)
    t2a = factory.commit(t2, message="a")

    source = isolated.package_root / "t3"
    source.mkdir(parents=True)
    dump_yaml({"name": "t3", "version": "1.0.0"}, source / "apm.yml")
    write(source / ".apm/instructions/t3.instructions.md", "---\napplyTo: '**'\n---\nT3.\n")
    t3 = factory.create("t3", source_tree=source)
    t3a = factory.commit(t3, message="a")

    source = isolated.package_root / "kit"
    source.mkdir(parents=True)
    dump_yaml(
        {
            "name": "kit",
            "version": "1.0.0",
            "dependencies": {
                "apm": [{"git": _url("t1"), "ref": t1a.sha}, {"git": _url("t3"), "ref": t3a.sha}]
            },
        },
        source / "apm.yml",
    )
    write(source / ".apm/skills/k/SKILL.md", skill("k", "K v1."))
    kit = factory.create("kit", source_tree=source)
    factory.tag(kit, "v1.0.0", factory.commit(kit, message="v1"))
    env = factory.url_rewrite_subprocess_env_many(
        tuple(
            (repo, _url(name)) for name, repo in (("t1", t1), ("t2", t2), ("t3", t3), ("kit", kit))
        )
    )

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude", "copilot"],
            "dependencies": {"apm": ["test/kit#^1.0.0"], "mcp": []},
        },
        project / "apm.yml",
    )
    write(project / ".gitignore", "apm_modules/\n")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, output(first)
    assert set(_locked(project)) == {"test/kit", "test/t1", "test/t3"}
    _commit_project(project, env)

    manifest = load_yaml(kit.worktree / "apm.yml")
    manifest["version"] = "1.1.0"
    manifest["dependencies"]["apm"] = [
        {"git": _url("t1"), "ref": t1b.sha},
        {"git": _url("t2"), "ref": t2a.sha},
    ]
    dump_yaml(manifest, kit.worktree / "apm.yml")
    factory.tag(kit, "v1.1.0", factory.commit(kit, message="v1.1"))

    clone = _fresh_clone(isolated, env, project, "clone")
    result = runner.run(("update", "--yes"), cwd=clone, env=env)
    assert result.returncode == 0, output(result)
    locked = _locked(clone)
    assert set(locked) == {"test/kit", "test/t1", "test/t2"}
    assert locked["test/t1"] == t1b.sha
    for root in (".agents/skills", ".claude/skills"):
        assert "T1 b." in (clone / root / "t1/SKILL.md").read_text(encoding="utf-8")
        assert not (clone / root / "t1/ref.md").exists()
        assert (clone / root / "t2/SKILL.md").is_file()
    assert not (clone / ".github/instructions/t3.instructions.md").exists()
    assert not (clone / ".claude/rules/t3.md").exists()

    after = tree(clone)
    git_after = status(clone, env)
    for command in (
        ("install", "--no-policy", "--frozen"),
        ("install", "--no-policy"),
        ("audit", "--ci", "--no-policy"),
    ):
        again = runner.run(command, cwd=clone, env=env)
        assert again.returncode == 0, output(again)
        assert tree(clone) == after, command
        assert status(clone, env) == git_after, command


def test_plugin_redownload_and_update_restamp_verify_on_fresh_clone(
    tmp_path, apm_binary_path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    source = isolated.package_root / "plug"
    write(source / ".claude-plugin/plugin.json", json.dumps({"name": "plug", "description": "P"}))
    write(source / "agents/pagent.md", "---\nname: pagent\ndescription: pa\n---\nPA v1.\n")
    write(source / "commands/pcmd.md", "---\ndescription: pc\n---\nPC v1.\n")
    plug = factory.create("plug", source_tree=source)
    factory.tag(plug, "v1.0.0", factory.commit(plug, message="v1"))
    env = factory.url_rewrite_subprocess_env(plug, _url("plug"))

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude", "copilot"],
            "dependencies": {"apm": ["test/plug#^1.0.0"], "mcp": []},
        },
        project / "apm.yml",
    )
    write(project / ".gitignore", "apm_modules/\n")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    assert runner.run(("install", "--no-policy"), cwd=project, env=env).returncode == 0
    module = project / "apm_modules/test/plug"
    assert "version: 0.0.0" not in (module / "apm.yml").read_text(encoding="utf-8")
    _commit_project(project, env)
    lock = (project / "apm.lock.yaml").read_bytes()

    agent = module / "agents/pagent.md"
    agent.write_bytes(agent.read_bytes() + b"tampered\n")
    repaired = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert repaired.returncode == 0, output(repaired)
    assert b"tampered" not in agent.read_bytes()
    assert "version: 0.0.0" not in (module / "apm.yml").read_text(encoding="utf-8")
    assert (project / "apm.lock.yaml").read_bytes() == lock

    write(plug.worktree / "agents/pagent.md", "---\nname: pagent\ndescription: pa\n---\nPA v2.\n")
    factory.tag(plug, "v1.1.0", factory.commit(plug, message="v1.1"))
    updated = runner.run(("install", "--update", "--no-policy"), cwd=project, env=env)
    assert updated.returncode == 0, output(updated)
    assert "resolved_tag: v1.1.0" in (project / "apm.lock.yaml").read_text(encoding="utf-8")
    git(project, env, "add", "--all")
    git(project, env, "commit", "-m", "update plug")

    for name in ("fresh-1", "fresh-2"):
        clone = _fresh_clone(isolated, env, project, name)
        frozen = runner.run(("install", "--no-policy", "--frozen"), cwd=clone, env=env)
        assert frozen.returncode == 0, output(frozen)
        assert status(clone, env) == ""


_BASELINE = os.environ.get("APM_BASELINE_BINARY", "")


@pytest.mark.skipif(not _BASELINE, reason="set APM_BASELINE_BINARY to an apm 0.35.x binary")
def test_v0351_deployed_tree_upgrades_without_false_drift(tmp_path, apm_binary_path) -> None:
    """A lock and tree produced by v0.35.1 pass main install/frozen/audit/update."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    kit_v100(isolated.package_root / "kit")
    repository = factory.create("kit", source_tree=isolated.package_root / "kit")
    factory.tag(repository, "v1.0.0", factory.commit(repository, message="v1.0.0"))
    env = factory.url_rewrite_subprocess_env(repository, KIT_URL)
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot", "claude", "codex"],
            "dependencies": {"apm": ["test/kit#^1.0.0"], "mcp": []},
        },
        project / "apm.yml",
    )
    write(project / ".gitignore", "apm_modules/\n")
    baseline = subprocess.run(
        (_BASELINE, "install", "--no-policy"),
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    _commit_project(project, env)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    kit = Kit(isolated, env, runner, factory, repository, project)
    before = tree(project)
    for command in (
        ("install", "--no-policy"),
        ("install", "--no-policy", "--frozen"),
        ("audit", "--ci", "--no-policy"),
    ):
        result = kit.run(*command)
        assert result.returncode == 0, output(result)
        assert tree(project) == before, command

    publish_v110(kit)
    evict_machine_state(kit)
    result = kit.run("update", "--yes")
    assert result.returncode == 0, output(result)
    assert locked_tag(project) == "v1.1.0"
    audit = kit.run("audit", "--ci", "--no-policy")
    assert audit.returncode == 0, output(audit)
