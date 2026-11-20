"""Tests for the apm install command auto-bootstrap feature (pure logic cases)."""

import types
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Transitive dep parent chain breadcrumb
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Global scope (--global / -g) tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Generic-host SSH-first validation tests
# ---------------------------------------------------------------------------


class TestInsecureDependencyWarnings:
    """Tests for install-time insecure dependency warnings."""

    def test_collect_insecure_dependency_infos_marks_direct_dependency(self):
        """Direct HTTP dependencies are collected without parent provenance."""
        from apm_cli.commands.install import (
            _collect_insecure_dependency_infos,
            _InsecureDependencyInfo,
        )
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("http://mirror.example.com/acme/rules")
        tree = MagicMock()
        tree.get_node.return_value = None
        graph = MagicMock(dependency_tree=tree)

        infos = _collect_insecure_dependency_infos([dep], graph)

        assert infos == [
            _InsecureDependencyInfo(
                url="http://mirror.example.com/acme/rules",
                is_transitive=False,
                introduced_by=None,
            )
        ]

    def test_collect_insecure_dependency_infos_marks_transitive_dependency(self):
        """Transitive HTTP dependencies carry introducer information."""
        from apm_cli.commands.install import _collect_insecure_dependency_infos
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("http://mirror.example.com/acme/transitive")
        parent_ref = DependencyReference.parse("owner/root-package")
        parent_node = types.SimpleNamespace(dependency_ref=parent_ref)
        node = types.SimpleNamespace(parent=parent_node)
        tree = MagicMock()
        tree.get_node.return_value = node
        graph = MagicMock(dependency_tree=tree)

        infos = _collect_insecure_dependency_infos([dep], graph)

        assert len(infos) == 1
        assert infos[0].url == "http://mirror.example.com/acme/transitive"
        assert infos[0].is_transitive is True
        assert infos[0].introduced_by == "owner/root-package"

    def test_format_insecure_dependency_warning_for_transitive_dep(self):
        """Transitive warning strings include the introducer."""
        from apm_cli.commands.install import (
            _format_insecure_dependency_warning,
            _InsecureDependencyInfo,
        )

        message = _format_insecure_dependency_warning(
            _InsecureDependencyInfo(
                url="http://mirror.example.com/acme/transitive",
                is_transitive=True,
                introduced_by="owner/root-package",
            )
        )

        assert "Insecure HTTP fetch (unencrypted)" in message
        assert "http://mirror.example.com/acme/transitive" in message
        assert "transitive, introduced by owner/root-package" in message


class TestTransitiveInsecureDependencyGuard:
    """Tests for host-based consent around transitive insecure dependencies."""

    def test_transitive_guard_blocks_unapproved_host(self):
        """Transitive insecure deps are blocked when their host is not approved."""
        from apm_cli.commands.install import (
            _guard_transitive_insecure_dependencies,
            _InsecureDependencyInfo,
        )
        from apm_cli.install.insecure_policy import InsecureDependencyPolicyError

        logger = MagicMock()

        with pytest.raises(InsecureDependencyPolicyError) as exc_info:
            _guard_transitive_insecure_dependencies(
                [
                    _InsecureDependencyInfo(
                        url="http://mirror.example.com/acme/transitive",
                        is_transitive=True,
                        introduced_by="owner/root-package",
                    )
                ],
                logger,
                allow_insecure=False,
                allow_insecure_hosts=(),
            )

        message = str(exc_info.value)
        assert message.startswith("Re-run with --allow-insecure-host mirror.example.com")
        assert "unapproved host(s): mirror.example.com" in message

    def test_transitive_guard_allows_same_host_as_direct_insecure_dependency(self):
        """A direct insecure dependency host also permits transitive deps on that host."""
        from apm_cli.commands.install import (
            _guard_transitive_insecure_dependencies,
            _InsecureDependencyInfo,
        )

        logger = MagicMock()

        _guard_transitive_insecure_dependencies(
            [
                _InsecureDependencyInfo(
                    url="http://mirror.example.com/acme/direct",
                    is_transitive=False,
                    introduced_by=None,
                ),
                _InsecureDependencyInfo(
                    url="http://mirror.example.com/acme/transitive",
                    is_transitive=True,
                    introduced_by="owner/root-package",
                ),
            ],
            logger,
            allow_insecure=True,
            allow_insecure_hosts=(),
        )

    def test_transitive_guard_accepts_explicit_host(self):
        """Explicitly allowed hosts permit transitive insecure dependencies."""
        from apm_cli.commands.install import (
            _guard_transitive_insecure_dependencies,
            _InsecureDependencyInfo,
        )

        logger = MagicMock()

        _guard_transitive_insecure_dependencies(
            [
                _InsecureDependencyInfo(
                    url="http://mirror.example.com/acme/transitive",
                    is_transitive=True,
                    introduced_by="owner/root-package",
                )
            ],
            logger,
            allow_insecure=False,
            allow_insecure_hosts=("mirror.example.com",),
        )

    def test_transitive_guard_blocks_different_host_without_explicit_allowance(self):
        """A direct insecure host does not permit transitive deps on other hosts."""
        from apm_cli.commands.install import (
            _guard_transitive_insecure_dependencies,
            _InsecureDependencyInfo,
        )
        from apm_cli.install.insecure_policy import InsecureDependencyPolicyError

        logger = MagicMock()

        with pytest.raises(InsecureDependencyPolicyError) as exc_info:
            _guard_transitive_insecure_dependencies(
                [
                    _InsecureDependencyInfo(
                        url="http://my-server.example.com/acme/direct",
                        is_transitive=False,
                        introduced_by=None,
                    ),
                    _InsecureDependencyInfo(
                        url="http://mirror.example.com/acme/transitive",
                        is_transitive=True,
                        introduced_by="owner/root-package",
                    ),
                ],
                logger,
                allow_insecure=True,
                allow_insecure_hosts=(),
            )

        message = str(exc_info.value)
        assert message.startswith("Re-run with --allow-insecure-host mirror.example.com")
        assert "unapproved host(s): mirror.example.com" in message


# ---------------------------------------------------------------------------
# `apm install --mcp NAME ...` flag tests (W3 T-install)
# ---------------------------------------------------------------------------
