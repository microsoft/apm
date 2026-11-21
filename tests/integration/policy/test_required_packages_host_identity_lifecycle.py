"""Lifecycle: ``dependencies.require`` on a non-default git host (#3993).

A consumer installs one package from ``gitlab.example.test`` and one from
the default host ``github.com`` (local bare repositories behind git URL
rewrites, so no network), then runs ``apm audit --ci --policy``. The
host-qualified entry must pass BOTH ``required-packages`` (manifest) and
``required-packages-deployed`` (lockfile, whose key carries the host). A
hostless entry names the default host: it keeps passing for the github.com
package, and for the GitLab package it fails both checks with a message that
names the qualified form. A package that is not installed fails both checks
rather than being skipped by one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

_HOST = "gitlab.example.test"
_PACKAGE = "group/sub/pkg"
_QUALIFIED = f"{_HOST}/{_PACKAGE}"
_DEFAULT_HOST_PACKAGE = "contoso/pkg"
_PRESENCE_CHECKS = ("required-packages", "required-packages-deployed")


def _checks(result: CommandResult) -> dict[str, dict]:
    return {check["name"]: check for check in json.loads(result.stdout)["checks"]}


@pytest.fixture
def installed(tmp_path: Path, apm_engine_command: tuple[str, ...]):
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env={})
    env = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    bundle = packages.create("pkg", targets=("copilot",))
    packages.add_skill(
        bundle, "pkg-skill", "---\nname: pkg-skill\ndescription: host fixture\n---\n# pkg\n"
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = repositories.create("pkg", source_tree=bundle.root)
    repositories.tag(repository, "v1.0.0", repositories.commit(repository, message="v1.0.0"))
    other = packages.create("other", targets=("copilot",))
    packages.add_skill(
        other, "other-skill", "---\nname: other-skill\ndescription: default host\n---\n# o\n"
    )
    default_repo = repositories.create("other", source_tree=other.root)
    repositories.tag(default_repo, "v1.0.0", repositories.commit(default_repo, message="v1"))
    env = repositories.url_rewrite_subprocess_env_many(
        (
            (repository, f"https://{_QUALIFIED}.git"),
            (default_repo, f"https://github.com/{_DEFAULT_HOST_PACKAGE}.git"),
        )
    )
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=(f"{_QUALIFIED}#v1.0.0", f"{_DEFAULT_HOST_PACKAGE}#v1.0.0"),
        targets=("copilot",),
    )
    runner = ApmLifecycleRunner(apm_engine_command, scenario_timeout_seconds=300)
    result = runner.run(
        ("install", "--target", "copilot", "--parallel-downloads", "0"),
        scenario_id="install",
        cwd=project.root,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = (load_yaml(project.root / "apm.lock.yaml") or {}).get("dependencies", [])
    assert sorted((row["repo_url"], row.get("host")) for row in rows) == [
        (_DEFAULT_HOST_PACKAGE, "github.com"),
        (_PACKAGE, _HOST),
    ]

    def audit(entry: str) -> CommandResult:
        policy = project.root / "org-policy.yml"
        dump_yaml(
            {
                "name": "require-on-host",
                "version": "1.0.0",
                "enforcement": "block",
                "dependencies": {"require": [entry]},
            },
            policy,
        )
        return runner.run(
            ("audit", "--ci", "--no-fail-fast", "--policy", str(policy), "--format", "json"),
            scenario_id=f"audit-{entry}",
            cwd=project.root,
            env=env,
        )

    return audit


@pytest.mark.parametrize(
    "entry",
    [_QUALIFIED, _DEFAULT_HOST_PACKAGE, f"github.com/{_DEFAULT_HOST_PACKAGE}"],
    ids=["non-default-host-qualified", "default-host-hostless", "default-host-qualified"],
)
def test_matching_require_passes_both_presence_checks(installed, entry: str) -> None:
    result = installed(entry)

    checks = _checks(result)
    assert result.returncode == 0, result.stdout
    for name in _PRESENCE_CHECKS:
        assert checks[name]["passed"], checks[name]


@pytest.mark.parametrize("entry", [_PACKAGE, f"{_HOST}/group/sub/other"])
def test_unmatched_require_fails_both_presence_checks(installed, entry: str) -> None:
    result = installed(entry)

    checks = _checks(result)
    assert result.returncode != 0
    for name in _PRESENCE_CHECKS:
        assert not checks[name]["passed"], checks[name]
        assert checks[name]["details"] == [entry]
        if entry == _PACKAGE:
            assert f"change the policy require entry to '{_QUALIFIED}'" in checks[name]["message"]
