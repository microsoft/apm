"""Tests for transitive MCP dependency collection and deduplication."""

from unittest.mock import MagicMock, patch

import toml
import yaml

from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import APMPackage, MCPDependency


# ---------------------------------------------------------------------------
# APMPackage – MCP dict parsing
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
        inline = {"name": "kb", "type": "sse", "url": "https://kb.example.com"}
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

    def test_invalid_lockfile_falls_back_to_rglob_scan(self, tmp_path):
        """If lock parsing fails, function falls back to scanning all apm.yml files."""
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

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        assert len(result) == 1
        assert result[0].name == "ghcr.io/a/server"

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
        """No lockfile → all self-defined skipped (conservative)."""
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
        # No lock_path provided
        result = MCPIntegrator.collect_transitive(tmp_path)
        assert len(result) == 1
        assert result[0].name == "ghcr.io/registry/server"


# ---------------------------------------------------------------------------
# _deduplicate_mcp_deps
# ---------------------------------------------------------------------------
class TestDeduplicateMCPDeps:
    def test_deduplicates_strings(self):
        deps = ["a", "b", "a", "c", "b"]
        assert MCPIntegrator.deduplicate(deps) == ["a", "b", "c"]

    def test_deduplicates_dicts_by_name(self):
        d1 = {"name": "srv", "type": "sse", "url": "https://one"}
        d2 = {"name": "srv", "type": "sse", "url": "https://two"}  # same name
        d3 = {"name": "other", "type": "sse", "url": "https://three"}
        result = MCPIntegrator.deduplicate([d1, d2, d3])
        assert len(result) == 2
        assert result[0]["url"] == "https://one"  # first wins

    def test_mixed_dedup(self):
        inline = {"name": "kb", "type": "sse", "url": "https://kb"}
        deps = ["a", inline, "a", {"name": "kb", "type": "sse", "url": "https://kb2"}]
        result = MCPIntegrator.deduplicate(deps)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], dict)

    def test_empty_list(self):
        assert MCPIntegrator.deduplicate([]) == []

    def test_dict_without_name_kept(self):
        """Dicts without 'name' are kept if not already in result."""
        d = {"type": "sse", "url": "https://x"}
        result = MCPIntegrator.deduplicate([d, d])
        assert len(result) == 1

    def test_root_deps_take_precedence_over_transitive(self):
        """When root and transitive share a key, the first (root) wins."""
        root = [{"name": "shared", "type": "sse", "url": "https://root-url"}]
        transitive = [{"name": "shared", "type": "sse", "url": "https://transitive-url"}]
        # Root deps come first in the combined list
        combined = root + transitive
        result = MCPIntegrator.deduplicate(combined)
        assert len(result) == 1
        assert result[0]["url"] == "https://root-url"


