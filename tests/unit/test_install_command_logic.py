"""Tests for the apm install command auto-bootstrap feature (pure logic cases)."""

import os
import types
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Transitive dep parent chain breadcrumb
# ---------------------------------------------------------------------------


class TestTransitiveDepParentChain:
    """Tests for DependencyNode.get_ancestor_chain() breadcrumb."""

    def test_get_ancestor_chain_returns_breadcrumb(self):
        """get_ancestor_chain walks up parent links and returns 'a > b > c'."""
        from apm_cli.deps.dependency_graph import DependencyNode
        from apm_cli.models.apm_package import APMPackage, DependencyReference

        root_ref = DependencyReference.parse("acme/root-pkg")
        mid_ref = DependencyReference.parse("acme/mid-pkg")
        leaf_ref = DependencyReference.parse("other-org/leaf-pkg")

        root_node = DependencyNode(
            package=APMPackage(name="root-pkg", version="1.0", source="acme/root-pkg"),
            dependency_ref=root_ref,
            depth=1,
        )
        mid_node = DependencyNode(
            package=APMPackage(name="mid-pkg", version="1.0", source="acme/mid-pkg"),
            dependency_ref=mid_ref,
            depth=2,
            parent=root_node,
        )
        leaf_node = DependencyNode(
            package=APMPackage(name="leaf-pkg", version="1.0", source="other-org/leaf-pkg"),
            dependency_ref=leaf_ref,
            depth=3,
            parent=mid_node,
        )

        chain = leaf_node.get_ancestor_chain()
        assert chain == "acme/root-pkg > acme/mid-pkg > other-org/leaf-pkg"

    def test_get_ancestor_chain_single_node(self):
        """Direct dep (no parent) returns just its own name."""
        from apm_cli.deps.dependency_graph import DependencyNode
        from apm_cli.models.apm_package import APMPackage, DependencyReference

        ref = DependencyReference.parse("acme/direct-pkg")
        node = DependencyNode(
            package=APMPackage(name="direct-pkg", version="1.0", source="acme/direct-pkg"),
            dependency_ref=ref,
            depth=1,
        )
        chain = node.get_ancestor_chain()
        assert chain == "acme/direct-pkg"

    def test_get_ancestor_chain_root_node(self):
        """Root node (no parent) returns just the node's display name."""
        from apm_cli.deps.dependency_graph import DependencyNode
        from apm_cli.models.apm_package import APMPackage, DependencyReference

        ref = DependencyReference.parse("acme/root-pkg")
        node = DependencyNode(
            package=APMPackage(name="root-pkg", version="1.0", source="acme/root-pkg"),
            dependency_ref=ref,
            depth=0,
        )
        assert node.get_ancestor_chain() == "acme/root-pkg"


class TestDownloadCallbackErrorMessages:
    """Tests for direct vs transitive dep error message differentiation."""

    def test_transitive_dep_key_not_in_direct_dep_keys(self):
        """Transitive dep keys are correctly absent from direct_dep_keys set.

        The download_callback uses this check to select the right error label.
        End-to-end transitive error flow is covered by
        TestTransitiveDepParentChain.test_download_callback_includes_chain_in_error.
        """
        from apm_cli.models.apm_package import DependencyReference

        direct_dep_keys = {"acme/root-pkg"}
        transitive_ref = DependencyReference.parse("other-org/leaf-pkg")

        # Transitive deps must NOT be in the direct set
        assert transitive_ref.get_unique_key() not in direct_dep_keys
        # Direct deps must be in the direct set
        assert "acme/root-pkg" in direct_dep_keys


class TestLocalPathValidationMessages:
    """Tests for improved local path validation error messages."""

    def test_local_path_failure_reason_remote_ref(self):
        """Remote refs return None (not a local path)."""
        from apm_cli.commands.install import _local_path_failure_reason
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference.parse("owner/repo")
        reason = _local_path_failure_reason(dep_ref)
        assert reason is None


# ---------------------------------------------------------------------------
# Global scope (--global / -g) tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Generic-host SSH-first validation tests
# ---------------------------------------------------------------------------


