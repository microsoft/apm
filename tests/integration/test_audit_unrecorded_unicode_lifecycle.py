"""Installed-binary lifecycle contracts for unrecorded Unicode scanning."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.core.deployment_state import (
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.utils.content_hash import compute_file_hash
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_RECORDED = ".claude/skills/alpha/SKILL.md"
_UNRECORDED = ".claude/skills/beta/SKILL.md"
_CLEAN_UNRECORDED = ".claude/skills/gamma/SKILL.md"
_SOURCE_ONLY = ".apm/skills/source-only/SKILL.md"
_OUTSIDE_CURRENT_TARGETS = ".agents/skills/legacy/SKILL.md"
_BIDI_BYTES = b"safe prefix\n\xe2\x80\xaepayload\xe2\x80\xac\n"
_CLEAN_BYTES = b"safe unrecorded content\n"
_CI_AUDIT = ("audit", "--ci", "--no-policy", "--no-drift", "--no-fail-fast")


@dataclass(frozen=True)
class _Lifecycle:
    """One installed project and its isolated public command boundary."""

    isolated: IsolatedApmEnvironment
    project: LocalPackage
    environment: dict[str, str]
    runner: ApmLifecycleRunner
    package_key: str


def _skill(name: str) -> str:
    """Return one stable package skill document."""
    return f"---\nname: {name}\ndescription: Audit lifecycle fixture\n---\n# {name}\n"


def _result_evidence(result: CommandResult) -> str:
    """Render exact process evidence for assertion diagnostics."""
    return (
        f"command={result.command!r}\n"
        f"cwd={result.cwd!s}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _run(
    lifecycle: _Lifecycle,
    args: tuple[str, ...],
    *,
    expected: int,
    scenario_id: str,
) -> CommandResult:
    """Run one installed-binary command and assert its public exit contract."""
    result = lifecycle.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=lifecycle.project.root,
        env=lifecycle.environment,
    )
    assert result.returncode == expected, _result_evidence(result)
    return result


def _install_lifecycle(root: Path, apm_binary_path: Path) -> _Lifecycle:
    """Create local package/Git fixtures and install a governed Claude tree."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    sources = LocalPackageFactory(isolated.package_root)
    source = sources.create("audit-source")
    sources.add_skill(source, "alpha", _skill("alpha"))

    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("audit-source", source_tree=source.root)
    commit = repositories.commit(repository, message="seed audit source")
    remote_url = "https://github.com/apm-fixture-org/audit-source"
    environment = repositories.url_rewrite_subprocess_env(repository, remote_url)

    consumers = LocalPackageFactory(isolated.work_root)
    project = consumers.create(
        "audit-consumer",
        dependencies=(
            {
                "git": remote_url,
                "ref": commit.sha,
                "alias": "audit-source",
            },
        ),
        targets=("claude",),
    )
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )
    install = runner.run(
        ("install", "--no-policy", "--parallel-downloads", "0"),
        scenario_id="unrecorded-unicode-install",
        cwd=project.root,
        env=environment,
    )
    assert install.returncode == 0, _result_evidence(install)
    assert (project.root / _RECORDED).read_text(encoding="utf-8") == _skill("alpha")

    lock = LockFile.read(get_lockfile_path(project.root))
    assert lock is not None
    assert len(lock.dependencies) == 1
    package_key = next(iter(lock.dependencies))
    assert _RECORDED in lock.dependencies[package_key].deployed_files
    return _Lifecycle(isolated, project, environment, runner, package_key)


def _snapshot(lifecycle: _Lifecycle) -> LifecycleStateSnapshot:
    """Capture every durable byte relevant to this audit lifecycle."""
    return LifecycleStateSnapshot.capture(
        lifecycle.project.root,
        targets=("claude",),
        config_paths=(
            PurePosixPath(_UNRECORDED),
            PurePosixPath(_CLEAN_UNRECORDED),
            PurePosixPath(_SOURCE_ONLY),
        ),
    )


def _assert_same_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    """Assert exact manifest, lock, ledger, config, and deployed state equality."""
    assert actual.manifest_bytes == expected.manifest_bytes
    assert actual.lockfile_bytes == expected.lockfile_bytes
    assert actual.deployment_records == expected.deployment_records
    assert actual.mcp_state_bytes == expected.mcp_state_bytes
    assert actual.lsp_state_bytes == expected.lsp_state_bytes
    assert actual.files == expected.files
    assert actual.semantic_bytes == expected.semantic_bytes


