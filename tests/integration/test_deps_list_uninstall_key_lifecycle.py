"""``apm deps list`` shows the key ``apm uninstall`` accepts (#3671).

For a user-scope dependency on a self-hosted GitLab declared with
``type: gitlab``, the Package column of ``apm deps list -g`` must be the
host-qualified identity that ``apm deps tree -g`` shows and that
``apm uninstall -g`` selects, and its Source column must say ``gitlab``.
Copying the listed key into ``apm uninstall -g`` removes the dependency.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_HOST = "gitlab.example.invalid"
_PACKAGE = "portable-pkg"
_REMOTE = f"https://{_HOST}/apm-fixture-org/{_PACKAGE}.git"
_KEY = f"{_HOST}/apm-fixture-org/{_PACKAGE}"


def _row(output: str) -> list[str]:
    for line in output.splitlines():
        if _PACKAGE in line:
            return [cell.strip() for cell in re.split(r"[|\u2502]", line) if cell.strip()]
    raise AssertionError(output)


@pytest.mark.parametrize("alias", [False, True], ids=["plain", "alias"])
def test_listed_key_uninstalls_a_self_hosted_gitlab_dependency(
    tmp_path: Path, apm_binary_path: Path, alias: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "gl", base_env=dict(os.environ))
    sources = LocalPackageFactory(isolated.package_root)
    repos = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    package = sources.create(_PACKAGE)
    sources.add_skill(package, "s", "---\nname: s\ndescription: s\n---\ns\n")
    repo = repos.create(_PACKAGE, source_tree=package.root)
    repos.commit(repo, message="seed")
    repos.install_url_rewrite(repo, _REMOTE)
    dependency: dict = {"git": _REMOTE, "type": "gitlab", "ref": "main"}
    if alias:
        dependency["alias"] = _PACKAGE
    manifest = {"name": "user", "version": "0.1.0", "targets": ["claude"]}
    manifest["dependencies"] = {"apm": [dependency]}
    dump_yaml(manifest, isolated.config_root / "apm.yml")
    (isolated.home / ".claude").mkdir(parents=True, exist_ok=True)
    env = isolated.subprocess_env()
    env.update(APM_NON_INTERACTIVE="1", COLUMNS="200")
    cwd = isolated.work_root / "cwd"
    cwd.mkdir(parents=True)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    def ok(*args: str) -> str:
        result = runner.run(args, cwd=cwd, env=env, scenario_id=" ".join(args))
        assert result.returncode == 0, f"{args}\n{result.stdout}\n{result.stderr}"
        return result.stdout + result.stderr

    ok("install", "-g", "--no-policy")
    listed = _row(ok("deps", "list", "-g"))
    assert listed[0] == _KEY, listed
    assert listed[2] == "gitlab", listed
    assert f"{_KEY}@" in ok("deps", "tree", "-g")

    ok("uninstall", "-g", listed[0])
    assert _PACKAGE not in (isolated.config_root / "apm.yml").read_text(encoding="utf-8")
    assert not (isolated.home / ".claude" / "skills" / "s").exists()
