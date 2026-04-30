"""Integration test: selective install collects transitive MCP dependencies.

Exercises the full CLI install path — dependency resolution, lockfile generation,
transitive MCP collection, deduplication, and lockfile mcp_servers bookkeeping —
using a synthetic package tree with no network calls.

This is the integration-level complement to the unit tests in
tests/unit/test_mcp_lifecycle_e2e.py, verifying the same flows through
the real CLI entry point instead of calling internal functions directly.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockedDependency, LockFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_apm_yml(path: Path, *, name: str = "test-project", deps: list = None, mcp: list = None):  # noqa: RUF013
    """Write a minimal apm.yml."""
    data = {"name": name, "version": "1.0.0", "dependencies": {}}
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
    name: str = None,  # noqa: RUF013
    mcp: list = None,  # noqa: RUF013
    apm_deps: list = None,  # noqa: RUF013
):
    """Create a package directory with apm.yml under apm_modules."""
    pkg_dir = apm_modules / repo_url
    pkg_dir.mkdir(parents=True, exist_ok=True)
    pkg_name = name or repo_url.split("/")[-1]
    _write_apm_yml(
        pkg_dir / "apm.yml",
        name=pkg_name,
        deps=apm_deps,
        mcp=mcp,
    )


def _seed_lockfile(path: Path, locked_deps: list, mcp_servers: list = None):  # noqa: RUF013
    """Write a lockfile pre-populated with given dependencies."""
    lf = LockFile()
    for dep in locked_deps:
        lf.add_dependency(dep)
    if mcp_servers:
        lf.mcp_servers = mcp_servers
    lf.write(path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_env(tmp_path):
    """Set up a synthetic project tree and return (tmp_path, runner).

    Layout::

        apm.yml (root)  — depends on acme/squad-alpha
        apm_modules/
          acme/squad-alpha/apm.yml  — depends on acme/infra-cloud
          acme/infra-cloud/apm.yml  — mcp: [ghcr.io/acme/mcp-alpha, ghcr.io/acme/mcp-beta]
        apm.lock  — pre-seeded so the install treats packages as cached
    """
    orig_cwd = os.getcwd()
    os.chdir(tmp_path)

    apm_modules = tmp_path / "apm_modules"

    # Root project declares squad-alpha as a dep
    _write_apm_yml(tmp_path / "apm.yml", deps=["acme/squad-alpha"])

    # squad-alpha has no MCP, depends on infra-cloud
    _make_pkg(apm_modules, "acme/squad-alpha", apm_deps=["acme/infra-cloud"])

    # infra-cloud declares two MCP servers
    _make_pkg(
        apm_modules,
        "acme/infra-cloud",
        mcp=[
            "ghcr.io/acme/mcp-alpha",
            "ghcr.io/acme/mcp-beta",
        ],
    )

    # Pre-seed a lockfile so the install loop treats packages as cached
    _seed_lockfile(
        tmp_path / "apm.lock.yaml",
        [
            LockedDependency(
                repo_url="acme/squad-alpha", depth=1, resolved_by=None, resolved_commit="cached"
            ),
            LockedDependency(
                repo_url="acme/infra-cloud",
                depth=2,
                resolved_by="acme/squad-alpha",
                resolved_commit="cached",
            ),
        ],
    )

    yield tmp_path, CliRunner()

    os.chdir(orig_cwd)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSelectiveInstallTransitiveMCPIntegration:
    """CLI-level integration: `apm install acme/squad-alpha` must collect
    transitive MCP deps from acme/infra-cloud and persist them."""

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_lockfile_records_transitive_mcp_servers(
        self, mock_dl_cls, mock_mcp_install, mock_validate, mock_updates, cli_env
    ):
        tmp_path, runner = cli_env
        from apm_cli.cli import cli

        result = runner.invoke(
            cli,
            [
                "install",
                "acme/squad-alpha",
                "--trust-transitive-mcp",
            ],
        )

        # The command should succeed (exit 0)
        assert result.exit_code == 0, (
            f"CLI failed:\n{result.output}\n{getattr(result, 'stderr', '')}"
        )

        # Lockfile must contain both packages
        lockfile = LockFile.read(tmp_path / "apm.lock.yaml")
        assert lockfile is not None
        dep_keys = set(lockfile.dependencies.keys())
        assert "acme/squad-alpha" in dep_keys
        assert "acme/infra-cloud" in dep_keys

        # Lockfile mcp_servers must list the transitive MCP deps
        assert "ghcr.io/acme/mcp-alpha" in lockfile.mcp_servers
        assert "ghcr.io/acme/mcp-beta" in lockfile.mcp_servers

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_install_mcp_receives_transitive_deps(
        self, mock_dl_cls, mock_mcp_install, mock_validate, mock_updates, cli_env
    ):
        """_install_mcp_dependencies must be called with transitive deps."""
        tmp_path, runner = cli_env  # noqa: RUF059
        from apm_cli.cli import cli

        runner.invoke(
            cli,
            [
                "install",
                "acme/squad-alpha",
                "--trust-transitive-mcp",
            ],
        )

        # _install_mcp_dependencies should have been called with the MCP deps
        mock_mcp_install.assert_called_once()
        mcp_deps_arg = mock_mcp_install.call_args[0][0]
        dep_names = {d.name for d in mcp_deps_arg}
        assert "ghcr.io/acme/mcp-alpha" in dep_names
        assert "ghcr.io/acme/mcp-beta" in dep_names


class TestDeepChainIntegration:
    """CLI-level: A → B → C → D where only D declares MCP.
    `apm install acme/pkg-a` must record D's MCP in the lockfile."""

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_deep_chain_mcp_in_lockfile(
        self, mock_dl_cls, mock_mcp_install, mock_validate, mock_updates, tmp_path
    ):
        orig_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            apm_modules = tmp_path / "apm_modules"

            _write_apm_yml(tmp_path / "apm.yml", deps=["acme/pkg-a"])
            _make_pkg(apm_modules, "acme/pkg-a", apm_deps=["acme/pkg-b"])
            _make_pkg(apm_modules, "acme/pkg-b", apm_deps=["acme/pkg-c"])
            _make_pkg(apm_modules, "acme/pkg-c", apm_deps=["acme/pkg-d"])
            _make_pkg(apm_modules, "acme/pkg-d", mcp=["ghcr.io/acme/mcp-deep"])

            _seed_lockfile(
                tmp_path / "apm.lock.yaml",
                [
                    LockedDependency(
                        repo_url="acme/pkg-a", depth=1, resolved_by=None, resolved_commit="cached"
                    ),
                    LockedDependency(
                        repo_url="acme/pkg-b",
                        depth=2,
                        resolved_by="acme/pkg-a",
                        resolved_commit="cached",
                    ),
                    LockedDependency(
                        repo_url="acme/pkg-c",
                        depth=3,
                        resolved_by="acme/pkg-b",
                        resolved_commit="cached",
                    ),
                    LockedDependency(
                        repo_url="acme/pkg-d",
                        depth=4,
                        resolved_by="acme/pkg-c",
                        resolved_commit="cached",
                    ),
                ],
            )

            from apm_cli.cli import cli

            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "install",
                    "acme/pkg-a",
                    "--trust-transitive-mcp",
                ],
            )

            assert result.exit_code == 0, (
                f"CLI failed:\n{result.output}\n{getattr(result, 'stderr', '')}"
            )

            lockfile = LockFile.read(tmp_path / "apm.lock.yaml")
            assert "acme/pkg-d" in lockfile.dependencies
            assert "ghcr.io/acme/mcp-deep" in lockfile.mcp_servers
        finally:
            os.chdir(orig_cwd)


