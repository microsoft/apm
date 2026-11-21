"""Tests for transitive MCP dependency collection and deduplication (filesystem, process and CLI boundary cases)."""

from unittest.mock import MagicMock, patch

import pytest
import toml
import yaml

from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import APMPackage, MCPDependency

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise(
        "P-OWN-mcp-passthrough-denylist", "P-OWN-apm-package-manifest-construction"
    ),
]


# ---------------------------------------------------------------------------
# APMPackage - MCP dict parsing
# ---------------------------------------------------------------------------
class TestAPMPackageMCPParsing:
    """Ensure apm_package preserves both string and dict MCP entries."""

    def test_parse_string_mcp_deps(self, tmp_path):
        """String-only MCP deps parse correctly."""
        yml = tmp_path / "apm.yml"
        yml.write_text(
            yaml.dump(
                {
                    "name": "pkg",
                    "version": "1.0.0",
                    "dependencies": {"mcp": ["ghcr.io/some/server"]},
                }
            )
        )
        pkg = APMPackage.from_apm_yml(yml)
        deps = pkg.get_mcp_dependencies()

        assert len(deps) == 1
        assert isinstance(deps[0], MCPDependency)
        assert deps[0].name == "ghcr.io/some/server"
        assert deps[0].is_registry_resolved

    def test_parse_dict_mcp_deps(self, tmp_path):
        """Inline dict MCP deps are preserved."""
        inline = {"name": "my-srv", "type": "sse", "url": "https://example.com"}
        yml = tmp_path / "apm.yml"
        yml.write_text(
            yaml.dump(
                {
                    "name": "pkg",
                    "version": "1.0.0",
                    "dependencies": {"mcp": [inline]},
                }
            )
        )
        pkg = APMPackage.from_apm_yml(yml)
        deps = pkg.get_mcp_dependencies()

        assert len(deps) == 1
        assert isinstance(deps[0], MCPDependency)
        assert deps[0].name == "my-srv"
        assert deps[0].transport == "sse"  # legacy 'type' mapped to 'transport'

    def test_parse_mixed_mcp_deps(self, tmp_path):
        """A mix of string and dict entries is preserved in order."""
        inline = {"name": "inline-srv", "type": "http", "url": "https://x"}
        yml = tmp_path / "apm.yml"
        yml.write_text(
            yaml.dump(
                {
                    "name": "pkg",
                    "version": "1.0.0",
                    "dependencies": {"mcp": ["registry-srv", inline]},
                }
            )
        )
        pkg = APMPackage.from_apm_yml(yml)
        deps = pkg.get_mcp_dependencies()

        assert len(deps) == 2
        assert isinstance(deps[0], MCPDependency)
        assert deps[0].name == "registry-srv"
        assert isinstance(deps[1], MCPDependency)
        assert deps[1].name == "inline-srv"

    def test_no_mcp_section(self, tmp_path):
        """Missing MCP section returns empty list."""
        yml = tmp_path / "apm.yml"
        yml.write_text(
            yaml.dump(
                {
                    "name": "pkg",
                    "version": "1.0.0",
                }
            )
        )
        pkg = APMPackage.from_apm_yml(yml)
        assert pkg.get_mcp_dependencies() == []

    def test_mcp_null_returns_empty(self, tmp_path):
        """mcp: null should return empty list, not raise TypeError."""
        yml = tmp_path / "apm.yml"
        yml.write_text(
            yaml.dump(
                {
                    "name": "pkg",
                    "version": "1.0.0",
                    "dependencies": {"mcp": None},
                }
            )
        )
        pkg = APMPackage.from_apm_yml(yml)
        assert pkg.get_mcp_dependencies() == []

    def test_mcp_empty_list_returns_empty(self, tmp_path):
        """mcp: [] should return empty list."""
        yml = tmp_path / "apm.yml"
        yml.write_text(
            yaml.dump(
                {
                    "name": "pkg",
                    "version": "1.0.0",
                    "dependencies": {"mcp": []},
                }
            )
        )
        pkg = APMPackage.from_apm_yml(yml)
        assert pkg.get_mcp_dependencies() == []


