"""Installed-CLI regression: the unpinned-dependency hint is correct and actionable.

Promise (install finalize owner, ``src/apm_cli/install/phases/finalize.py``):
when ``apm install`` reports an unpinned dependency, the remedy it prints works
for that dependency. A transitive dependency is not declared in the consumer's
``apm.yml``, and a repository without release tags cannot satisfy a semver
range, so the hint must name the exact commit pin the lockfile already holds.
Following the hint must pin the dependency and clear the warning.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_PRIMARY = "https://github.com/apm-fixture-org/pinhint-primary"
_SECONDARY = "https://github.com/apm-fixture-org/pinhint-secondary"
_INSTALL = ("install", "--target", "claude", "--parallel-downloads", "0")


def _package(root: Path, name: str, dependencies: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {"name": name, "version": "1.0.0"}
    if dependencies:
        manifest["dependencies"] = {"apm": dependencies}
    dump_yaml(manifest, root / "apm.yml")
    skill = root / ".apm" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\nname: {name}\ndescription: {name}\n---\n\nBody.\n", encoding="utf-8")


def _apm(binary: Path, project: Path, env: dict[str, str], *args: str):
    return subprocess.run(
        (str(binary), *args),
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_unpinned_transitive_hint_names_a_working_pin(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    secondary = repositories.create("pinhint-secondary")
    _package(secondary.worktree, "secondary", [])
    secondary_commit = repositories.commit(secondary, message="secondary").sha
    primary = repositories.create("pinhint-primary")
    _package(primary.worktree, "primary", [_SECONDARY])
    repositories.tag(primary, "v1.0.0", repositories.commit(primary, message="primary"))
    env = repositories.url_rewrite_subprocess_env_many(
        ((primary, _PRIMARY), (secondary, _SECONDARY))
    )
    project = isolated.work_root / "consumer"
    _package(project, "consumer", [f"{_PRIMARY}#v1.0.0"])

    first = _apm(apm_binary_path, project, env, *_INSTALL)
    output = first.stdout + first.stderr
    assert first.returncode == 0, output
    warning = next(line for line in output.splitlines() if "unpinned" in line)
    assert "pinhint-secondary" in warning, output
    pins = re.findall(r"(\S+#[0-9a-f]{7,40})\b", output)
    assert pins, f"the unpinned hint names no commit pin to copy:\n{output}"
    assert any(pin.endswith(secondary_commit[: len(pin.split("#")[1])]) for pin in pins), output

    manifest = load_yaml(project / "apm.yml")
    manifest["dependencies"]["apm"].append(next(p for p in pins if "pinhint-secondary" in p))
    dump_yaml(manifest, project / "apm.yml")
    second = _apm(apm_binary_path, project, env, *_INSTALL)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "unpinned" not in second.stdout + second.stderr