# ---------------------------------------------------------------------------
# _install_mcp_dependencies
# ---------------------------------------------------------------------------
class TestInstallMCPDependencies:
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.registry.operations.MCPServerOperations")
    def test_already_configured_registry_servers_not_counted_as_new(self, mock_ops_cls, _console):
        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["ghcr.io/org/server"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        count = MCPIntegrator.install(["ghcr.io/org/server"], runtime="vscode")

        assert count == 0

    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.registry.operations.MCPServerOperations")
    def test_counts_only_newly_configured_registry_servers(
        self, mock_ops_cls, _console, mock_install_runtime
    ):
        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (
            ["ghcr.io/org/already", "ghcr.io/org/new"],
            [],
        )
        mock_ops.check_servers_needing_installation.return_value = ["ghcr.io/org/new"]
        mock_ops.batch_fetch_server_info.return_value = {"ghcr.io/org/new": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        count = MCPIntegrator.install(["ghcr.io/org/already", "ghcr.io/org/new"], runtime="vscode")

        assert count == 1
        mock_install_runtime.assert_called_once()

    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.registry.operations.MCPServerOperations")
    def test_mixed_registry_servers_show_already_configured_and_count_only_new(
        self, mock_ops_cls, mock_install_runtime
    ):
        mock_console = MagicMock()
        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (
            ["ghcr.io/org/already", "ghcr.io/org/new"],
            [],
        )
        mock_ops.check_servers_needing_installation.return_value = ["ghcr.io/org/new"]
        mock_ops.batch_fetch_server_info.return_value = {"ghcr.io/org/new": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        with patch("apm_cli.integration.mcp_integrator._get_console", return_value=mock_console):
            count = MCPIntegrator.install(
                ["ghcr.io/org/already", "ghcr.io/org/new"], runtime="vscode"
            )

        assert count == 1
        mock_install_runtime.assert_called_once()
        printed_lines = "\n".join(
            str(call.args[0]) for call in mock_console.print.call_args_list if call.args
        )
        assert "ghcr.io/org/already" in printed_lines
        assert "already configured" in printed_lines


# ---------------------------------------------------------------------------
# _check_self_defined_servers_needing_installation
# ---------------------------------------------------------------------------
class TestCheckSelfDefinedServersNeeding:
    @patch("apm_cli.core.conflict_detector.MCPConflictDetector")
    @patch("apm_cli.factory.ClientFactory")
    def test_all_servers_need_installation_when_none_configured(
        self, mock_factory_cls, mock_detector_cls
    ):
        """All servers need installation when config is empty."""
        mock_client = MagicMock()
        mock_factory_cls.create_client.return_value = mock_client
        mock_detector = MagicMock()
        mock_detector.get_existing_server_configs.return_value = {}
        mock_detector_cls.return_value = mock_detector

        result = MCPIntegrator._check_self_defined_servers_needing_installation(
            ["atlassian", "zephyr"], ["copilot", "vscode"]
        )
        assert sorted(result) == ["atlassian", "zephyr"]

    @patch("apm_cli.core.conflict_detector.MCPConflictDetector")
    @patch("apm_cli.factory.ClientFactory")
    def test_no_servers_need_installation_when_all_configured(
        self, mock_factory_cls, mock_detector_cls
    ):
        """No servers need installation when all are present in all runtimes."""
        mock_client = MagicMock()
        mock_factory_cls.create_client.return_value = mock_client
        mock_detector = MagicMock()
        mock_detector.get_existing_server_configs.return_value = {
            "atlassian": {"type": "http"},
            "zephyr": {"type": "http"},
        }
        mock_detector_cls.return_value = mock_detector

        result = MCPIntegrator._check_self_defined_servers_needing_installation(
            ["atlassian", "zephyr"], ["copilot", "vscode"]
        )
        assert result == []

    @patch("apm_cli.core.conflict_detector.MCPConflictDetector")
    @patch("apm_cli.factory.ClientFactory")
    def test_server_needs_installation_when_missing_in_one_runtime(
        self, mock_factory_cls, mock_detector_cls
    ):
        """Server needs install if missing from at least one target runtime."""
        mock_client = MagicMock()
        mock_factory_cls.create_client.return_value = mock_client

        # First runtime has it, second does not
        copilot_config = {"atlassian": {"type": "http"}}
        vscode_config = {}

        mock_detector = MagicMock()
        mock_detector.get_existing_server_configs.side_effect = [
            copilot_config,
            vscode_config,
        ]
        mock_detector_cls.return_value = mock_detector

        result = MCPIntegrator._check_self_defined_servers_needing_installation(
            ["atlassian"], ["copilot", "vscode"]
        )
        assert result == ["atlassian"]

    @patch("apm_cli.factory.ClientFactory")
    def test_config_read_failure_assumes_needs_installation(self, mock_factory_cls):
        """If config read fails, assume server needs installation."""
        mock_factory_cls.create_client.side_effect = Exception("config error")

        result = MCPIntegrator._check_self_defined_servers_needing_installation(
            ["atlassian"], ["copilot"]
        )
        assert result == ["atlassian"]

    def test_empty_runtimes_returns_empty(self):
        """With no target runtimes, no server is found missing."""
        result = MCPIntegrator._check_self_defined_servers_needing_installation(["a", "b"], [])
        # With no runtimes to check, no server is found missing → none need install
        assert result == []

    @patch("apm_cli.core.conflict_detector.MCPConflictDetector")
    @patch("apm_cli.factory.ClientFactory")
    def test_reads_each_runtime_config_once_for_multiple_servers(
        self, mock_factory_cls, mock_detector_cls
    ):
        """Runtime config reads are cached instead of repeated per server."""
        mock_factory_cls.create_client.side_effect = [MagicMock(), MagicMock()]

        mock_copilot_detector = MagicMock()
        mock_copilot_detector.get_existing_server_configs.return_value = {}
        mock_vscode_detector = MagicMock()
        mock_vscode_detector.get_existing_server_configs.return_value = {}
        mock_detector_cls.side_effect = [mock_copilot_detector, mock_vscode_detector]

        result = MCPIntegrator._check_self_defined_servers_needing_installation(
            ["atlassian", "zephyr"], ["copilot", "vscode"]
        )

        assert sorted(result) == ["atlassian", "zephyr"]
        assert [call.args[0] for call in mock_factory_cls.create_client.call_args_list] == [
            "copilot",
            "vscode",
        ]
        assert mock_copilot_detector.get_existing_server_configs.call_count == 1
        assert mock_vscode_detector.get_existing_server_configs.call_count == 1


# ---------------------------------------------------------------------------
# _install_mcp_dependencies – self-defined skip logic
# ---------------------------------------------------------------------------
class TestInstallSelfDefinedSkipLogic:
    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    def test_already_configured_self_defined_servers_skipped(
        self,
        _console,
        mock_install_runtime,
        mock_check,
    ):
        """Self-defined servers already configured should not trigger _install_for_runtime."""
        mock_check.return_value = []  # none need installation

        dep = MCPDependency(
            name="atlassian",
            transport="http",
            url="https://atlassian.example.com",
            registry=False,
        )
        count = MCPIntegrator.install([dep], runtime="vscode")

        assert count == 0
        mock_install_runtime.assert_not_called()

    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    def test_new_self_defined_server_installed(self, _console, mock_install_runtime, mock_check):
        """Self-defined servers NOT already configured should be installed."""
        mock_check.return_value = ["atlassian"]
        mock_install_runtime.return_value = True

        dep = MCPDependency(
            name="atlassian",
            transport="http",
            url="https://atlassian.example.com",
            registry=False,
        )
        count = MCPIntegrator.install([dep], runtime="vscode")

        assert count == 1
        assert mock_install_runtime.call_count == 1

    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    def test_mixed_self_defined_shows_already_configured(self, mock_install_runtime, mock_check):
        """Mix of new and existing self-defined servers: only new ones installed, existing shown as configured."""
        mock_check.return_value = ["new-srv"]
        mock_install_runtime.return_value = True
        mock_console = MagicMock()

        deps = [
            MCPDependency(
                name="existing-srv",
                transport="http",
                url="https://existing.example.com",
                registry=False,
            ),
            MCPDependency(
                name="new-srv",
                transport="http",
                url="https://new.example.com",
                registry=False,
            ),
        ]

        with patch(
            "apm_cli.integration.mcp_integrator._get_console",
            return_value=mock_console,
        ):
            count = MCPIntegrator.install(deps, runtime="vscode")

        assert count == 1
        assert mock_install_runtime.call_count == 1

        printed_lines = "\n".join(
            str(call.args[0]) for call in mock_console.print.call_args_list if call.args
        )
        assert "existing-srv" in printed_lines
        assert "already configured" in printed_lines


# ---------------------------------------------------------------------------
# _detect_mcp_config_drift
# ---------------------------------------------------------------------------
class TestDetectMCPConfigDrift:
    """Tests for config drift detection between manifest and lockfile."""

    def test_no_drift_when_configs_match(self):
        """No drift when manifest config matches stored config."""
        dep = MCPDependency(name="github", transport="stdio")
        stored = {"github": {"name": "github", "transport": "stdio"}}
        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert result == set()

    def test_drift_detected_when_env_changes(self):
        """Drift detected when env vars change."""
        dep = MCPDependency(name="github", transport="stdio", env={"TOKEN": "new-value"})
        stored = {"github": {"name": "github", "transport": "stdio", "env": {"TOKEN": "old-value"}}}
        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert result == {"github"}

    def test_drift_detected_when_url_changes(self):
        """Drift detected when URL changes for self-defined server."""
        dep = MCPDependency(
            name="internal-kb",
            registry=False,
            transport="http",
            url="https://new-kb.example.com",
        )
        stored = {
            "internal-kb": {
                "name": "internal-kb",
                "registry": False,
                "transport": "http",
                "url": "https://old-kb.example.com",
            }
        }
        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert result == {"internal-kb"}

    def test_drift_detected_when_transport_changes(self):
        """Drift detected when transport type changes."""
        dep = MCPDependency(name="github", transport="stdio")
        stored = {"github": {"name": "github", "transport": "sse"}}
        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert result == {"github"}

    def test_drift_detected_when_args_change(self):
        """Drift detected when args change."""
        dep = MCPDependency(name="github", transport="stdio", args=["--new-flag"])
        stored = {"github": {"name": "github", "transport": "stdio", "args": ["--old-flag"]}}
        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert result == {"github"}

    def test_drift_detected_when_tools_change(self):
        """Drift detected when tools list changes."""
        dep = MCPDependency(name="github", tools=["repos", "issues"])
        stored = {"github": {"name": "github", "tools": ["repos"]}}
        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert result == {"github"}

    def test_no_drift_when_server_not_in_stored(self):
        """No drift when server has no stored baseline."""
        dep = MCPDependency(name="new-server", transport="stdio")
        stored = {"other-server": {"name": "other-server"}}
        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert result == set()

    def test_no_drift_with_empty_stored_configs(self):
        """No drift when stored configs are empty (first install)."""
        dep = MCPDependency(name="github", transport="stdio")
        result = MCPIntegrator._detect_mcp_config_drift([dep], {})
        assert result == set()

    def test_multiple_deps_mixed_drift(self):
        """Only drifted deps are returned in a mixed set."""
        deps = [
            MCPDependency(name="unchanged", transport="stdio"),
            MCPDependency(name="changed", transport="http", url="https://new.example.com"),
        ]
        stored = {
            "unchanged": {"name": "unchanged", "transport": "stdio"},
            "changed": {
                "name": "changed",
                "transport": "http",
                "url": "https://old.example.com",
            },
        }
        result = MCPIntegrator._detect_mcp_config_drift(deps, stored)
        assert result == {"changed"}

    def test_no_drift_when_registry_field_matches(self):
        """No drift when registry field (True/None) is the same."""
        dep = MCPDependency(name="github")
        stored = {"github": {"name": "github"}}
        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert result == set()

    def test_drift_when_headers_added(self):
        """Drift detected when headers are added to existing server."""
        dep = MCPDependency(name="github", headers={"Authorization": "Bearer token"})
        stored = {"github": {"name": "github"}}
        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert result == {"github"}

    def test_plain_strings_are_skipped(self):
        """Plain string deps (no to_dict) are ignored by drift detection."""
        result = MCPIntegrator._detect_mcp_config_drift(
            ["ghcr.io/org/server"], {"ghcr.io/org/server": {"name": "ghcr.io/org/server"}}
        )
        assert result == set()


# ---------------------------------------------------------------------------
# get_server_configs
# ---------------------------------------------------------------------------
class TestGetServerConfigs:
    """Tests for extracting server configs from MCP dependencies."""

    def test_extracts_configs_from_mcp_deps(self):
        deps = [
            MCPDependency(name="github", transport="stdio"),
            MCPDependency(
                name="internal-kb",
                registry=False,
                transport="http",
                url="https://kb.example.com",
            ),
        ]
        configs = MCPIntegrator.get_server_configs(deps)
        assert configs == {
            "github": {"name": "github", "transport": "stdio"},
            "internal-kb": {
                "name": "internal-kb",
                "registry": False,
                "transport": "http",
                "url": "https://kb.example.com",
            },
        }

    def test_extracts_configs_from_plain_strings(self):
        configs = MCPIntegrator.get_server_configs(["ghcr.io/org/server"])
        assert configs == {"ghcr.io/org/server": {"name": "ghcr.io/org/server"}}

    def test_empty_list(self):
        assert MCPIntegrator.get_server_configs([]) == {}


# ---------------------------------------------------------------------------
# Diff-aware install — self-defined servers with config drift
# ---------------------------------------------------------------------------
class TestDiffAwareSelfDefinedInstall:
    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    def test_config_drift_triggers_reinstall(self, _console, mock_install_runtime, mock_check):
        """Self-defined server with config drift should be re-installed."""
        # Server is already configured (check returns empty)
        mock_check.return_value = []
        mock_install_runtime.return_value = True

        dep = MCPDependency(
            name="internal-kb",
            transport="http",
            url="https://new-kb.example.com",
            registry=False,
        )
        stored_configs = {
            "internal-kb": {
                "name": "internal-kb",
                "transport": "http",
                "url": "https://old-kb.example.com",
                "registry": False,
            }
        }

        count = MCPIntegrator.install(
            [dep],
            runtime="vscode",
            stored_mcp_configs=stored_configs,
        )

        assert count == 1
        mock_install_runtime.assert_called_once()

    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    def test_no_drift_keeps_skip(self, _console, mock_install_runtime, mock_check):
        """Self-defined server with no config drift should still be skipped."""
        mock_check.return_value = []

        dep = MCPDependency(
            name="internal-kb",
            transport="http",
            url="https://kb.example.com",
            registry=False,
        )
        stored_configs = {
            "internal-kb": {
                "name": "internal-kb",
                "transport": "http",
                "url": "https://kb.example.com",
                "registry": False,
            }
        }

        count = MCPIntegrator.install(
            [dep],
            runtime="vscode",
            stored_mcp_configs=stored_configs,
        )

        assert count == 0
        mock_install_runtime.assert_not_called()

    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    def test_drift_shows_updated_label(self, mock_install_runtime, mock_check):
        """Config-drifted server should show 'updated' in CLI output."""
        mock_check.return_value = []
        mock_install_runtime.return_value = True
        mock_console = MagicMock()

        dep = MCPDependency(
            name="internal-kb",
            transport="http",
            url="https://new-kb.example.com",
            registry=False,
        )
        stored_configs = {
            "internal-kb": {
                "name": "internal-kb",
                "transport": "http",
                "url": "https://old-kb.example.com",
                "registry": False,
            }
        }

        with patch(
            "apm_cli.integration.mcp_integrator._get_console",
            return_value=mock_console,
        ):
            count = MCPIntegrator.install(
                [dep],
                runtime="vscode",
                stored_mcp_configs=stored_configs,
            )

        assert count == 1
        printed_lines = "\n".join(
            str(call.args[0]) for call in mock_console.print.call_args_list if call.args
        )
        assert "updated" in printed_lines

    @patch("apm_cli.core.null_logger._rich_success")
    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    def test_no_stored_configs_preserves_existing_behavior(
        self,
        _console,
        mock_install_runtime,
        mock_check,
        mock_rich_success,
    ):
        """Without stored configs (first install), behavior unchanged."""
        mock_check.return_value = []

        dep = MCPDependency(
            name="internal-kb",
            transport="http",
            url="https://kb.example.com",
            registry=False,
        )

        count = MCPIntegrator.install([dep], runtime="vscode")

        assert count == 0
        mock_install_runtime.assert_not_called()
        mock_rich_success.assert_called_once()
        assert "already configured" in mock_rich_success.call_args.args[0]


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
    def test_explicit_codex_runtime_still_requires_active_project_target(
        self,
        mock_manager_cls,
        mock_create_client,
        mock_ops_cls,
        mock_install_runtime,
        _vscode,
        _console,
        tmp_path,
    ):
        """Explicit runtime selection should not bypass Codex project gating."""
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

        assert count == 0
        mock_install_runtime.assert_not_called()

    @patch("apm_cli.core.null_logger._rich_info")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.factory.ClientFactory.create_client")
    @patch("apm_cli.runtime.manager.RuntimeManager")
    def test_codex_gating_reports_why_it_was_skipped(
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
        """Codex gating should tell the user why project MCP config was skipped."""
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

        assert count == 0
        mock_install_runtime.assert_not_called()
        mock_info.assert_any_call(
            "Codex not an active project target -- skipping MCP config "
            "(create .codex/ or set target: codex in apm.yml)",
            symbol="info",
        )

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
                        "stale": {"command": "npx"},
                    }
                }
            ),
            encoding="utf-8",
        )

        MCPIntegrator.remove_stale(
            {"stale"},
            runtime="codex",
            project_root=tmp_path,
        )

        data = toml.loads(config_path.read_text(encoding="utf-8"))
        assert "keep" in data["mcp_servers"]
        assert "stale" not in data["mcp_servers"]
