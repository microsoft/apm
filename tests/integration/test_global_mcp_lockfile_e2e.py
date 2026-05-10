"""Integration tests: --global MCP install writes lockfile to ~/.apm/ (#794).

Regression suite for #794.  Verifies that ``MCPIntegrator.update_lockfile``
receives the scope-resolved lockfile path so MCP server entries are persisted
in the user-scope lockfile (``~/.apm/apm.lock.yaml``) rather than the
project-local lockfile in ``Path.cwd()``.

Uses the CliRunner (in-process) pattern from ``test_selective_install_mcp.py``
with ``Path.home()`` overridden to an isolated temporary directory.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockedDependency, LockFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_apm_yml(
    path: Path,
    *,
    name: str = "test-project",
    deps: list | None = None,
    mcp: list | None = None,
):
    """Write a minimal apm.yml."""
    data: dict = {"name": name, "version": "1.0.0", "dependencies": {}}
    if deps:
        data["dependencies"]["apm"] = deps
    if mcp:
        data["dependencies"]["mcp"] = mcp
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _make_pkg(
    apm_modules: Path,
    repo_url: str,
    *,
    name: str | None = None,
    mcp: list | None = None,
    apm_deps: list | None = None,
):
    """Create a package directory with apm.yml under *apm_modules*."""
    pkg_dir = apm_modules / repo_url
    pkg_dir.mkdir(parents=True, exist_ok=True)
    pkg_name = name or repo_url.split("/")[-1]
    _write_apm_yml(
        pkg_dir / "apm.yml",
        name=pkg_name,
        deps=apm_deps,
        mcp=mcp,
    )


def _seed_lockfile(
    path: Path,
    locked_deps: list | None = None,
    mcp_servers: list | None = None,
):
    """Write a lockfile pre-populated with given dependencies."""
    lf = LockFile()
    for dep in locked_deps or []:
        lf.add_dependency(dep)
    if mcp_servers:
        lf.mcp_servers = mcp_servers
    lf.write(path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def global_env(tmp_path):
    """Set up an isolated global + project environment.

    Returns ``(fake_home, work_dir, runner)`` where:

    * ``fake_home/.apm/`` is the global APM directory
    * ``work_dir`` is a separate CWD (to detect lockfile leakage)
    """
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()

    work_dir = tmp_path / "workdir"
    work_dir.mkdir()

    # Create ~/.apm/ structure
    apm_dir = fake_home / ".apm"
    apm_dir.mkdir(parents=True)
    (apm_dir / "apm_modules").mkdir()

    return fake_home, work_dir, CliRunner()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGlobalMCPLockfilePlacement:
    """Regression for #794: MCP lockfile entries at --global scope."""

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_global_install_writes_mcp_servers_to_global_lockfile(
        self,
        mock_dl_cls,
        mock_mcp_install,
        mock_validate,
        mock_updates,
        global_env,
    ):
        """MCP server entries must land in ~/.apm/apm.lock.yaml, not cwd."""
        fake_home, work_dir, runner = global_env
        apm_dir = fake_home / ".apm"
        apm_modules = apm_dir / "apm_modules"

        # Root manifest with a dependency that transitively declares MCP
        _write_apm_yml(
            apm_dir / "apm.yml",
            name="global-project",
            deps=["acme/squad-alpha"],
        )

        # squad-alpha depends on infra-cloud
        _make_pkg(apm_modules, "acme/squad-alpha", apm_deps=["acme/infra-cloud"])

        # infra-cloud declares MCP servers
        _make_pkg(
            apm_modules,
            "acme/infra-cloud",
            mcp=[
                "ghcr.io/acme/mcp-alpha",
                "ghcr.io/acme/mcp-beta",
            ],
        )

        # Pre-seed the global lockfile (must exist for update_lockfile to write)
        _seed_lockfile(
            apm_dir / "apm.lock.yaml",
            [
                LockedDependency(
                    repo_url="acme/squad-alpha",
                    depth=1,
                    resolved_by=None,
                    resolved_commit="cached",
                ),
                LockedDependency(
                    repo_url="acme/infra-cloud",
                    depth=2,
                    resolved_by="acme/squad-alpha",
                    resolved_commit="cached",
                ),
            ],
        )

        from apm_cli.cli import cli

        orig_cwd = os.getcwd()
        try:
            os.chdir(work_dir)
            with patch.object(Path, "home", return_value=fake_home):
                result = runner.invoke(
                    cli,
                    ["install", "--global", "--trust-transitive-mcp"],
                )
        finally:
            os.chdir(orig_cwd)

        assert result.exit_code == 0, f"CLI failed (exit {result.exit_code}):\n{result.output}"

        # POSITIVE: global lockfile contains MCP server entries
        global_lock = LockFile.read(apm_dir / "apm.lock.yaml")
        assert global_lock is not None, "Global lockfile missing after install"
        assert "ghcr.io/acme/mcp-alpha" in global_lock.mcp_servers
        assert "ghcr.io/acme/mcp-beta" in global_lock.mcp_servers

        # NEGATIVE: no lockfile leaked into the working directory
        assert not (work_dir / "apm.lock.yaml").exists(), "Lockfile leaked into working directory"
        assert not (work_dir / "apm.lock").exists(), "Legacy lockfile leaked into working directory"

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.remove_stale")
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_global_install_no_mcp_clears_servers_in_global_lockfile(
        self,
        mock_dl_cls,
        mock_remove_stale,
        mock_mcp_install,
        mock_validate,
        mock_updates,
        global_env,
    ):
        """When the manifest has no MCP deps, global lockfile mcp_servers is cleared."""
        fake_home, work_dir, runner = global_env
        apm_dir = fake_home / ".apm"
        apm_modules = apm_dir / "apm_modules"

        # Root manifest with NO MCP dependencies
        _write_apm_yml(
            apm_dir / "apm.yml",
            name="global-project",
            deps=["acme/plain-pkg"],
        )

        # plain-pkg has no MCP
        _make_pkg(apm_modules, "acme/plain-pkg")

        # Pre-seed global lockfile WITH stale mcp_servers from a prior install
        _seed_lockfile(
            apm_dir / "apm.lock.yaml",
            [
                LockedDependency(
                    repo_url="acme/plain-pkg",
                    depth=1,
                    resolved_by=None,
                    resolved_commit="cached",
                ),
            ],
            mcp_servers=["ghcr.io/acme/old-server"],
        )

        from apm_cli.cli import cli

        orig_cwd = os.getcwd()
        try:
            os.chdir(work_dir)
            with patch.object(Path, "home", return_value=fake_home):
                result = runner.invoke(
                    cli,
                    ["install", "--global"],
                )
        finally:
            os.chdir(orig_cwd)

        assert result.exit_code == 0, f"CLI failed (exit {result.exit_code}):\n{result.output}"

        # POSITIVE: global lockfile mcp_servers is cleared
        global_lock = LockFile.read(apm_dir / "apm.lock.yaml")
        assert global_lock is not None, "Global lockfile missing after install"
        assert global_lock.mcp_servers == [], (
            f"Expected empty mcp_servers, got: {global_lock.mcp_servers}"
        )

        # NEGATIVE: no lockfile leaked into the working directory
        assert not (work_dir / "apm.lock.yaml").exists(), "Lockfile leaked into working directory"
        assert not (work_dir / "apm.lock").exists(), "Legacy lockfile leaked into working directory"
