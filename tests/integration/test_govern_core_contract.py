"""Real-binary Govern contract from authored policy to enforced state."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.policy.parser import load_policy
from apm_cli.utils.path_security import ensure_path_within, validate_path_segments
from apm_cli.utils.yaml_io import load_yaml, load_yaml_str
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import GitCommit, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_POLICY_SOURCE = "dev.azure.com/contoso/_apm/_apm"
_POLICY_REMOTE = "https://dev.azure.com/contoso/project/_git/govern-contract"
_POLICY_DIAGNOSTIC = f"Policy: org:{_POLICY_SOURCE} (cached"
_EXPECTED_BUNDLE_FILES = {
    "instructions/govern-rules.instructions.md",
    "skills/govern-skill/SKILL.md",
}
_EXPECTED_DEPLOYED_FILES = {
    ".agents/skills/govern-skill/SKILL.md",
    ".github/instructions/govern-rules.instructions.md",
}


@dataclass(frozen=True)
class _PolicyMatrix:
    """Structured cold and warm evidence for one project state."""

    cold_status: dict[str, object]
    warm_status: dict[str, object]
    cold_audit: dict[str, object]
    warm_audit: dict[str, object]
    cold_cache_policy: bytes
    warm_cache_policy: bytes
    cold_cache_metadata: bytes
    warm_cache_metadata: bytes
    cold_status_returncode: int
    warm_status_returncode: int
    cold_audit_returncode: int
    warm_audit_returncode: int
    cold_status_stderr: str
    warm_status_stderr: str
    cold_audit_stderr: str
    warm_audit_stderr: str
    expected_warm_extends_chain: tuple[str, ...]


@dataclass(frozen=True)
class _GovernReceipt:
    """All command and persisted-state evidence for the Govern contract."""

    positive: _PolicyMatrix
    pinned_negative: _PolicyMatrix
    hash_negative: _PolicyMatrix
    consumer_positive: _PolicyMatrix
    producer_governed_install: CommandResult
    pinned_governed_install: CommandResult
    pinned_lock_before: bytes
    pinned_lock_after: bytes
    pinned_snapshot_before: ArtifactSnapshot
    pinned_snapshot_after: ArtifactSnapshot
    repaired_install: CommandResult
    materialized_original: bytes
    materialized_tampered: bytes
    materialized_repaired: bytes
    dependency_hash_before: str
    dependency_hash_after: str
    producer_lock: dict[str, object]
    bundle_lock: dict[str, object]
    consumer_lock: dict[str, object]
    bundle_before: ArtifactSnapshot
    bundle_after: ArtifactSnapshot
    consumer_install: CommandResult
    consumer_audit: dict[str, object]
    lockless_blocked_install: CommandResult
    lockless_bypass_install: CommandResult
    lockless_before: ArtifactSnapshot
    lockless_after_block: ArtifactSnapshot


def _owned_path(root: Path, relative: str) -> Path:
    """Resolve a test-owned relative path through the path-security owner."""
    validate_path_segments(relative, context="Govern contract path", reject_empty=True)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    ensure_path_within(candidate, root)
    return candidate


def _run_expected(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    expected_returncode: int,
    scenario_id: str,
    cwd: Path,
    env: dict[str, str],
) -> CommandResult:
    """Run one real command and retain stable failure evidence."""
    result = runner.run(args, scenario_id=scenario_id, cwd=cwd, env=env)
    assert result.returncode == expected_returncode, (
        f"scenario={scenario_id!r}\n"
        f"expected_returncode={expected_returncode}\n"
        f"actual_returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    return result


def _initialize_policy_remote(project_root: Path, *, env: dict[str, str]) -> None:
    """Give auto-discovery one ADO candidate whose cache key is deterministic."""
    subprocess.run(
        ("git", "init", "--initial-branch=main"),
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    subprocess.run(
        ("git", "remote", "add", "origin", _POLICY_REMOTE),
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _strict_policy(project_root: Path) -> Path:
    """Author the strict policy through the canonical local package helper."""
    factory = LocalPackageFactory(project_root)
    package = factory.create("strict-policy")
    return factory.write_policy(
        package,
        {
            "name": "govern-core-contract",
            "version": "1",
            "enforcement": "block",
            "fetch_failure": "block",
            "dependencies": {"require_pinned_constraint": True},
            "security": {
                "audit": {"fail_on_drift": True},
                "integrity": {"require_hashes": True},
            },
        },
    )


def _write_policy_leaf(project_root: Path, strict_policy: Path) -> Path:
    """Write the local leaf whose source string is also the ADO cache key."""
    from apm_cli.utils.yaml_io import dump_yaml

    leaf = _owned_path(project_root, _POLICY_SOURCE)
    leaf.parent.mkdir(parents=True, exist_ok=True)
    relative_parent = strict_policy.relative_to(project_root).as_posix()
    dump_yaml(
        {
            "name": "govern-local-leaf",
            "extends": relative_parent,
        },
        leaf,
    )
    return leaf


def _policy_cache_bytes(project_root: Path) -> tuple[bytes, bytes]:
    """Return the sole persisted merged-policy and metadata byte images."""
    cache_root = _owned_path(project_root, "apm_modules/.policy-cache")
    metadata_paths = tuple(cache_root.glob("*.meta.json"))
    policy_paths = tuple(cache_root.glob("*.yml"))
    assert len(metadata_paths) == 1
    assert len(policy_paths) == 1
    return policy_paths[0].read_bytes(), metadata_paths[0].read_bytes()


def _policy_status_args(*, cold: bool) -> tuple[str, ...]:
    """Return public status arguments for one discovery temperature."""
    args = (
        "policy",
        "status",
        "--policy-source",
        _POLICY_SOURCE,
        "--json",
        "--check",
    )
    return (*args, "--no-cache") if cold else args


def _audit_args(*, cold: bool, policy_source: bool = True) -> tuple[str, ...]:
    """Return structured public audit arguments for the Govern matrix."""
    args = ["audit", "--ci", "--no-fail-fast", "--no-drift", "--format", "json"]
    if policy_source:
        args.extend(("--policy", _POLICY_SOURCE))
    if cold:
        args.append("--no-cache")
    return tuple(args)


def _json_output(result: CommandResult) -> dict[str, object]:
    """Parse one structured command result."""
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def _run_policy_matrix(
    runner: ApmLifecycleRunner,
    project_root: Path,
    *,
    env: dict[str, str],
    strict_policy: Path,
    expected_audit_returncode: int,
    scenario_id: str,
    expected_status_returncode: int = 0,
) -> _PolicyMatrix:
    """Evaluate the same real project from local source and merged cache."""
    leaf = _write_policy_leaf(project_root, strict_policy)
    cold_status = _run_expected(
        runner,
        _policy_status_args(cold=True),
        expected_returncode=expected_status_returncode,
        scenario_id=f"{scenario_id}-cold-status",
        cwd=project_root,
        env=env,
    )
    cold_audit = _run_expected(
        runner,
        _audit_args(cold=True),
        expected_returncode=expected_audit_returncode,
        scenario_id=f"{scenario_id}-cold-audit",
        cwd=project_root,
        env=env,
    )
    cold_cache_policy, cold_cache_metadata = _policy_cache_bytes(project_root)

    leaf.unlink()
    warm_status = _run_expected(
        runner,
        _policy_status_args(cold=False),
        expected_returncode=expected_status_returncode,
        scenario_id=f"{scenario_id}-warm-status",
        cwd=project_root,
        env=env,
    )
    warm_audit = _run_expected(
        runner,
        _audit_args(cold=False),
        expected_returncode=expected_audit_returncode,
        scenario_id=f"{scenario_id}-warm-audit",
        cwd=project_root,
        env=env,
    )
    warm_cache_policy, warm_cache_metadata = _policy_cache_bytes(project_root)

    return _PolicyMatrix(
        cold_status=_json_output(cold_status),
        warm_status=_json_output(warm_status),
        cold_audit=_json_output(cold_audit),
        warm_audit=_json_output(warm_audit),
        cold_cache_policy=cold_cache_policy,
        warm_cache_policy=warm_cache_policy,
        cold_cache_metadata=cold_cache_metadata,
        warm_cache_metadata=warm_cache_metadata,
        cold_status_returncode=cold_status.returncode,
        warm_status_returncode=warm_status.returncode,
        cold_audit_returncode=cold_audit.returncode,
        warm_audit_returncode=warm_audit.returncode,
        cold_status_stderr=cold_status.stderr,
        warm_status_stderr=warm_status.stderr,
        cold_audit_stderr=cold_audit.stderr,
        warm_audit_stderr=warm_audit.stderr,
        expected_warm_extends_chain=(strict_policy.relative_to(project_root).as_posix(),),
    )


def _create_project(
    factory: LocalPackageFactory,
    name: str,
    *,
    git_source: str,
    reference: str,
    env: dict[str, str],
) -> LocalPackage:
    """Author a consumer with one real Git dependency and policy remote."""
    project = factory.create(
        name,
        dependencies=(
            {
                "git": git_source,
                "type": "gitlab",
                "ref": reference,
            },
        ),
        targets=("copilot",),
    )
    _initialize_policy_remote(project.root, env=env)
    return project


def _single_dependency(lockfile: dict[str, object]) -> dict[str, object]:
    """Return the only persisted dependency record."""
    dependencies = lockfile["dependencies"]
    assert isinstance(dependencies, list)
    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    return dependency


def _remove_dependency_hash(lock_path: Path) -> tuple[bytes, str]:
    """Delete one hash line from genuine command-produced lock bytes."""
    lockfile = load_yaml(lock_path)
    dependency = _single_dependency(lockfile)
    content_hash = dependency["content_hash"]
    assert isinstance(content_hash, str)
    original = lock_path.read_bytes()
    hash_line = f"  content_hash: {content_hash}\n".encode()
    assert original.count(hash_line) == 1
    lock_path.write_bytes(original.replace(hash_line, b"", 1))
    return original, content_hash


def _artifact_fingerprints(
    snapshot: ArtifactSnapshot,
    *,
    prefix: str,
) -> dict[str, str]:
    """Project snapshot entries under one bundle-relative prefix."""
    return {
        entry.relative_path.removeprefix(prefix): entry.fingerprint
        for entry in snapshot.entries
        if entry.kind == "file"
        and entry.fingerprint is not None
        and entry.relative_path.startswith(prefix)
    }


def _configure_source_rewrite(
    repository_url: str,
    git_source: str,
    *,
    env: dict[str, str],
    cwd: Path,
) -> None:
    """Route one production-shaped Git source to the local bare origin."""
    subprocess.run(
        (
            "git",
            "config",
            "--global",
            f"url.{repository_url}.insteadOf",
            git_source,
        ),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _source_fixture(
    isolated: IsolatedApmEnvironment,
    *,
    env: dict[str, str],
) -> tuple[LocalGitRepositoryFactory, GitCommit, str]:
    """Author and publish the hermetic package governed by the scenario."""
    package_factory = LocalPackageFactory(isolated.package_root)
    source = package_factory.create("govern-source", targets=("copilot",))
    package_factory.add_skill(
        source,
        "govern-skill",
        ("---\nname: govern-skill\ndescription: Govern contract skill\n---\n# Governed skill\n"),
    )
    package_factory.add_instruction(
        source,
        "govern-rules",
        ("---\napplyTo: '**'\ndescription: Govern contract instruction\n---\n# Governed rules\n"),
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = repositories.create("govern-source", source_tree=source.root)
    commit = repositories.commit(repository, message="seed Govern source")
    git_source = "git@gitlab.example.invalid:fixtures/govern-source.git"
    _configure_source_rewrite(
        repository.file_url,
        git_source,
        env=env,
        cwd=isolated.root,
    )
    return repositories, commit, git_source


def _run_govern_contract(root: Path, binary: Path) -> _GovernReceipt:
    """Run author, enforce, pack, install, mutate, repair, and audit."""
    isolated = IsolatedApmEnvironment.create(
        _owned_path(root, "isolated"),
        base_env=dict(os.environ),
    )
    env = isolated.subprocess_env()
    _repositories, commit, git_source = _source_fixture(isolated, env=env)
    projects = LocalPackageFactory(isolated.work_root)
    runner = ApmLifecycleRunner(
        (str(binary),),
        timeout_seconds=180,
        scenario_timeout_seconds=600,
    )

    producer = _create_project(
        projects,
        "govern-producer",
        git_source=git_source,
        reference=commit.sha,
        env=env,
    )
    _run_expected(
        runner,
        ("install", "--target", "copilot", "--no-policy"),
        expected_returncode=0,
        scenario_id="govern-bootstrap-command-state",
        cwd=producer.root,
        env=env,
    )
    producer_policy = _strict_policy(producer.root)
    positive = _run_policy_matrix(
        runner,
        producer.root,
        env=env,
        strict_policy=producer_policy,
        expected_audit_returncode=0,
        scenario_id="govern-positive",
    )
    producer_governed_install = _run_expected(
        runner,
        ("install", "--target", "copilot"),
        expected_returncode=0,
        scenario_id="govern-warm-install",
        cwd=producer.root,
        env=env,
    )
    _run_expected(
        runner,
        ("compile", "--target", "copilot", "--force-instructions"),
        expected_returncode=0,
        scenario_id="govern-compile",
        cwd=producer.root,
        env=env,
    )
    _run_expected(
        runner,
        ("pack", "--format", "plugin", "--offline"),
        expected_returncode=0,
        scenario_id="govern-pack",
        cwd=producer.root,
        env=env,
    )
    producer_manifest = load_yaml(producer.manifest_path)
    bundle = _owned_path(
        producer.root,
        f"build/{producer.name}-{producer_manifest['version']}",
    )
    bundle_before = ArtifactSnapshot.capture(bundle)
    bundle_lock = load_yaml(_owned_path(bundle, "apm.lock.yaml"))

    unpinned = _create_project(
        projects,
        "govern-unpinned",
        git_source=git_source,
        reference="main",
        env=env,
    )
    _run_expected(
        runner,
        ("install", "--target", "copilot", "--no-policy"),
        expected_returncode=0,
        scenario_id="govern-unpinned-bootstrap",
        cwd=unpinned.root,
        env=env,
    )
    unpinned_policy = _strict_policy(unpinned.root)
    pinned_negative = _run_policy_matrix(
        runner,
        unpinned.root,
        env=env,
        strict_policy=unpinned_policy,
        expected_audit_returncode=1,
        scenario_id="govern-pinned-negative",
    )
    pinned_lock_path = _owned_path(unpinned.root, "apm.lock.yaml")
    pinned_lock_before = pinned_lock_path.read_bytes()
    pinned_snapshot_before = ArtifactSnapshot.capture(unpinned.root)
    pinned_governed_install = _run_expected(
        runner,
        ("install", "--target", "copilot"),
        expected_returncode=1,
        scenario_id="govern-pinned-install-negative",
        cwd=unpinned.root,
        env=env,
    )
    pinned_lock_after = pinned_lock_path.read_bytes()
    pinned_snapshot_after = ArtifactSnapshot.capture(unpinned.root)

    producer_lock_path = _owned_path(producer.root, "apm.lock.yaml")
    genuine_lock_bytes, dependency_hash_before = _remove_dependency_hash(producer_lock_path)
    hash_negative = _run_policy_matrix(
        runner,
        producer.root,
        env=env,
        strict_policy=producer_policy,
        expected_audit_returncode=1,
        scenario_id="govern-hash-negative",
    )
    producer_lock_path.write_bytes(genuine_lock_bytes)

    module_skill = next(
        _owned_path(producer.root, "apm_modules").rglob("skills/govern-skill/SKILL.md")
    )
    materialized_original = module_skill.read_bytes()
    materialized_tampered = materialized_original + b"\nTAMPERED MATERIALIZED TREE\n"
    module_skill.write_bytes(materialized_tampered)
    repaired_install = _run_expected(
        runner,
        ("install", "--target", "copilot"),
        expected_returncode=0,
        scenario_id="govern-materialized-tree-repair",
        cwd=producer.root,
        env=env,
    )
    materialized_repaired = module_skill.read_bytes()
    producer_lock = load_yaml(producer_lock_path)
    dependency_hash_after = _single_dependency(producer_lock)["content_hash"]
    assert isinstance(dependency_hash_after, str)

    consumer = projects.create("govern-consumer", targets=("copilot",))
    _initialize_policy_remote(consumer.root, env=env)
    consumer_policy = _strict_policy(consumer.root)
    consumer_positive = _run_policy_matrix(
        runner,
        consumer.root,
        env=env,
        strict_policy=consumer_policy,
        expected_audit_returncode=0,
        scenario_id="govern-consumer-positive",
    )
    consumer_install = _run_expected(
        runner,
        ("install", str(bundle), "--target", "copilot"),
        expected_returncode=0,
        scenario_id="govern-bundle-install",
        cwd=consumer.root,
        env=env,
    )
    bundle_after = ArtifactSnapshot.capture(bundle)
    consumer_lock = load_yaml(_owned_path(consumer.root, "apm.lock.yaml"))
    consumer_audit_result = _run_expected(
        runner,
        _audit_args(cold=False, policy_source=False),
        expected_returncode=0,
        scenario_id="govern-bundle-audit",
        cwd=consumer.root,
        env=env,
    )

    lockless_bundle = _owned_path(isolated.work_root, "govern-lockless-bundle")
    shutil.copytree(bundle, lockless_bundle)
    _owned_path(lockless_bundle, "apm.lock.yaml").unlink()
    lockless_consumer = projects.create("govern-lockless-consumer", targets=("copilot",))
    _initialize_policy_remote(lockless_consumer.root, env=env)
    lockless_policy = _strict_policy(lockless_consumer.root)
    _run_policy_matrix(
        runner,
        lockless_consumer.root,
        env=env,
        strict_policy=lockless_policy,
        expected_audit_returncode=0,
        scenario_id="govern-lockless-consumer-positive",
    )
    lockless_before = ArtifactSnapshot.capture(lockless_consumer.root)
    lockless_blocked_install = _run_expected(
        runner,
        ("install", str(lockless_bundle), "--target", "copilot"),
        expected_returncode=1,
        scenario_id="govern-lockless-bundle-blocked",
        cwd=lockless_consumer.root,
        env=env,
    )
    lockless_after_block = ArtifactSnapshot.capture(lockless_consumer.root)
    lockless_bypass_install = _run_expected(
        runner,
        (
            "install",
            str(lockless_bundle),
            "--target",
            "copilot",
            "--no-policy",
        ),
        expected_returncode=0,
        scenario_id="govern-lockless-bundle-bypass",
        cwd=lockless_consumer.root,
        env=env,
    )

    return _GovernReceipt(
        positive=positive,
        pinned_negative=pinned_negative,
        hash_negative=hash_negative,
        consumer_positive=consumer_positive,
        producer_governed_install=producer_governed_install,
        pinned_governed_install=pinned_governed_install,
        pinned_lock_before=pinned_lock_before,
        pinned_lock_after=pinned_lock_after,
        pinned_snapshot_before=pinned_snapshot_before,
        pinned_snapshot_after=pinned_snapshot_after,
        repaired_install=repaired_install,
        materialized_original=materialized_original,
        materialized_tampered=materialized_tampered,
        materialized_repaired=materialized_repaired,
        dependency_hash_before=dependency_hash_before,
        dependency_hash_after=dependency_hash_after,
        producer_lock=producer_lock,
        bundle_lock=bundle_lock,
        consumer_lock=consumer_lock,
        bundle_before=bundle_before,
        bundle_after=bundle_after,
        consumer_install=consumer_install,
        consumer_audit=_json_output(consumer_audit_result),
        lockless_blocked_install=lockless_blocked_install,
        lockless_bypass_install=lockless_bypass_install,
        lockless_before=lockless_before,
        lockless_after_block=lockless_after_block,
    )


def _checks(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    """Index structured audit checks by canonical check name."""
    checks = payload["checks"]
    assert isinstance(checks, list)
    return {
        check["name"]: check
        for check in checks
        if isinstance(check, dict) and isinstance(check.get("name"), str)
    }


def _failed_check_names(payload: dict[str, object]) -> set[str]:
    """Return the exact failed-check set from one audit payload."""
    return {name for name, check in _checks(payload).items() if check.get("passed") is not True}


def _assert_policy_cache_contract(matrix: _PolicyMatrix) -> None:
    """Assert discovery temperature and serialized strict fields."""
    assert matrix.cold_status["outcome"] == "found"
    assert matrix.cold_status["cached"] is False
    assert matrix.cold_status["enforcement"] == "block"
    assert matrix.warm_status["outcome"] == "found"
    assert matrix.warm_status["cached"] is True
    assert matrix.warm_status["enforcement"] == "block"
    assert matrix.cold_cache_policy == matrix.warm_cache_policy
    assert matrix.cold_cache_metadata == matrix.warm_cache_metadata

    cached_policy = load_yaml_str(matrix.cold_cache_policy.decode("utf-8"))
    assert cached_policy["dependencies"]["require_pinned_constraint"] is True
    assert cached_policy["security"]["integrity"]["require_hashes"] is True
    assert cached_policy["security"]["audit"]["fail_on_drift"] is True


def _status_diagnostics(payload: dict[str, object]) -> dict[str, object]:
    """Drop source- and temperature-specific status fields."""
    ignored = {
        "cache_age_human",
        "cache_age_seconds",
        "cached",
        "extends_chain",
        "source",
    }
    return {key: value for key, value in payload.items() if key not in ignored}


def _assert_policy_observations_equal(matrix: _PolicyMatrix) -> None:
    """Cold and warm public command observations must agree."""
    assert matrix.cold_status_returncode == matrix.warm_status_returncode
    assert matrix.cold_audit_returncode == matrix.warm_audit_returncode
    assert matrix.cold_status_stderr == matrix.warm_status_stderr
    assert matrix.cold_audit_stderr == matrix.warm_audit_stderr
    assert matrix.cold_status["extends_chain"] == []
    assert matrix.warm_status["extends_chain"] == list(matrix.expected_warm_extends_chain)
    assert _status_diagnostics(matrix.cold_status) == _status_diagnostics(matrix.warm_status)
    assert matrix.cold_audit == matrix.warm_audit


def test_real_govern_core_contract(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Govern strict policy identically cold/warm through pack and audit."""
    govern_receipt = _run_govern_contract(tmp_path, apm_binary_path)

    for matrix in (
        govern_receipt.positive,
        govern_receipt.pinned_negative,
        govern_receipt.hash_negative,
        govern_receipt.consumer_positive,
    ):
        _assert_policy_cache_contract(matrix)
        _assert_policy_observations_equal(matrix)

    assert _failed_check_names(govern_receipt.positive.cold_audit) == set()
    assert _failed_check_names(govern_receipt.consumer_positive.cold_audit) == set()
    assert _failed_check_names(govern_receipt.pinned_negative.cold_audit) == {
        "dependency-pinned-constraint"
    }
    assert _failed_check_names(govern_receipt.hash_negative.cold_audit) == {
        "dependency-content-hashes"
    }
    hash_check = _checks(govern_receipt.hash_negative.cold_audit)["dependency-content-hashes"]
    assert hash_check["passed"] is False
    assert hash_check["details"]

    assert govern_receipt.pinned_governed_install.returncode == 1
    assert "dependency-pinned-constraint" in govern_receipt.pinned_governed_install.stdout
    assert govern_receipt.pinned_lock_after == govern_receipt.pinned_lock_before
    assert_unchanged(
        govern_receipt.pinned_snapshot_before,
        govern_receipt.pinned_snapshot_after,
    )

    assert govern_receipt.materialized_tampered != govern_receipt.materialized_original
    assert govern_receipt.repaired_install.returncode == 0
    assert govern_receipt.materialized_repaired == govern_receipt.materialized_original
    assert govern_receipt.dependency_hash_after == govern_receipt.dependency_hash_before
    assert govern_receipt.producer_governed_install.returncode == 0
    assert _POLICY_DIAGNOSTIC in govern_receipt.producer_governed_install.stdout
    assert govern_receipt.consumer_install.returncode == 0
    assert _POLICY_DIAGNOSTIC in govern_receipt.consumer_install.stdout
    assert_unchanged(govern_receipt.bundle_before, govern_receipt.bundle_after)

    pack = govern_receipt.bundle_lock["pack"]
    assert isinstance(pack, dict)
    bundle_files = pack["bundle_files"]
    assert isinstance(bundle_files, dict)
    assert set(bundle_files) >= _EXPECTED_BUNDLE_FILES
    bundle_fingerprints = _artifact_fingerprints(
        govern_receipt.bundle_before,
        prefix="",
    )
    assert {path: bundle_files[path] for path in _EXPECTED_BUNDLE_FILES} == {
        path: bundle_fingerprints[path] for path in _EXPECTED_BUNDLE_FILES
    }

    assert set(govern_receipt.consumer_lock["local_deployed_files"]) == (_EXPECTED_DEPLOYED_FILES)
    deployed_hashes = govern_receipt.consumer_lock["local_deployed_file_hashes"]
    assert isinstance(deployed_hashes, dict)
    assert set(deployed_hashes) == _EXPECTED_DEPLOYED_FILES
    assert all(str(value).startswith("sha256:") for value in deployed_hashes.values())
    deployment_rows = govern_receipt.consumer_lock["deployments"]
    assert isinstance(deployment_rows, list)
    deployed_rows = {
        row["value"]: row
        for row in deployment_rows
        if isinstance(row, dict) and row.get("value") in _EXPECTED_DEPLOYED_FILES
    }
    assert set(deployed_rows) == _EXPECTED_DEPLOYED_FILES
    assert {row["active_owner"] for row in deployed_rows.values()} == {"local-bundle"}

    assert govern_receipt.consumer_audit["passed"] is True
    assert govern_receipt.consumer_audit["summary"]["failed"] == 0
    assert _checks(govern_receipt.consumer_audit)["dependency-content-hashes"]["passed"] is True
    assert govern_receipt.lockless_blocked_install.returncode == 1
    assert "org policy requires integrity hashes" in govern_receipt.lockless_blocked_install.stdout
    assert_unchanged(
        govern_receipt.lockless_before,
        govern_receipt.lockless_after_block,
    )
    assert govern_receipt.lockless_bypass_install.returncode == 0
    assert (
        "Policy enforcement disabled by --no-policy"
        in govern_receipt.lockless_bypass_install.stdout
    )


