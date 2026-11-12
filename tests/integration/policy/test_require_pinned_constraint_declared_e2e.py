"""Real-binary contract for #3224: ``require_pinned_constraint`` judges the
declared constraint, and recognises this package's own release tags.

One local bare monorepo publishes a subdirectory package ``pkg-a``. The
repository root is tagged ``v1.0.0`` at the first commit only; the package
is tagged ``pkg-a-v1.1.0`` and ``pkg-a--v1.1.0`` at the second. A consumer
declaring ``^1.0.0`` therefore resolves to ``pkg-a-v1.1.0``, which is the
tag the old gate classified as a bare branch after resolution rewrote the
in-memory reference.

Policy reaches ``install`` the way the Govern contract does: the project
carries an ADO-shaped origin, the strict policy lives behind a leaf whose
path doubles as auto-discovery's cache key, and ``apm policy status`` warms
the cache so the offline install, frozen replay, update and audit all
enforce the same policy. Every governed command asserts the cached-policy
diagnostic, so a pass can never come from the policy silently not loading.

This contract owns one verdict: ``dependency-pinned-constraint``. Audits
are read as JSON and judged by that check, not by the exit code, because
other checks report on what replay records for this package shape and that
belongs to the lock contracts.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_POLICY_SOURCE = "dev.azure.com/contoso/apm/apm-policy"
_POLICY_REMOTE = "https://dev.azure.com/contoso/project/_git/pinned-declared"
_POLICY_DIAGNOSTIC = f"Policy: org:{_POLICY_SOURCE} (cached"
_GIT_URL = "https://github.com/acme/mono.git"
_SUBDIR_DEP = "acme/mono/packages/pkg-a"
_ROOT_DEP = "acme/mono"
_SKILL = "pkg-a-skill"
_DEPLOYED_SKILL = Path(".agents") / "skills" / _SKILL / "SKILL.md"
_COMMON = ("--target", "copilot", "--parallel-downloads", "0")
_AUDIT = (
    "audit",
    "--ci",
    "--no-fail-fast",
    "--no-drift",
    "--format",
    "json",
    "--policy",
    _POLICY_SOURCE,
)


def _skill_document(marker: str) -> str:
    return (
        f"---\nname: {_SKILL}\ndescription: Declared-constraint contract skill\n---\n\n# {marker}\n"
    )


def _write_subpackage(repo_root: Path, version: str, marker: str) -> None:
    pkg = repo_root / "packages" / "pkg-a"
    (pkg / ".apm" / "skills" / _SKILL).mkdir(parents=True, exist_ok=True)
    dump_yaml({"name": "pkg-a", "version": version, "targets": ["copilot"]}, pkg / "apm.yml")
    (pkg / ".apm" / "skills" / _SKILL / "SKILL.md").write_text(_skill_document(marker))


def _publish_monorepo(
    isolated: IsolatedApmEnvironment, env: dict[str, str]
) -> tuple[dict[str, str], Callable[[], None]]:
    packages = LocalPackageFactory(isolated.package_root)
    bundle = packages.create("mono", targets=("copilot",))
    packages.add_skill(
        bundle, "mono-skill", "---\nname: mono-skill\ndescription: root\n---\n# root\n"
    )
    _write_subpackage(bundle.root, "1.0.0", "pkg-a one")

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = repositories.create("mono", source_tree=bundle.root)
    first = repositories.commit(repository, message="pkg-a 1.0.0")
    repositories.tag(repository, "v1.0.0", first)
    repositories.tag(repository, "pkg-a-v1.0.0", first)

    _write_subpackage(repository.worktree, "1.1.0", "pkg-a two")
    second = repositories.commit(repository, message="pkg-a 1.1.0")
    repositories.tag(repository, "pkg-a-v1.1.0", second)
    repositories.tag(repository, "pkg-a--v1.1.0", second)

    env = repositories.url_rewrite_subprocess_env(repository, _GIT_URL)

    def publish_next() -> None:
        # A newer compatible release, so ``apm update`` has something to
        # select and must run the gate against the declared range again.
        _write_subpackage(repository.worktree, "1.2.0", "pkg-a three")
        third = repositories.commit(repository, message="pkg-a 1.2.0")
        repositories.tag(repository, "pkg-a-v1.2.0", third)

    return env, publish_next


def _git(args: tuple[str, ...], *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=30)


def _consumer(root: Path, name: str, dependency: str, env: dict[str, str]) -> Path:
    project = LocalPackageFactory(root).create(
        name, dependencies=(dependency,), targets=("copilot",)
    )
    _git(("git", "init", "--initial-branch=main"), cwd=project.root, env=env)
    _git(("git", "remote", "add", "origin", _POLICY_REMOTE), cwd=project.root, env=env)
    # The strict policy is a separate file and the leaf extends it: only a
    # leaf with ``extends`` goes through chain resolution, which is what
    # persists the merged policy under the leaf's path. That path is also
    # the cache key auto-discovery derives from the ADO origin, so the
    # offline install/update/frozen runs below enforce it as ``cached``.
    strict = project.root / ".apm-policy" / "strict.yml"
    strict.parent.mkdir()
    dump_yaml(
        {
            "name": "pinned-declared-contract",
            "version": "1",
            "enforcement": "block",
            "fetch_failure": "block",
            "dependencies": {"require_pinned_constraint": True},
        },
        strict,
    )
    leaf = project.root.joinpath(*PurePosixPath(_POLICY_SOURCE).parts)
    leaf.parent.mkdir(parents=True)
    dump_yaml(
        {"name": "pinned-declared-leaf", "extends": strict.relative_to(project.root).as_posix()},
        leaf,
    )
    return project.root


def _run(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    scenario_id: str,
    expected: int | None,
) -> CommandResult:
    result = runner.run(args, scenario_id=scenario_id, cwd=cwd, env=env)
    assert expected is None or result.returncode == expected, (
        f"{scenario_id}: expected exit {expected}, got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result


def _governed(result: CommandResult) -> CommandResult:
    """Fail loudly if the command ran without the cached org policy in force."""
    text = result.stdout + result.stderr
    assert _POLICY_DIAGNOSTIC in text, f"policy was not enforced from cache:\n{text}"
    return result


def _warm_policy(runner: ApmLifecycleRunner, cwd: Path, env: dict[str, str], tag: str) -> None:
    _run(
        runner,
        ("policy", "status", "--policy-source", _POLICY_SOURCE, "--json", "--check", "--no-cache"),
        cwd=cwd,
        env=env,
        scenario_id=f"{tag}-warm-policy",
        expected=0,
    )


def _pinned_verdict(runner, cwd: Path, env: dict[str, str], tag: str) -> dict[str, object]:
    """Audit under the policy and return the ``dependency-pinned-constraint`` check."""
    audit = _run(runner, _AUDIT, cwd=cwd, env=env, scenario_id=f"{tag}-audit", expected=None)
    payload = json.loads(audit.stdout)
    assert payload["policy"]["enforced"] is True, payload["policy"]
    checks = {check["name"]: check for check in payload["checks"]}
    assert "dependency-pinned-constraint" in checks, sorted(checks)
    return checks["dependency-pinned-constraint"]


def _lock_entry(project_root: Path) -> dict[str, object]:
    lock = load_yaml(project_root / "apm.lock.yaml")
    deps = lock["dependencies"]
    assert len(deps) == 1, deps
    return deps[0]


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    root = tmp_path_factory.mktemp("pinned-declared")
    isolated = IsolatedApmEnvironment.create(root / "isolated", base_env=dict(os.environ))
    env, publish_next = _publish_monorepo(isolated, isolated.subprocess_env())
    return isolated, env, publish_next


def test_declared_range_passes_across_install_replay_update_and_audit(scenario, apm_binary_path):
    isolated, env, publish_next = scenario
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=600
    )
    project = _consumer(isolated.work_root, "range-consumer", f"{_SUBDIR_DEP}#^1.0.0", env)
    _warm_policy(runner, project, env, "range")

    # Fresh install: the range resolves to the package-qualified tag, which
    # is exactly what the gate used to reject after the in-memory rewrite.
    fresh = _governed(
        _run(
            runner,
            ("install", *_COMMON),
            cwd=project,
            env=env,
            scenario_id="range-fresh",
            expected=0,
        )
    )
    assert "blocked by org policy" not in (fresh.stdout + fresh.stderr).lower()
    entry = _lock_entry(project)
    assert entry["constraint"] == "^1.0.0", entry
    assert entry["resolved_tag"] == "pkg-a-v1.1.0", entry
    assert "pkg-a two" in (project / _DEPLOYED_SKILL).read_text()
    assert _pinned_verdict(runner, project, env, "range-fresh")["passed"] is True

    # Frozen replay and a repeat ordinary install re-run the gate against
    # the manifest range; the verdict must not move.
    for tag, args in (
        ("range-frozen", ("install", *_COMMON, "--frozen")),
        ("range-repeat-install", ("install", *_COMMON)),
    ):
        _governed(_run(runner, args, cwd=project, env=env, scenario_id=tag, expected=0))
        assert _pinned_verdict(runner, project, env, tag)["passed"] is True

    # A deliberate update selects the newer compatible release; the gate
    # still judges the declared range, not the newly selected tag.
    publish_next()
    _governed(
        _run(
            runner,
            ("update", "--yes", *_COMMON),
            cwd=project,
            env=env,
            scenario_id="range-update",
            expected=0,
        )
    )
    entry = _lock_entry(project)
    assert entry["constraint"] == "^1.0.0", entry
    assert entry["resolved_tag"] == "pkg-a-v1.2.0", entry
    assert "pkg-a three" in (project / _DEPLOYED_SKILL).read_text()
    assert _pinned_verdict(runner, project, env, "range-update")["passed"] is True

    manifest = load_yaml(project / "apm.yml")
    assert manifest["dependencies"]["apm"] == [f"{_SUBDIR_DEP}#^1.0.0"], manifest


@pytest.mark.parametrize(
    ("name", "reference"),
    [
        ("literal-dash", "pkg-a-v1.1.0"),
        ("literal-double-dash", "pkg-a--v1.1.0"),
    ],
)
def test_package_qualified_literal_tags_install_and_audit(
    scenario, apm_binary_path, name, reference
):
    isolated, env, _ = scenario
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=600
    )
    project = _consumer(isolated.work_root, f"{name}-consumer", f"{_SUBDIR_DEP}#{reference}", env)
    _warm_policy(runner, project, env, name)
    _governed(
        _run(
            runner,
            ("install", *_COMMON),
            cwd=project,
            env=env,
            scenario_id=f"{name}-install",
            expected=0,
        )
    )
    assert "pkg-a two" in (project / _DEPLOYED_SKILL).read_text()
    assert _pinned_verdict(runner, project, env, f"{name}-fresh")["passed"] is True
    _governed(
        _run(
            runner,
            ("install", *_COMMON, "--frozen"),
            cwd=project,
            env=env,
            scenario_id=f"{name}-frozen",
            expected=0,
        )
    )
    assert _pinned_verdict(runner, project, env, f"{name}-frozen")["passed"] is True


def test_root_repository_range_still_passes(scenario, apm_binary_path):
    isolated, env, _ = scenario
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=600
    )
    project = _consumer(isolated.work_root, "root-range-consumer", f"{_ROOT_DEP}#^1.0.0", env)
    _warm_policy(runner, project, env, "root-range")
    _governed(
        _run(
            runner,
            ("install", *_COMMON),
            cwd=project,
            env=env,
            scenario_id="root-range-install",
            expected=0,
        )
    )
    entry = _lock_entry(project)
    assert entry["constraint"] == "^1.0.0" and entry["resolved_tag"] == "v1.0.0", entry
    assert _pinned_verdict(runner, project, env, "root-range")["passed"] is True


@pytest.mark.parametrize(
    ("name", "reference", "hint"),
    [
        ("branch", "main", "bare branch 'main'"),
        ("other-package-tag", "pkg-b-v1.1.0", "bare branch 'pkg-b-v1.1.0'"),
        ("open-upper", ">=1.0.0", "unbounded upper"),
    ],
)
def test_unbounded_declarations_stay_blocked(scenario, apm_binary_path, name, reference, hint):
    isolated, env, _ = scenario
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=600
    )
    project = _consumer(isolated.work_root, f"{name}-consumer", f"{_SUBDIR_DEP}#{reference}", env)
    _warm_policy(runner, project, env, name)
    blocked = _governed(
        _run(
            runner,
            ("install", *_COMMON),
            cwd=project,
            env=env,
            scenario_id=f"{name}-install",
            expected=1,
        )
    )
    text = blocked.stdout + blocked.stderr
    assert "blocked by org policy" in text.lower(), text
    assert hint in text, text
    assert not (project / "apm.lock.yaml").exists()
    verdict = _pinned_verdict(runner, project, env, name)
    assert verdict["passed"] is False, verdict
    assert any(hint in detail for detail in verdict["details"]), verdict


def _set_manifest_dependencies(project: Path, *, apm: list, dev_apm: list | None = None) -> None:
    manifest = load_yaml(project / "apm.yml")
    manifest["dependencies"] = {"apm": apm}
    if dev_apm is not None:
        manifest["devDependencies"] = {"apm": dev_apm}
    dump_yaml(manifest, project / "apm.yml")


def test_object_form_range_passes_install_and_audit(scenario, apm_binary_path):
    isolated, env, _ = scenario
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=600
    )
    project = _consumer(isolated.work_root, "object-consumer", f"{_SUBDIR_DEP}#^1.0.0", env)
    _set_manifest_dependencies(
        project, apm=[{"git": _GIT_URL, "path": "packages/pkg-a", "ref": "^1.0.0"}]
    )
    _warm_policy(runner, project, env, "object")
    _governed(
        _run(
            runner,
            ("install", *_COMMON),
            cwd=project,
            env=env,
            scenario_id="object-install",
            expected=0,
        )
    )
    entry = _lock_entry(project)
    assert entry["constraint"] == "^1.0.0", entry
    # The range test may already have published pkg-a-v1.2.0 in this module.
    assert entry["resolved_tag"] in {"pkg-a-v1.1.0", "pkg-a-v1.2.0"}, entry
    assert _pinned_verdict(runner, project, env, "object")["passed"] is True


def test_bounded_dev_declaration_does_not_cover_a_branch(scenario, apm_binary_path):
    # One package declared twice shares a unique key: a bounded
    # devDependencies entry must not stand in for the branch in dependencies.
    isolated, env, _ = scenario
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=600
    )
    project = _consumer(isolated.work_root, "dup-consumer", f"{_SUBDIR_DEP}#main", env)
    _set_manifest_dependencies(
        project, apm=[f"{_SUBDIR_DEP}#main"], dev_apm=[f"{_SUBDIR_DEP}#pkg-a-v1.1.0"]
    )
    _warm_policy(runner, project, env, "dup")
    blocked = _governed(
        _run(
            runner,
            ("install", *_COMMON),
            cwd=project,
            env=env,
            scenario_id="dup-install",
            expected=1,
        )
    )
    text = blocked.stdout + blocked.stderr
    assert "blocked by org policy" in text.lower(), text
    assert "bare branch 'main'" in text, text
    assert not (project / "apm.lock.yaml").exists()
    verdict = _pinned_verdict(runner, project, env, "dup")
    assert verdict["passed"] is False, verdict
    assert any("bare branch 'main'" in detail for detail in verdict["details"]), verdict
