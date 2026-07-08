"""Unit tests for ``apm_cli.install.validation``.

Covers branches not hit by existing validation tests:

* ``_is_tls_failure`` -- SSLError, TLS prefix, CERTIFICATE_VERIFY_FAILED,
  chained causes, max-depth guard
* ``_log_tls_failure`` -- non-verbose and verbose branches
* ``_local_path_failure_reason`` -- all branches (non-local, not-exists,
  not-dir, no-markers)
* ``_generic_host_ambiguous_subpath_hint`` -- unrecognised FQDN with an
  ambiguous multi-segment repo path (issue #2066), and the guard branches
  that suppress the hint (local, virtual, GitHub, ADO, GitLab-recognised,
  two-segment repo path)
* ``_local_path_no_markers_hint`` -- no found, with logger, without logger,
  > 5 items
* ``_validate_package_exists`` -- local exists with apm.yml, with SKILL.md,
  not dir, virtual + enforce_only, virtual subdir non-github,
  ADO enforce_only, github fallback enforce_only, fallback invalid slug,
  exception parse fallback
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

# ---------------------------------------------------------------------------
# _is_tls_failure
# ---------------------------------------------------------------------------


class TestIsTlsFailure:
    def test_ssl_error_returns_true(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")
        assert _is_tls_failure(exc) is True

    def test_tls_error_prefix_returns_true(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("TLS verification failed during handshake")
        assert _is_tls_failure(exc) is True

    def test_certificate_verify_failed_string_returns_true(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("CERTIFICATE_VERIFY_FAILED: self signed certificate")
        assert _is_tls_failure(exc) is True

    def test_chained_ssl_error_returns_true(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        inner = requests.exceptions.SSLError("ssl")
        outer = RuntimeError("connection failed")
        outer.__cause__ = inner
        assert _is_tls_failure(outer) is True

    def test_generic_exception_returns_false(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("network unreachable")
        assert _is_tls_failure(exc) is False

    def test_none_cause_terminates_cleanly(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = ValueError("unrelated")
        assert _is_tls_failure(exc) is False

    def test_max_depth_guard_prevents_infinite_loop(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        # Build a chain longer than 8
        exc = RuntimeError("base")
        current = exc
        for _ in range(12):
            next_exc = RuntimeError("wrap")
            next_exc.__cause__ = current
            current = next_exc
        # Should terminate without recursion error
        assert _is_tls_failure(current) is False


# ---------------------------------------------------------------------------
# _log_tls_failure
# ---------------------------------------------------------------------------


class TestLogTlsFailure:
    def test_non_verbose_emits_single_warning(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        exc = RuntimeError("TLS verification failed")
        _log_tls_failure("example.com", exc, verbose_log=None, logger=logger)
        logger.warning.assert_called_once()
        assert "REQUESTS_CA_BUNDLE" in logger.warning.call_args[0][0]

    def test_verbose_log_called_with_host_and_exc(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        calls: list[str] = []
        exc = RuntimeError("TLS verification failed: self signed")
        _log_tls_failure("example.com", exc, verbose_log=calls.append, logger=logger)
        assert any(re.search(r"\bexample\.com\b", c) for c in calls)
        assert any("TLS" in c or "self signed" in c for c in calls)

    def test_verbose_none_skips_verbose_log(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        # Should not raise when verbose_log is None
        _log_tls_failure("example.com", RuntimeError("TLS"), verbose_log=None, logger=logger)
        logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# _local_path_failure_reason
# ---------------------------------------------------------------------------


class TestLocalPathFailureReason:
    def test_non_local_dep_returns_none(self) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        dep = MagicMock()
        dep.is_local = False
        dep.local_path = None
        assert _local_path_failure_reason(dep) is None

    def test_no_local_path_returns_none(self) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        dep = MagicMock()
        dep.is_local = True
        dep.local_path = None
        assert _local_path_failure_reason(dep) is None

    def test_path_does_not_exist_returns_message(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        dep = MagicMock()
        dep.is_local = True
        dep.local_path = str(tmp_path / "nonexistent")
        reason = _local_path_failure_reason(dep)
        assert reason == "path does not exist"

    def test_path_is_file_not_dir_returns_message(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        f = tmp_path / "file.txt"
        f.write_text("x", encoding="utf-8")
        dep = MagicMock()
        dep.is_local = True
        dep.local_path = str(f)
        reason = _local_path_failure_reason(dep)
        assert reason == "path is not a directory"

    def test_dir_without_markers_returns_message(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        d = tmp_path / "emptydir"
        d.mkdir()
        dep = MagicMock()
        dep.is_local = True
        dep.local_path = str(d)
        reason = _local_path_failure_reason(dep)
        assert reason is not None
        assert "apm.yml" in reason or "SKILL.md" in reason


# ---------------------------------------------------------------------------
# _generic_host_ambiguous_subpath_hint
# ---------------------------------------------------------------------------


class TestGenericHostAmbiguousSubpathHint:
    """Issue #2066: an unrecognised self-hosted FQDN silently swallows a
    monorepo subpath into ``repo_url`` instead of splitting it, then fails
    validation with a misleading "not accessible" error. This hint should
    fire only for that exact shape and point the user at GITLAB_HOST /
    APM_GITLAB_HOSTS instead.
    """

    def _dep(self, **overrides):
        dep = MagicMock()
        dep.host = "gitlab.company.com"
        dep.is_local = False
        dep.is_virtual = False
        dep.is_azure_devops.return_value = False
        dep.repo_url = "my-org/my-monorepo/primitives/skills/my-skill"
        for key, value in overrides.items():
            setattr(dep, key, value)
        return dep

    def test_no_host_returns_none(self, monkeypatch) -> None:
        from apm_cli.install.validation import _generic_host_ambiguous_subpath_hint

        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)
        assert _generic_host_ambiguous_subpath_hint(self._dep(host=None)) is None

    def test_local_dep_returns_none(self, monkeypatch) -> None:
        from apm_cli.install.validation import _generic_host_ambiguous_subpath_hint

        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)
        assert _generic_host_ambiguous_subpath_hint(self._dep(is_local=True)) is None

    def test_virtual_dep_returns_none(self, monkeypatch) -> None:
        from apm_cli.install.validation import _generic_host_ambiguous_subpath_hint

        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)
        assert _generic_host_ambiguous_subpath_hint(self._dep(is_virtual=True)) is None

    def test_github_host_returns_none(self, monkeypatch) -> None:
        from apm_cli.install.validation import _generic_host_ambiguous_subpath_hint

        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)
        assert _generic_host_ambiguous_subpath_hint(self._dep(host="github.com")) is None

    def test_ado_host_returns_none(self, monkeypatch) -> None:
        from apm_cli.install.validation import _generic_host_ambiguous_subpath_hint

        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)
        dep = self._dep(host="dev.azure.com")
        dep.is_azure_devops.return_value = True
        assert _generic_host_ambiguous_subpath_hint(dep) is None

    def test_gitlab_com_returns_none(self, monkeypatch) -> None:
        from apm_cli.install.validation import _generic_host_ambiguous_subpath_hint

        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)
        assert _generic_host_ambiguous_subpath_hint(self._dep(host="gitlab.com")) is None

    def test_selfhosted_host_already_registered_via_env_returns_none(self, monkeypatch) -> None:
        from apm_cli.install.validation import _generic_host_ambiguous_subpath_hint

        monkeypatch.setenv("GITLAB_HOST", "gitlab.company.com")
        monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)
        assert _generic_host_ambiguous_subpath_hint(self._dep()) is None

    def test_two_segment_repo_path_returns_none(self, monkeypatch) -> None:
        """A plain owner/repo on a generic host isn't ambiguous -- don't hint."""
        from apm_cli.install.validation import _generic_host_ambiguous_subpath_hint

        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)
        assert _generic_host_ambiguous_subpath_hint(self._dep(repo_url="owner/repo")) is None

    def test_unrecognised_selfhosted_ambiguous_path_returns_hint(self, monkeypatch) -> None:
        from apm_cli.install.validation import _generic_host_ambiguous_subpath_hint

        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)
        reason = _generic_host_ambiguous_subpath_hint(self._dep())
        assert reason is not None
        assert "GITLAB_HOST=gitlab.company.com" in reason
        assert "APM_GITLAB_HOSTS" in reason
        assert "gitlab.company.com" in reason


# ---------------------------------------------------------------------------
# _local_path_no_markers_hint
# ---------------------------------------------------------------------------


class TestLocalPathNoMarkersHint:
    def test_no_sub_packages_produces_no_output(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        logger = MagicMock()
        _local_path_no_markers_hint(empty_dir, logger=logger)
        logger.progress.assert_not_called()

    def test_sub_package_with_logger_uses_logger(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        parent = tmp_path / "parent"
        child = parent / "mypkg"
        child.mkdir(parents=True)
        (child / "apm.yml").write_text("name: mypkg\n", encoding="utf-8")

        logger = MagicMock()
        _local_path_no_markers_hint(parent, logger=logger)
        logger.progress.assert_called_once()

    def test_sub_package_without_logger_uses_rich(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        parent = tmp_path / "parent"
        child = parent / "mypkg"
        child.mkdir(parents=True)
        (child / "SKILL.md").write_text("# skill\n", encoding="utf-8")

        with patch("apm_cli.install.validation._rich_info") as mock_info:
            _local_path_no_markers_hint(parent, logger=None)
        mock_info.assert_called_once()

    def test_more_than_five_hints_truncated(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        parent = tmp_path / "parent"
        parent.mkdir()
        for i in range(7):
            child = parent / f"pkg{i}"
            child.mkdir()
            (child / "apm.yml").write_text("name: pkg\n", encoding="utf-8")

        logger = MagicMock()
        _local_path_no_markers_hint(parent, logger=logger)
        # verbose_detail calls include the "... and X more" line
        all_calls = [str(c) for c in logger.verbose_detail.call_args_list]
        assert any("more" in c for c in all_calls)


# ---------------------------------------------------------------------------
# _validate_package_exists: local paths
# ---------------------------------------------------------------------------


class TestValidatePackageExistsLocal:
    def _make_auth_resolver(self) -> MagicMock:
        resolver = MagicMock()
        resolver.classify_host.return_value = MagicMock(
            kind="github", api_base="https://api.github.com", display_name="github.com"
        )
        return resolver

    def test_local_path_with_apm_yml_returns_true(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "apm.yml").write_text("name: mypkg\n", encoding="utf-8")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(pkg)

        result = _validate_package_exists(
            str(pkg), auth_resolver=self._make_auth_resolver(), dep_ref=dep_ref
        )
        assert result is True

    def test_local_path_with_skill_md_returns_true(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "SKILL.md").write_text("# skill\n", encoding="utf-8")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(pkg)

        result = _validate_package_exists(
            str(pkg), auth_resolver=self._make_auth_resolver(), dep_ref=dep_ref
        )
        assert result is True

    def test_local_path_not_dir_returns_false(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        f = tmp_path / "file.txt"
        f.write_text("x", encoding="utf-8")

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(f)

        result = _validate_package_exists(
            str(f), auth_resolver=self._make_auth_resolver(), dep_ref=dep_ref
        )
        assert result is False

    def test_local_path_dir_no_markers_calls_hint_and_returns_false(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        pkg = tmp_path / "empty_pkg"
        pkg.mkdir()

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(pkg)

        with patch("apm_cli.install.validation._local_path_no_markers_hint") as mock_hint:
            result = _validate_package_exists(
                str(pkg), auth_resolver=self._make_auth_resolver(), dep_ref=dep_ref
            )

        assert result is False
        mock_hint.assert_called_once()


# ---------------------------------------------------------------------------
# _validate_package_exists: virtual + enforce_only
# ---------------------------------------------------------------------------


class TestValidatePackageExistsVirtualEnforceOnly:
    def test_virtual_enforce_only_returns_true_without_probe(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        dep_ref.is_virtual = True
        dep_ref.is_virtual_subdirectory.return_value = False
        dep_ref.host = "github.com"
        dep_ref.is_azure_devops.return_value = False
        dep_ref.repo_url = "owner/repo"
        dep_ref.port = None

        resolver = MagicMock()
        resolver.classify_host.return_value = MagicMock(
            kind="github", api_base="https://api.github.com", display_name="github.com"
        )

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True):
            result = _validate_package_exists(
                "owner/repo#main:skills/foo",
                auth_resolver=resolver,
                dep_ref=dep_ref,
            )

        assert result is True

    def test_github_enforce_only_returns_true_without_api(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        dep_ref.is_virtual = False
        dep_ref.is_azure_devops.return_value = False
        dep_ref.host = "github.com"
        dep_ref.repo_url = "owner/repo"
        dep_ref.port = None

        resolver = MagicMock()
        resolver.classify_host.return_value = MagicMock(
            kind="github", api_base="https://api.github.com", display_name="github.com"
        )

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True):
            result = _validate_package_exists(
                "owner/repo",
                auth_resolver=resolver,
                dep_ref=dep_ref,
            )

        assert result is True


# ---------------------------------------------------------------------------
# _validate_package_exists: ADO enforce_only
# ---------------------------------------------------------------------------


class TestValidatePackageExistsAdoEnforceOnly:
    def test_ado_enforce_only_returns_true_without_probe(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        dep_ref.is_virtual = False
        dep_ref.is_azure_devops.return_value = True
        dep_ref.host = "dev.azure.com"
        dep_ref.repo_url = "org/proj/_git/repo"
        dep_ref.port = None
        dep_ref.explicit_scheme = None
        dep_ref.is_insecure = False

        resolver = MagicMock()
        resolver.classify_host.return_value = MagicMock(kind="ado")
        resolver.resolve_for_dep.return_value = MagicMock(
            token="pat", auth_scheme="basic", git_env={}
        )

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True):
            result = _validate_package_exists(
                "dev.azure.com/org/proj/_git/repo",
                auth_resolver=resolver,
                dep_ref=dep_ref,
            )

        assert result is True


# ---------------------------------------------------------------------------
# _validate_package_exists: exception parse fallback
# ---------------------------------------------------------------------------


class TestValidatePackageExistsFallback:
    def test_invalid_slug_returns_false(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        # Make DependencyReference.parse raise so we hit the except branch
        resolver = MagicMock()
        resolver.classify_host.return_value = MagicMock(
            kind="github", api_base="https://api.github.com", display_name="github.com"
        )

        # A slug with invalid chars triggers the regex guard
        with patch("apm_cli.models.apm_package.DependencyReference") as mock_dr:
            mock_dr.parse.side_effect = Exception("parse error")
            result = _validate_package_exists(
                "../../etc/passwd",
                auth_resolver=resolver,
            )

        assert result is False

    def test_valid_slug_uses_api_fallback(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        resolver = MagicMock()
        resolver.classify_host.return_value = MagicMock(
            kind="github", api_base="https://api.github.com", display_name="github.com"
        )
        resolver.try_with_fallback.return_value = True

        with (
            patch("apm_cli.models.apm_package.DependencyReference") as mock_dr,
            patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False),
        ):
            mock_dr.parse.side_effect = Exception("parse error")
            result = _validate_package_exists("owner/repo", auth_resolver=resolver)

        assert result is True

    def test_enforce_only_fallback_returns_true_without_probe(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        resolver = MagicMock()
        resolver.classify_host.return_value = MagicMock(
            kind="github", api_base="https://api.github.com", display_name="github.com"
        )

        with (
            patch("apm_cli.models.apm_package.DependencyReference") as mock_dr,
            patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True),
        ):
            mock_dr.parse.side_effect = Exception("parse error")
            result = _validate_package_exists("owner/valid-repo", auth_resolver=resolver)

        assert result is True


# ---------------------------------------------------------------------------
# _validate_package_exists: TLS failure path
# ---------------------------------------------------------------------------


class TestValidatePackageExistsTlsFailure:
    def test_tls_failure_from_api_returns_false(self) -> None:
        from apm_cli.install.validation import _validate_package_exists

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        dep_ref.is_virtual = False
        dep_ref.is_azure_devops.return_value = False
        dep_ref.host = "github.com"
        dep_ref.repo_url = "owner/repo"
        dep_ref.port = None

        resolver = MagicMock()
        resolver.classify_host.return_value = MagicMock(
            kind="github", api_base="https://api.github.com", display_name="github.com"
        )
        tls_exc = requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")
        resolver.try_with_fallback.side_effect = tls_exc

        logger = MagicMock()

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            result = _validate_package_exists(
                "owner/repo",
                auth_resolver=resolver,
                dep_ref=dep_ref,
                logger=logger,
            )

        assert result is False
        logger.warning.assert_called_once()
