"""Hermetic lifecycle matrix for virtual and manifestless packages."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from git import Repo

from apm_cli.cli import cli
from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.deps.git_file_transport import GitFileFetchResult
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_rate_limit import GitHubThrottle, GitHubThrottleError
from apm_cli.deps.lockfile import LockedDependency
from apm_cli.models.apm_package import (
    DependencyReference,
    GitReferenceType,
    ResolvedReference,
    clear_apm_yml_cache,
)
from apm_cli.utils.content_hash import compute_file_hash, compute_package_hash
from apm_cli.utils.yaml_io import load_yaml, yaml_to_str
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory
from tests.utils.scenario_rows import LifecycleAction, ScenarioRow

pytestmark = [pytest.mark.integration, pytest.mark.lifecycle_smoke]

_REPO_NAME = "virtual-lifecycle"
_SKILL_NAME = "auth"
_SKILL_PATH = "skills/security/auth"
_VIRTUAL_FILE_PATH = "instructions/guard.instructions.md"
_SKILL_REMOTE = f"ssh://git@gitlab.example.invalid/acme/{_REPO_NAME}.git"
_VIRTUAL_FILE_DEPENDENCY = f"acme/{_REPO_NAME}/{_VIRTUAL_FILE_PATH}#main"
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
_FROZEN_ARGS = (*_INSTALL_ARGS, "--frozen")
_UPDATE_ARGS = (
    "update",
    "--yes",
    "--verbose",
    "--target",
    "copilot",
    "--parallel-downloads",
    "0",
)
_AUDIT_ARGS = (
    "audit",
    "--ci",
    "--no-policy",
    "--no-fail-fast",
    "--format",
    "json",
)
_SKILL_BYTES = (
    f"---\nname: {_SKILL_NAME}\ndescription: Authentication guidance\n---\n# Authentication\n"
).encode()
_VIRTUAL_FILE_BYTES = (
    b"---\napplyTo: '**'\ndescription: Hermetic synthetic virtual instruction\n---\n# Guard\n"
)


@dataclass(frozen=True)
class _LifecycleCase:
    """Describe one positive or negative virtual lifecycle row."""

    id: str
    mutation: str
    newline_domain: str = "lf"
    evict_modules_before_frozen: bool = False
    frozen_returncode: int = 0
    update_returncode: int = 0
    audit_returncode: int = 0


@dataclass(frozen=True)
class _VirtualScenario:
    """Own the source repository, consumer, and isolated process boundary."""

    environment: dict[str, str]
    repositories: LocalGitRepositoryFactory
    repository: LocalGitRepository
    initial_commit: str
    project: Path
    skill_source: Path
    virtual_file_source: Path


@dataclass(frozen=True)
class _LastGoodState:
    """Capture durable bytes that a failed resolution must preserve."""

    lock_bytes: bytes
    deployed_bytes: dict[str, bytes]


_CASES = (
    _LifecycleCase(id="unchanged-on-disk-cache", mutation="none"),
    _LifecycleCase(
        id="unchanged-rehydrated-cache",
        mutation="none",
        newline_domain="crlf",
        evict_modules_before_frozen=True,
    ),
    _LifecycleCase(
        id="source-drift-missing-skill",
        mutation="missing-skill",
        update_returncode=1,
    ),
    _LifecycleCase(
        id="metadata-drift-invalid-frontmatter",
        mutation="invalid-frontmatter",
        update_returncode=1,
    ),
    _LifecycleCase(
        id="content-drift-pinned-virtual-file",
        mutation="pinned-content",
        evict_modules_before_frozen=True,
        frozen_returncode=1,
    ),
)


def _create_scenario(root: Path) -> _VirtualScenario:
    """Author one local repository with both supported virtual package shapes."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    source = isolated.package_root / _REPO_NAME
    skill_source = source / _SKILL_PATH / "SKILL.md"
    virtual_file_source = source / _VIRTUAL_FILE_PATH
    skill_source.parent.mkdir(parents=True)
    virtual_file_source.parent.mkdir(parents=True)
    skill_source.write_bytes(_SKILL_BYTES)
    virtual_file_source.write_bytes(_VIRTUAL_FILE_BYTES)
    assert not (source / "apm.yml").exists()
    assert not skill_source.with_name("apm.yml").exists()

    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create(_REPO_NAME, source_tree=source)
    commit = repositories.commit(repository, message="seed virtual lifecycle")
    project = LocalPackageFactory(isolated.work_root).create(
        "virtual-lifecycle-consumer",
        dependencies=(
            {
                "git": _SKILL_REMOTE,
                "type": "gitlab",
                "path": _SKILL_PATH,
                "ref": "main",
            },
            _VIRTUAL_FILE_DEPENDENCY,
        ),
        targets=("copilot",),
    )
    return _VirtualScenario(
        environment=environment,
        repositories=repositories,
        repository=repository,
        initial_commit=commit.sha,
        project=project.root,
        skill_source=repository.worktree / _SKILL_PATH / "SKILL.md",
        virtual_file_source=repository.worktree / _VIRTUAL_FILE_PATH,
    )


