"""Hermetic E2E coverage for frozen install on cold cache with git apm_package deps.

Issue #2456 regression: ``apm install --frozen`` failed before any hydration ran
when a git apm_package dep declared MCP servers that were stored in the lockfile.
The MCP config view walked absent apm.yml files and emitted McpSourceProblem errors,
causing enforce_frozen to raise FrozenInstallError before the download pipeline ran.

These tests drive the real install command through Click and stub only the network /
download seam so the enforce_frozen structural check (the bug's location) and the
MCP config derivation are exercised end-to-end without a real git server or PAT.
"""

from __future__ import annotations

import os
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

_COMMIT = "b" * 40
_TOKEN_VARS = ("GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN", "ADO_APM_PAT")
_PKG_REPO = "owner/mcp-skill-pkg"
_MCP_SERVER = "my-mcp-tool"


@pytest.fixture(autouse=True)
def _clear_package_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _make_locked_dep() -> LockedDependency:
    return LockedDependency(
        repo_url=_PKG_REPO,
        resolved_ref="v1.0.0",
        resolved_commit=_COMMIT,
        package_type="apm_package",
        depth=1,
        name="mcp-skill-pkg",
    )


def _write_project(project: Path, dep: LockedDependency, *, with_mcp: bool) -> LockFile:
    """Write apm.yml and apm.lock.yaml; return the lockfile object."""
    project.mkdir(parents=True)
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "frozen-cold-cache-test",
                "version": "1.0.0",
                "targets": ["copilot"],
                "dependencies": {"apm": [{"git": _PKG_REPO, "ref": "v1.0.0"}]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    lockfile = LockFile()
    lockfile.add_dependency(dep)
    if with_mcp:
        # Simulate a package that declared an MCP server at install time.
        # The dep's install dir does NOT exist yet (cold cache).
        install_dir_name = dep.to_dependency_ref().get_install_path(project / "apm_modules").name
        lockfile.mcp_servers = [_MCP_SERVER]
        lockfile.mcp_configs = {
            _MCP_SERVER: {"name": _MCP_SERVER, "command": "npx", "args": ["-y", _MCP_SERVER]}
        }
        lockfile.mcp_config_provenance = {_MCP_SERVER: install_dir_name}
    lockfile.write(project / "apm.lock.yaml")
    return lockfile


def _stub_download_package(
    _self: GitHubPackageDownloader,
    dep_ref: DependencyReference,
    install_path: Path,
    *_args: object,
    **_kwargs: object,
) -> PackageInfo:
    """Simulate a successful package download without network access."""
    install_path.mkdir(parents=True, exist_ok=True)
    (install_path / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "mcp-skill-pkg",
                "version": "1.0.0",
                "description": "Cold-cache E2E fixture",
                "mcp": {"servers": {_MCP_SERVER: {"command": "npx", "args": ["-y", _MCP_SERVER]}}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    package = APMPackage.from_apm_yml(install_path / "apm.yml")
    return PackageInfo(
        package=package,
        install_path=install_path,
        resolved_reference=ResolvedReference(
            original_ref="v1.0.0",
            ref_type=GitReferenceType.COMMIT,
            resolved_commit=_COMMIT,
            ref_name="v1.0.0",
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
            kind="github",
            has_public_repos=True,
            api_base="https://api.github.com",
            port=port,
        ),
        git_env={},
        auth_scheme="basic",
    )


def _run_frozen_install(project: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """Run ``apm install --frozen`` in project, stub download, return result."""
    monkeypatch.chdir(project)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    clean_env = {k: v for k, v in os.environ.items() if k not in _TOKEN_VARS}

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
                original_ref="v1.0.0",
                ref_type=GitReferenceType.COMMIT,
                resolved_commit=_COMMIT,
                ref_name="v1.0.0",
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
        download_called = download.call_count >= 1
    return result, download_called


def test_frozen_cold_cache_git_apm_package_with_mcp_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``apm install --frozen`` passes on cold cache when git dep declares MCP servers.

    Before the fix (issue #2456) this raised FrozenInstallError because
    CurrentMcpConfigView.derive emitted McpSourceProblem for the absent dep dir,
    causing enforce_frozen to see spurious lock_only MCP entries and fail before
    any hydration ran.

    The fix must make enforce_frozen pass AND let the install pipeline run
    (proven by download_called).
    """
    dep = _make_locked_dep()
    project = tmp_path / "with-mcp"
    _write_project(project, dep, with_mcp=True)

    # Confirm cold cache: apm_modules/ does not exist
    assert not (project / "apm_modules").exists()

    result, download_called = _run_frozen_install(project, monkeypatch)

    assert result.exit_code == 0, (
        f"Expected frozen install to succeed on cold cache with MCP servers.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The pipeline ran (enforce_frozen passed) -- if it had raised before this
    # the download stub would never be invoked.
    assert download_called, (
        "download_package was never called -- enforce_frozen likely raised early"
    )
    assert "out of sync" not in result.output
    assert "McpSourceProblem" not in result.output


def test_frozen_cold_cache_git_apm_package_without_mcp_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``apm install --frozen`` passes on cold cache when git dep has no MCP servers."""
    dep = _make_locked_dep()
    project = tmp_path / "no-mcp"
    _write_project(project, dep, with_mcp=False)

    assert not (project / "apm_modules").exists()

    result, download_called = _run_frozen_install(project, monkeypatch)

    assert result.exit_code == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert download_called


def test_frozen_cold_cache_real_mcp_drift_still_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real MCP drift (server missing from apm.yml but in lockfile) still fails --frozen.

    The cold-cache exemption must not mask genuine drift: a server present in
    lockfile.mcp_configs but NOT attributed to any absent dep via provenance
    should still produce a FrozenInstallError.
    """
    dep = _make_locked_dep()
    project = tmp_path / "real-drift"
    _write_project(project, dep, with_mcp=False)

    # Manually inject a stale MCP server with no provenance -> lock_only drift
    lockfile = LockFile.read(project / "apm.lock.yaml")
    assert lockfile is not None
    lockfile.mcp_servers = ["ghost-server"]
    lockfile.mcp_configs = {"ghost-server": {"name": "ghost-server"}}
    lockfile.mcp_config_provenance = {}  # no provenance -> NOT cold-cache exempt
    lockfile.save(project / "apm.lock.yaml")

    assert not (project / "apm_modules").exists()

    result, _ = _run_frozen_install(project, monkeypatch)

    assert result.exit_code != 0, (
        "Expected frozen install to fail when lockfile has an unprovenanced MCP server"
    )
    assert "out of sync" in result.output or result.exit_code == 1
