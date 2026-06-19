"""Tests for per-dep registry URL routing in run_mcp_install.

Verifies that when MCP deps carry a ``registry:`` string field, each unique
registry URL causes a separate ``MCPServerOperations`` instance to be
constructed with the correct ``registry_url`` kwarg.  Deps without a
``registry:`` field (None) must continue to use
``MCPServerOperations(registry_url=None)``.
"""

from unittest.mock import MagicMock, call, patch

from apm_cli.models.dependency.mcp import MCPDependency

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep(name: str, registry=None, **kwargs) -> MCPDependency:
    """Factory for registry-resolved MCPDependency (registry is not False)."""
    return MCPDependency(name=name, registry=registry, **kwargs)


def _make_operations_mock(server_names: list[str]) -> MagicMock:
    """Return a MCPServerOperations-like mock that validates and installs all servers."""
    ops = MagicMock()
    ops.validate_servers_exist.return_value = (server_names, [])
    ops.check_servers_needing_installation.return_value = server_names
    ops.batch_fetch_server_info.return_value = {n: {} for n in server_names}
    ops.collect_environment_variables.return_value = {}
    ops.collect_runtime_variables.return_value = {}
    return ops


# ---------------------------------------------------------------------------
# Shared patch targets
# ---------------------------------------------------------------------------

_OPS_PATH = "apm_cli.registry.operations.MCPServerOperations"
_INTEGRATOR_PATH = "apm_cli.integration.mcp_integrator.MCPIntegrator"


# ---------------------------------------------------------------------------
# test_per_dep_registry_creates_separate_operations
# ---------------------------------------------------------------------------


class TestPerDepRegistryCreatesSeparateOperations:
    """When deps have different registry: URLs, separate MCPServerOperations
    instances must be created -- one per distinct URL."""

    def test_two_custom_registries_create_two_instances(self):
        url_a = "https://registry-a.example.com"
        url_b = "https://registry-b.example.com"
        dep_a = _make_dep("server-a", registry=url_a)
        dep_b = _make_dep("server-b", registry=url_b)

        ops_mock_a = _make_operations_mock(["server-a"])
        ops_mock_b = _make_operations_mock(["server-b"])

        with (
            patch(_OPS_PATH, side_effect=[ops_mock_a, ops_mock_b]) as ops_cls,
            patch(_INTEGRATOR_PATH) as integrator_mock,
        ):
            integrator_mock._apply_overlay.return_value = None
            integrator_mock._detect_mcp_config_drift.return_value = []
            integrator_mock._append_drifted_to_install_list.return_value = None
            integrator_mock._install_for_runtime.return_value = True

            from apm_cli.integration.mcp_integrator_install import run_mcp_install

            run_mcp_install(
                mcp_deps=[dep_a, dep_b],
                runtime="copilot",
                logger=MagicMock(),
            )

        # Two distinct URLs -> two constructor calls
        assert ops_cls.call_count == 2
        called_urls = {c.kwargs.get("registry_url") for c in ops_cls.call_args_list}
        assert called_urls == {url_a, url_b}

    def test_single_custom_registry_creates_one_instance(self):
        url = "https://private.registry.internal"
        dep = _make_dep("my-server", registry=url)
        ops_mock = _make_operations_mock(["my-server"])

        with (
            patch(_OPS_PATH, return_value=ops_mock) as ops_cls,
            patch(_INTEGRATOR_PATH) as integrator_mock,
        ):
            integrator_mock._apply_overlay.return_value = None
            integrator_mock._detect_mcp_config_drift.return_value = []
            integrator_mock._append_drifted_to_install_list.return_value = None
            integrator_mock._install_for_runtime.return_value = True

            from apm_cli.integration.mcp_integrator_install import run_mcp_install

            run_mcp_install(
                mcp_deps=[dep],
                runtime="copilot",
                logger=MagicMock(),
            )

        assert ops_cls.call_count == 1
        assert ops_cls.call_args == call(registry_url=url)


# ---------------------------------------------------------------------------
# test_default_registry_deps_use_no_url
# ---------------------------------------------------------------------------


