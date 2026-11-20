"""Tests for transitive MCP dependency collection and deduplication."""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import MCPDependency

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _FakeResolvedTargets:
    """Lightweight stand-in for ResolvedTargets used in unit tests."""

    targets: list
    source: str = "--target flag"
    auto_create: bool = True


@pytest.fixture(autouse=False)
def _bypass_target_gate():
    """Mock resolve_targets so tests don't need harness marker files on disk."""
    fake = _FakeResolvedTargets(targets=["copilot"])
    with patch("apm_cli.core.target_detection.resolve_targets", return_value=fake):
        yield


@pytest.fixture
def _isolated_targets():
    """Decouple MCPIntegrator.install tests from cwd-based harness
    detection. PR #1336 made the install gate call resolve_targets(cwd)
    and fail closed with NoHarnessError when cwd lacks harness markers.
    Under pytest-xdist worksteal scheduling, sibling tests can leave a
    worker in a tmp cwd, so any test that relies on the install path
    reaching _install_for_runtime must stub the resolver."""
    with patch("apm_cli.core.target_detection.resolve_targets") as mock_resolve:
        mock_resolve.return_value = MagicMock(targets=["copilot"])
        yield mock_resolve


# ---------------------------------------------------------------------------
# APMPackage - MCP dict parsing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _collect_transitive_mcp_deps
# ---------------------------------------------------------------------------


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


# Name-only "already configured" characterisation for adapters that opt out of
# native content comparison (compares_native_connections = False).
_NAME_ONLY_RUNTIMES = patch(
    "apm_cli.install.mcp.ownership.native_comparison_clients", new=lambda *_a, **_k: {}
)


# ---------------------------------------------------------------------------
# _install_mcp_dependencies
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_bypass_target_gate")
class TestInstallMCPDependencies:
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.registry.operations.MCPServerOperations")
    def test_already_configured_registry_servers_not_counted_as_new(self, mock_ops_cls, _console):
        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["ghcr.io/org/server"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        count = MCPIntegrator.install(["ghcr.io/org/server"], runtime="copilot")

        assert count == 0

    @_NAME_ONLY_RUNTIMES
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.registry.operations.MCPServerOperations")
    def test_counts_only_newly_configured_registry_servers(
        self, mock_ops_cls, _console, mock_install_runtime, _isolated_targets
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

        count = MCPIntegrator.install(["ghcr.io/org/already", "ghcr.io/org/new"], runtime="copilot")

        assert count == 1
        mock_install_runtime.assert_called_once()

    @_NAME_ONLY_RUNTIMES
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.registry.operations.MCPServerOperations")
    def test_mixed_registry_servers_show_already_configured_and_count_only_new(
        self, mock_ops_cls, mock_install_runtime, _isolated_targets
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
                ["ghcr.io/org/already", "ghcr.io/org/new"], runtime="copilot"
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
# _install_mcp_dependencies - self-defined skip logic
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_bypass_target_gate")
class TestInstallSelfDefinedSkipLogic:
    @_NAME_ONLY_RUNTIMES
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
        count = MCPIntegrator.install([dep], runtime="copilot")

        assert count == 0
        mock_install_runtime.assert_not_called()

    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    def test_new_self_defined_server_installed(
        self, _console, mock_install_runtime, mock_check, _isolated_targets
    ):
        """Self-defined servers NOT already configured should be installed."""
        mock_check.return_value = ["atlassian"]
        mock_install_runtime.return_value = True

        dep = MCPDependency(
            name="atlassian",
            transport="http",
            url="https://atlassian.example.com",
            registry=False,
        )
        count = MCPIntegrator.install([dep], runtime="copilot")

        assert count == 1
        assert mock_install_runtime.call_count == 1

    @_NAME_ONLY_RUNTIMES
    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    def test_mixed_self_defined_shows_already_configured(
        self, mock_install_runtime, mock_check, _isolated_targets
    ):
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
            count = MCPIntegrator.install(deps, runtime="copilot")

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


# ---------------------------------------------------------------------------
# Diff-aware install — self-defined servers with config drift
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_bypass_target_gate")
class TestDiffAwareSelfDefinedInstall:
    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    def test_config_drift_triggers_reinstall(
        self, _console, mock_install_runtime, mock_check, _isolated_targets
    ):
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
            runtime="copilot",
            stored_mcp_configs=stored_configs,
        )

        assert count == 1
        mock_install_runtime.assert_called_once()

    @_NAME_ONLY_RUNTIMES
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
            runtime="copilot",
            stored_mcp_configs=stored_configs,
        )

        assert count == 0
        mock_install_runtime.assert_not_called()

    @patch(
        "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation"
    )
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    def test_drift_shows_updated_label(self, mock_install_runtime, mock_check, _isolated_targets):
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
                runtime="copilot",
                stored_mcp_configs=stored_configs,
            )

        assert count == 1
        printed_lines = "\n".join(
            str(call.args[0]) for call in mock_console.print.call_args_list if call.args
        )
        assert "updated" in printed_lines

    @_NAME_ONLY_RUNTIMES
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
        _isolated_targets,
    ):
        """Without stored configs (first install), behavior unchanged."""
        mock_check.return_value = []

        dep = MCPDependency(
            name="internal-kb",
            transport="http",
            url="https://kb.example.com",
            registry=False,
        )

        count = MCPIntegrator.install([dep], runtime="copilot")

        assert count == 0
        mock_install_runtime.assert_not_called()
        mock_rich_success.assert_called_once()
        assert "already configured" in mock_rich_success.call_args.args[0]


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


class TestMCPUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "https://a.example\\@api.githubcopilot.com/mcp/",
            "https://a.example /mcp",
            "https://a\tb/",
        ],
    )
    def test_url_with_parser_dependent_characters_is_rejected(self, url):
        with pytest.raises(ValueError, match="Invalid MCP url"):
            MCPDependency.from_dict(
                {"name": "srv", "registry": False, "transport": "http", "url": url}
            )
