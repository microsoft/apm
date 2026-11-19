"""Install derives expected state from apm.yml + apm.lock.yaml + locked sources.

Machine-local facts never decide what an install expects:

* A stale ``apm_modules`` copy left by an older lock (#3884): the resolver
  must not read its outdated ``apm.yml`` and widen a locked skill subset.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._lockfile_chain_kit import output, skill, write
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _url(name: str) -> str:
    return f"https://github.com/test/{name}.git"


def _consumer(root: Path, parent_ref: str) -> Path:
    root.mkdir(parents=True)
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [{"git": _url("parent"), "ref": parent_ref}]},
        },
        root / "apm.yml",
    )
    return root


def test_stale_module_copy_keeps_locked_skill_subset_on_first_frozen_install(
    tmp_path, apm_binary_path
) -> None:
    """#3884: a copy from the previous lock is refreshed before resolution reads it."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())

    source = isolated.package_root / "bundle"
    source.mkdir(parents=True)
    dump_yaml({"name": "bundle", "version": "1.0.0"}, source / "apm.yml")
    write(source / "skills/alpha/SKILL.md", skill("alpha", "Alpha."))
    write(source / "skills/beta/SKILL.md", skill("beta", "Beta."))
    bundle = factory.create("bundle", source_tree=source)
    factory.tag(bundle, "v1", factory.commit(bundle, message="bundle"))

    source = isolated.package_root / "parent"
    write(source / ".apm/instructions/parent.instructions.md", "---\napplyTo: '**'\n---\nP.\n")
    manifest = {
        "name": "parent",
        "version": "1.0.0",
        "dependencies": {"apm": [{"git": _url("bundle"), "ref": "v1"}]},
    }
    dump_yaml(manifest, source / "apm.yml")
    parent = factory.create("parent", source_tree=source)
    factory.tag(parent, "v1", factory.commit(parent, message="all skills"))
    manifest["version"] = "1.1.0"
    manifest["dependencies"]["apm"][0]["skills"] = ["alpha"]
    dump_yaml(manifest, parent.worktree / "apm.yml")
    factory.tag(parent, "v2", factory.commit(parent, message="alpha only"))

    env = factory.url_rewrite_subprocess_env_many(
        ((bundle, _url("bundle")), (parent, _url("parent")))
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    install = ("install", "--no-policy", "--parallel-downloads", "0")

    project = _consumer(isolated.work_root / "consumer", "v1")
    first = runner.run(install, cwd=project, env=env)
    assert first.returncode == 0, output(first)
    stale_copy = project / "apm_modules/test/parent/apm.yml"
    assert "skills" not in stale_copy.read_text(encoding="utf-8")

    # A teammate bumps the parent and commits the new lock; this checkout
    # pulls apm.yml + apm.lock.yaml but keeps its previous apm_modules copy.
    upstream = _consumer(isolated.work_root / "upstream", "v2")
    bumped = runner.run(install, cwd=upstream, env=env)
    assert bumped.returncode == 0, output(bumped)
    lock = (upstream / "apm.lock.yaml").read_bytes()
    locked = {dep["repo_url"]: dep for dep in load_yaml(upstream / "apm.lock.yaml")["dependencies"]}
    assert locked["test/bundle"]["skill_subset"] == ["alpha"]
    for name in ("apm.yml", "apm.lock.yaml"):
        (project / name).write_bytes((upstream / name).read_bytes())

    frozen = runner.run((*install, "--frozen"), cwd=project, env=env)
    assert frozen.returncode == 0, output(frozen)
    assert "selections differ" not in output(frozen)
    assert (project / "apm.lock.yaml").read_bytes() == lock
    assert load_yaml(stale_copy)["dependencies"]["apm"][0]["skills"] == ["alpha"]
    assert (project / ".agents/skills/alpha/SKILL.md").is_file()