class TestDefaultRegistryDepsUseNoUrl:
    """Deps without a registry: field (or registry=None) must construct
    MCPServerOperations with registry_url=None."""

    def test_plain_object_dep_uses_none_url(self):
        dep = _make_dep("plain-server")  # registry=None by default
        assert dep.registry is None

        ops_mock = _make_operations_mock(["plain-server"])

        with (
            patch(_OPS_PATH, return_value=ops_mock) as ops_cls,
            patch(_INTEGRATOR_PATH) as integrator_mock,
        ):
            integrator_mock._apply_overlay.return_value = None
            integrator_mock._detect_mcp_config_drift.return_value = []
            integrator_mock._append_drifted_to_install_list.return_value = None
            integrator_mock._install_for_runtime.return_value = True

            from apm_cli.integration.mcp_integrator_install import run_mcp_install

            run_mcp_install(
                mcp_deps=[dep],
                runtime="copilot",
                logger=MagicMock(),
            )

        assert ops_cls.call_count == 1
        assert ops_cls.call_args == call(registry_url=None)

    def test_plain_string_dep_uses_none_url(self):
        """Plain string deps (backward-compat) must also land in the default group."""
        ops_mock = _make_operations_mock(["io.github.example/server"])

        with (
            patch(_OPS_PATH, return_value=ops_mock) as ops_cls,
            patch(_INTEGRATOR_PATH) as integrator_mock,
        ):
            integrator_mock._apply_overlay.return_value = None
            integrator_mock._detect_mcp_config_drift.return_value = []
            integrator_mock._append_drifted_to_install_list.return_value = None
            integrator_mock._install_for_runtime.return_value = True

            from apm_cli.integration.mcp_integrator_install import run_mcp_install

            run_mcp_install(
                mcp_deps=["io.github.example/server"],
                runtime="copilot",
                logger=MagicMock(),
            )

        assert ops_cls.call_count == 1
        assert ops_cls.call_args == call(registry_url=None)


# ---------------------------------------------------------------------------
# test_mixed_registry_deps_grouped_correctly
# ---------------------------------------------------------------------------


class TestMixedRegistryDepsGroupedCorrectly:
    """A mix of default-registry deps and custom-registry deps must be split
    into the correct groups."""

    def test_default_and_custom_groups_are_independent(self):
        custom_url = "https://custom.registry.example.com"

        dep_default_1 = _make_dep("default-server-1")
        dep_default_2 = _make_dep("default-server-2")
        dep_custom = _make_dep("custom-server", registry=custom_url)

        ops_default = _make_operations_mock(["default-server-1", "default-server-2"])
        ops_custom = _make_operations_mock(["custom-server"])

        constructed_urls = []

        def ops_factory(registry_url=None):
            constructed_urls.append(registry_url)
            if registry_url == custom_url:
                return ops_custom
            return ops_default

        with (
            patch(_OPS_PATH, side_effect=ops_factory),
            patch(_INTEGRATOR_PATH) as integrator_mock,
        ):
            integrator_mock._apply_overlay.return_value = None
            integrator_mock._detect_mcp_config_drift.return_value = []
            integrator_mock._append_drifted_to_install_list.return_value = None
            integrator_mock._install_for_runtime.return_value = True

            from apm_cli.integration.mcp_integrator_install import run_mcp_install

            run_mcp_install(
                mcp_deps=[dep_default_1, dep_default_2, dep_custom],
                runtime="copilot",
                logger=MagicMock(),
            )

        # Exactly two groups -> two MCPServerOperations instances
        assert len(constructed_urls) == 2
        assert None in constructed_urls
        assert custom_url in constructed_urls

        # Default group validated with both default servers
        default_validate_call = ops_default.validate_servers_exist.call_args
        assert set(default_validate_call[0][0]) == {"default-server-1", "default-server-2"}

        # Custom group validated with only the custom server
        custom_validate_call = ops_custom.validate_servers_exist.call_args
        assert custom_validate_call[0][0] == ["custom-server"]

    def test_false_registry_deps_are_excluded_from_registry_groups(self):
        """registry=False (self-defined) deps must NOT appear in any registry group."""
        dep_reg = _make_dep("registry-server")
        dep_self = MCPDependency(
            name="self-server",
            registry=False,
            transport="stdio",
            command="npx",
        )

        ops_mock = _make_operations_mock(["registry-server"])

        with (
            patch(_OPS_PATH, return_value=ops_mock) as ops_cls,
            patch(_INTEGRATOR_PATH) as integrator_mock,
        ):
            integrator_mock._apply_overlay.return_value = None
            integrator_mock._detect_mcp_config_drift.return_value = []
            integrator_mock._append_drifted_to_install_list.return_value = None
            integrator_mock._install_for_runtime.return_value = True
            integrator_mock._check_self_defined_servers_needing_installation.return_value = []
            integrator_mock._detect_runtimes.return_value = []
            integrator_mock._gate_project_scoped_runtimes.return_value = ["copilot"]

            from apm_cli.integration.mcp_integrator_install import run_mcp_install

            run_mcp_install(
                mcp_deps=[dep_reg, dep_self],
                runtime="copilot",
                logger=MagicMock(),
            )

        # Only one registry group (the default), self-defined dep is separate
        assert ops_cls.call_count == 1
        assert ops_cls.call_args == call(registry_url=None)
        # The registry group only sees registry-server
        validate_call = ops_mock.validate_servers_exist.call_args
        assert validate_call[0][0] == ["registry-server"]
