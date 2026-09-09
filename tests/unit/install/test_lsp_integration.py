"""Unit tests for ``apm_cli.install.lsp.integration.run_lsp_integration``.

Covers the high-level orchestration that wires together LSPIntegrator
calls: transitive collection, deduplication, install, stale cleanup,
and lockfile persistence.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.constants import InstallMode
from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.install.lsp.integration import (
    reconcile_lsp_after_uninstall,
    run_lsp_integration,
    run_owned_lsp_integration,
)
from apm_cli.models.dependency.lsp import LSPDependency

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep(name: str, **kwargs) -> LSPDependency:
    defaults = {
        "command": kwargs.pop("command", f"{name}-langserver"),
        "extension_to_language": kwargs.pop("extension_to_language", {".py": "python"}),
    }
    defaults.update(kwargs)
    return LSPDependency(name=name, **defaults)


def _mock_logger():
    logger = MagicMock()
    logger.verbose_detail = MagicMock()
    logger.progress = MagicMock()
    return logger


def _mock_lock(
    *,
    lsp_servers=None,
    lsp_configs=None,
    lsp_config_provenance=None,
    lsp_target_servers=None,
    target_servers_present=False,
):
    lock = MagicMock()
    lock.lsp_servers = lsp_servers or []
    lock.lsp_configs = lsp_configs or {}
    lock.lsp_config_provenance = lsp_config_provenance or {}
    lock.lsp_target_servers = lsp_target_servers or {}
    lock._lsp_target_servers_present = target_servers_present
    return lock


_PATCH_TARGET = "apm_cli.integration.lsp_integrator.LSPIntegrator"


class TestOwnedLspIntegration:
    """Bundle LSP state is reconciled by stable owner identity."""

    @patch(_PATCH_TARGET)
    def test_records_owner_and_removes_only_its_stale_servers(
        self, mock_integrator, tmp_path
    ) -> None:
        lock_path = tmp_path / "apm.lock.yaml"
        LockFile(
            lsp_servers=["old", "project"],
            lsp_configs={"old": {}, "project": {}},
            lsp_config_provenance={"old": "bundle#1"},
            lsp_target_servers={"copilot": ["old"]},
            _lsp_target_servers_present=True,
        ).write(lock_path)
        dependency = _make_dep("new")
        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"new"}
        mock_integrator.get_server_configs.return_value = {"new": dependency.to_lsp_json_entry()}
        mock_integrator.supported_target_runtimes.return_value = ["copilot"]

        count = run_owned_lsp_integration(
            dependencies=[dependency],
            owner="bundle#1",
            lock_path=lock_path,
            project_root=tmp_path,
            user_scope=False,
            target_runtimes=["copilot"],
            logger=_mock_logger(),
        )

        assert count == 1
        mock_integrator.remove_stale.assert_called_once()
        assert mock_integrator.remove_stale.call_args.args[0] == {"old"}
        lockfile = LockFile.read(lock_path)
        assert lockfile is not None
        assert lockfile.lsp_servers == ["new", "project"]
        assert lockfile.lsp_config_provenance == {"new": "bundle:bundle#1"}
        assert lockfile.lsp_target_servers == {"copilot": ["new"]}

    @patch(_PATCH_TARGET)
    def test_rejects_name_owned_by_another_source(self, mock_integrator, tmp_path) -> None:
        lock_path = tmp_path / "apm.lock.yaml"
        LockFile(
            lsp_servers=["pyright"],
            lsp_configs={"pyright": {}},
            lsp_config_provenance={"pyright": "other#1"},
        ).write(lock_path)
        mock_integrator.get_server_names.return_value = {"pyright"}

        with pytest.raises(ValueError, match="conflicts with another owner") as exc_info:
            run_owned_lsp_integration(
                dependencies=[_make_dep("pyright")],
                owner="bundle#1",
                lock_path=lock_path,
                project_root=tmp_path,
                user_scope=False,
                target_runtimes=["copilot"],
                logger=_mock_logger(),
            )
        assert "other#1" in str(exc_info.value)
        assert "--force does not transfer ownership" in str(exc_info.value)

    @patch(_PATCH_TARGET)
    def test_legacy_bundle_provenance_does_not_infer_current_target(
        self, mock_integrator, tmp_path
    ) -> None:
        """A pre-target-map owner must not authorize deletion on a new target."""
        lock_path = tmp_path / "apm.lock.yaml"
        LockFile(
            lsp_servers=["shared"],
            lsp_configs={"shared": {"command": "legacy"}},
            lsp_config_provenance={"shared": "bundle#1"},
        ).write(lock_path)
        mock_integrator.get_server_names.return_value = set()
        mock_integrator.get_server_configs.return_value = {}

        run_owned_lsp_integration(
            dependencies=[],
            owner="bundle#1",
            lock_path=lock_path,
            project_root=tmp_path,
            user_scope=False,
            target_runtimes=["claude"],
            logger=_mock_logger(),
        )

        mock_integrator.remove_stale.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_bundle_id_cannot_impersonate_project_owner(self, mock_integrator, tmp_path) -> None:
        lock_path = tmp_path / "apm.lock.yaml"
        LockFile(
            lsp_servers=["project"],
            lsp_configs={"project": {"command": "project-lsp"}},
            lsp_config_provenance={"project": "project:."},
            lsp_target_servers={"claude": ["project"]},
            _lsp_target_servers_present=True,
        ).write(lock_path)
        mock_integrator.get_server_names.return_value = set()

        count = run_owned_lsp_integration(
            dependencies=[],
            owner="project:.",
            lock_path=lock_path,
            project_root=tmp_path,
            user_scope=False,
            target_runtimes=["claude"],
            logger=_mock_logger(),
        )

        assert count == 0
        mock_integrator.remove_stale.assert_not_called()
        lockfile = LockFile.read(lock_path)
        assert lockfile is not None
        assert lockfile.lsp_servers == ["project"]
        assert lockfile.lsp_config_provenance == {"project": "project:."}

    def test_local_bundle_force_reaches_owned_lsp_writer(self, tmp_path) -> None:
        from types import SimpleNamespace

        from apm_cli.install.local_bundle_handler import _wire_bundle_lsp_servers

        with patch(
            "apm_cli.install.lsp.integration.run_owned_lsp_integration",
            return_value=0,
        ) as run_owned:
            _wire_bundle_lsp_servers(
                bundle_dir=tmp_path / "bundle",
                targets=[SimpleNamespace(name="claude")],
                project_root=tmp_path,
                user_scope=False,
                verbose=False,
                logger=_mock_logger(),
                deps=[_make_dep("pyright")],
                owner="bundle#1",
                force=True,
            )

        assert run_owned.call_args.kwargs["force"] is True


# ===========================================================================
# Basic orchestration
# ===========================================================================


class TestRunLspIntegration:
    @patch(_PATCH_TARGET)
    def test_no_lsp_deps_no_old_servers(self, mock_integrator, tmp_path):
        """No LSP deps and no previous state -- nothing to do."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = []

        count = run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        assert count == 0
        mock_integrator.install.assert_not_called()
        mock_integrator.resolve_target_runtimes.assert_not_called()
        mock_integrator.update_lockfile.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_installs_direct_deps(self, mock_integrator, tmp_path):
        """Direct LSP deps are installed when should_install is True."""
        deps = [_make_dep("pyright")]
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = deps

        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"pyright"}
        mock_integrator.get_server_configs.return_value = {"pyright": {}}
        mock_integrator.collect_transitive.return_value = []

        modules = tmp_path / "apm_modules"
        modules.mkdir()

        count = run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=modules,
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        assert count == 1
        mock_integrator.install.assert_called_once()

    @patch(_PATCH_TARGET)
    def test_manifest_conflict_names_bundle_owner_and_recovery(self, mock_integrator, tmp_path):
        """A cross-owner conflict must not imply that --force transfers it."""
        dependency = _make_dep("shared")
        package = MagicMock()
        package.get_lsp_dependencies.return_value = [dependency]
        old_lock = _mock_lock(
            lsp_servers=["shared"],
            lsp_configs={"shared": {}},
            lsp_config_provenance={"shared": "bundle:vendor/tool"},
            lsp_target_servers={"claude": ["shared"]},
            target_servers_present=True,
        )
        mock_integrator.resolve_target_runtimes.return_value = ["claude"]
        mock_integrator.get_server_names.return_value = {"shared"}

        with pytest.raises(
            ValueError,
            match="installed bundle owner",
        ) as exc_info:
            run_lsp_integration(
                apm_package=package,
                apm_modules_path=tmp_path / "apm_modules",
                lock_path=tmp_path / "apm.lock.yaml",
                existing_lock=old_lock,
                project_root=tmp_path,
                user_scope=False,
                should_install=True,
                logger=_mock_logger(),
            )

        assert "bundle:vendor/tool" in str(exc_info.value)
        assert "--force does not transfer ownership" in str(exc_info.value)

    @patch(_PATCH_TARGET)
    def test_filters_unapproved_lsp_dependencies(self, mock_integrator, tmp_path):
        """An explicit executable gate blocks LSP servers until approved."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = []
        apm_package.allow_executables = {}
        mock_integrator.collect_transitive.return_value = [
            _make_dep(
                "pyright",
                resolved_by="owner/package",
                approval_keys=("owner/package",),
            )
        ]
        mock_integrator.deduplicate.return_value = []
        mock_integrator.get_server_names.return_value = set()
        mock_integrator.get_server_configs.return_value = {}
        modules = tmp_path / "apm_modules"
        modules.mkdir()

        count = run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=modules,
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
            effective_allow_executables={},
        )

        assert count == 0
        mock_integrator.install.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_resolves_targets_for_install(self, mock_integrator, tmp_path):
        """Install orchestration writes only to resolved LSP targets."""
        deps = [_make_dep("pyright")]
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = deps

        mock_integrator.resolve_target_runtimes.return_value = ["copilot"]
        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"pyright"}
        mock_integrator.get_server_configs.return_value = {"pyright": {}}
        mock_integrator.collect_transitive.return_value = []
        logger = _mock_logger()

        count = run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=logger,
        )

        assert count == 1
        mock_integrator.resolve_target_runtimes.assert_called_once()
        mock_integrator.install.assert_called_once_with(
            deps,
            project_root=tmp_path,
            user_scope=False,
            logger=logger,
            diagnostics=None,
            target_runtimes=["copilot"],
            fail_on_write_error=False,
            managed_target_servers={},
            force=False,
        )

    @patch(_PATCH_TARGET)
    def test_deduplicates_transitive(self, mock_integrator, tmp_path):
        """When transitive deps exist, deduplication is applied."""
        direct = [_make_dep("pyright")]
        transitive = [_make_dep("ruff-lsp")]

        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = direct

        mock_integrator.collect_transitive.return_value = transitive
        mock_integrator.deduplicate.return_value = direct + transitive
        mock_integrator.install.return_value = 2
        mock_integrator.get_server_names.return_value = {"pyright", "ruff-lsp"}
        mock_integrator.get_server_configs.return_value = {}

        modules = tmp_path / "apm_modules"
        modules.mkdir()

        count = run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=modules,
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        assert count == 2
        mock_integrator.deduplicate.assert_called_once()

    @patch(_PATCH_TARGET)
    def test_precomputed_disabled_allow_map_does_not_rediscover_policy(
        self, mock_integrator, tmp_path
    ):
        """A resolved disabled gate is distinct from an unresolved trust map."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = []
        transitive = _make_dep(
            "pyright",
            resolved_by="owner/package",
            approval_keys=("owner/package",),
        )
        mock_integrator.collect_transitive.return_value = [transitive]
        mock_integrator.deduplicate.return_value = [transitive]
        mock_integrator.resolve_target_runtimes.return_value = ["claude"]
        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"pyright"}
        mock_integrator.get_server_configs.return_value = {"pyright": {}}
        modules = tmp_path / "apm_modules"
        modules.mkdir()

        with patch("apm_cli.policy.discovery.discover_policy_with_chain") as discover:
            count = run_lsp_integration(
                apm_package=apm_package,
                apm_modules_path=modules,
                lock_path=tmp_path / "apm.lock.yaml",
                existing_lock=None,
                project_root=tmp_path,
                user_scope=False,
                should_install=True,
                logger=_mock_logger(),
                effective_allow_executables=None,
                effective_allow_resolved=True,
            )

        assert count == 1
        discover.assert_not_called()