def test_unrecorded_unicode_detect_strip_and_idempotent_audit(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The installed CLI detects, preserves state, strips, and re-audits cleanly."""
    lifecycle = _install_lifecycle(tmp_path / "detect-strip", apm_binary_path)
    project_root = lifecycle.project.root
    dangerous = project_root / _UNRECORDED
    dangerous.parent.mkdir(parents=True)
    dangerous.write_bytes(_BIDI_BYTES)
    clean = project_root / _CLEAN_UNRECORDED
    clean.parent.mkdir(parents=True, exist_ok=True)
    clean.write_bytes(_CLEAN_BYTES)
    source_only = project_root / _SOURCE_ONLY
    source_only.parent.mkdir(parents=True)
    source_only.write_bytes(_BIDI_BYTES)

    lock = LockFile.read(get_lockfile_path(project_root))
    assert lock is not None
    assert _UNRECORDED not in lock.dependencies[lifecycle.package_key].deployed_files

    package_audit = _run(
        lifecycle,
        ("audit", lifecycle.package_key),
        expected=0,
        scenario_id="package-audit-remains-lockfile-scoped",
    )
    assert _UNRECORDED not in package_audit.stdout
    assert dangerous.read_bytes() == _BIDI_BYTES

    before_failure = _snapshot(lifecycle)
    failed = _run(
        lifecycle,
        _CI_AUDIT,
        expected=1,
        scenario_id="ci-no-drift-detects-unrecorded-unicode",
    )
    assert "content-integrity" in failed.stdout
    assert f"unicode: {_UNRECORDED}" in failed.stdout
    assert "No critical hidden Unicode or hash drift detected" not in failed.stdout
    assert "All checks passed" not in failed.stdout
    _assert_same_state(before_failure, _snapshot(lifecycle))

    stripped = _run(
        lifecycle,
        ("audit", "--strip"),
        expected=0,
        scenario_id="strip-unrecorded-unicode",
    )
    assert _UNRECORDED in stripped.stdout
    assert b"\xe2\x80\xae" not in dangerous.read_bytes()
    assert b"\xe2\x80\xac" not in dangerous.read_bytes()
    assert clean.read_bytes() == _CLEAN_BYTES
    assert source_only.read_bytes() == _BIDI_BYTES

    exact_count = _run(
        lifecycle,
        ("audit", "--no-drift", "--format", "json"),
        expected=0,
        scenario_id="whole-project-unique-scan-count",
    )
    assert json.loads(exact_count.stdout)["summary"]["files_scanned"] == 3

    after_strip = _snapshot(lifecycle)
    first_clean = _run(
        lifecycle,
        _CI_AUDIT,
        expected=0,
        scenario_id="ci-no-drift-clean-after-strip",
    )
    assert "content-integrity" in first_clean.stdout
    assert "No critical hidden Unicode" in first_clean.stdout
    _assert_same_state(after_strip, _snapshot(lifecycle))

    repeated = _run(
        lifecycle,
        _CI_AUDIT,
        expected=0,
        scenario_id="ci-no-drift-idempotent-repeat",
    )
    assert "check(s) passed" in repeated.stdout
    _assert_same_state(after_strip, _snapshot(lifecycle))


def test_symlinked_deploy_root_never_scans_outside_project(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A governed root symlink cannot expand audit coverage outside the project."""
    lifecycle = _install_lifecycle(tmp_path / "symlink-root", apm_binary_path)
    project_root = lifecycle.project.root
    deploy_root = project_root / ".claude"
    outside = lifecycle.isolated.root / "outside-deploy"
    shutil.move(deploy_root, outside)
    payload = outside / "skills/beta/SKILL.md"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(_BIDI_BYTES)
    deploy_root.symlink_to(outside, target_is_directory=True)

    audit = _run(
        lifecycle,
        _CI_AUDIT,
        expected=0,
        scenario_id="symlinked-deploy-root-contained",
    )
    assert _UNRECORDED not in audit.stdout
    assert payload.read_bytes() == _BIDI_BYTES


def test_recorded_path_outside_current_targets_retains_unicode_coverage(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The lockfile scan remains complementary to current target-tree scanning."""
    lifecycle = _install_lifecycle(tmp_path / "recorded-outside-targets", apm_binary_path)
    project_root = lifecycle.project.root
    legacy = project_root / _OUTSIDE_CURRENT_TARGETS
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(_BIDI_BYTES)

    unrecorded = _run(
        lifecycle,
        _CI_AUDIT,
        expected=0,
        scenario_id="outside-current-targets-unrecorded",
    )
    assert _OUTSIDE_CURRENT_TARGETS not in unrecorded.stdout

    lock = LockFile.read(get_lockfile_path(project_root))
    assert lock is not None
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="codex",
        value=_OUTSIDE_CURRENT_TARGETS,
        runtime=None,
        scope="project",
    )
    lock.deployment_ledger.records[locator.key] = DeploymentRecord(
        locator=locator,
        owners=(lifecycle.package_key,),
        active_owner=lifecycle.package_key,
        content_hash=compute_file_hash(legacy),
    )
    lock.write(get_lockfile_path(project_root))
    persisted = LockFile.read(get_lockfile_path(project_root))
    assert persisted is not None
    assert _OUTSIDE_CURRENT_TARGETS in persisted.dependencies[lifecycle.package_key].deployed_files

    recorded = _run(
        lifecycle,
        _CI_AUDIT,
        expected=1,
        scenario_id="outside-current-targets-recorded",
    )
    assert "content-integrity" in recorded.stdout
    assert f"unicode: {_OUTSIDE_CURRENT_TARGETS}" in recorded.stdout