class TestDiamondDependencyIntegration:
    """CLI-level: A → B, A → C, B → D, C → D where D has MCP.
    MCP from D must appear once in lockfile."""

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_diamond_mcp_in_lockfile(
        self, mock_dl_cls, mock_mcp_install, mock_validate, mock_updates, tmp_path
    ):
        orig_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            apm_modules = tmp_path / "apm_modules"

            _write_apm_yml(tmp_path / "apm.yml", deps=["acme/pkg-a"])
            _make_pkg(apm_modules, "acme/pkg-a", apm_deps=["acme/pkg-b", "acme/pkg-c"])
            _make_pkg(apm_modules, "acme/pkg-b", apm_deps=["acme/pkg-d"])
            _make_pkg(apm_modules, "acme/pkg-c", apm_deps=["acme/pkg-d"])
            _make_pkg(
                apm_modules,
                "acme/pkg-d",
                mcp=[
                    "ghcr.io/acme/mcp-shared",
                ],
            )

            _seed_lockfile(
                tmp_path / "apm.lock.yaml",
                [
                    LockedDependency(
                        repo_url="acme/pkg-a", depth=1, resolved_by=None, resolved_commit="cached"
                    ),
                    LockedDependency(
                        repo_url="acme/pkg-b",
                        depth=2,
                        resolved_by="acme/pkg-a",
                        resolved_commit="cached",
                    ),
                    LockedDependency(
                        repo_url="acme/pkg-c",
                        depth=2,
                        resolved_by="acme/pkg-a",
                        resolved_commit="cached",
                    ),
                    LockedDependency(
                        repo_url="acme/pkg-d",
                        depth=3,
                        resolved_by="acme/pkg-b",
                        resolved_commit="cached",
                    ),
                ],
            )

            from apm_cli.cli import cli

            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "install",
                    "acme/pkg-a",
                    "--trust-transitive-mcp",
                ],
            )

            assert result.exit_code == 0, (
                f"CLI failed:\n{result.output}\n{getattr(result, 'stderr', '')}"
            )

            lockfile = LockFile.read(tmp_path / "apm.lock.yaml")
            assert "ghcr.io/acme/mcp-shared" in lockfile.mcp_servers
            # No duplicates in lockfile
            assert lockfile.mcp_servers.count("ghcr.io/acme/mcp-shared") == 1
        finally:
            os.chdir(orig_cwd)


