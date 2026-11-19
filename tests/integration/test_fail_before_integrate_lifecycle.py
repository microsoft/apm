"""Installed-CLI contract: a failed multi-package install integrates nothing (#3874).

Every requested package resolves and downloads before any is integrated, so
when one direct dependency fails, the packages that succeeded leave no
deployed, untracked files behind. A retry with only the valid package
installs and locks it normally.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._lockfile_chain_kit import output, skill
from tests.integration._path_corner_world_kit import write
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]


def _url(name: str) -> str:
    return f"https://github.com/test/{name}.git"


def test_failed_direct_dependency_integrates_nothing(tmp_path, apm_binary_path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())

    source = isolated.package_root / "good"
    write(source / "SKILL.md", skill("good", "Good."))
    good = factory.create("good", source_tree=source)
    factory.tag(good, "v1", factory.commit(good, message="skill"))

    source = isolated.package_root / "notapkg"
    write(source / "README.md", "Not an APM package.\n")
    bad = factory.create("notapkg", source_tree=source)
    factory.tag(bad, "v1", factory.commit(bad, message="docs only"))

    env = factory.url_rewrite_subprocess_env_many(((good, _url("good")), (bad, _url("notapkg"))))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    project = isolated.work_root / "consumer"
    project.mkdir(parents=True)
    dump_yaml(
        {"name": "consumer", "version": "1.0.0", "targets": ["claude"]}, project / "apm.yml"
    )
    (project / ".claude").mkdir()
    install = ("install", "--no-policy", "--parallel-downloads", "0")

    failed = runner.run(
        (*install, "test/good#v1", "test/notapkg#v1"), cwd=project, env=env
    )

    assert failed.returncode != 0, output(failed)
    assert not (project / ".claude/skills/good").exists(), output(failed)
    assert not (project / "apm.lock.yaml").exists()

    retry = runner.run((*install, "test/good#v1"), cwd=project, env=env)

    assert retry.returncode == 0, output(retry)
    assert (project / ".claude/skills/good/SKILL.md").is_file()
    assert "test/good" in (project / "apm.lock.yaml").read_text(encoding="utf-8")