# ---------------------------------------------------------------------------
# _collect_transitive_mcp_deps
# ---------------------------------------------------------------------------
class TestCollectTransitiveMCPDeps:
    """Tests for scanning apm_modules/ for MCP deps."""

    def test_empty_when_dir_missing(self, tmp_path):
        result = MCPIntegrator.collect_transitive(tmp_path / "nonexistent")
        assert result == []

    def test_collects_string_deps(self, tmp_path):
        pkg_dir = tmp_path / "org" / "pkg-a"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "pkg-a",
                    "version": "1.0.0",
                    "dependencies": {"mcp": ["ghcr.io/a/server"]},
                }
            )
        )
        result = MCPIntegrator.collect_transitive(tmp_path)
        assert len(result) == 1
        assert isinstance(result[0], MCPDependency)
        assert result[0].name == "ghcr.io/a/server"

    def test_collects_dict_deps(self, tmp_path):
        inline = {"name": "kb", "transport": "sse", "version": "1.0.0"}
        pkg_dir = tmp_path / "org" / "pkg-b"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "pkg-b",
                    "version": "1.0.0",
                    "dependencies": {"mcp": [inline]},
                }
            )
        )
        result = MCPIntegrator.collect_transitive(tmp_path)
        assert len(result) == 1
        assert isinstance(result[0], MCPDependency)
        assert result[0].name == "kb"

    def test_collects_from_multiple_packages(self, tmp_path):
        for i, dep in enumerate(["ghcr.io/a/s1", "ghcr.io/b/s2"]):
            d = tmp_path / "org" / f"pkg-{i}"
            d.mkdir(parents=True)
            (d / "apm.yml").write_text(
                yaml.dump(
                    {
                        "name": f"pkg-{i}",
                        "version": "1.0.0",
                        "dependencies": {"mcp": [dep]},
                    }
                )
            )
        result = MCPIntegrator.collect_transitive(tmp_path)
        assert len(result) == 2

    def test_skips_unparseable_apm_yml(self, tmp_path):
        pkg_dir = tmp_path / "org" / "bad-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("invalid: yaml: [")
        # Should not raise
        result = MCPIntegrator.collect_transitive(tmp_path)
        assert result == []

    def test_lockfile_scopes_collection_to_locked_packages(self, tmp_path):
        """Lock-file filtering should only collect MCP deps from locked packages."""
        apm_modules = tmp_path / "apm_modules"
        # Package that IS in the lock file
        locked_dir = apm_modules / "org" / "locked-pkg"
        locked_dir.mkdir(parents=True)
        (locked_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "locked-pkg",
                    "version": "1.0.0",
                    "dependencies": {"mcp": ["ghcr.io/locked/server"]},
                }
            )
        )
        # Package that is NOT in the lock file (orphan)
        orphan_dir = apm_modules / "org" / "orphan-pkg"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "orphan-pkg",
                    "version": "1.0.0",
                    "dependencies": {"mcp": ["ghcr.io/orphan/server"]},
                }
            )
        )
        # Write lock file referencing only the locked package
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            yaml.dump(
                {
                    "lockfile_version": "1",
                    "dependencies": [
                        {"repo_url": "org/locked-pkg", "host": "github.com"},
                    ],
                }
            )
        )
        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        assert len(result) == 1
        assert isinstance(result[0], MCPDependency)
        assert result[0].name == "ghcr.io/locked/server"

    def test_lockfile_with_virtual_path(self, tmp_path):
        """Lock-file filtering works for subdirectory (virtual_path) packages."""
        apm_modules = tmp_path / "apm_modules"
        # Subdirectory package matching lock entry
        sub_dir = apm_modules / "org" / "monorepo" / "skills" / "azure"
        sub_dir.mkdir(parents=True)
        (sub_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "azure-skill",
                    "version": "1.0.0",
                    "dependencies": {
                        "mcp": [
                            {"name": "learn", "type": "http", "url": "https://learn.example.com"}
                        ]
                    },
                }
            )
        )
        # Another subdirectory NOT in the lock
        other_dir = apm_modules / "org" / "monorepo" / "skills" / "other"
        other_dir.mkdir(parents=True)
        (other_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "other-skill",
                    "version": "1.0.0",
                    "dependencies": {"mcp": ["ghcr.io/other/server"]},
                }
            )
        )
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            yaml.dump(
                {
                    "lockfile_version": "1",
                    "dependencies": [
                        {
                            "repo_url": "org/monorepo",
                            "host": "github.com",
                            "virtual_path": "skills/azure",
                            "is_virtual": True,
                        },
                    ],
                }
            )
        )
        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        assert len(result) == 1
        assert isinstance(result[0], MCPDependency)
        assert result[0].name == "learn"

    def test_lockfile_paths_do_not_use_full_rglob_scan(self, tmp_path):
        """When lock-derived paths are available, avoid full recursive scanning."""
        apm_modules = tmp_path / "apm_modules"
        locked_dir = apm_modules / "org" / "locked-pkg"
        locked_dir.mkdir(parents=True)
        (locked_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "locked-pkg",
                    "version": "1.0.0",
                    "dependencies": {"mcp": ["ghcr.io/locked/server"]},
                }
            )
        )

        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            yaml.dump(
                {
                    "lockfile_version": "1",
                    "dependencies": [
                        {"repo_url": "org/locked-pkg", "host": "github.com"},
                    ],
                }
            )
        )

        with patch("pathlib.Path.rglob", side_effect=AssertionError("rglob should not be called")):
            result = MCPIntegrator.collect_transitive(apm_modules, lock_path)

        assert len(result) == 1
        assert result[0].name == "ghcr.io/locked/server"

    def test_invalid_lockfile_fails_closed(self, tmp_path):
        """A malformed lock graph must not broaden discovery to every package."""
        from apm_cli.deps.lockfile import LockfileFormatError

        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "org" / "pkg-a"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "pkg-a",
                    "version": "1.0.0",
                    "dependencies": {"mcp": ["ghcr.io/a/server"]},
                }
            )
        )

        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text("dependencies: [")

        with pytest.raises(LockfileFormatError):
            MCPIntegrator.collect_transitive(apm_modules, lock_path)

    def test_skips_self_defined_by_default(self, tmp_path):
        """Self-defined servers from transitive packages are skipped without the flag."""
        pkg_dir = tmp_path / "org" / "pkg-a"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "pkg-a",
                    "version": "1.0.0",
                    "dependencies": {
                        "mcp": [
                            "ghcr.io/registry/server",
                            {
                                "name": "private-srv",
                                "registry": False,
                                "transport": "http",
                                "url": "https://private.example.com",
                            },
                        ]
                    },
                }
            )
        )
        result = MCPIntegrator.collect_transitive(tmp_path)
        assert len(result) == 1
        assert result[0].name == "ghcr.io/registry/server"

    def test_trust_private_includes_self_defined(self, tmp_path):
        """With trust_private=True, self-defined servers are collected."""
        pkg_dir = tmp_path / "org" / "pkg-a"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "pkg-a",
                    "version": "1.0.0",
                    "dependencies": {
                        "mcp": [
                            "ghcr.io/registry/server",
                            {
                                "name": "private-srv",
                                "registry": False,
                                "transport": "http",
                                "url": "https://private.example.com",
                            },
                        ]
                    },
                }
            )
        )
        result = MCPIntegrator.collect_transitive(tmp_path, trust_private=True)
        assert len(result) == 2
        names = [d.name for d in result]
        assert "ghcr.io/registry/server" in names
        assert "private-srv" in names

    def test_trust_private_false_is_default_behavior(self, tmp_path):
        """Explicitly passing trust_private=False behaves same as default."""
        pkg_dir = tmp_path / "org" / "pkg-a"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "pkg-a",
                    "version": "1.0.0",
                    "dependencies": {
                        "mcp": [
                            {
                                "name": "private-srv",
                                "registry": False,
                                "transport": "http",
                                "url": "https://private.example.com",
                            },
                        ]
                    },
                }
            )
        )
        result = MCPIntegrator.collect_transitive(tmp_path, trust_private=False)
        assert len(result) == 0

    def test_direct_dep_self_defined_auto_trusted(self, tmp_path):
        """Depth=1 package with self-defined MCP → collected without flag."""
        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "org" / "direct-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "direct-pkg",
                    "version": "1.0.0",
                    "dependencies": {
                        "mcp": [
                            {
                                "name": "private-srv",
                                "registry": False,
                                "transport": "http",
                                "url": "https://private.example.com",
                            },
                        ]
                    },
                }
            )
        )
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            yaml.dump(
                {
                    "lockfile_version": "1",
                    "dependencies": [
                        {"repo_url": "org/direct-pkg", "host": "github.com", "depth": 1},
                    ],
                }
            )
        )
        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        assert len(result) == 1
        assert result[0].name == "private-srv"

    def test_transitive_dep_self_defined_still_skipped(self, tmp_path):
        """Depth=2 package with self-defined MCP → skipped without flag."""
        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "org" / "transitive-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "transitive-pkg",
                    "version": "1.0.0",
                    "dependencies": {
                        "mcp": [
                            {
                                "name": "private-srv",
                                "registry": False,
                                "transport": "http",
                                "url": "https://private.example.com",
                            },
                        ]
                    },
                }
            )
        )
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            yaml.dump(
                {
                    "lockfile_version": "1",
                    "dependencies": [
                        {"repo_url": "org/transitive-pkg", "host": "github.com", "depth": 2},
                    ],
                }
            )
        )
        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        assert len(result) == 0

    def test_transitive_dep_trusted_with_flag(self, tmp_path):
        """Depth=2 + trust_private=True → collected."""
        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "org" / "transitive-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "transitive-pkg",
                    "version": "1.0.0",
                    "dependencies": {
                        "mcp": [
                            {
                                "name": "private-srv",
                                "registry": False,
                                "transport": "http",
                                "url": "https://private.example.com",
                            },
                        ]
                    },
                }
            )
        )
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            yaml.dump(
                {
                    "lockfile_version": "1",
                    "dependencies": [
                        {"repo_url": "org/transitive-pkg", "host": "github.com", "depth": 2},
                    ],
                }
            )
        )
        result = MCPIntegrator.collect_transitive(apm_modules, lock_path, trust_private=True)
        assert len(result) == 1
        assert result[0].name == "private-srv"

    def test_no_lockfile_conservative(self, tmp_path):
        """Missing lockfile: depth is unknown, so even a would-be direct dep is not trusted."""
        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "org" / "direct-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "direct-pkg",
                    "version": "1.0.0",
                    "dependencies": {
                        "mcp": [
                            "ghcr.io/registry/server",
                            {
                                "name": "local-srv",
                                "registry": False,
                                "transport": "stdio",
                                "command": "local-mcp",
                            },
                        ]
                    },
                }
            )
        )
        lock_path = tmp_path / "apm.lock.yaml"
        assert not lock_path.exists()

        unlocked = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        assert [d.name for d in unlocked] == ["ghcr.io/registry/server"]

        lock_path.write_text(
            yaml.dump(
                {
                    "lockfile_version": "1",
                    "dependencies": [
                        {"repo_url": "org/direct-pkg", "host": "github.com", "depth": 1},
                    ],
                }
            )
        )
        locked = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        assert sorted(d.name for d in locked) == ["ghcr.io/registry/server", "local-srv"]


