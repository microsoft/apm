"""Integration tests: ``apm update`` reconciles MCP/LSP servers (#2077).

``apm update`` calls ``_install_apm_dependencies`` directly instead of
going through ``_install_apm_packages`` (see ``commands/install.py``), so it
historically skipped the ``MCPIntegrator``/``LSPIntegrator`` reconciliation
that runs after a normal ``apm install``. Two distinct failure modes:

* MCP: the lockfile phase intentionally carries ``mcp_servers``/
  ``mcp_configs`` forward unreconciled
  (``install/phases/lockfile.py::_preserve_existing_mcp_state``), so without
  a downstream reconcile step orphaned entries survive ``apm update``
  indefinitely (the bug reported in #2077).
* LSP: there is no equivalent carry-forward for ``lsp_servers``/
  ``lsp_configs`` -- the lockfile builder always starts them empty -- so
  without ``run_lsp_integration`` running, servers actually declared in
  apm.yml never make it into the lockfile after ``apm update`` at all.

Mirrors the mocking pattern from ``test_global_mcp_lockfile_e2e.py``:
``GitHubPackageDownloader`` is stubbed so the resolver never touches the
network, and a resolved-commit mismatch against the pre-seeded lockfile
drives an "update" plan entry so ``apm update --yes`` proceeds past the
consent gate.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockedDependency, LockFile

_OLD_SHA = "a" * 40
_NEW_SHA = "b" * 40


def _stub_downloader_for_lockfile(mock_dl_cls) -> None:
    """Configure a patched ``GitHubPackageDownloader`` so the resolver
    never touches the network and reports a commit change (``_NEW_SHA``)
    against the pre-seeded lockfile's ``_OLD_SHA`` -- driving an "update"
    plan entry so ``apm update --yes`` proceeds past the consent gate.
    """
    instance = mock_dl_cls.return_value
    pkg_info = MagicMock()
    pkg_info.resolved_reference.resolved_commit = _NEW_SHA
    pkg_info.resolved_reference.ref_name = "main"
    pkg_info.resolved_reference.is_branch = True
    pkg_info.resolved_reference.is_tag = False
    pkg_info.resolved_reference.is_sha = False
    pkg_info.package_type.value = "apm_package"
    pkg_info.package.name = "stub-package"
    pkg_info.package.version = "0.0.0"
    instance.download_package.return_value = pkg_info


def _write_apm_yml(
    path: Path,
    *,
    name: str = "test-project",
    deps: list | None = None,
    mcp: list | None = None,
    lsp: list | None = None,
) -> None:
    data: dict = {"name": name, "version": "1.0.0", "dependencies": {"apm": deps or []}}
    if mcp is not None:
        data["dependencies"]["mcp"] = mcp
    if lsp is not None:
        data["dependencies"]["lsp"] = lsp
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _make_pkg(apm_modules: Path, repo_url: str, *, name: str | None = None) -> None:
    """Create a package directory with a minimal (no MCP/LSP) apm.yml."""
    pkg_dir = apm_modules / repo_url
    pkg_dir.mkdir(parents=True, exist_ok=True)
    _write_apm_yml(pkg_dir / "apm.yml", name=name or repo_url.split("/")[-1])


def _seed_lockfile(
    path: Path,
    locked_deps: list,
    *,
    mcp_servers: list | None = None,
    mcp_configs: dict | None = None,
    lsp_servers: list | None = None,
    lsp_configs: dict | None = None,
) -> None:
    lf = LockFile()
    for dep in locked_deps:
        lf.add_dependency(dep)
    if mcp_servers:
        lf.mcp_servers = mcp_servers
    if mcp_configs:
        lf.mcp_configs = mcp_configs
    if lsp_servers:
        lf.lsp_servers = lsp_servers
    if lsp_configs:
        lf.lsp_configs = lsp_configs
    lf.write(path)


def _seed_plain_dependency(path: Path, *, resolved_commit: str = _OLD_SHA) -> None:
    """Seed a lockfile with a single ``acme/plain-pkg`` entry, no MCP/LSP."""
    _seed_lockfile(
        path,
        [
            LockedDependency(
                repo_url="acme/plain-pkg",
                depth=1,
                resolved_by=None,
                resolved_ref="main",
                resolved_commit=resolved_commit,
            ),
        ],
    )


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """Isolated project directory with ``apm_modules/`` prepared."""
    (tmp_path / "apm_modules").mkdir()
    monkeypatch.setenv("APM_E2E_TESTS", "1")
    orig_cwd = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(orig_cwd)


class TestUpdatePrunesOrphanedMCPServers:
    """Regression for #2077: ``apm update`` must prune MCP servers no
    longer declared anywhere in the dependency tree, not just carry them
    forward from the previous lockfile."""

    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_apm_update_prunes_orphaned_mcp_server(self, mock_dl_cls, project):
        """A manifest with no MCP deps must have previously-locked orphaned
        MCP servers pruned from ``apm.lock.yaml`` after ``apm update --yes``."""
        _stub_downloader_for_lockfile(mock_dl_cls)
        apm_modules = project / "apm_modules"

        # Root manifest depends on plain-pkg and declares no MCP servers
        # (simulating a dependency that has dropped its MCP server).
        _write_apm_yml(project / "apm.yml", deps=["acme/plain-pkg"])
        _make_pkg(apm_modules, "acme/plain-pkg")

        # Pre-seed the lockfile with an orphaned MCP server entry left over
        # from a prior install, plus a resolved_commit that will differ
        # from the mocked downloader's response so the update plan shows a
        # change and proceeds past the consent gate.
        _seed_lockfile(
            project / "apm.lock.yaml",
            [
                LockedDependency(
                    repo_url="acme/plain-pkg",
                    depth=1,
                    resolved_by=None,
                    resolved_ref="main",
                    resolved_commit=_OLD_SHA,
                ),
            ],
            mcp_servers=["ghcr.io/acme/old-mcp-server"],
            mcp_configs={"ghcr.io/acme/old-mcp-server": {"name": "ghcr.io/acme/old-mcp-server"}},
        )

        from apm_cli.cli import cli

        result = CliRunner().invoke(cli, ["update", "--yes", "--target", "claude"])

        assert result.exit_code == 0, f"CLI failed (exit {result.exit_code}):\n{result.output}"

        updated_lock = LockFile.read(project / "apm.lock.yaml")
        assert updated_lock is not None, "Lockfile missing after update"
        assert updated_lock.mcp_servers == [], (
            f"Expected orphaned MCP server to be pruned, got: {updated_lock.mcp_servers}"
        )
        assert updated_lock.mcp_configs == {}, (
            f"Expected orphaned MCP config to be pruned, got: {updated_lock.mcp_configs}"
        )

    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_apm_update_dry_run_does_not_touch_mcp_state(self, mock_dl_cls, project):
        """``apm update --dry-run`` must not mutate MCP lockfile state."""
        _stub_downloader_for_lockfile(mock_dl_cls)
        apm_modules = project / "apm_modules"

        _write_apm_yml(project / "apm.yml", deps=["acme/plain-pkg"])
        _make_pkg(apm_modules, "acme/plain-pkg")
        _seed_lockfile(
            project / "apm.lock.yaml",
            [
                LockedDependency(
                    repo_url="acme/plain-pkg",
                    depth=1,
                    resolved_by=None,
                    resolved_ref="main",
                    resolved_commit=_OLD_SHA,
                ),
            ],
            mcp_servers=["ghcr.io/acme/old-mcp-server"],
        )

        from apm_cli.cli import cli

        result = CliRunner().invoke(cli, ["update", "--dry-run"])

        assert result.exit_code == 0, f"CLI failed (exit {result.exit_code}):\n{result.output}"

        untouched_lock = LockFile.read(project / "apm.lock.yaml")
        assert untouched_lock is not None
        assert untouched_lock.mcp_servers == ["ghcr.io/acme/old-mcp-server"], (
            f"Dry run must not prune MCP servers: got {untouched_lock.mcp_servers}"
        )


class TestUpdateRunsLSPIntegration:
    """Regression for #2077's LSP parity decision: ``apm update`` must run
    ``run_lsp_integration`` the same way ``apm install`` does, not just
    ``run_mcp_integration``. Unlike MCP, the lockfile builder never carries
    ``lsp_servers`` forward, so without this fix a declared LSP dependency
    never reaches ``apm.lock.yaml`` via ``apm update`` at all."""

    @patch("apm_cli.deps.github_downloader.GitHubPackageDownloader")
    def test_apm_update_writes_declared_lsp_server_to_lockfile(self, mock_dl_cls, project):
        """A manifest that declares an LSP server must have it recorded in
        ``apm.lock.yaml`` after ``apm update --yes``."""
        _stub_downloader_for_lockfile(mock_dl_cls)
        apm_modules = project / "apm_modules"

        _write_apm_yml(
            project / "apm.yml",
            deps=["acme/plain-pkg"],
            lsp=[
                {
                    "name": "test-lsp",
                    "command": "test-lsp-cmd",
                    "extensionToLanguage": {".test": "test-lang"},
                }
            ],
        )
        _make_pkg(apm_modules, "acme/plain-pkg")
        _seed_plain_dependency(project / "apm.lock.yaml")

        from apm_cli.cli import cli

        result = CliRunner().invoke(cli, ["update", "--yes", "--target", "claude"])

        assert result.exit_code == 0, f"CLI failed (exit {result.exit_code}):\n{result.output}"

        updated_lock = LockFile.read(project / "apm.lock.yaml")
        assert updated_lock is not None, "Lockfile missing after update"
        assert updated_lock.lsp_servers == ["test-lsp"], (
            f"Expected declared LSP server to be recorded, got: {updated_lock.lsp_servers}"
        )
