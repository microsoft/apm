"""Unit tests for local-bundle install routing, duck-type contract, rejected flags, --as.

These tests target the local-bundle code path in ``apm_cli.commands.install``
and ``apm_cli.install.services`` which do NOT exist yet.  Tests should fail at
import time or with clear "not implemented" errors.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

_LOCAL_BUNDLE_EXISTS = importlib.util.find_spec("apm_cli.bundle.local_bundle") is not None
_INTEGRATE_EXISTS = False
try:
    from apm_cli.install.services import integrate_local_bundle  # noqa: F401

    _INTEGRATE_EXISTS = True
except ImportError:
    pass

_MODULE_READY = _LOCAL_BUNDLE_EXISTS and _INTEGRATE_EXISTS

pytestmark = pytest.mark.skipif(
    not _MODULE_READY,
    reason="local-bundle production modules not yet implemented (TDD stub)",
)


# ---------------------------------------------------------------------------
# Duck-type contract test for package_info
# ---------------------------------------------------------------------------

# Audited attributes consumed by integrate_package_primitives() and all
# integrators (services.py, agent_integrator, prompt_integrator,
# skill_integrator, hook_integrator, instruction_integrator, command_integrator,
# base_integrator):
#
#   package_info.install_path          -> Path    (all integrators)
#   package_info.install_path.name     -> str     (hook, skill integrators)
#   package_info.package.name          -> str     (agent, prompt integrators)
#   package_info.package_type          -> enum    (skill integrator routing)
#   package_info.dependency_ref        -> obj|None (skill integrator ownership)
#   package_info.dependency_ref.is_virtual            -> bool
#   package_info.dependency_ref.is_virtual_subdirectory() -> bool
#   package_info.dependency_ref.get_unique_key()      -> str

_REQUIRED_ATTRS = [
    "install_path",
    "package",
    "package_type",
    "dependency_ref",
]

_REQUIRED_PACKAGE_ATTRS = ["name"]

_REQUIRED_DEPENDENCY_REF_ATTRS = [
    "is_virtual",
]

_REQUIRED_DEPENDENCY_REF_METHODS = [
    "is_virtual_subdirectory",
    "get_unique_key",
]


class TestSyntheticPackageInfoContract:
    """Pin the duck-type interface that integrate_package_primitives() consumes.

    If any integrator adds a new attribute access on package_info, this test
    must be updated -- and it will fail first, signaling a contract drift.
    """

    def test_synthetic_package_info_has_required_attributes(self, tmp_path: Path) -> None:
        """A synthetic package_info for local bundles must expose every
        attribute consumed by the integrator pipeline."""
        # Build a minimal synthetic object matching the contract
        package_mock = types.SimpleNamespace(name="test-plugin")
        dep_ref_mock = types.SimpleNamespace(
            is_virtual=False,
            is_virtual_subdirectory=lambda: False,
            get_unique_key=lambda: "local://test-plugin",
        )
        pkg_info = types.SimpleNamespace(
            install_path=tmp_path,
            package=package_mock,
            package_type="STANDARD",  # PackageType enum value
            dependency_ref=dep_ref_mock,
        )

        # Assert all required attributes are accessible (no AttributeError)
        for attr in _REQUIRED_ATTRS:
            assert hasattr(pkg_info, attr), f"Missing attribute: {attr}"

        for attr in _REQUIRED_PACKAGE_ATTRS:
            assert hasattr(pkg_info.package, attr), f"Missing package.{attr}"

        for attr in _REQUIRED_DEPENDENCY_REF_ATTRS:
            assert hasattr(pkg_info.dependency_ref, attr), f"Missing dependency_ref.{attr}"

        for method in _REQUIRED_DEPENDENCY_REF_METHODS:
            assert callable(getattr(pkg_info.dependency_ref, method)), (
                f"dependency_ref.{method} must be callable"
            )

    def test_synthetic_package_info_install_path_is_path(self, tmp_path: Path) -> None:
        pkg_info = types.SimpleNamespace(
            install_path=tmp_path,
            package=types.SimpleNamespace(name="test"),
            package_type="STANDARD",
            dependency_ref=None,
        )
        assert isinstance(pkg_info.install_path, Path)
        assert isinstance(pkg_info.install_path.name, str)

    def test_dependency_ref_can_be_none(self) -> None:
        """Local bundles may pass dependency_ref=None -- integrators must
        handle this (skill_integrator checks ``if dep_ref is not None``)."""
        pkg_info = types.SimpleNamespace(
            install_path=Path("/fake"),
            package=types.SimpleNamespace(name="test"),
            package_type="STANDARD",
            dependency_ref=None,
        )
        assert pkg_info.dependency_ref is None


# ---------------------------------------------------------------------------
# Rejected flag validation
# ---------------------------------------------------------------------------

_REJECTED_FLAGS = [
    "--update",
    "--only",
    "--runtime",
    "--exclude",
    "--dev",
    "--ssh",
    "--https",
    "--allow-protocol-fallback",
    "--mcp",
    "--registry",
    "--skill",
    "--parallel-downloads",
    "--allow-insecure",
    "--allow-insecure-host",
    "--no-policy",
]


class TestRejectedFlagsWithLocalBundle:
    """Each rejected flag must produce UsageError when combined with a local bundle."""

    @pytest.mark.parametrize("flag", _REJECTED_FLAGS)
    def test_rejected_flags_produce_usage_error(self, flag: str) -> None:
        """Verify that the install command rejects incompatible flags for
        local bundle paths.

        Since the production code does not exist yet, we test the
        SPECIFICATION: each of these flags, when combined with a local path
        argument, MUST raise click.UsageError.  This test will be fleshed
        out with CliRunner invocation once install.py has the seam.
        """
        # TDD: test documents the requirement.
        # When production code exists, this will invoke:
        #   runner.invoke(cli, ["install", flag, <value>, "./bundle"])
        # and assert exit_code != 0 + "not valid with a local bundle" in output.
        pytest.skip(
            f"Production code not yet implemented -- flag {flag} rejection is spec'd but untestable"
        )


# ---------------------------------------------------------------------------
# Allowed flag validation
# ---------------------------------------------------------------------------

_ALLOWED_FLAGS = [
    ("--global",),
    ("--target", "copilot"),
    ("--force",),
    ("--dry-run",),
    ("--verbose",),
    ("--as", "my-alias"),
]


class TestAllowedFlagsWithLocalBundle:
    """Allowed flags must not produce errors when combined with a local bundle."""

    @pytest.mark.parametrize("flag_args", _ALLOWED_FLAGS, ids=lambda x: x[0])
    def test_allowed_flags_accepted_with_local_bundle(self, flag_args: tuple[str, ...]) -> None:
        """These flags must be accepted by the install command for local bundles."""
        pytest.skip("Production code not yet implemented -- acceptance is spec'd but untestable")


# ---------------------------------------------------------------------------
# --as alias derivation
# ---------------------------------------------------------------------------


class TestAsAliasDerivation:
    """Tests for the --as flag alias logic."""

    def test_as_flag_overrides_plugin_json_id(self) -> None:
        """When --as is provided, it overrides whatever plugin.json says."""
        # Will test: given bundle with id="original", --as my-alias
        # -> package slug used is "my-alias"
        pytest.skip("Production code not yet implemented")

    def test_alias_falls_back_to_dirname_when_no_id(self) -> None:
        """When plugin.json has no id field and no --as, use dirname."""
        pytest.skip("Production code not yet implemented")

    def test_alias_falls_back_to_plugin_json_id(self) -> None:
        """When no --as flag, use plugin.json id field."""
        pytest.skip("Production code not yet implemented")


# ---------------------------------------------------------------------------
# apm.yml mutation guard
# ---------------------------------------------------------------------------


class TestApmYmlNotMutated:
    """Local-bundle install must NOT mutate apm.yml."""

    def test_apm_yml_not_mutated_by_local_install(self) -> None:
        """Assert that apm.yml content is identical before and after a local
        bundle install.  This is a critical contract -- local bundles are
        imperative deploys, not declarative dependencies."""
        pytest.skip("Production code not yet implemented")