# ---------------------------------------------------------------------------
# _deduplicate_mcp_deps
# ---------------------------------------------------------------------------


# Name-only "already configured" characterisation for adapters that opt out of
# native content comparison (compares_native_connections = False).


# ---------------------------------------------------------------------------
# _install_mcp_dependencies
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _check_self_defined_servers_needing_installation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _install_mcp_dependencies - self-defined skip logic
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _detect_mcp_config_drift
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_server_configs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Diff-aware install — self-defined servers with config drift
# ---------------------------------------------------------------------------


class TestCodexProjectScopedMCP:
    """Tests for project-local Codex MCP activation and cleanup."""

    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.factory.ClientFactory.create_client")
    @patch("apm_cli.runtime.manager.RuntimeManager")
    def test_codex_skipped_when_not_active_project_target(
        self,
        mock_manager_cls,
        mock_create_client,
        mock_ops_cls,
        mock_install_runtime,
        _vscode,
        _console,
        tmp_path,
    ):
        """Installed Codex should not receive MCP config unless the project targets Codex."""
        mock_manager = mock_manager_cls.return_value
        mock_manager.is_runtime_available.side_effect = lambda runtime: runtime == "codex"
        mock_create_client.return_value = MagicMock()

        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["ghcr.io/org/new"], [])
        mock_ops.check_servers_needing_installation.return_value = ["ghcr.io/org/new"]
        mock_ops.batch_fetch_server_info.return_value = {"ghcr.io/org/new": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        count = MCPIntegrator.install(
            ["ghcr.io/org/new"],
            project_root=tmp_path,
            apm_config={},
        )

        assert count == 0
        mock_install_runtime.assert_not_called()

    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.factory.ClientFactory.create_client")
    @patch("apm_cli.runtime.manager.RuntimeManager")
    def test_codex_installed_when_explicit_project_target(
        self,
        mock_manager_cls,
        mock_create_client,
        mock_ops_cls,
        mock_install_runtime,
        _vscode,
        _console,
        tmp_path,
    ):
        """Explicit Codex targeting should install MCP config into the project scope."""
        mock_manager = mock_manager_cls.return_value
        mock_manager.is_runtime_available.side_effect = lambda runtime: runtime == "codex"
        mock_create_client.return_value = MagicMock()
        mock_install_runtime.return_value = True

        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["ghcr.io/org/new"], [])
        mock_ops.check_servers_needing_installation.return_value = ["ghcr.io/org/new"]
        mock_ops.batch_fetch_server_info.return_value = {"ghcr.io/org/new": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        count = MCPIntegrator.install(
            ["ghcr.io/org/new"],
            project_root=tmp_path,
            apm_config={"target": "codex"},
            explicit_target="codex",
        )

        assert count == 1
        mock_install_runtime.assert_called_once()
        assert mock_install_runtime.call_args.kwargs["project_root"] == tmp_path
        assert mock_install_runtime.call_args.kwargs["user_scope"] is False

    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.factory.ClientFactory.create_client")
    @patch("apm_cli.runtime.manager.RuntimeManager")
    def test_explicit_codex_runtime_overrides_missing_project_signal(
        self,
        mock_manager_cls,
        mock_create_client,
        mock_ops_cls,
        mock_install_runtime,
        _vscode,
        _console,
        tmp_path,
    ):
        """Explicit runtime selection outranks absent project target signals."""
        mock_manager = mock_manager_cls.return_value
        mock_manager.is_runtime_available.side_effect = lambda runtime: runtime == "codex"
        mock_create_client.return_value = MagicMock()

        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["ghcr.io/org/new"], [])
        mock_ops.check_servers_needing_installation.return_value = ["ghcr.io/org/new"]
        mock_ops.batch_fetch_server_info.return_value = {"ghcr.io/org/new": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        count = MCPIntegrator.install(
            ["ghcr.io/org/new"],
            runtime="codex",
            project_root=tmp_path,
            apm_config={},
        )

        assert count == 1
        mock_install_runtime.assert_called_once()

    @patch("apm_cli.core.null_logger._rich_info")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.factory.ClientFactory.create_client")
    @patch("apm_cli.runtime.manager.RuntimeManager")
    def test_explicit_codex_runtime_does_not_emit_inactive_target_skip(
        self,
        mock_manager_cls,
        mock_create_client,
        mock_ops_cls,
        mock_install_runtime,
        _vscode,
        _console,
        mock_info,
        tmp_path,
    ):
        """Explicit runtime selection must not emit an inactive-target skip."""
        mock_manager = mock_manager_cls.return_value
        mock_manager.is_runtime_available.side_effect = lambda runtime: runtime == "codex"
        mock_create_client.return_value = MagicMock()

        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["ghcr.io/org/new"], [])
        mock_ops.check_servers_needing_installation.return_value = ["ghcr.io/org/new"]
        mock_ops.batch_fetch_server_info.return_value = {"ghcr.io/org/new": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        count = MCPIntegrator.install(
            ["ghcr.io/org/new"],
            runtime="codex",
            project_root=tmp_path,
            apm_config={},
        )

        assert count == 1
        mock_install_runtime.assert_called_once()
        for call in mock_info.call_args_list:
            assert "Codex not an active project target" not in str(call)

    def test_remove_stale_codex_uses_project_config(self, tmp_path):
        """Stale cleanup should edit the project .codex/config.toml."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config_path = codex_dir / "config.toml"
        config_path.write_text(
            toml.dumps(
                {
                    "mcp_servers": {
                        "keep": {"command": "npx"},
                        "stale": {"command": "npx", "args": [], "env": {}},
                    }
                }
            ),
            encoding="utf-8",
        )

        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="codex",
            project_root=tmp_path,
            stored_configs={
                "stale": {
                    "name": "stale",
                    "registry": False,
                    "transport": "stdio",
                    "command": "npx",
                }
            },
        )

        data = toml.loads(config_path.read_text(encoding="utf-8"))
        assert "keep" in data["mcp_servers"]
        assert "stale" not in data["mcp_servers"]


class TestTransitiveTrustClassification:
    """A transitive declaration is trusted only as a plain registry reference."""

    @pytest.mark.parametrize(
        "entry",
        [
            "io.github.acme/server",
            {"name": "io.github.acme/server"},
            {"name": "io.github.acme/server", "version": "1.0.0", "transport": "stdio"},
            {"name": "io.github.acme/server", "package": "npm", "tools": ["read"]},
        ],
    )
    def test_plain_registry_reference_needs_no_trust(self, entry):
        dep = (
            MCPDependency.from_string(entry)
            if isinstance(entry, str)
            else MCPDependency.from_dict(entry)
        )
        assert dep.requires_transitive_trust is False

    @pytest.mark.parametrize(
        "entry",
        [
            {"name": "srv", "registry": False, "transport": "stdio", "command": "srv"},
            {"name": "srv", "registry": "https://registry.example.com"},
            {"name": "srv", "headers": {"X-Key": "${SOME_VAR}"}},
            {"name": "srv", "env": {"LEVEL": "debug"}},
            {"name": "srv", "args": {"flag": "value"}},
            {"name": "srv", "url": "https://srv.example.com/mcp"},
        ],
    )
    def test_custom_source_or_overlay_needs_trust(self, entry):
        assert MCPDependency.from_dict(entry).requires_transitive_trust is True

    def test_compat_collection_skips_overlay_without_trust(self, tmp_path):
        pkg_dir = tmp_path / "org" / "pkg-overlay"
        pkg_dir.mkdir(parents=True)
        entry = {"name": "srv", "registry": "https://registry.example.com", "headers": {"X": "y"}}
        (pkg_dir / "apm.yml").write_text(
            yaml.dump({"name": "pkg-overlay", "version": "1.0.0", "dependencies": {"mcp": [entry]}})
        )
        assert MCPIntegrator.collect_transitive(tmp_path) == []
        trusted = MCPIntegrator.collect_transitive(tmp_path, trust_private=True)
        assert [dep.name for dep in trusted] == ["srv"]