@pytest.mark.parametrize(
    ("policy_data", "expected_warning_fragment"),
    (
        (
            {
                "name": "minimal",
                "version": "1",
            },
            None,
        ),
        (
            {
                "name": "explicit-default-legacy",
                "version": "1",
                "bin_deploy": {
                    "deny_all": False,
                    "deny": [],
                },
            },
            "'bin_deploy' is deprecated",
        ),
    ),
    ids=("implicit-default", "explicit-default-legacy"),
)
def test_minimal_policy_cache_is_observationally_equivalent(
    tmp_path: Path,
    apm_binary_path: Path,
    policy_data: dict[str, object],
    expected_warning_fragment: str | None,
) -> None:
    """A default policy stays warning-free across command and cache boundaries."""
    isolated = IsolatedApmEnvironment.create(
        _owned_path(tmp_path, "minimal-isolated"),
        base_env=dict(os.environ),
    )
    env = isolated.subprocess_env()
    project = LocalPackageFactory(isolated.work_root).create(
        "minimal-policy-project",
        targets=("copilot",),
    )
    _initialize_policy_remote(project.root, env=env)
    policy_factory = LocalPackageFactory(project.root)
    policy_package = policy_factory.create("minimal-policy")
    policy_path = policy_factory.write_policy(
        policy_package,
        policy_data,
    )
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=180,
        scenario_timeout_seconds=300,
    )

    matrix = _run_policy_matrix(
        runner,
        project.root,
        env=env,
        strict_policy=policy_path,
        expected_audit_returncode=0,
        expected_status_returncode=1,
        scenario_id="govern-minimal-policy",
    )

    _assert_policy_observations_equal(matrix)
    assert matrix.cold_status["outcome"] == "empty"
    assert matrix.warm_status["outcome"] == "empty"
    cold_warnings = matrix.cold_status["warnings"]
    warm_warnings = matrix.warm_status["warnings"]
    assert cold_warnings == warm_warnings
    if expected_warning_fragment is None:
        assert cold_warnings == []
    else:
        assert any(expected_warning_fragment in str(warning) for warning in cold_warnings)
    _cached_policy, cache_warnings = load_policy(matrix.warm_cache_policy.decode("utf-8"))
    assert cache_warnings == []
    assert "bin_deploy:" not in matrix.warm_cache_policy.decode("utf-8")
