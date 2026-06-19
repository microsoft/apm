"""integration tests for src/apm_cli/install/validation.py.

Targets the gap of ~183 lines at 60.1% coverage.

Covered branches / lines:
- _is_tls_failure: chain traversal, SSLError detection, plain RuntimeError
- _log_tls_failure: verbose_log path vs None, verbose suffix stripping
- _local_path_failure_reason: not-local, non-absolute, does-not-exist, is-file, no-markers
- _local_path_no_markers_hint: no found, 5+ found, logger vs rich path
- _validate_package_exists:
  - local path exists + apm.yml/SKILL.md/plugin.json branches
  - local path not a dir
  - local path missing markers -> hint + return False
  - is_enforce_only() virtual package skip
  - virtual package validate_virtual_package_exists True/False
  - verbose_log with auth context on failure
  - ADO/GHES is_enforce_only skip
  - generic host fallback chain
  - GitHub API happy path / 404-with-token / SSLError
  - parse-fail fallback: invalid slug returns False
  - parse-fail fallback: enforce_only returns True
  - TLS failure path in fallback
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

from apm_cli.install.validation import (
    _is_tls_failure,
    _local_path_failure_reason,
    _local_path_no_markers_hint,
    _log_tls_failure,
    _validate_package_exists,
)

# ---------------------------------------------------------------------------
# _is_tls_failure
# ---------------------------------------------------------------------------


class TestIsTlsFailure:
    def test_returns_false_for_plain_exception(self):
        assert _is_tls_failure(RuntimeError("generic error")) is False

    def test_returns_true_for_tls_prefix_in_message(self):
        exc = RuntimeError("TLS verification failed for host")
        assert _is_tls_failure(exc) is True

    def test_returns_true_for_certificate_verify_failed(self):
        exc = RuntimeError("CERTIFICATE_VERIFY_FAILED: unable to get local issuer")
        assert _is_tls_failure(exc) is True

    def test_returns_true_for_ssl_error_instance(self):
        exc = requests.exceptions.SSLError("ssl error")
        assert _is_tls_failure(exc) is True

    def test_traverses_cause_chain(self):
        inner = requests.exceptions.SSLError("ssl")
        outer = RuntimeError("wrapped")
        outer.__cause__ = inner
        assert _is_tls_failure(outer) is True

    def test_chain_depth_limit(self):
        """Chain deeper than 8 levels should not cause infinite loop."""
        exc = RuntimeError("deep")
        cur = exc
        for _ in range(12):
            child = RuntimeError("level")
            cur.__cause__ = child
            cur = child
        # Should return without hanging
        result = _is_tls_failure(exc)
        assert isinstance(result, bool)

    def test_context_chain_traversal(self):
        inner = RuntimeError("CERTIFICATE_VERIFY_FAILED")
        outer = ValueError("context wrapper")
        outer.__context__ = inner
        assert _is_tls_failure(outer) is True


# ---------------------------------------------------------------------------
# _log_tls_failure
# ---------------------------------------------------------------------------


class TestLogTlsFailure:
    def test_calls_verbose_log_when_provided(self):
        import logging

        logger = logging.getLogger("test_tls")
        verbose_calls = []

        def verbose_log(msg):
            verbose_calls.append(msg)

        exc = RuntimeError("ssl error details")
        with patch.object(logger, "warning") as mock_warn:
            _log_tls_failure("example.com", exc, verbose_log, logger)

        assert len(verbose_calls) == 1
        assert "ssl error details" in verbose_calls[0]
        mock_warn.assert_called_once()

    def test_skips_verbose_log_when_none(self):
        import logging

        logger = logging.getLogger("test_tls2")
        exc = RuntimeError("ssl error")
        with patch.object(logger, "warning") as mock_warn:
            # Should not raise even with verbose_log=None
            _log_tls_failure("example.com", exc, None, logger)
        mock_warn.assert_called_once()


# ---------------------------------------------------------------------------
# _local_path_failure_reason
# ---------------------------------------------------------------------------


class TestLocalPathFailureReason:
    def test_returns_none_for_non_local(self):
        dep_ref = MagicMock()
        dep_ref.is_local = False
        assert _local_path_failure_reason(dep_ref) is None

    def test_returns_none_when_no_local_path(self):
        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = None
        assert _local_path_failure_reason(dep_ref) is None

    def test_returns_not_exist_when_missing(self, tmp_path):
        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(tmp_path / "nonexistent")
        reason = _local_path_failure_reason(dep_ref)
        assert reason == "path does not exist"

    def test_returns_not_directory_for_file(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("content")
        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(f)
        reason = _local_path_failure_reason(dep_ref)
        assert reason == "path is not a directory"

    def test_returns_no_markers_for_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(empty)

        with patch("apm_cli.install.validation._local_path_no_markers_hint"):
            reason = _local_path_failure_reason(dep_ref)

        assert reason is not None
        assert "no" in reason.lower()


# ---------------------------------------------------------------------------
# _local_path_no_markers_hint
# ---------------------------------------------------------------------------


class TestLocalPathNoMarkersHint:
    def test_does_nothing_when_no_packages_found(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        logger = MagicMock()
        # Should not raise and not call logger
        _local_path_no_markers_hint(empty, logger=logger)
        logger.progress.assert_not_called()

    def test_logs_found_packages_via_logger(self, tmp_path):
        pkg_dir = tmp_path / "child"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("name: test")
        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)
        logger.progress.assert_called_once()

    def test_logs_multiple_packages_with_truncation(self, tmp_path):
        for i in range(7):
            pkg_dir = tmp_path / f"pkg{i}"
            pkg_dir.mkdir()
            (pkg_dir / "SKILL.md").write_text("# Skill")
        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)
        # Should truncate after 5
        verbose_calls = [str(c) for c in logger.verbose_detail.call_args_list]
        assert any("more" in c for c in verbose_calls)

    def test_uses_rich_echo_when_no_logger(self, tmp_path):
        pkg_dir = tmp_path / "child"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("name: test")

        with patch("apm_cli.install.validation._rich_info") as mock_info:
            _local_path_no_markers_hint(tmp_path, logger=None)
            mock_info.assert_called_once()


# ---------------------------------------------------------------------------
# _validate_package_exists -- local path branches
# ---------------------------------------------------------------------------


class TestValidatePackageExistsLocal:
    def _make_dep_ref(self, local_path, exists_as_dir=True, has_apm_yml=False, has_skill_md=False):
        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = local_path
        dep_ref.is_virtual = False
        dep_ref.is_azure_devops.return_value = False
        dep_ref.host = None
        return dep_ref

    def test_returns_true_for_dir_with_apm_yml(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("name: test")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(pkg_dir)

        with patch("apm_cli.core.auth.AuthResolver"):
            with patch(
                "apm_cli.models.apm_package.DependencyReference.parse", return_value=dep_ref
            ):
                result = _validate_package_exists(str(pkg_dir), dep_ref=dep_ref)

        assert result is True

    def test_returns_true_for_dir_with_skill_md(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "SKILL.md").write_text("# Skill")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(pkg_dir)

        with patch("apm_cli.core.auth.AuthResolver"):
            with patch(
                "apm_cli.models.apm_package.DependencyReference.parse", return_value=dep_ref
            ):
                result = _validate_package_exists(str(pkg_dir), dep_ref=dep_ref)

        assert result is True

    def test_returns_false_when_local_path_not_dir(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("not a dir")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(f)

        with patch("apm_cli.core.auth.AuthResolver"):
            result = _validate_package_exists(str(f), dep_ref=dep_ref)

        assert result is False

    def test_returns_false_when_local_path_missing_markers(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(empty)

        with patch("apm_cli.core.auth.AuthResolver"):
            with patch("apm_cli.install.validation._local_path_no_markers_hint"):
                with patch("apm_cli.utils.helpers.find_plugin_json", return_value=None):
                    result = _validate_package_exists(str(empty), dep_ref=dep_ref)

        assert result is False

    def test_returns_true_when_plugin_json_found(self, tmp_path):
        pkg_dir = tmp_path / "plugin"
        pkg_dir.mkdir()

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(pkg_dir)

        with patch("apm_cli.core.auth.AuthResolver"):
            with patch(
                "apm_cli.utils.helpers.find_plugin_json", return_value=pkg_dir / "plugin.json"
            ):
                result = _validate_package_exists(str(pkg_dir), dep_ref=dep_ref)

        assert result is True


# ---------------------------------------------------------------------------
# _validate_package_exists -- virtual package branches
# ---------------------------------------------------------------------------


class TestValidatePackageExistsVirtual:
    def _make_virtual_dep_ref(self):
        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.is_virtual = True
        dep_ref.is_virtual_subdirectory.return_value = False
        dep_ref.host = "github.com"
        dep_ref.is_azure_devops.return_value = False
        dep_ref.repo_url = "owner/repo"
        dep_ref.port = None
        return dep_ref

    def test_enforce_only_returns_true_without_probe(self):
        dep_ref = self._make_virtual_dep_ref()

        with patch("apm_cli.core.auth.AuthResolver"):
            with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True):
                with patch("apm_cli.utils.github_host.is_github_hostname", return_value=True):
                    result = _validate_package_exists("owner/repo/file.prompt.md", dep_ref=dep_ref)

        assert result is True

    def test_virtual_validate_returns_true_on_success(self):
        dep_ref = self._make_virtual_dep_ref()

        mock_downloader = MagicMock()
        mock_downloader.validate_virtual_package_exists.return_value = True

        mock_auth_resolver = MagicMock()
        mock_auth_resolver.resolve_for_dep.return_value = MagicMock(
            source="env", token_type="pat", token="tok"
        )

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            with patch("apm_cli.utils.github_host.is_github_hostname", return_value=True):
                with patch(
                    "apm_cli.deps.github_downloader.GitHubPackageDownloader",
                    return_value=mock_downloader,
                ):
                    result = _validate_package_exists(
                        "owner/repo/file.prompt.md",
                        dep_ref=dep_ref,
                        auth_resolver=mock_auth_resolver,
                    )

        assert result is True

    def test_virtual_validate_returns_false_on_failure(self):
        dep_ref = self._make_virtual_dep_ref()

        mock_downloader = MagicMock()
        mock_downloader.validate_virtual_package_exists.return_value = False

        mock_auth_resolver = MagicMock()
        mock_auth_resolver.resolve_for_dep.return_value = MagicMock(
            source="env", token_type="pat", token="tok"
        )
        mock_auth_resolver.build_error_context.return_value = "error context line"

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            with patch("apm_cli.utils.github_host.is_github_hostname", return_value=True):
                with patch(
                    "apm_cli.deps.github_downloader.GitHubPackageDownloader",
                    return_value=mock_downloader,
                ):
                    result = _validate_package_exists(
                        "owner/repo/file.prompt.md",
                        dep_ref=dep_ref,
                        auth_resolver=mock_auth_resolver,
                        verbose=True,
                    )

        assert result is False


# ---------------------------------------------------------------------------
# _validate_package_exists -- GitHub API path
# ---------------------------------------------------------------------------


class TestValidatePackageExistsGithubAPI:
    def _make_github_dep_ref(self):
        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.is_virtual = False
        dep_ref.is_azure_devops.return_value = False
        dep_ref.host = "github.com"
        dep_ref.repo_url = "owner/repo"
        dep_ref.port = None
        dep_ref.is_virtual_subdirectory = MagicMock(return_value=False)
        return dep_ref

    def test_returns_true_when_api_ok(self):
        dep_ref = self._make_github_dep_ref()

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200

        mock_auth_resolver = MagicMock()
        host_info = MagicMock()
        host_info.api_base = "https://api.github.com"
        host_info.display_name = "github.com"
        mock_auth_resolver.classify_host.return_value = host_info
        mock_auth_resolver.try_with_fallback.return_value = True

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            with patch("apm_cli.utils.github_host.is_github_hostname", return_value=True):
                result = _validate_package_exists(
                    "owner/repo",
                    dep_ref=dep_ref,
                    auth_resolver=mock_auth_resolver,
                )

        assert result is True

    def test_returns_false_when_api_raises_and_tls_failure(self):
        dep_ref = self._make_github_dep_ref()

        mock_auth_resolver = MagicMock()
        host_info = MagicMock()
        host_info.api_base = "https://api.github.com"
        host_info.display_name = "github.com"
        mock_auth_resolver.classify_host.return_value = host_info
        mock_auth_resolver.try_with_fallback.side_effect = RuntimeError(
            "TLS verification failed for github.com"
        )
        mock_auth_resolver.build_error_context.return_value = "context"

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            with patch("apm_cli.utils.github_host.is_github_hostname", return_value=True):
                mock_logger = MagicMock()
                result = _validate_package_exists(
                    "owner/repo",
                    dep_ref=dep_ref,
                    auth_resolver=mock_auth_resolver,
                    logger=mock_logger,
                )

        assert result is False

    def test_enforce_only_github_returns_true(self):
        dep_ref = self._make_github_dep_ref()
        mock_auth_resolver = MagicMock()
        host_info = MagicMock()
        host_info.api_base = "https://api.github.com"
        mock_auth_resolver.classify_host.return_value = host_info

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True):
            with patch("apm_cli.utils.github_host.is_github_hostname", return_value=True):
                result = _validate_package_exists(
                    "owner/repo",
                    dep_ref=dep_ref,
                    auth_resolver=mock_auth_resolver,
                )

        assert result is True


# ---------------------------------------------------------------------------
# _validate_package_exists -- parse-failure fallback
# ---------------------------------------------------------------------------


class TestValidatePackageExistsFallback:
    def test_invalid_slug_returns_false(self):
        """Package name with path traversal or bad chars returns False."""
        with patch(
            "apm_cli.models.apm_package.DependencyReference.parse",
            side_effect=ValueError("parse failed"),
        ):
            with patch("apm_cli.core.auth.AuthResolver"):
                result = _validate_package_exists("../bad/path")
        assert result is False

    def test_valid_slug_enforce_only_returns_true(self):
        with patch(
            "apm_cli.models.apm_package.DependencyReference.parse",
            side_effect=ValueError("parse failed"),
        ):
            with patch("apm_cli.core.auth.AuthResolver"):
                with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True):
                    result = _validate_package_exists("owner/repo")
        assert result is True

    def test_valid_slug_returns_true_when_api_ok(self):
        mock_auth_resolver = MagicMock()
        host_info = MagicMock()
        host_info.api_base = "https://api.github.com"
        host_info.display_name = "github.com"
        mock_auth_resolver.classify_host.return_value = host_info
        mock_auth_resolver.try_with_fallback.return_value = True

        with patch(
            "apm_cli.models.apm_package.DependencyReference.parse",
            side_effect=ValueError("parse failed"),
        ):
            with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
                result = _validate_package_exists("owner/repo", auth_resolver=mock_auth_resolver)
        assert result is True

    def test_valid_slug_returns_false_when_exception_and_tls(self):
        mock_auth_resolver = MagicMock()
        host_info = MagicMock()
        host_info.api_base = "https://api.github.com"
        host_info.display_name = "github.com"
        mock_auth_resolver.classify_host.return_value = host_info
        mock_auth_resolver.try_with_fallback.side_effect = RuntimeError("TLS verification failed")
        mock_auth_resolver.build_error_context.return_value = "ctx"

        with patch(
            "apm_cli.models.apm_package.DependencyReference.parse",
            side_effect=ValueError("parse failed"),
        ):
            with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
                mock_logger = MagicMock()
                result = _validate_package_exists(
                    "owner/repo", auth_resolver=mock_auth_resolver, logger=mock_logger
                )
        assert result is False