def _invoke(
    scenario: _VirtualScenario,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    *,
    newline_domain: str,
    use_throttle_fallback: bool = False,
) -> Result:
    """Run one real CLI command with remote I/O redirected to the local repository."""

    def resolve_local_ref(
        _self: GitHubPackageDownloader,
        dep_ref: DependencyReference,
    ) -> ResolvedReference:
        reference = dep_ref.reference or "main"
        resolved = subprocess.run(
            ("git", "rev-parse", reference),
            cwd=scenario.repository.worktree,
            env=scenario.environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        return ResolvedReference(
            original_ref=reference,
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=resolved,
            ref_name=reference,
        )

    def clone_local_bare(
        _self: GitHubPackageDownloader,
        _repo_url_base: str,
        bare_target: Path,
        **_kwargs: object,
    ) -> None:
        repo = Repo.clone_from(
            str(scenario.repository.origin),
            str(bare_target),
            bare=True,
        )
        repo.close()

    def resolve_virtual_commit(
        _self: GitHubPackageDownloader,
        _dep_ref: DependencyReference,
        _ref: str,
    ) -> str | None:
        if use_throttle_fallback:
            return None
        return resolve_local_ref(_self, _dep_ref).resolved_commit or ""

    def download_virtual_file(
        _self: GitHubPackageDownloader,
        _dep_ref: DependencyReference,
        file_path: str,
        _ref: str,
    ) -> bytes:
        return (scenario.repository.worktree / file_path).read_bytes()

    throttle = GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com")

    def validate_virtual_package(
        _self: GitHubPackageDownloader,
        dep_ref: DependencyReference,
        **_kwargs: object,
    ) -> bool:
        if use_throttle_fallback and dep_ref.is_virtual_file():
            raise throttle
        return True

    def download_throttled_virtual_file(
        _self: GitHubPackageDownloader,
        _dep_ref: DependencyReference,
        _file_path: str,
        _ref: str,
    ) -> bytes:
        raise throttle

    def sparse_throttle_fallback(
        _self: DownloadDelegate,
        _dep_ref: DependencyReference,
        file_path: str,
        _ref: str,
        error: GitHubThrottleError,
    ) -> GitFileFetchResult:
        assert error is throttle
        return GitFileFetchResult(
            (scenario.repository.worktree / file_path).read_bytes(),
            scenario.initial_commit,
        )

    original_write_text = Path.write_text

    def write_with_platform_newlines(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path.name == "apm.yml" and "apm_modules" in path.parts:
            canonical = data.replace("\r\n", "\n")
            data = canonical.replace("\n", "\r\n") if newline_domain == "crlf" else canonical
            newline = ""
        return original_write_text(
            path,
            data,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    clear_apm_yml_cache()
    with monkeypatch.context() as patch:
        patch.chdir(scenario.project)
        patch.setattr(Path, "write_text", write_with_platform_newlines)
        patch.setattr(
            GitHubPackageDownloader,
            "resolve_git_reference",
            resolve_local_ref,
        )
        patch.setattr(
            GitHubPackageDownloader,
            "_bare_clone_with_fallback",
            clone_local_bare,
        )
        patch.setattr(
            GitHubPackageDownloader,
            "validate_virtual_package_exists",
            validate_virtual_package,
        )
        patch.setattr(
            GitHubPackageDownloader,
            "_resolve_commit_sha_for_ref",
            resolve_virtual_commit,
        )
        patch.setattr(
            GitHubPackageDownloader,
            "download_raw_file",
            download_throttled_virtual_file if use_throttle_fallback else download_virtual_file,
        )
        if use_throttle_fallback:
            patch.setattr(
                DownloadDelegate,
                "download_github_file_via_throttle_fallback",
                sparse_throttle_fallback,
            )
        return CliRunner().invoke(cli, list(args), env=scenario.environment)


def _assert_result(result: Result, expected_returncode: int, scenario_id: str) -> None:
    """Fail with stable command evidence when one lifecycle action diverges."""
    assert result.exit_code == expected_returncode, (
        f"scenario={scenario_id!r}\n"
        f"expected_returncode={expected_returncode}\n"
        f"actual_returncode={result.exit_code}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}\n"
        f"exception={result.exception!r}"
    )


def _lock_dependencies(project: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Return the manifestless skill and synthetic-file lock entries."""
    dependencies = load_yaml(project / "apm.lock.yaml")["dependencies"]
    assert len(dependencies) == 2
    skill = next(
        dependency
        for dependency in dependencies
        if dependency.get("package_type") == "claude_skill"
    )
    virtual_file = next(
        dependency
        for dependency in dependencies
        if dependency.get("virtual_path") == _VIRTUAL_FILE_PATH
    )
    return skill, virtual_file


def _module_root(project: Path, locked: dict[str, object]) -> Path:
    """Resolve an installed module through the canonical lockfile model."""
    return (
        LockedDependency.from_dict(locked)
        .to_dependency_ref()
        .get_install_path(project / "apm_modules")
    )


def _expected_synthetic_manifest() -> bytes:
    """Render expected generated metadata through the canonical YAML serializer."""
    dependency = DependencyReference.parse(_VIRTUAL_FILE_DEPENDENCY)
    return yaml_to_str(
        {
            "name": dependency.get_virtual_package_name(),
            "version": "1.0.0",
            "description": "Hermetic synthetic virtual instruction",
            "author": "acme",
        }
    ).encode()


def _assert_lock_and_materialization(
    scenario: _VirtualScenario,
    *,
    expected_commit: str,
) -> None:
    """Assert identity, provenance, generated bytes, hashes, and deployments."""
    skill, virtual_file = _lock_dependencies(scenario.project)
    assert (
        skill["name"],
        skill["version"],
        skill["package_type"],
        skill["virtual_path"],
        skill["is_virtual"],
    ) == (_SKILL_NAME, "unknown", "claude_skill", _SKILL_PATH, True)
    assert (
        skill["repo_url"],
        skill["host"],
        skill["host_type"],
        skill["resolved_ref"],
        skill["resolved_commit"],
    ) == (
        f"acme/{_REPO_NAME}",
        "gitlab.example.invalid",
        "gitlab",
        "main",
        expected_commit,
    )
    assert (
        virtual_file["repo_url"],
        virtual_file["host"],
        virtual_file.get("host_type"),
        virtual_file["virtual_path"],
        virtual_file["is_virtual"],
        virtual_file["resolved_commit"],
    ) == (
        f"acme/{_REPO_NAME}",
        "github.com",
        None,
        _VIRTUAL_FILE_PATH,
        True,
        expected_commit,
    )

    skill_root = _module_root(scenario.project, skill)
    virtual_file_root = _module_root(scenario.project, virtual_file)
    assert (skill_root / "SKILL.md").read_bytes() == _SKILL_BYTES
    assert not (skill_root / "apm.yml").exists()
    assert (
        virtual_file_root / ".apm" / "instructions" / "guard.instructions.md"
    ).read_bytes() == _VIRTUAL_FILE_BYTES
    synthetic_manifest = (virtual_file_root / "apm.yml").read_bytes()
    assert synthetic_manifest == _expected_synthetic_manifest()
    assert b"\r\n" not in synthetic_manifest
    assert skill["content_hash"] == compute_package_hash(skill_root)
    assert virtual_file["content_hash"] == compute_package_hash(virtual_file_root)

    for dependency in (skill, virtual_file):
        deployed_files = dependency["deployed_files"]
        deployed_hashes = dependency["deployed_file_hashes"]
        assert set(deployed_hashes).issubset(deployed_files)
        assert all((scenario.project / deployed_file).exists() for deployed_file in deployed_files)
        for deployed_file in deployed_hashes:
            deployed_path = scenario.project / deployed_file
            assert deployed_path.is_file()
            assert deployed_hashes[deployed_file] == compute_file_hash(deployed_path)

    skill_deployed = scenario.project / ".agents" / "skills" / _SKILL_NAME / "SKILL.md"
    virtual_deployed = scenario.project / ".github" / "instructions" / "guard.instructions.md"
    assert skill_deployed.read_bytes() == _SKILL_BYTES
    assert virtual_deployed.read_bytes() == _VIRTUAL_FILE_BYTES


def _capture_last_good(project: Path) -> _LastGoodState:
    """Snapshot the lock and every lock-owned deployed file."""
    lock_path = project / "apm.lock.yaml"
    deployed_files: set[str] = set()
    for dependency in load_yaml(lock_path)["dependencies"]:
        deployed_files.update(dependency.get("deployed_files") or ())
    deployed_files = {
        relative_path for relative_path in deployed_files if (project / relative_path).is_file()
    }
    return _LastGoodState(
        lock_bytes=lock_path.read_bytes(),
        deployed_bytes={
            relative_path: (project / relative_path).read_bytes()
            for relative_path in sorted(deployed_files)
        },
    )


def _assert_last_good_preserved(project: Path, state: _LastGoodState) -> None:
    """Prove a failed source decision did not commit partial durable state."""
    assert (project / "apm.lock.yaml").read_bytes() == state.lock_bytes
    assert {
        relative_path: (project / relative_path).read_bytes()
        for relative_path in state.deployed_bytes
    } == state.deployed_bytes


def _audit_payload(result: Result) -> dict[str, object]:
    """Decode the JSON audit payload after any preceding CLI diagnostics."""
    json_start = result.stdout.find("{")
    assert json_start >= 0, result.stdout
    return json.loads(result.stdout[json_start:])


def _assert_clean_audit(result: Result) -> None:
    """Assert the full public audit contract is clean."""
    payload = _audit_payload(result)
    assert payload["passed"] is True
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["config-consistency"]["passed"] is True
    assert checks["content-integrity"]["passed"] is True
    assert checks["drift"]["passed"] is True
    assert payload["summary"] == {
        "total": len(checks),
        "passed": len(checks),
        "failed": 0,
    }


def _apply_before_frozen_mutation(
    scenario: _VirtualScenario,
    case: _LifecycleCase,
) -> None:
    """Apply cache eviction or a same-commit virtual-file byte mutation."""
    if case.evict_modules_before_frozen:
        shutil.rmtree(scenario.project / "apm_modules")
    if case.mutation == "pinned-content":
        scenario.virtual_file_source.write_bytes(
            _VIRTUAL_FILE_BYTES.replace(b"# Guard", b"# Tampered guard")
        )


def _apply_before_update_mutation(
    scenario: _VirtualScenario,
    case: _LifecycleCase,
) -> str:
    """Advance the source branch to one invalid manifestless-skill state."""
    if case.mutation == "missing-skill":
        scenario.skill_source.unlink()
    elif case.mutation == "invalid-frontmatter":
        scenario.skill_source.write_bytes(b"---\nname: [\n---\n")
    else:
        return scenario.initial_commit
    return scenario.repositories.commit(
        scenario.repository,
        message=f"apply {case.mutation} drift",
    ).sha


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.id)
def test_virtual_package_lifecycle_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _LifecycleCase,
) -> None:
    """Compose full, cached, frozen, update, and audit virtual-package paths."""
    scenario = _create_scenario(tmp_path / case.id)
    row = ScenarioRow(
        id=case.id,
        source_inputs=(scenario.skill_source, scenario.virtual_file_source),
        lifecycle_actions=(
            LifecycleAction(_INSTALL_ARGS),
            LifecycleAction(_LOCK_ARGS),
            LifecycleAction(_FROZEN_ARGS, case.frozen_returncode),
            LifecycleAction(_UPDATE_ARGS, case.update_returncode),
            LifecycleAction(_AUDIT_ARGS, case.audit_returncode),
        ),
    )

    install = _invoke(
        scenario,
        monkeypatch,
        row.lifecycle_actions[0].args,
        newline_domain=case.newline_domain,
    )
    _assert_result(install, 0, f"{row.id}-install")
    _assert_lock_and_materialization(
        scenario,
        expected_commit=scenario.initial_commit,
    )
    install_state = _capture_last_good(scenario.project)

    locked = _invoke(
        scenario,
        monkeypatch,
        row.lifecycle_actions[1].args,
        newline_domain=case.newline_domain,
    )
    _assert_result(locked, 0, f"{row.id}-lock")
    _assert_lock_and_materialization(
        scenario,
        expected_commit=scenario.initial_commit,
    )
    assert _capture_last_good(scenario.project) == install_state

    _apply_before_frozen_mutation(scenario, case)
    frozen = _invoke(
        scenario,
        monkeypatch,
        row.lifecycle_actions[2].args,
        newline_domain=case.newline_domain,
    )
    _assert_result(
        frozen,
        row.lifecycle_actions[2].expected_returncode,
        f"{row.id}-frozen",
    )
    if case.frozen_returncode:
        failure_text = " ".join((frozen.stdout + frozen.stderr).split())
        assert "Content hash mismatch" in failure_text
        assert "This may indicate a supply-chain attack." in failure_text
        _assert_last_good_preserved(scenario.project, install_state)
        scenario.virtual_file_source.write_bytes(_VIRTUAL_FILE_BYTES)
    else:
        _assert_lock_and_materialization(
            scenario,
            expected_commit=scenario.initial_commit,
        )
        assert _capture_last_good(scenario.project) == install_state

    changed_commit = _apply_before_update_mutation(scenario, case)
    updated = _invoke(
        scenario,
        monkeypatch,
        row.lifecycle_actions[3].args,
        newline_domain=case.newline_domain,
    )
    _assert_result(
        updated,
        row.lifecycle_actions[3].expected_returncode,
        f"{row.id}-update",
    )
    if case.update_returncode:
        failure_text = " ".join((updated.stdout + updated.stderr).split())
        if case.mutation == "missing-skill":
            assert _SKILL_PATH in failure_text
            assert "One or more direct dependencies failed validation" in failure_text
        else:
            assert "Failed to process SKILL.md" in failure_text
        assert changed_commit != scenario.initial_commit
        _assert_last_good_preserved(scenario.project, install_state)
    else:
        if case.mutation == "pinned-content":
            assert "Restored dependency cache without changing refs." in updated.stdout
        _assert_lock_and_materialization(
            scenario,
            expected_commit=scenario.initial_commit,
        )
        assert _capture_last_good(scenario.project) == install_state

    audited = _invoke(
        scenario,
        monkeypatch,
        row.lifecycle_actions[4].args,
        newline_domain=case.newline_domain,
    )
    _assert_result(
        audited,
        row.lifecycle_actions[4].expected_returncode,
        f"{row.id}-audit",
    )
    if case.audit_returncode:
        payload = _audit_payload(audited)
        assert payload["passed"] is False
        failed_checks = {check["name"]: check for check in payload["checks"] if not check["passed"]}
        assert set(failed_checks) == {"config-consistency"}
        assert len(failed_checks["config-consistency"]["details"]) == 2
    else:
        _assert_clean_audit(audited)
        if case.update_returncode == 0:
            _assert_lock_and_materialization(
                scenario,
                expected_commit=scenario.initial_commit,
            )
    _assert_last_good_preserved(scenario.project, install_state)


def test_virtual_throttle_fallback_records_fetch_head_sha_and_content_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttled virtual API path locks the exact sparse-Git commit and bytes."""
    scenario = _create_scenario(tmp_path / "throttle-fallback")

    installed = _invoke(
        scenario,
        monkeypatch,
        _INSTALL_ARGS,
        newline_domain="lf",
        use_throttle_fallback=True,
    )

    _assert_result(installed, 0, "throttle-fallback-install")
    _assert_lock_and_materialization(scenario, expected_commit=scenario.initial_commit)
