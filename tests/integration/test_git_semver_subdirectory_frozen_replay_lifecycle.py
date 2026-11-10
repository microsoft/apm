"""Real-binary Git-semver subdirectory replay contract (#3382).

One local bare monorepo carries ``packages/pkg-a`` at two releases. The root
``v1.0.0`` tag and ``pkg-a-v1.0.0`` point at release 1.0.0; ``pkg-a-v1.1.0``
and ``pkg-a--v1.1.0`` point at release 1.1.0. The consumer declares
``acme/mono/packages/pkg-a#^1.0.0``. Every lifecycle step must deploy the
locked 1.1.0 bytes, keep the semver provenance recorded by the first install
and pass ``apm audit --ci``.

The bare-version layout tags releases ``1.0.0`` / ``1.1.0``. A bare tag is
itself a semver string, so drift must be judged against the declared range.
Frozen steps run with the origin and Git cache removed: a cached replay with
no drift must not fetch.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]

_COMMON = ("--target", "copilot", "--no-policy", "--parallel-downloads", "0")
_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("install", ("install", *_COMMON)),
    ("frozen", ("install", *_COMMON, "--frozen")),
    ("reinstall", ("install", *_COMMON)),
    ("update", ("update", "--yes", "--target", "copilot", "--parallel-downloads", "0")),
    ("frozen-again", ("install", *_COMMON, "--frozen")),
)
_AUDIT = ("audit", "--ci", "--no-policy", "--format", "json")
_SKILL = Path(".agents") / "skills" / "s" / "SKILL.md"
_PROVENANCE = ("constraint", "resolved_tag", "resolved_ref", "resolved_commit", "version")
_LAYOUTS = {
    "issue-3382": (("v1.0.0", "pkg-a-v1.0.0"), ("pkg-a-v1.1.0", "pkg-a--v1.1.0"), "pkg-a-v1.1.0"),
    "bare-version": (("1.0.0",), ("1.1.0",), "1.1.0"),
}


def _skill_bytes(marker: str) -> bytes:
    return f"---\nname: s\ndescription: d\n---\n# {marker}\n".encode()


def _write_release(root: Path, version: str, marker: str) -> None:
    package = root / "packages" / "pkg-a"
    skill = package / ".apm" / "skills" / "s"
    skill.mkdir(parents=True, exist_ok=True)
    dump_yaml({"name": "pkg-a", "version": version, "targets": ["copilot"]}, package / "apm.yml")
    (skill / "SKILL.md").write_bytes(_skill_bytes(marker))


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nreturncode={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


@contextmanager
def _offline(*paths: Path) -> Iterator[None]:
    """Hide the origin and Git cache so a cached replay cannot fetch."""
    moved = [(path, path.with_name(f"{path.name}.offline")) for path in paths if path.exists()]
    for path, hidden in moved:
        path.rename(hidden)
    try:
        yield
    finally:
        for path, hidden in moved:
            shutil.rmtree(path, ignore_errors=True)
            hidden.rename(path)


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_git_semver_subdirectory_replays_locked_release_across_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
    layout: str,
) -> None:
    first_tags, second_tags, expected_tag = _LAYOUTS[layout]
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    source = LocalPackageFactory(isolated.package_root).create("mono", targets=("copilot",))
    _write_release(source.root, "1.0.0", "one")

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("mono", source_tree=source.root)
    first = repositories.commit(repository, message="pkg-a 1.0.0")
    for tag in first_tags:
        repositories.tag(repository, tag, first)
    _write_release(repository.worktree, "1.1.0", "two")
    second = repositories.commit(repository, message="pkg-a 1.1.0")
    for tag in second_tags:
        repositories.tag(repository, tag, second)
    environment = repositories.url_rewrite_subprocess_env(
        repository, "https://github.com/acme/mono.git"
    )

    project = (
        LocalPackageFactory(isolated.work_root)
        .create(
            "consumer",
            dependencies=("acme/mono/packages/pkg-a#^1.0.0",),
            targets=("copilot",),
        )
        .root
    )
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=600
    )
    expected_skill = _skill_bytes("two")
    expected_hash = f"sha256:{hashlib.sha256(expected_skill).hexdigest()}"
    baseline: dict[str, object] | None = None

    for step, args in _STEPS:
        if step.startswith("frozen"):
            with _offline(repository.origin, isolated.cache_root):
                result = runner.run(
                    args, scenario_id=f"3382-{layout}-{step}", cwd=project, env=environment
                )
        else:
            result = runner.run(
                args, scenario_id=f"3382-{layout}-{step}", cwd=project, env=environment
            )
        assert result.returncode == 0, _evidence(result)

        assert (project / _SKILL).read_bytes() == expected_skill, f"{step} deployed wrong release"
        locked = load_yaml(project / "apm.lock.yaml")["dependencies"]
        assert len(locked) == 1
        provenance = {field: locked[0].get(field) for field in _PROVENANCE}
        if baseline is None:
            assert provenance == {
                "constraint": "^1.0.0",
                "resolved_tag": expected_tag,
                "resolved_ref": expected_tag,
                "resolved_commit": second.sha,
                "version": "1.1.0",
            }
            baseline = provenance
        assert provenance == baseline, f"{step} changed semver provenance"
        assert locked[0]["deployed_file_hashes"][_SKILL.as_posix()] == expected_hash

        audit = runner.run(
            _AUDIT, scenario_id=f"3382-{layout}-{step}-audit", cwd=project, env=environment
        )
        assert audit.returncode == 0, _evidence(audit)
        payload = json.loads(audit.stdout)
        assert payload["passed"] is True
        assert payload["summary"]["failed"] == 0
