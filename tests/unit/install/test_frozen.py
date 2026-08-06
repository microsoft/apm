"""Unit tests for ``InstallService.enforce_frozen``.

Issue: https://github.com/microsoft/apm/issues/1203 (P0).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.errors import FrozenInstallError
from apm_cli.install.request import InstallRequest
from apm_cli.install.service import InstallService
from apm_cli.models.dependency.reference import DependencyReference


def _write_lockfile(project_dir: Path, deps: list[LockedDependency]) -> None:
    lock = LockFile(
        lockfile_version="1",
        generated_at="2025-01-01T00:00:00+00:00",
        apm_version="0.0.0-test",
    )
    for dep in deps:
        lock.add_dependency(dep)
    (project_dir / "apm.lock.yaml").write_text(lock.to_yaml())


def _write_apm_yml(project_dir: Path) -> None:
    (project_dir / "apm.yml").write_text("name: test\nversion: 1.0.0\n")


def _make_request(*, project_dir: Path, manifest_deps: list[DependencyReference]) -> InstallRequest:
    pkg = MagicMock()
    pkg.package_path = project_dir / "apm.yml"
    pkg.get_apm_dependencies.return_value = manifest_deps
    pkg.get_dev_apm_dependencies.return_value = []
    return InstallRequest(apm_package=pkg, frozen=True)


class TestEnforceFrozen:
    def test_raises_when_lockfile_missing(self, tmp_path: Path):
        _write_apm_yml(tmp_path)
        req = _make_request(project_dir=tmp_path, manifest_deps=[])

        with pytest.raises(FrozenInstallError, match=r"requires apm\.lock\.yaml"):
            InstallService.enforce_frozen(req)

    def test_raises_when_manifest_dep_missing_from_lockfile(self, tmp_path: Path):
        _write_apm_yml(tmp_path)
        _write_lockfile(tmp_path, [])
        dep = DependencyReference(repo_url="https://github.com/declared/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])

        with pytest.raises(FrozenInstallError, match="out of sync") as exc_info:
            InstallService.enforce_frozen(req)

        assert any("declared/r" in r for r in exc_info.value.reasons)

    def test_succeeds_when_lockfile_has_all_manifest_deps(self, tmp_path: Path):
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            [
                LockedDependency(
                    repo_url="https://github.com/o/r",
                    resolved_ref="main",
                    resolved_commit="a" * 40,
                    depth=1,
                ),
            ],
        )
        dep = DependencyReference(repo_url="https://github.com/o/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])

        InstallService.enforce_frozen(req)

    def test_orphan_lockfile_entries_dont_fail(self, tmp_path: Path):
        """Mirrors npm ci: extra lock entries are tolerated; only direct deps must be present."""
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            [
                LockedDependency(
                    repo_url="https://github.com/o/r",
                    resolved_ref="main",
                    resolved_commit="a" * 40,
                    depth=1,
                ),
                LockedDependency(
                    repo_url="https://github.com/orphan/r",
                    resolved_ref="main",
                    resolved_commit="b" * 40,
                    depth=1,
                ),
            ],
        )
        dep = DependencyReference(repo_url="https://github.com/o/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])

        InstallService.enforce_frozen(req)

    def test_mixed_project_rejects_stale_locked_mcp_state(self, tmp_path: Path):
        from apm_cli.integration.mcp_config_view import CurrentMcpConfigView

        _write_apm_yml(tmp_path)
        locked_dep = LockedDependency(
            repo_url="https://github.com/o/r",
            resolved_ref="main",
            resolved_commit="a" * 40,
            depth=1,
        )
        _write_lockfile(tmp_path, [locked_dep])
        lock = LockFile.read(tmp_path / "apm.lock.yaml")
        assert lock is not None
        lock.mcp_servers = ["stale-mcp"]
        lock.mcp_configs = {"stale-mcp": {"name": "stale-mcp"}}
        lock.save(tmp_path / "apm.lock.yaml")
        dep = DependencyReference(repo_url="https://github.com/o/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])
        req.apm_package.get_all_mcp_dependencies.return_value = []
        current = CurrentMcpConfigView(
            dependencies=(),
            configs={},
            provenance={},
            problems=(),
        )

        with (
            patch.object(CurrentMcpConfigView, "derive", return_value=current),
            pytest.raises(FrozenInstallError, match="out of sync") as exc_info,
        ):
            InstallService.enforce_frozen(req)

        assert any("stale-mcp" in reason for reason in exc_info.value.reasons)


class TestEnforceFrozenColdCache:
    """Regression tests for #2456: --frozen on a cold cache with git apm_package deps."""

    def _make_git_apm_package_dep(self) -> LockedDependency:
        return LockedDependency(
            repo_url="owner/some-pkg",
            resolved_ref="v1.0.0",
            resolved_commit="a" * 40,
            package_type="apm_package",
            depth=1,
        )

    def test_cold_cache_git_apm_package_dep_passes(self, tmp_path: Path) -> None:
        """--frozen passes on a cold cache when a git apm_package dep dir is absent.

        Before the fix this raised FrozenInstallError with a McpSourceProblem
        because _collect_locked_dependencies could not read the absent apm.yml.
        """
        _write_apm_yml(tmp_path)
        dep = self._make_git_apm_package_dep()
        _write_lockfile(tmp_path, [dep])

        # Lockfile has MCP state (root-declared) to trigger the MCP check path.
        lock = LockFile.read(tmp_path / "apm.lock.yaml")
        assert lock is not None
        lock.mcp_servers = ["root-mcp"]
        lock.mcp_configs = {"root-mcp": {"name": "root-mcp"}}
        lock.save(tmp_path / "apm.lock.yaml")

        manifest_dep = DependencyReference(repo_url="owner/some-pkg")
        req = _make_request(project_dir=tmp_path, manifest_deps=[manifest_dep])
        # Root manifest declares the MCP server directly.
        from apm_cli.models.dependency.mcp import MCPDependency

        req.apm_package.get_all_mcp_dependencies.return_value = [MCPDependency(name="root-mcp")]

        # Must not raise: the absent apm_package dep dir is a cold-cache artifact.
        InstallService.enforce_frozen(req)

    def test_cold_cache_git_apm_package_with_own_mcp_passes(self, tmp_path: Path) -> None:
        """--frozen passes when the absent apm_package dep itself declared MCP.

        The dep's MCP servers are stored in lockfile.mcp_configs and will be
        hydrated by the frozen install.  They must not appear as lock_only drift.
        """
        _write_apm_yml(tmp_path)
        dep = self._make_git_apm_package_dep()
        dep_name = dep.to_dependency_ref().get_install_path(tmp_path / "apm_modules").name
        _write_lockfile(tmp_path, [dep])

        lock = LockFile.read(tmp_path / "apm.lock.yaml")
        assert lock is not None
        lock.mcp_servers = ["pkg-mcp"]
        lock.mcp_configs = {"pkg-mcp": {"name": "pkg-mcp"}}
        # Provenance marks the server as contributed by the absent package.
        lock.mcp_config_provenance = {"pkg-mcp": dep_name}
        lock.save(tmp_path / "apm.lock.yaml")

        manifest_dep = DependencyReference(repo_url="owner/some-pkg")
        req = _make_request(project_dir=tmp_path, manifest_deps=[manifest_dep])
        req.apm_package.get_all_mcp_dependencies.return_value = []

        # Must not raise: pkg-mcp is exclusively from the absent dep (cold cache).
        InstallService.enforce_frozen(req)

    def test_cold_cache_does_not_mask_real_mcp_drift_from_installed_dep(
        self, tmp_path: Path
    ) -> None:
        """Real MCP drift from an INSTALLED dep is still caught on cold cache.

        The cold-cache exemption only covers absent packages; installed packages
        with stale MCP in the lockfile must still fail.
        """
        from apm_cli.integration.mcp_config_view import CurrentMcpConfigView

        _write_apm_yml(tmp_path)
        absent_dep = self._make_git_apm_package_dep()
        _write_lockfile(tmp_path, [absent_dep])

        lock = LockFile.read(tmp_path / "apm.lock.yaml")
        assert lock is not None
        # stale-mcp has no provenance entry -> cannot be attributed to absent dep.
        lock.mcp_servers = ["stale-mcp"]
        lock.mcp_configs = {"stale-mcp": {"name": "stale-mcp"}}
        lock.mcp_config_provenance = {}
        lock.save(tmp_path / "apm.lock.yaml")

        manifest_dep = DependencyReference(repo_url="owner/some-pkg")
        req = _make_request(project_dir=tmp_path, manifest_deps=[manifest_dep])
        req.apm_package.get_all_mcp_dependencies.return_value = []
        current = CurrentMcpConfigView(
            dependencies=(),
            configs={},
            provenance={},
            problems=(),
        )

        with (
            patch.object(CurrentMcpConfigView, "derive", return_value=current),
            pytest.raises(FrozenInstallError, match="out of sync") as exc_info,
        ):
            InstallService.enforce_frozen(req)

        assert any("stale-mcp" in reason for reason in exc_info.value.reasons)

    def test_co_owned_server_not_exempted_by_cold_cache(self, tmp_path: Path) -> None:
        """A server co-owned by an absent dep AND another owner is NOT cold-cache exempt.

        The issubset guard ensures only servers whose EVERY owner is absent are
        augmented. A co-owned server must remain subject to live comparison so
        that real drift introduced by the non-absent co-owner is not silently
        dropped. This regression-traps the issubset check in
        _absent_apm_package_mcp_configs.
        """
        from apm_cli.integration.mcp_config_view import CurrentMcpConfigView

        _write_apm_yml(tmp_path)
        dep = self._make_git_apm_package_dep()
        dep_name = dep.to_dependency_ref().get_install_path(tmp_path / "apm_modules").name
        _write_lockfile(tmp_path, [dep])

        lock = LockFile.read(tmp_path / "apm.lock.yaml")
        assert lock is not None
        # server-a: exclusively owned by absent dep -> should be augmented (no drift)
        # server-b: co-owned by absent dep + another owner -> NOT augmented
        lock.mcp_servers = ["server-a", "server-b"]
        lock.mcp_configs = {
            "server-a": {"name": "server-a"},
            "server-b": {"name": "server-b"},
        }
        lock.mcp_config_provenance = {
            "server-a": dep_name,
            "server-b": [dep_name, "some-other-present-owner"],
        }
        lock.save(tmp_path / "apm.lock.yaml")

        manifest_dep = DependencyReference(repo_url="owner/some-pkg")
        req = _make_request(project_dir=tmp_path, manifest_deps=[manifest_dep])
        req.apm_package.get_all_mcp_dependencies.return_value = []
        current = CurrentMcpConfigView(
            dependencies=(),
            configs={},
            provenance={},
            problems=(),
        )

        with (
            patch.object(CurrentMcpConfigView, "derive", return_value=current),
            pytest.raises(FrozenInstallError, match="out of sync") as exc_info,
        ):
            InstallService.enforce_frozen(req)

        # server-b must appear as lock_only drift (it was NOT exempted)
        assert any("server-b" in reason for reason in exc_info.value.reasons)
        # server-a is exclusively absent and should NOT produce a drift reason
        assert not any("server-a" in reason for reason in exc_info.value.reasons)

    def test_cold_cache_mcp_restoration_emits_verbose_log(self, tmp_path: Path) -> None:
        """enforce_frozen emits a verbose_detail log when cold-cache MCP servers are restored (DX-3).

        This regression-traps the request.logger.verbose_detail() call added in
        _absent_apm_package_mcp_configs augmentation path. If the call is removed,
        the assertion on verbose_messages fails.
        """
        from apm_cli.core.command_logger import InstallLogger

        class _RecordingInstallLogger(InstallLogger):
            def __init__(self) -> None:
                super().__init__(verbose=True)
                self.verbose_messages: list[str] = []

            def verbose_detail(self, message: str) -> None:
                self.verbose_messages.append(message)

        _write_apm_yml(tmp_path)
        dep = self._make_git_apm_package_dep()
        dep_name = dep.to_dependency_ref().get_install_path(tmp_path / "apm_modules").name
        _write_lockfile(tmp_path, [dep])

        lock = LockFile.read(tmp_path / "apm.lock.yaml")
        assert lock is not None
        lock.mcp_servers = ["pkg-mcp"]
        lock.mcp_configs = {"pkg-mcp": {"name": "pkg-mcp"}}
        lock.mcp_config_provenance = {"pkg-mcp": dep_name}
        lock.save(tmp_path / "apm.lock.yaml")

        manifest_dep = DependencyReference(repo_url="owner/some-pkg")
        recording_logger = _RecordingInstallLogger()
        pkg = MagicMock()
        pkg.package_path = tmp_path / "apm.yml"
        pkg.get_apm_dependencies.return_value = [manifest_dep]
        pkg.get_dev_apm_dependencies.return_value = []
        pkg.get_all_mcp_dependencies.return_value = []
        req = InstallRequest(apm_package=pkg, frozen=True, logger=recording_logger)

        InstallService.enforce_frozen(req)

        cold_cache_logs = [
            m for m in recording_logger.verbose_messages if "cold-cache" in m.lower()
        ]
        assert len(cold_cache_logs) >= 1, (
            f"Expected at least one cold-cache verbose log from enforce_frozen; got: {recording_logger.verbose_messages}"
        )
