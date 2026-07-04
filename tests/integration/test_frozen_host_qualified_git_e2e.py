"""Hermetic E2E coverage for frozen installs with host-qualified git locks.

Issue #1996 failed in the real ``apm install --frozen`` entry point before
the download pipeline ran: manifest-side git dependency keys were host-blind,
while lockfile entries for private git hosts are host-qualified.  These tests
drive the real install command through Click and stub only network/token seams
so the frozen structural check and install service boundary are exercised
end-to-end without a private server or PAT.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType, clear_apm_yml_cache
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

pytestmark = [pytest.mark.integration, pytest.mark.requires_e2e_mode]

_COMMIT = "a" * 40
_TOKEN_VARS = ("GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN", "ADO_APM_PAT")


@dataclass(frozen=True)
class _FrozenGitCase:
    name: str
    manifest_dep: dict[str, str]
    locked_dep: LockedDependency
    expected_key: str


def _cases() -> tuple[_FrozenGitCase, ...]:
    return (
        _FrozenGitCase(
            name="private-generic-ssh-host",
            manifest_dep={
                "git": "git@git.example.com:org/private-skills.git",
                "ref": "2026.06.10",
            },
            locked_dep=LockedDependency(
                repo_url="org/private-skills",
                host="git.example.com",
                resolved_ref="2026.06.10",
                resolved_commit=_COMMIT,
                depth=1,
                name="private-skills",
            ),
            expected_key="git.example.com/org/private-skills",
        ),
        _FrozenGitCase(
            name="github-default-ssh-host",
            manifest_dep={
                "git": "git@github.com:org/public-skills.git",
                "ref": "main",
            },
            locked_dep=LockedDependency(
                repo_url="org/public-skills",
                host="github.com",
                resolved_ref="main",
                resolved_commit=_COMMIT,
                depth=1,
                name="public-skills",
            ),
            expected_key="org/public-skills",
        ),
    )


@pytest.fixture(autouse=True)
def _clear_package_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _write_project(project: Path, case: _FrozenGitCase) -> None:
    project.mkdir(parents=True)
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": f"frozen-{case.name}",
                "version": "1.0.0",
                "targets": ["copilot"],
                "dependencies": {"apm": [case.manifest_dep]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    lockfile = LockFile()
    lockfile.add_dependency(case.locked_dep)
    lockfile.write(project / "apm.lock.yaml")


def _package_name(dep_ref: DependencyReference) -> str:
    return dep_ref.repo_url.rsplit("/", maxsplit=1)[-1]


def _stub_download_package(
    _self: GitHubPackageDownloader,
    dep_ref: DependencyReference,
    install_path: Path,
    *_args: object,
    **_kwargs: object,
) -> PackageInfo:
    install_path.mkdir(parents=True, exist_ok=True)
    (install_path / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": _package_name(dep_ref),
                "version": "1.0.0",
                "description": "Frozen host-qualified E2E fixture",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    instructions = install_path / ".apm" / "instructions"
    instructions.mkdir(parents=True, exist_ok=True)
    (instructions / "frozen-host.instructions.md").write_text(
        "# Frozen host fixture\nInstall pipeline reached package integration.\n",
        encoding="utf-8",
    )
    package = APMPackage.from_apm_yml(install_path / "apm.yml")
    return PackageInfo(
        package=package,
        install_path=install_path,
        resolved_reference=ResolvedReference(
            original_ref=dep_ref.reference or "main",
            ref_type=GitReferenceType.COMMIT,
            resolved_commit=_COMMIT,
            ref_name=dep_ref.reference or "main",
        ),
        dependency_ref=dep_ref,
        package_type=PackageType.APM_PACKAGE,
    )


def _stub_auth_resolve(
    _self: AuthResolver,
    host: str,
    org: str | None = None,
    port: int | None = None,
) -> AuthContext:
    return AuthContext(
        token=None,
        source="none",
        token_type="none",
        host_info=HostInfo(
            host=host,
            kind="generic",
            has_public_repos=False,
            api_base="",
            port=port,
        ),
        git_env={},
        auth_scheme="basic",
    )


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.name)
def test_frozen_install_accepts_host_qualified_git_lock_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _FrozenGitCase,
) -> None:
    """``apm install --frozen`` matches manifest keys to lockfile keys."""
    project = tmp_path / case.name
    _write_project(project, case)
    monkeypatch.chdir(project)
    monkeypatch.setenv("APM_NO_CACHE", "1")

    clean_env = {key: value for key, value in os.environ.items() if key not in _TOKEN_VARS}
    lockfile = LockFile.read(project / "apm.lock.yaml")
    assert lockfile is not None
    assert sorted(lockfile.dependencies) == [case.expected_key]

    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch("apm_cli.install.phases.resolve._maybe_resolve_git_semver", return_value=None),
        patch.object(GitHubPackageDownloader, "download_package", autospec=True) as download,
        patch.object(
            GitHubPackageDownloader,
            "resolve_git_reference",
            autospec=True,
            return_value=ResolvedReference(
                original_ref=case.locked_dep.resolved_ref or "main",
                ref_type=GitReferenceType.COMMIT,
                resolved_commit=_COMMIT,
                ref_name=case.locked_dep.resolved_ref or "main",
            ),
        ),
        patch.object(AuthResolver, "resolve", autospec=True, side_effect=_stub_auth_resolve),
    ):
        download.side_effect = _stub_download_package
        result = CliRunner().invoke(
            cli,
            [
                "install",
                "--frozen",
                "--no-policy",
                "--parallel-downloads",
                "0",
                "--target",
                "copilot",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, (
        f"install failed for {case.name}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "missing from apm.lock.yaml" not in result.output
    assert download.call_count >= 1
    downloaded_dep = download.call_args_list[0].args[1]
    assert downloaded_dep.get_unique_key() == case.expected_key
    deployed = project / ".github" / "instructions" / "frozen-host.instructions.md"
    assert deployed.is_file()


def test_frozen_install_rejects_host_mismatched_lock_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``apm install --frozen`` still rejects a lockfile for the wrong host."""
    case = _FrozenGitCase(
        name="host-mismatch",
        manifest_dep={
            "git": "git@github.com:org/private-skills.git",
            "ref": "main",
        },
        locked_dep=LockedDependency(
            repo_url="org/private-skills",
            host="git.example.com",
            resolved_ref="main",
            resolved_commit=_COMMIT,
            depth=1,
            name="private-skills",
        ),
        expected_key="git.example.com/org/private-skills",
    )
    project = tmp_path / case.name
    _write_project(project, case)
    monkeypatch.chdir(project)

    clean_env = {key: value for key, value in os.environ.items() if key not in _TOKEN_VARS}
    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch.object(GitHubPackageDownloader, "download_package", autospec=True) as download,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "install",
                "--frozen",
                "--no-policy",
                "--parallel-downloads",
                "0",
                "--target",
                "copilot",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code != 0
    assert "--frozen: apm.lock.yaml is out of sync with apm.yml" in result.output
    assert "org/private-skills is declared in apm.yml but missing from apm.lock.yaml" in (
        result.output
    )
    download.assert_not_called()