class TestGenericHostSshFirstValidation:
    """Tests for the SSH-first ls-remote logic added for generic (non-GitHub/ADO) hosts."""

    def _make_completed_process(self, returncode, stderr=""):
        """Return a minimal subprocess.CompletedProcess-like mock."""
        mock = MagicMock()
        mock.returncode = returncode
        mock.stderr = stderr
        mock.stdout = ""
        return mock

    @patch("subprocess.run")
    def test_generic_host_tries_ssh_first_and_succeeds(self, mock_run):
        """SSH URL is tried first for generic hosts and used when it succeeds."""
        from apm_cli.commands.install import _validate_package_exists

        # SSH probe succeeds on the first call
        mock_run.return_value = self._make_completed_process(returncode=0)

        result = _validate_package_exists("git@git.example.org:org/group/repo.git", verbose=False)

        assert result is True
        # subprocess.run must have been called at least once
        assert mock_run.call_count >= 1
        # First call must use the SSH URL
        first_call_cmd = mock_run.call_args_list[0][0][0]
        assert any("git@git.example.org" in arg for arg in first_call_cmd), (
            f"Expected SSH URL in first ls-remote call, got: {first_call_cmd}"
        )

    @patch("subprocess.run")
    def test_explicit_ssh_url_does_not_fall_back_to_https(self, mock_run):
        """Strict-by-default (issue #992): explicit SSH URLs must NOT silently
        fall back to HTTPS, mirroring ``_clone_with_fallback`` semantics. The
        legacy permissive chain stays available behind
        ``APM_ALLOW_PROTOCOL_FALLBACK=1``."""
        from apm_cli.commands.install import _validate_package_exists

        # SSH probe fails; previously this would have silently retried HTTPS.
        mock_run.return_value = self._make_completed_process(
            returncode=128, stderr="ssh: connect to host"
        )

        result = _validate_package_exists("git@git.example.org:org/group/repo.git", verbose=False)

        assert result is False
        assert mock_run.call_count == 1, (
            f"explicit ssh:// must be strict; got {[c[0][0] for c in mock_run.call_args_list]!r}"
        )
        first_cmd = mock_run.call_args_list[0][0][0]
        assert any("git@git.example.org:" in arg for arg in first_cmd), (
            f"Expected SSH URL in only call, got: {first_cmd}"
        )

    @patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": "1"})
    @patch("subprocess.run")
    def test_explicit_ssh_falls_back_to_https_with_allow_fallback_env(self, mock_run):
        """Legacy permissive chain restored when the env opt-in is set."""
        from urllib.parse import urlsplit

        from apm_cli.commands.install import _validate_package_exists

        mock_run.side_effect = [
            self._make_completed_process(returncode=128, stderr="ssh: connect to host"),
            self._make_completed_process(returncode=0),
        ]

        result = _validate_package_exists("git@git.example.org:org/group/repo.git", verbose=False)

        assert result is True
        assert mock_run.call_count == 2
        first_cmd = mock_run.call_args_list[0][0][0]
        assert any("git@git.example.org:" in arg for arg in first_cmd)
        second_cmd = mock_run.call_args_list[1][0][0]
        https_arg_found = any(
            urlsplit(arg).scheme == "https" and urlsplit(arg).netloc == "git.example.org"
            for arg in second_cmd
        )
        assert https_arg_found, (
            f"Expected https://git.example.org URL in second call, got: {second_cmd}"
        )

    @patch("subprocess.run")
    def test_generic_host_returns_false_when_explicit_ssh_fails(self, mock_run):
        """Strict mode: a single failed SSH probe is the only attempt."""
        from apm_cli.commands.install import _validate_package_exists

        mock_run.return_value = self._make_completed_process(
            returncode=128, stderr="fatal: could not read Username"
        )

        result = _validate_package_exists("git@git.example.org:org/group/repo.git", verbose=False)

        assert result is False
        assert mock_run.call_count == 1  # strict: SSH only, no HTTPS retry

    @patch("subprocess.run")
    def test_explicit_http_generic_host_tries_http_first(self, mock_run):
        """Explicit HTTP must probe HTTP before any SSH fallback."""
        from urllib.parse import urlsplit

        from apm_cli.commands.install import _validate_package_exists

        mock_run.return_value = self._make_completed_process(returncode=0)

        result = _validate_package_exists(
            "http://gitlab.company.internal/acme/rules.git", verbose=False
        )

        assert result is True
        assert mock_run.call_count == 1
        first_cmd = mock_run.call_args_list[0][0][0]
        # Parse each arg as a URL and check scheme + netloc explicitly to
        # avoid substring false-positives (e.g. the hostname appearing in a
        # path segment or query value on an otherwise-SSH URL).
        http_arg_found = False
        for arg in first_cmd:
            parts = urlsplit(arg)
            if parts.scheme == "http" and parts.netloc == "gitlab.company.internal":
                http_arg_found = True
                break
        assert http_arg_found, (
            f"Expected http://gitlab.company.internal URL in first call, got: {first_cmd}"
        )
        assert all("git@" not in arg for arg in first_cmd), (
            f"Expected no SSH URL in first call, got: {first_cmd}"
        )

    @patch("subprocess.run")
    def test_github_host_skips_ssh_attempt(self, mock_run):
        """GitHub.com repositories do NOT go through the SSH-first ls-remote path."""

        from apm_cli.commands.install import _validate_package_exists

        with patch("apm_cli.install.validation._github_api_get") as mock_get:
            mock_get.return_value = MagicMock(
                ok=False, status_code=404, reason="Not Found", headers={}
            )
            result = _validate_package_exists("owner/repo", verbose=False)

        assert result is False
        # No ls-remote call should have been made for a github.com host
        ls_remote_calls = [
            call
            for call in mock_run.call_args_list
            if "ls-remote" in (call[0][0] if call[0] else [])
        ]
        assert len(ls_remote_calls) == 0, (
            f"Expected no ls-remote calls for github.com, got: {ls_remote_calls}"
        )

    @patch("subprocess.run")
    def test_ghes_host_skips_ssh_attempt(self, mock_run):
        """A GHES host is treated as GitHub, not generic SSH probe is skipped."""
        from apm_cli.commands.install import _validate_package_exists

        mock_run.return_value = self._make_completed_process(returncode=0)

        result = _validate_package_exists("company.ghe.com/team/internal-repo", verbose=False)

        assert result is True
        ls_remote_calls = [
            call
            for call in mock_run.call_args_list
            if "ls-remote" in (call[0][0] if call[0] else [])
        ]
        assert len(ls_remote_calls) == 1, (
            f"Expected exactly 1 ls-remote call for GHES host, got: {ls_remote_calls}"
        )
        only_cmd = ls_remote_calls[0][0][0]
        # Must use HTTPS, not SSH
        assert all("git@" not in arg for arg in only_cmd), (
            f"Expected HTTPS-only URL for GHES host, got: {only_cmd}"
        )


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