class TestMultiPackageSelectiveInstallIntegration:
    """CLI-level: `apm install acme/pkg-x acme/pkg-y` — each package
    brings its own transitive MCP deps, both must appear."""

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_multiple_packages_mcp_merged(
        self, mock_dl_cls, mock_mcp_install, mock_validate, mock_updates, tmp_path
    ):
        orig_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            apm_modules = tmp_path / "apm_modules"

            _write_apm_yml(tmp_path / "apm.yml", deps=["acme/pkg-x", "acme/pkg-y"])

            # pkg-x → dep-x (has mcp-x)
            _make_pkg(apm_modules, "acme/pkg-x", apm_deps=["acme/dep-x"])
            _make_pkg(apm_modules, "acme/dep-x", mcp=["ghcr.io/acme/mcp-x"])

            # pkg-y → dep-y (has mcp-y)
            _make_pkg(apm_modules, "acme/pkg-y", apm_deps=["acme/dep-y"])
            _make_pkg(apm_modules, "acme/dep-y", mcp=["ghcr.io/acme/mcp-y"])

            _seed_lockfile(
                tmp_path / "apm.lock.yaml",
                [
                    LockedDependency(
                        repo_url="acme/pkg-x", depth=1, resolved_by=None, resolved_commit="cached"
                    ),
                    LockedDependency(
                        repo_url="acme/dep-x",
                        depth=2,
                        resolved_by="acme/pkg-x",
                        resolved_commit="cached",
                    ),
                    LockedDependency(
                        repo_url="acme/pkg-y", depth=1, resolved_by=None, resolved_commit="cached"
                    ),
                    LockedDependency(
                        repo_url="acme/dep-y",
                        depth=2,
                        resolved_by="acme/pkg-y",
                        resolved_commit="cached",
                    ),
                ],
            )

            from apm_cli.cli import cli

            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "install",
                    "acme/pkg-x",
                    "acme/pkg-y",
                    "--trust-transitive-mcp",
                ],
            )

            assert result.exit_code == 0, (
                f"CLI failed:\n{result.output}\n{getattr(result, 'stderr', '')}"
            )

            lockfile = LockFile.read(tmp_path / "apm.lock.yaml")
            assert "ghcr.io/acme/mcp-x" in lockfile.mcp_servers
            assert "ghcr.io/acme/mcp-y" in lockfile.mcp_servers
        finally:
            os.chdir(orig_cwd)


class TestFullInstallTransitiveMCPIntegration:
    """CLI-level integration: plain `apm install` (no specific packages)
    must also collect transitive MCP deps."""

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_full_install_collects_transitive_mcp(
        self, mock_dl_cls, mock_mcp_install, mock_updates, cli_env
    ):
        tmp_path, runner = cli_env
        from apm_cli.cli import cli

        result = runner.invoke(cli, ["install", "--trust-transitive-mcp"])

        assert result.exit_code == 0, (
            f"CLI failed:\n{result.output}\n{getattr(result, 'stderr', '')}"
        )

        lockfile = LockFile.read(tmp_path / "apm.lock.yaml")
        assert lockfile is not None
        assert "ghcr.io/acme/mcp-alpha" in lockfile.mcp_servers
        assert "ghcr.io/acme/mcp-beta" in lockfile.mcp_servers


