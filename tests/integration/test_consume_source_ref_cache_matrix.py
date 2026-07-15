"""Real-binary Consume contracts across source, reference, and cache states.

The package shape is intentionally fixed: one skill bundle, one skill, and the
``copilot`` target. Remote-shaped URLs are rewritten by isolated Git config to
one local bare origin. This proves public parsing, host classification,
resolution, lock provenance, cache replay, deployment, audit, and update
contracts without claiming remote authentication or host API coverage.

Omitted candidates:

* Local directories and GitLab SCP/HTTPS branch convergence are already proved
  by ``test_real_consume_contracts.py``.
* Direct ``file://`` dependency input is not supported by the public parser;
  file transport is fixture infrastructure only.
* Registry, marketplace, auth, policy, and primitive-shape variation are owned
  by separate contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.core.host_providers import classify_host_provider
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")
_INSTALL_ARGS = (
    "install",
    "--target",
    "copilot",
    "--no-policy",
    "--parallel-downloads",
    "0",
)
_LOCK_ARGS = (
    "lock",
    "--target",
    "copilot",
    "--no-policy",
    "--parallel-downloads",
    "0",
)
_UPDATE_ARGS = (
    "update",
    "--yes",
    "--target",
    "copilot",
    "--parallel-downloads",
    "0",
)
_SKILL_NAME = "source-ref-cache"
_SKILL_PATH = Path(".agents") / "skills" / _SKILL_NAME / "SKILL.md"
_REPO_NAME = "consume-matrix"


@dataclass(frozen=True)
class _MatrixRow:
    id: str
    source: str
    rewrite_urls: tuple[str, ...]
    expected_host: str
    expected_kind: str
    expected_repo: str
    ref_selector: str
    host_type: str | None = None
    mode: str = "standard"
    transition_source: str | None = None
    transition_rewrite_urls: tuple[str, ...] = ()


_ROWS = (
    _MatrixRow(
        id="github-https-full-sha",
        source="https://github.com/acme/consume-matrix.git",
        rewrite_urls=("https://github.com/acme/consume-matrix.git",),
        expected_host="github.com",
        expected_kind="github",
        expected_repo="acme/consume-matrix",
        ref_selector="first-sha",
    ),
    _MatrixRow(
        id="github-scp-full-sha",
        source="git@github.com:acme/consume-matrix.git",
        rewrite_urls=("git@github.com:acme/consume-matrix.git",),
        expected_host="github.com",
        expected_kind="github",
        expected_repo="acme/consume-matrix",
        ref_selector="second-sha",
    ),
    _MatrixRow(
        id="ghe-cloud-ssh-full-sha",
        source="ssh://git@contoso.ghe.com/acme/consume-matrix.git",
        rewrite_urls=("git@contoso.ghe.com:acme/consume-matrix.git",),
        expected_host="contoso.ghe.com",
        expected_kind="ghe_cloud",
        expected_repo="acme/consume-matrix",
        ref_selector="first-sha",
    ),
    _MatrixRow(
        id="gitlab-ssh-semver-range",
        source="ssh://git@gitlab.example.invalid/group/consume-matrix.git",
        rewrite_urls=(
            "git@gitlab.example.invalid:group/consume-matrix.git",
            "https://gitlab.example.invalid/group/consume-matrix.git",
        ),
        expected_host="gitlab.example.invalid",
        expected_kind="gitlab",
        expected_repo="group/consume-matrix",
        ref_selector="semver",
        host_type="gitlab",
    ),
    _MatrixRow(
        id="ado-https-literal-tag",
        source="https://dev.azure.com/contoso/platform/_git/consume-matrix",
        rewrite_urls=("https://dev.azure.com/contoso/platform/_git/consume-matrix",),
        expected_host="dev.azure.com",
        expected_kind="ado",
        expected_repo="contoso/platform/consume-matrix",
        ref_selector="tag",
    ),
    _MatrixRow(
        id="ado-scp-full-sha",
        source="git@dev.azure.com:contoso/platform/consume-matrix.git",
        rewrite_urls=("git@ssh.dev.azure.com:v3/contoso/platform/consume-matrix",),
        expected_host="dev.azure.com",
        expected_kind="ado",
        expected_repo="contoso/platform/consume-matrix",
        ref_selector="second-sha",
    ),
    _MatrixRow(
        id="github-https-warm-cache-only",
        source="https://github.com/acme/consume-matrix.git",
        rewrite_urls=("https://github.com/acme/consume-matrix.git",),
        expected_host="github.com",
        expected_kind="github",
        expected_repo="acme/consume-matrix",
        ref_selector="first-sha",
        mode="warm-cache",
    ),
    _MatrixRow(
        id="gitlab-source-ref-transition",
        source="https://gitlab.example.invalid/group/consume-matrix.git",
        rewrite_urls=("https://gitlab.example.invalid/group/consume-matrix.git",),
        expected_host="gitlab.example.invalid",
        expected_kind="gitlab",
        expected_repo="group/consume-matrix",
        ref_selector="first-sha",
        host_type="gitlab",
        mode="transition",
        transition_source="git@gitlab.example.invalid:group/consume-matrix.git",
        transition_rewrite_urls=("git@gitlab.example.invalid:group/consume-matrix.git",),
    ),
)


@dataclass(frozen=True)
class _Scenario:
    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    repositories: LocalGitRepositoryFactory
    repository: LocalGitRepository
    first_commit: GitCommit
    second_commit: GitCommit
    project_root: Path
    initial_ref: str
    expected_commit: GitCommit
    expected_skill_bytes: bytes


def _skill_document(marker: str) -> str:
    return (
        "---\n"
        f"name: {_SKILL_NAME}\n"
        "description: Consume source ref cache matrix skill\n"
        "---\n"
        f"# {marker}\n"
    )


def _configure_rewrites(
    repository: LocalGitRepository,
    urls: tuple[str, ...],
    *,
    environment: dict[str, str],
) -> None:
    for url in urls:
        subprocess.run(
            (
                "git",
                "config",
                "--global",
                "--add",
                f"url.{repository.file_url}.insteadOf",
                url,
            ),
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )


def _reference_for(
    row: _MatrixRow,
    first_commit: GitCommit,
    second_commit: GitCommit,
) -> tuple[str, GitCommit, bytes]:
    if row.ref_selector == "first-sha":
        return first_commit.sha, first_commit, _skill_document("version one").encode()
    if row.ref_selector == "second-sha":
        return second_commit.sha, second_commit, _skill_document("version two").encode()
    if row.ref_selector == "tag":
        return "v1.0.0", first_commit, _skill_document("version one").encode()
    if row.ref_selector == "semver":
        return "^1.0.0", second_commit, _skill_document("version two").encode()
    raise AssertionError(f"Unknown ref selector: {row.ref_selector}")


def _manifest_entry(row: _MatrixRow, source: str, reference: str) -> dict[str, str]:
    entry = {"git": source, "ref": reference}
    if row.host_type:
        entry["type"] = row.host_type
    return entry


def _create_scenario(root: Path, row: _MatrixRow) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    bundle = packages.create(_REPO_NAME, targets=("copilot",))
    skill_path = packages.add_skill(
        bundle,
        _SKILL_NAME,
        _skill_document("version one"),
    )

    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create(_REPO_NAME, source_tree=bundle.root)
    first_commit = repositories.commit(repository, message="seed matrix bundle")
    repositories.tag(repository, "v1.0.0", first_commit)

    repository_skill = repository.worktree / skill_path.relative_to(bundle.root)
    repository_skill.write_bytes(_skill_document("version two").encode())
    second_commit = repositories.commit(repository, message="advance matrix bundle")
    repositories.tag(repository, "v1.1.0", second_commit)

    _configure_rewrites(
        repository,
        (*row.rewrite_urls, *row.transition_rewrite_urls),
        environment=environment,
    )
    reference, expected_commit, expected_skill_bytes = _reference_for(
        row,
        first_commit,
        second_commit,
    )
    consumer = LocalPackageFactory(isolated.work_root).create(
        f"consumer-{row.id}",
        dependencies=(_manifest_entry(row, row.source, reference),),
        targets=("copilot",),
    )
    return _Scenario(
        isolated=isolated,
        environment=environment,
        repositories=repositories,
        repository=repository,
        first_commit=first_commit,
        second_commit=second_commit,
        project_root=consumer.root,
        initial_ref=reference,
        expected_commit=expected_commit,
        expected_skill_bytes=expected_skill_bytes,
    )


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    return ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=360,
    )


def _assert_success(result: CommandResult) -> None:
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _locked_dependency(project_root: Path) -> dict[str, object]:
    lock = load_yaml(project_root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert len(dependencies) == 1
    return dependencies[0]


def _manifest_dependency(project_root: Path) -> dict[str, object]:
    manifest = load_yaml(project_root / "apm.yml")
    dependencies = manifest["dependencies"]["apm"]
    assert len(dependencies) == 1
    return dependencies[0]


def _audit_payload(result: CommandResult) -> dict[str, object]:
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert payload["summary"]["failed"] == 0
    return payload


def _update_plan_entries(output: str) -> list[str]:
    return [
        line.strip()
        for line in output.splitlines()
        if line.startswith("  [~] ") and line.strip() != "[~] updated"
    ]


def _assert_lock_provenance(
    row: _MatrixRow,
    scenario: _Scenario,
    *,
    expected_source: str,
    expected_ref: str,
    expected_commit: GitCommit,
) -> dict[str, object]:
    locked = _locked_dependency(scenario.project_root)
    assert locked["host"] == row.expected_host
    assert locked["repo_url"] == row.expected_repo
    assert locked.get("source") is None
    assert locked.get("host_type") == row.host_type
    assert (
        classify_host_provider(
            str(locked["host"]),
            host_type=locked.get("host_type"),
        ).kind
        == row.expected_kind
    )
    assert locked["resolved_commit"] == expected_commit.sha
    if row.ref_selector == "semver" and expected_ref == scenario.initial_ref:
        assert locked["constraint"] == "^1.0.0"
        assert locked["resolved_tag"] == "v1.1.0"
        assert locked["resolved_ref"] == "v1.1.0"
        assert locked["version"] == "1.1.0"
    else:
        assert locked["resolved_ref"] == expected_ref
    assert _manifest_dependency(scenario.project_root) == _manifest_entry(
        row,
        expected_source,
        expected_ref,
    )
    return locked


def _assert_deployment(
    scenario: _Scenario,
    *,
    expected_skill_bytes: bytes,
) -> None:
    deployed = scenario.project_root / _SKILL_PATH
    assert deployed.read_bytes() == expected_skill_bytes
    locked = _locked_dependency(scenario.project_root)
    deployed_path = _SKILL_PATH.as_posix()
    assert deployed_path in locked["deployed_files"]
    expected_hash = f"sha256:{hashlib.sha256(expected_skill_bytes).hexdigest()}"
    assert locked["deployed_file_hashes"][deployed_path] == expected_hash
    assert str(locked["content_hash"]).startswith("sha256:")


def _run_initial_contract(
    row: _MatrixRow,
    scenario: _Scenario,
    *,
    apm_binary_path: Path,
) -> None:
    runner = _runner(apm_binary_path)
    lock_result = runner.run(
        _LOCK_ARGS,
        scenario_id=f"{row.id}-cold-lock",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    _assert_success(lock_result)
    assert not (scenario.project_root / _SKILL_PATH).exists()
    _assert_lock_provenance(
        row,
        scenario,
        expected_source=row.source,
        expected_ref=scenario.initial_ref,
        expected_commit=scenario.expected_commit,
    )

    install_result = runner.run(
        _INSTALL_ARGS,
        scenario_id=f"{row.id}-install",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    _assert_success(install_result)
    _assert_deployment(
        scenario,
        expected_skill_bytes=scenario.expected_skill_bytes,
    )

    audit_result = runner.run(
        _AUDIT_ARGS,
        scenario_id=f"{row.id}-audit",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    _assert_success(audit_result)
    _audit_payload(audit_result)


def _assert_unchanged_convergence(
    row: _MatrixRow,
    scenario: _Scenario,
    *,
    apm_binary_path: Path,
) -> None:
    before = ArtifactSnapshot.capture(scenario.project_root)
    if row.ref_selector in {"semver", "tag"}:
        args = _UPDATE_ARGS
        scenario_suffix = "unchanged-update"
    else:
        args = _INSTALL_ARGS
        scenario_suffix = "unchanged-install"
    result = _runner(apm_binary_path).run(
        args,
        scenario_id=f"{row.id}-{scenario_suffix}",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    _assert_success(result)
    if row.ref_selector in {"semver", "tag"}:
        output = result.stdout + result.stderr
        assert _update_plan_entries(output) == []
        assert "All dependencies already at their latest matching refs." in output
    assert_unchanged(before, ArtifactSnapshot.capture(scenario.project_root))


@pytest.mark.parametrize(
    "row",
    tuple(row for row in _ROWS if row.mode == "standard"),
    ids=lambda row: row.id,
)
def test_immutable_source_ref_rows_resolve_deploy_audit_and_converge(
    tmp_path: Path,
    apm_binary_path: Path,
    record_property: Callable[[str, object], None],
    row: _MatrixRow,
) -> None:
    started = time.monotonic()
    scenario = _create_scenario(tmp_path / row.id, row)
    _run_initial_contract(row, scenario, apm_binary_path=apm_binary_path)
    _assert_unchanged_convergence(row, scenario, apm_binary_path=apm_binary_path)
    record_property("scenario_seconds", round(time.monotonic() - started, 3))


def test_warm_cache_only_audit_replays_after_origin_becomes_unavailable(
    tmp_path: Path,
    apm_binary_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    row = next(row for row in _ROWS if row.mode == "warm-cache")
    started = time.monotonic()
    scenario = _create_scenario(tmp_path / row.id, row)
    _run_initial_contract(row, scenario, apm_binary_path=apm_binary_path)

    offline_origin = scenario.repository.origin.with_name(
        f"{scenario.repository.origin.name}.offline"
    )
    scenario.repository.origin.rename(offline_origin)
    warm_audit = _runner(apm_binary_path).run(
        _AUDIT_ARGS,
        scenario_id=f"{row.id}-warm-cache-only-audit",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    _assert_success(warm_audit)
    _audit_payload(warm_audit)
    assert "[>] Replaying install (cache-only)..." in warm_audit.stderr
    _assert_deployment(
        scenario,
        expected_skill_bytes=scenario.expected_skill_bytes,
    )
    record_property("scenario_seconds", round(time.monotonic() - started, 3))


def test_source_ref_transition_applies_once_and_invalid_twin_preserves_state(
    tmp_path: Path,
    apm_binary_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    row = next(row for row in _ROWS if row.mode == "transition")
    assert row.transition_source is not None
    started = time.monotonic()
    scenario = _create_scenario(tmp_path / row.id, row)
    _run_initial_contract(row, scenario, apm_binary_path=apm_binary_path)

    manifest_path = scenario.project_root / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["dependencies"]["apm"] = [
        _manifest_entry(
            row,
            row.transition_source,
            scenario.second_commit.sha,
        )
    ]
    dump_yaml(manifest, manifest_path)

    changed = _runner(apm_binary_path).run(
        _INSTALL_ARGS,
        scenario_id=f"{row.id}-changed",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    _assert_success(changed)
    _assert_lock_provenance(
        row,
        scenario,
        expected_source=row.transition_source,
        expected_ref=scenario.second_commit.sha,
        expected_commit=scenario.second_commit,
    )
    expected_skill_bytes = _skill_document("version two").encode()
    _assert_deployment(
        scenario,
        expected_skill_bytes=expected_skill_bytes,
    )
    clean_audit = _runner(apm_binary_path).run(
        _AUDIT_ARGS,
        scenario_id=f"{row.id}-changed-audit",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    _assert_success(clean_audit)
    _audit_payload(clean_audit)

    converged = ArtifactSnapshot.capture(scenario.project_root)
    unchanged = _runner(apm_binary_path).run(
        _INSTALL_ARGS,
        scenario_id=f"{row.id}-converged",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    _assert_success(unchanged)
    assert_unchanged(converged, ArtifactSnapshot.capture(scenario.project_root))

    lock_bytes = (scenario.project_root / "apm.lock.yaml").read_bytes()
    deployed_bytes = (scenario.project_root / _SKILL_PATH).read_bytes()
    invalid_commit = "f" * 40
    manifest = load_yaml(manifest_path)
    manifest["dependencies"]["apm"] = [_manifest_entry(row, row.transition_source, invalid_commit)]
    dump_yaml(manifest, manifest_path)

    invalid = _runner(apm_binary_path).run(
        _INSTALL_ARGS,
        scenario_id=f"{row.id}-invalid-ref",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    assert invalid.returncode == 1
    invalid_output = " ".join((invalid.stdout + invalid.stderr).split())
    assert "Failed to download dependency" in invalid_output
    assert "No install transaction changes were committed." in invalid_output
    assert (scenario.project_root / "apm.lock.yaml").read_bytes() == lock_bytes
    assert (scenario.project_root / _SKILL_PATH).read_bytes() == deployed_bytes
    record_property("scenario_seconds", round(time.monotonic() - started, 3))
