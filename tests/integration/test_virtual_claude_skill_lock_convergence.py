"""Lockfile convergence for a virtual Claude Skill without ``apm.yml``."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from git import Repo

from apm_cli.cli import cli
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import GitReferenceType, ResolvedReference, clear_apm_yml_cache
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.component, pytest.mark.lifecycle_smoke]

_REMOTE = "ssh://git@gitlab.example.invalid/acme/skill-repo.git"
_REWRITE_URLS = (
    "git@gitlab.example.invalid:acme/skill-repo.git",
    "https://gitlab.example.invalid/acme/skill-repo.git",
)
_REPO_NAME = "skill-repo"
_SKILL_NAME = "auth"
_VIRTUAL_PATH = "skills/security/auth"
_DEPLOYED_SKILL = Path(".agents") / "skills" / _SKILL_NAME / "SKILL.md"
_INSTALL_ARGS = (
    "install",
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


@dataclass(frozen=True)
class _Scenario:
    environment: dict[str, str]
    repositories: LocalGitRepositoryFactory
    repository: LocalGitRepository
    project: Path
    cached_skill: Path


def _skill_document(marker: str) -> str:
    return f"---\nname: {_SKILL_NAME}\ndescription: Authentication guidance\n---\n# {marker}\n"


def _create_scenario(root: Path) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_NO_CACHE"] = "1"
    source = isolated.package_root / _REPO_NAME
    skill = source / _VIRTUAL_PATH / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_skill_document("version one"), encoding="utf-8")

    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create(_REPO_NAME, source_tree=source)
    repositories.commit(repository, message="seed virtual skill")
    for rewrite_url in _REWRITE_URLS:
        subprocess.run(
            (
                "git",
                "config",
                "--global",
                "--add",
                f"url.{repository.file_url}.insteadOf",
                rewrite_url,
            ),
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=(
            {
                "git": _REMOTE,
                "type": "gitlab",
                "path": _VIRTUAL_PATH,
                "ref": "main",
            },
        ),
        targets=("copilot",),
    )
    cached_skill = consumer.root / "apm_modules" / "acme" / _REPO_NAME / _VIRTUAL_PATH / "SKILL.md"
    return _Scenario(
        environment=environment,
        repositories=repositories,
        repository=repository,
        project=consumer.root,
        cached_skill=cached_skill,
    )


def _invoke(
    scenario: _Scenario,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> Result:
    def resolve_local_ref(
        _self: GitHubPackageDownloader,
        dep_ref: object,
    ) -> ResolvedReference:
        reference = getattr(dep_ref, "reference", None) or "main"
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

    clear_apm_yml_cache()
    with monkeypatch.context() as patch:
        patch.chdir(scenario.project)
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
        return CliRunner().invoke(cli, list(args), env=scenario.environment)


def _assert_success(result: Result) -> None:
    assert result.exit_code == 0, (
        f"stdout:\n{result.output}\nstderr:\n{result.stderr}\nexception={result.exception!r}"
    )


def _lock_receipt(project: Path) -> tuple[tuple[object, ...], bytes]:
    lock_path = project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    dependencies = lock["dependencies"]
    assert len(dependencies) == 1
    dependency = dependencies[0]
    identity = (
        dependency.get("name"),
        dependency.get("version"),
        dependency.get("package_type"),
        dependency.get("virtual_path"),
        dependency.get("is_virtual"),
    )
    return identity, lock_path.read_bytes()


def test_virtual_claude_skill_identity_and_lock_bytes_converge_across_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh, cached, frozen, and update paths persist one identity."""
    scenario = _create_scenario(tmp_path / "convergence")
    expected_identity = (
        _SKILL_NAME,
        "unknown",
        "claude_skill",
        _VIRTUAL_PATH,
        True,
    )

    initial = _invoke(scenario, monkeypatch, _INSTALL_ARGS)
    _assert_success(initial)
    receipts = [_lock_receipt(scenario.project)]

    frozen = _invoke(
        scenario,
        monkeypatch,
        (*_INSTALL_ARGS, "--frozen"),
    )
    _assert_success(frozen)
    receipts.append(_lock_receipt(scenario.project))

    repository_skill = scenario.repository.worktree / _VIRTUAL_PATH / "SKILL.md"
    changed_document = _skill_document("version two")
    repository_skill.write_text(changed_document, encoding="utf-8")
    second_commit = scenario.repositories.commit(
        scenario.repository,
        message="change virtual skill content",
    )
    update = _invoke(scenario, monkeypatch, _UPDATE_ARGS)
    _assert_success(update)
    receipts.append(_lock_receipt(scenario.project))
    assert scenario.cached_skill.read_text(encoding="utf-8") == changed_document

    second_frozen = _invoke(
        scenario,
        monkeypatch,
        (*_INSTALL_ARGS, "--frozen"),
    )
    _assert_success(second_frozen)
    receipts.append(_lock_receipt(scenario.project))

    converged_update = _invoke(
        scenario,
        monkeypatch,
        _UPDATE_ARGS,
    )
    _assert_success(converged_update)
    receipts.append(_lock_receipt(scenario.project))

    first_lock = receipts[0][1]
    changed_lock = receipts[2][1]
    observed = [
        (receipts[0][0], receipts[0][1] == first_lock),
        (receipts[1][0], receipts[1][1] == first_lock),
        (receipts[2][0], receipts[2][1] != first_lock),
        (receipts[3][0], receipts[3][1] == changed_lock),
        (receipts[4][0], receipts[4][1] == changed_lock),
    ]
    assert observed == [(expected_identity, True)] * 5

    changed_dependency = load_yaml(scenario.project / "apm.lock.yaml")["dependencies"][0]
    assert changed_dependency["resolved_commit"] == second_commit.sha
    assert (scenario.project / _DEPLOYED_SKILL).read_text(encoding="utf-8") == changed_document


def test_malformed_cached_virtual_claude_skill_fails_without_rewriting_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed cached frontmatter fails closed through the install contract."""
    scenario = _create_scenario(tmp_path / "malformed")
    initial = _invoke(scenario, monkeypatch, _INSTALL_ARGS)
    _assert_success(initial)
    assert scenario.cached_skill.is_file()
    scenario.cached_skill.write_text("---\nname: [\n---\n", encoding="utf-8")
    lock_path = scenario.project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    lock["dependencies"][0]["content_hash"] = compute_package_hash(scenario.cached_skill.parent)
    dump_yaml(lock, lock_path)
    lock_before = lock_path.read_bytes()

    malformed = _invoke(
        scenario,
        monkeypatch,
        (*_INSTALL_ARGS, "--frozen"),
    )

    assert malformed.exit_code == 1, f"stdout:\n{malformed.output}\nstderr:\n{malformed.stderr}"
    output = " ".join((malformed.output + malformed.stderr).split())
    assert "Cached Claude Skill is invalid" in output
    assert "Failed to process SKILL.md" in output
    assert lock_path.read_bytes() == lock_before