def test_mcp_only_service_install_does_not_reconcile_lsp(tmp_path) -> None:
    """The explicit MCP filter must not deploy an LSP plugin."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.service_integration import run_service_integrations

    context = SimpleNamespace(
        project_root=tmp_path,
        scope=InstallScope.PROJECT,
        install_mode=InstallMode.MCP,
        logger=_mock_logger(),
        runtime=None,
        exclude=None,
        trust_transitive_mcp=False,
        no_policy=True,
        verbose=False,
        force=False,
        exec_allow_map=None,
        exec_allow_resolved=True,
    )
    package = MagicMock()
    package.get_lsp_dependencies.return_value = [_make_dep("must-not-deploy")]
    target_decision = MagicMock()
    target_decision.value = ["claude"]
    with (
        patch("apm_cli.install.mcp.run_mcp_integration", return_value=(0, {})),
        patch("apm_cli.install.lsp.run_lsp_integration", return_value=0) as run_lsp,
    ):
        run_service_integrations(
            context,
            apm_package=package,
            mcp_deps=[],
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=None,
            old_mcp_servers=set(),
            old_mcp_configs={},
            old_mcp_provenance={},
            old_mcp_target_servers={},
            old_mcp_target_servers_present=True,
            diagnostics=None,
            explicit_target=["claude"],
            target_decision=target_decision,
        )

    assert run_lsp.call_args.kwargs["should_install"] is False


def test_legacy_lock_name_does_not_authorize_foreign_plugin_overwrite(tmp_path) -> None:
    """Legacy `.lsp.json` state proves no ownership of the new plugin path."""
    plugin_path = tmp_path / ".claude" / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json"
    plugin_path.parent.mkdir(parents=True)
    foreign = b'{"name":"apm-lsp","lspServers":{"pyright":{"command":"foreign"}}}\n'
    plugin_path.write_bytes(foreign)
    lock_path = tmp_path / "apm.lock.yaml"
    old_lock = LockFile(
        lsp_servers=["pyright"],
        lsp_configs={"pyright": {"command": "legacy"}},
    )
    old_lock.write(lock_path)
    package = MagicMock()
    package.get_lsp_dependencies.return_value = [_make_dep("pyright")]
    from apm_cli.install.errors import RequiredIntegrationError

    with pytest.raises(RequiredIntegrationError, match="not managed by APM"):
        run_lsp_integration(
            apm_package=package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=lock_path,
            existing_lock=old_lock,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
            runtime="claude",
            fail_on_write_error=True,
        )

    assert plugin_path.read_bytes() == foreign


def test_uninstall_reconciliation_removes_only_departed_package_lsp(tmp_path) -> None:
    """Uninstall must revoke one package without touching surviving owners."""
    plugin_path = tmp_path / ".claude" / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json"
    plugin_path.parent.mkdir(parents=True)
    plugin_path.write_text(
        '{"name":"apm-lsp","lspServers":'
        '{"removed":{"command":"gone"},"root":{"command":"keep"},'
        '"bundle":{"command":"keep"}}}\n',
        encoding="ascii",
    )
    configs = {
        name: {"name": name, "command": command}
        for name, command in (
            ("removed", "gone"),
            ("root", "keep"),
            ("bundle", "keep"),
        )
    }
    lockfile = LockFile(
        lsp_servers=list(configs),
        lsp_configs=configs,
        lsp_config_provenance={
            "removed": "package:departed/package",
            "root": "project:.",
            "bundle": "bundle:local-bundle",
        },
    )
    DeploymentLedgerCodec.replace_lsp_target_servers(
        lockfile,
        {"claude": list(configs)},
    )
    package = MagicMock()
    package.get_lsp_dependencies.return_value = [_make_dep("root")]

    changed = reconcile_lsp_after_uninstall(
        apm_package=package,
        lockfile=lockfile,
        lock_path=tmp_path / "apm.lock.yaml",
        modules_dir=tmp_path / "apm_modules",
        project_root=tmp_path,
        user_scope=False,
        logger=_mock_logger(),
    )

    assert changed is True
    assert set(lockfile.lsp_servers) == {"root", "bundle"}
    assert lockfile.lsp_target_servers == {"claude": ["bundle", "root"]}
    assert set(json.loads(plugin_path.read_text())["lspServers"]) == {"root", "bundle"}


def test_uninstall_transfers_first_wins_lsp_to_surviving_package(tmp_path) -> None:
    """A same-name survivor must replace the departed declaration."""
    from apm_cli.deps.lockfile import LockedDependency
    from apm_cli.models.apm_package import APMPackage

    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "name: root\nversion: 1.0.0\ntargets: [claude]\n",
        encoding="ascii",
    )
    plugin_path = tmp_path / ".claude" / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json"
    plugin_path.parent.mkdir(parents=True)
    plugin_path.write_text(
        '{"name":"apm-lsp","lspServers":{"shared":{"command":"departed"}}}\n',
        encoding="ascii",
    )
    lockfile = LockFile(
        lsp_servers=["shared"],
        lsp_configs={"shared": {"name": "shared", "command": "departed"}},
        lsp_config_provenance={"shared": "package:departed/package"},
    )
    lockfile.add_dependency(LockedDependency(repo_url="surviving/package"))
    DeploymentLedgerCodec.replace_lsp_target_servers(
        lockfile,
        {"claude": ["shared"]},
    )
    survivor = _make_dep(
        "shared",
        command="survivor",
        resolved_by="surviving/package",
        approval_keys=("surviving/package",),
    )
    modules_dir = tmp_path / "apm_modules"
    modules_dir.mkdir()

    with patch(
        "apm_cli.integration.lsp_integrator.LSPIntegrator.collect_transitive",
        return_value=[survivor],
    ):
        changed = reconcile_lsp_after_uninstall(
            apm_package=APMPackage.from_apm_yml(manifest),
            lockfile=lockfile,
            lock_path=tmp_path / "apm.lock.yaml",
            modules_dir=modules_dir,
            project_root=tmp_path,
            user_scope=False,
            logger=_mock_logger(),
        )

    assert changed is True
    assert lockfile.lsp_config_provenance == {"shared": "package:surviving/package"}
    assert json.loads(plugin_path.read_text())["lspServers"]["shared"]["command"] == "survivor"


# ===========================================================================
# Stale cleanup
# ===========================================================================


class TestStaleCleanup:
    @patch(_PATCH_TARGET)
    def test_removes_stale_servers(self, mock_integrator, tmp_path):
        """Servers in old lockfile but not in new deps are removed."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = [_make_dep("pyright")]

        old_lock = _mock_lock(
            lsp_servers=["pyright", "old-server"],
            lsp_config_provenance={
                "pyright": "project:.",
                "old-server": "project:.",
            },
            lsp_target_servers={"claude": ["pyright", "old-server"]},
            target_servers_present=True,
        )

        mock_integrator.resolve_target_runtimes.return_value = ["claude"]
        mock_integrator.collect_transitive.return_value = []
        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"pyright"}
        mock_integrator.get_server_configs.return_value = {}

        modules = tmp_path / "apm_modules"
        modules.mkdir()

        run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=modules,
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=old_lock,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        mock_integrator.remove_stale.assert_called_once()
        stale_arg = mock_integrator.remove_stale.call_args
        assert "old-server" in stale_arg.args[0] or "old-server" in stale_arg[0][0]

    @patch(_PATCH_TARGET)
    def test_removes_all_old_when_no_deps_remain(self, mock_integrator, tmp_path):
        """When no LSP deps remain, all old servers are removed."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = []

        old_lock = _mock_lock(
            lsp_servers=["old-a", "old-b"],
            lsp_config_provenance={
                "old-a": "project:.",
                "old-b": "project:.",
            },
            lsp_target_servers={"claude": ["old-a", "old-b"]},
            target_servers_present=True,
        )
        mock_integrator.resolve_target_runtimes.return_value = ["claude"]

        run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=old_lock,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        mock_integrator.remove_stale.assert_called_once()
        stale_arg = mock_integrator.remove_stale.call_args[0][0]
        assert stale_arg == {"old-a", "old-b"}


# ===========================================================================
# --only=apm (should_install=False)
# ===========================================================================


class TestSkipInstall:
    @patch(_PATCH_TARGET)
    def test_restores_old_lockfile_when_not_installing(self, mock_integrator, tmp_path):
        """When should_install=False with old servers, lockfile is restored."""
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = []

        old_lock = _mock_lock(
            lsp_servers=["preserved"],
            lsp_configs={"preserved": {"name": "preserved"}},
        )

        run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=old_lock,
            project_root=tmp_path,
            user_scope=False,
            should_install=False,
            logger=_mock_logger(),
        )

        mock_integrator.update_lockfile.assert_called_once()
        mock_integrator.install.assert_not_called()

    @patch(_PATCH_TARGET)
    def test_legacy_lock_names_do_not_authorize_new_plugin_overwrite(
        self, mock_integrator, tmp_path
    ):
        """A pre-plugin lock name is not path-level ownership."""
        dependency = _make_dep("pyright")
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = [dependency]
        old_lock = _mock_lock(
            lsp_servers=["pyright"],
            lsp_configs={"pyright": {"command": "legacy"}},
        )
        mock_integrator.resolve_target_runtimes.return_value = ["claude"]
        mock_integrator.collect_transitive.return_value = []
        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"pyright"}
        mock_integrator.get_server_configs.return_value = {"pyright": {}}

        run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=old_lock,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        assert mock_integrator.install.call_args.kwargs["managed_target_servers"] == {}

    @patch(_PATCH_TARGET)
    def test_target_contraction_removes_only_recorded_old_target(self, mock_integrator, tmp_path):
        """Switching from Claude to Copilot revokes the old Claude entry."""
        dependency = _make_dep("pyright")
        apm_package = MagicMock()
        apm_package.get_lsp_dependencies.return_value = [dependency]
        old_lock = _mock_lock(
            lsp_servers=["pyright"],
            lsp_configs={"pyright": {}},
            lsp_config_provenance={"pyright": "project:."},
            lsp_target_servers={"claude": ["pyright"]},
            target_servers_present=True,
        )
        mock_integrator.resolve_target_runtimes.return_value = ["copilot"]
        mock_integrator.collect_transitive.return_value = []
        mock_integrator.install.return_value = 1
        mock_integrator.get_server_names.return_value = {"pyright"}
        mock_integrator.get_server_configs.return_value = {"pyright": {}}

        run_lsp_integration(
            apm_package=apm_package,
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=tmp_path / "apm.lock.yaml",
            existing_lock=old_lock,
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=_mock_logger(),
        )

        mock_integrator.remove_stale.assert_called_once_with(
            {"pyright"},
            project_root=tmp_path,
            user_scope=False,
            logger=mock_integrator.install.call_args.kwargs["logger"],
            target_runtimes=["claude"],
            fail_on_write_error=False,
        )
        assert mock_integrator.update_lockfile.call_args.kwargs["lsp_target_servers"] == {
            "claude": set(),
            "copilot": {"pyright"},
        }