class TestStaleRemovalAfterUpdate:
    """When a package drops/renames an MCP server, stale entries must be
    removed from .vscode/mcp.json during install --update."""

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_stale_mcp_removed_on_update(
        self, mock_dl_cls, mock_mcp_install, mock_updates, tmp_path
    ):
        orig_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            apm_modules = tmp_path / "apm_modules"

            # Root depends on infra-cloud
            _write_apm_yml(tmp_path / "apm.yml", deps=["acme/infra-cloud"])

            # infra-cloud NOW declares only mcp-beta (dropped mcp-alpha)
            _make_pkg(
                apm_modules,
                "acme/infra-cloud",
                mcp=[
                    "ghcr.io/acme/mcp-beta",
                ],
            )

            # Pre-existing lockfile still references both servers
            _seed_lockfile(
                tmp_path / "apm.lock.yaml",
                [
                    LockedDependency(
                        repo_url="acme/infra-cloud",
                        depth=1,
                        resolved_by=None,
                        resolved_commit="cached",
                    ),
                ],
                mcp_servers=["ghcr.io/acme/mcp-alpha", "ghcr.io/acme/mcp-beta"],
            )

            # Pre-existing .vscode/mcp.json has both servers
            mcp_json = tmp_path / ".vscode" / "mcp.json"
            mcp_json.parent.mkdir(parents=True, exist_ok=True)
            mcp_json.write_text(
                json.dumps(
                    {
                        "servers": {
                            "ghcr.io/acme/mcp-alpha": {"command": "npx", "args": ["alpha"]},
                            "ghcr.io/acme/mcp-beta": {"command": "npx", "args": ["beta"]},
                        }
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            from apm_cli.cli import cli

            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "install",
                    "--trust-transitive-mcp",
                ],
            )

            assert result.exit_code == 0, (
                f"CLI failed:\n{result.output}\n{getattr(result, 'stderr', '')}"
            )

            # Stale server must be removed from mcp.json
            updated = json.loads(mcp_json.read_text(encoding="utf-8"))
            assert "ghcr.io/acme/mcp-alpha" not in updated["servers"]
            assert "ghcr.io/acme/mcp-beta" in updated["servers"]

            # Lockfile must only list the remaining server
            lockfile = LockFile.read(tmp_path / "apm.lock.yaml")
            assert "ghcr.io/acme/mcp-alpha" not in lockfile.mcp_servers
            assert "ghcr.io/acme/mcp-beta" in lockfile.mcp_servers

        finally:
            os.chdir(orig_cwd)


class TestNoMCPWhenOnlyAPM:
    """With --only=apm, MCP collection must be skipped but existing
    lockfile mcp_servers must be preserved."""

    @patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_only_apm_preserves_mcp_servers(self, mock_dl_cls, mock_updates, cli_env):
        tmp_path, runner = cli_env

        # Seed lockfile with existing MCP servers
        _seed_lockfile(
            tmp_path / "apm.lock.yaml",
            [
                LockedDependency(
                    repo_url="acme/squad-alpha", depth=1, resolved_by=None, resolved_commit="cached"
                ),
                LockedDependency(
                    repo_url="acme/infra-cloud",
                    depth=2,
                    resolved_by="acme/squad-alpha",
                    resolved_commit="cached",
                ),
            ],
            mcp_servers=["ghcr.io/acme/mcp-alpha", "ghcr.io/acme/mcp-beta"],
        )

        from apm_cli.cli import cli

        result = runner.invoke(cli, ["install", "--only=apm"])

        assert result.exit_code == 0, (
            f"CLI failed:\n{result.output}\n{getattr(result, 'stderr', '')}"
        )

        # MCP servers must be preserved (not wiped) even with --only=apm
        lockfile = LockFile.read(tmp_path / "apm.lock.yaml")
        assert "ghcr.io/acme/mcp-alpha" in lockfile.mcp_servers
        assert "ghcr.io/acme/mcp-beta" in lockfile.mcp_servers
