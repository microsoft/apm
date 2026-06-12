"""Unit tests for apm_cli.adapters.package_manager.base.MCPPackageManagerAdapter.

Covers:
- Direct instantiation of the ABC raises TypeError
- Concrete subclass implementing all methods can be instantiated
- Each abstract method works correctly when implemented
- Partial implementation (missing one or more methods) cannot be instantiated
"""

from __future__ import annotations

import pytest

from apm_cli.adapters.package_manager.base import MCPPackageManagerAdapter

# ---------------------------------------------------------------------------
# Helper concrete implementations
# ---------------------------------------------------------------------------


class FullAdapter(MCPPackageManagerAdapter):
    """Concrete adapter that implements every abstract method."""

    def install(self, package_name: str, version: str | None = None) -> str:
        return f"installed {package_name}=={version}"

    def uninstall(self, package_name: str) -> str:
        return f"uninstalled {package_name}"

    def list_installed(self) -> list[str]:
        return ["pkg-a", "pkg-b"]

    def search(self, query: str) -> list[str]:
        return [f"result-for-{query}"]


class MissingInstall(MCPPackageManagerAdapter):
    """Missing install() — cannot be instantiated."""

    def uninstall(self, package_name: str) -> None:
        pass

    def list_installed(self) -> list:
        return []

    def search(self, query: str) -> list:
        return []


class MissingSearch(MCPPackageManagerAdapter):
    """Missing search() — cannot be instantiated."""

    def install(self, package_name: str, version: str | None = None) -> None:
        pass

    def uninstall(self, package_name: str) -> None:
        pass

    def list_installed(self) -> list:
        return []


class MissingMultiple(MCPPackageManagerAdapter):
    """Missing both list_installed and search — cannot be instantiated."""

    def install(self, package_name: str, version: str | None = None) -> None:
        pass

    def uninstall(self, package_name: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMCPPackageManagerAdapterABC:
    """Tests for the MCPPackageManagerAdapter ABC contract."""

    # ------------------------------------------------------------------
    # Direct instantiation must fail
    # ------------------------------------------------------------------

    def test_cannot_instantiate_base_class_directly(self) -> None:
        """MCPPackageManagerAdapter is abstract and cannot be instantiated."""
        with pytest.raises(TypeError):
            MCPPackageManagerAdapter()  # type: ignore[abstract]

    # ------------------------------------------------------------------
    # Full concrete implementation
    # ------------------------------------------------------------------

    def test_full_implementation_can_be_instantiated(self) -> None:
        """A class implementing all abstract methods is instantiable."""
        adapter = FullAdapter()
        assert isinstance(adapter, MCPPackageManagerAdapter)

    def test_install_returns_expected_value(self) -> None:
        adapter = FullAdapter()
        result = adapter.install("my-package", version="1.0.0")
        assert result == "installed my-package==1.0.0"

    def test_install_without_version(self) -> None:
        adapter = FullAdapter()
        result = adapter.install("my-package")
        assert result == "installed my-package==None"

    def test_uninstall_returns_expected_value(self) -> None:
        adapter = FullAdapter()
        result = adapter.uninstall("my-package")
        assert result == "uninstalled my-package"

    def test_list_installed_returns_list(self) -> None:
        adapter = FullAdapter()
        result = adapter.list_installed()
        assert isinstance(result, list)
        assert "pkg-a" in result

    def test_search_returns_results(self) -> None:
        adapter = FullAdapter()
        result = adapter.search("foo")
        assert result == ["result-for-foo"]

    # ------------------------------------------------------------------
    # Partial implementations cannot be instantiated
    # ------------------------------------------------------------------

    def test_missing_install_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            MissingInstall()  # type: ignore[abstract]

    def test_missing_search_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            MissingSearch()  # type: ignore[abstract]

    def test_missing_multiple_methods_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            MissingMultiple()  # type: ignore[abstract]

    # ------------------------------------------------------------------
    # issubclass / isinstance checks
    # ------------------------------------------------------------------

    def test_full_adapter_is_subclass(self) -> None:
        assert issubclass(FullAdapter, MCPPackageManagerAdapter)

    def test_full_adapter_instance_is_instance_of_base(self) -> None:
        assert isinstance(FullAdapter(), MCPPackageManagerAdapter)


class TestAbstractMethodBodiesCoverage:
    """Call abstract method bodies via super() to reach the pass statements."""

    def test_super_install_body_reachable(self) -> None:
        """The abstract method body for install() can be called via super()."""

        class ConcreteAdapter(MCPPackageManagerAdapter):
            def install(self, package_name, version=None):
                return super().install(package_name, version)

            def uninstall(self, package_name):
                return super().uninstall(package_name)

            def list_installed(self):
                return super().list_installed()

            def search(self, query):
                return super().search(query)

        adapter = ConcreteAdapter()
        assert adapter.install("pkg") is None
        assert adapter.uninstall("pkg") is None
        assert adapter.list_installed() is None
        assert adapter.search("query") is None
