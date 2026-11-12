"""Real installed-CLI Cursor ``alwaysApply`` mapping at project and user scope.

Issue #1744 / req-tg-018: an instruction whose normalized ``applyTo`` is
exactly one universal ``**`` deploys as Cursor's always-on rule
(``alwaysApply: true``, no ``globs``). Scoped, mixed, absent, empty and null
``applyTo`` keep today's output. Reinstall is idempotent, replaces a rule an
earlier APM release generated as ``globs: **``, and preserves handwritten
neighboring rules.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

# name -> (applyTo frontmatter lines, expected rule key line or "")
_CASES: dict[str, tuple[str, str]] = {
    "scalar": ("applyTo: '**'\n", "alwaysApply: true\n"),
    "padded": ("applyTo: '  **  '\n", "alwaysApply: true\n"),
    "single-list": ("applyTo:\n  - '**'\n", "alwaysApply: true\n"),
    "scoped": ("applyTo: 'src/**/*.py'\n", "globs: src/**/*.py\n"),
    "multi": ("applyTo: 'src/**/*.py, tests/**/*.py'\n", "globs: src/**/*.py, tests/**/*.py\n"),
    "mixed": ("applyTo:\n  - '**'\n  - 'src/**/*.py'\n", "globs: **, src/**/*.py\n"),
    "absent": ("", ""),
    "empty": ("applyTo: ''\n", ""),
    "null": ("applyTo:\n", ""),
}
_HANDWRITTEN = "---\ndescription: Mine\nalwaysApply: true\n---\nHANDWRITTEN\n"


def _source(name: str) -> str:
    return f"---\ndescription: Rule {name}\n{_CASES[name][0]}---\nBODY_{name}\n"


def _expected(name: str) -> str:
    return f"---\ndescription: Rule {name}\n{_CASES[name][1]}---\n\nBODY_{name}\n"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_cursor_universal_apply_to_installs_always_apply(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_NO_CACHE"] = "1"
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)
    factory = LocalPackageFactory(isolated.package_root / "repository")
    package = factory.create("always-kit", targets=("cursor",))
    for name in _CASES:
        factory.add_instruction(package, name, _source(name))
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create("always-kit", source_tree=package.root)
    repositories.commit(repo, message="seed cursor always-apply cases")
    remote = "https://gitlab.example.invalid/cursor/always-kit.git"
    repositories.install_url_rewrite(repo, remote)
    dependency = {"git": remote, "type": "gitlab", "ref": "main", "alias": "always-kit"}
    project = (
        LocalPackageFactory(isolated.work_root)
        .create("consumer", dependencies=(dependency,), targets=("cursor",))
        .root
    )
    scope = ("--global",) if user_scope else ()
    state_root = isolated.config_root if user_scope else project
    if user_scope:
        dump_yaml(load_yaml(project / "apm.yml"), state_root / "apm.yml")
    target_root = (isolated.home if user_scope else project) / ".cursor"
    rules = target_root / "rules"
    rules.mkdir(parents=True)
    handwritten = rules / "personal.mdc"
    handwritten.write_text(_HANDWRITTEN, encoding="utf-8")
    install = ("install", *scope, "--target", "cursor", "--no-policy", "--parallel-downloads", "0")

    def run(*args: str) -> None:
        result = runner.run(args, scenario_id="cursor-always-apply", cwd=project, env=environment)
        assert result.returncode == 0, f"{args}\n{result.stdout}\n{result.stderr}"

    def snapshot() -> ArtifactSnapshotSet:
        return ArtifactSnapshotSet.capture(
            {"state": state_root, **({"native": target_root} if user_scope else {})}
        )

    def lock_path() -> Path:
        return state_root / "apm.lock.yaml"

    def cursor_records() -> dict[str, dict]:
        return {
            record["value"]: record
            for record in load_yaml(lock_path())["deployments"]
            if record["target"] == "cursor"
        }

    run(*install)
    rendered = {name: (rules / f"{name}.mdc").read_text(encoding="utf-8") for name in _CASES}
    assert rendered == {name: _expected(name) for name in _CASES}
    assert handwritten.read_text(encoding="utf-8") == _HANDWRITTEN
    if user_scope:
        assert not (project / ".cursor").exists()
    records = cursor_records()
    assert ".cursor/rules/personal.mdc" not in records
    for name in _CASES:
        assert records[f".cursor/rules/{name}.mdc"]["content_hash"] == _sha(rules / f"{name}.mdc")

    before = snapshot()
    run(*install)
    assert snapshot() == before

    # Simulate a rule an earlier APM release generated as ``globs: **``: the
    # file and its recorded hash agree, so it is APM-owned and unmodified.
    legacy = rules / "scalar.mdc"
    legacy.write_text(
        "---\ndescription: Rule scalar\nglobs: **\n---\n\nBODY_scalar\n", encoding="utf-8"
    )
    lock = load_yaml(lock_path())
    for record in lock["deployments"]:
        if record["target"] == "cursor" and record["value"] == ".cursor/rules/scalar.mdc":
            record["content_hash"] = _sha(legacy)
    dump_yaml(lock, lock_path())

    run(*install)
    assert legacy.read_text(encoding="utf-8") == _expected("scalar")
    assert cursor_records()[".cursor/rules/scalar.mdc"]["content_hash"] == _sha(legacy)
    assert handwritten.read_text(encoding="utf-8") == _HANDWRITTEN
    assert snapshot() == before

    run(*install)
    assert snapshot() == before
