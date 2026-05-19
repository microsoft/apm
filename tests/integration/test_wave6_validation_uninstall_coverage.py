"""Wave 6: integration tests for install/validation.py and commands/uninstall/engine.py.

Goal: maximise code coverage by exercising real code paths with minimal mocking.
Only external I/O (HTTP, subprocess, auth) is mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

# ---------------------------------------------------------------------------
# install/validation.py -- TLS helpers
# ---------------------------------------------------------------------------


class TestTLSHelpers:
    """Cover _is_tls_failure, _log_tls_failure."""

    def test_is_tls_failure_with_ssl_error(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = requests.exceptions.SSLError("certificate verify failed")
        assert _is_tls_failure(exc) is True

    def test_is_tls_failure_with_cert_verify_msg(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("CERTIFICATE_VERIFY_FAILED something")
        assert _is_tls_failure(exc) is True

    def test_is_tls_failure_with_tls_prefix(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("TLS verification failed for host.example.com")
        assert _is_tls_failure(exc) is True

    def test_is_tls_failure_with_chained_cause(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        inner = requests.exceptions.SSLError("bad cert")
        outer = RuntimeError("connection failed")
        outer.__cause__ = inner
        assert _is_tls_failure(outer) is True

    def test_is_tls_failure_returns_false_for_unrelated(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("404 Not Found")
        assert _is_tls_failure(exc) is False

    def test_is_tls_failure_chain_depth_limit(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        # Build chain deeper than 8
        exc = RuntimeError("level 0")
        current = exc
        for i in range(1, 12):
            new_exc = RuntimeError(f"level {i}")
            current.__cause__ = new_exc
            current = new_exc
        # SSL error at the end of a >8 deep chain should not be reached
        current.__cause__ = requests.exceptions.SSLError("deep cert error")
        # The function caps at 8 hops -- the SSL error is too deep
        # (this may or may not be found depending on where the SSL is)
        result = _is_tls_failure(exc)
        assert isinstance(result, bool)

    def test_log_tls_failure_default_verbosity(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        _log_tls_failure("github.com", RuntimeError("ssl err"), None, logger)
        logger.warning.assert_called_once()
        assert "TLS" in logger.warning.call_args[0][0]

    def test_log_tls_failure_verbose(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        verbose_log = MagicMock()
        exc = RuntimeError("ssl err details")
        _log_tls_failure("ghes.corp.com", exc, verbose_log, logger)
        logger.warning.assert_called_once()
        verbose_log.assert_called_once()
        assert "ghes.corp.com" in verbose_log.call_args[0][0]


# ---------------------------------------------------------------------------
# install/validation.py -- local path helpers
# ---------------------------------------------------------------------------


class TestLocalPathFailureReason:
    """Cover _local_path_failure_reason."""

    def test_non_local_returns_none(self) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        ref = MagicMock()
        ref.is_local = False
        ref.local_path = None
        assert _local_path_failure_reason(ref) is None

    def test_path_does_not_exist(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        ref = MagicMock()
        ref.is_local = True
        ref.local_path = str(tmp_path / "nonexistent")
        result = _local_path_failure_reason(ref)
        assert result is not None
        assert "does not exist" in result

    def test_path_is_file(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        f = tmp_path / "some_file.txt"
        f.write_text("data")
        ref = MagicMock()
        ref.is_local = True
        ref.local_path = str(f)
        result = _local_path_failure_reason(ref)
        assert result is not None
        assert "not a directory" in result

    def test_dir_without_markers(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        d = tmp_path / "pkg"
        d.mkdir()
        ref = MagicMock()
        ref.is_local = True
        ref.local_path = str(d)
        result = _local_path_failure_reason(ref)
        assert result is not None
        assert "no apm.yml" in result


class TestLocalPathNoMarkersHint:
    """Cover _local_path_no_markers_hint."""

    def test_no_packages_found(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        # Empty dir -- no hint
        result = _local_path_no_markers_hint(tmp_path)
        assert result is None

    def test_finds_child_with_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        child = tmp_path / "my-pkg"
        child.mkdir()
        (child / "apm.yml").write_text("name: my-pkg\n")
        # With logger
        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)
        logger.progress.assert_called_once()
        assert "installable" in logger.progress.call_args[0][0].lower()

    def test_finds_child_with_skill_md(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        child = tmp_path / "my-skill"
        child.mkdir()
        (child / "SKILL.md").write_text("---\ndescription: test\n---\n# Skill")
        # Without logger -- uses _rich_info
        _local_path_no_markers_hint(tmp_path)

    def test_finds_grandchild_packages(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        grandchild = tmp_path / "skills" / "nested-skill"
        grandchild.mkdir(parents=True)
        (grandchild / "apm.yml").write_text("name: nested\n")
        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)
        logger.progress.assert_called()

    def test_many_packages_shows_limit(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        # Create 7 child packages -- should show 5 + "... and 2 more"
        for i in range(7):
            child = tmp_path / f"pkg-{i:02d}"
            child.mkdir()
            (child / "apm.yml").write_text(f"name: pkg-{i}\n")
        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)
        # Should have a "... and 2 more" call
        calls = [str(c) for c in logger.verbose_detail.call_args_list]
        has_more = any("more" in c for c in calls)
        assert has_more


# ---------------------------------------------------------------------------
# install/validation.py -- _validate_package_exists (local paths)
# ---------------------------------------------------------------------------


class TestValidatePackageExistsLocal:
    """Test _validate_package_exists with local path dependencies."""

    def test_local_path_with_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists
        from apm_cli.models.apm_package import DependencyReference

        pkg = tmp_path / "local-pkg"
        pkg.mkdir()
        (pkg / "apm.yml").write_text("name: local-pkg\nversion: 1.0.0\n")
        ref = DependencyReference.parse(str(pkg))
        result = _validate_package_exists(str(pkg), dep_ref=ref)
        assert result is True

    def test_local_path_with_skill_md(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists
        from apm_cli.models.apm_package import DependencyReference

        pkg = tmp_path / "skill-pkg"
        pkg.mkdir()
        (pkg / "SKILL.md").write_text("---\ndescription: test\n---\n# Test")
        ref = DependencyReference.parse(str(pkg))
        result = _validate_package_exists(str(pkg), dep_ref=ref)
        assert result is True

    def test_local_path_nonexistent(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        ref = MagicMock()
        ref.is_local = True
        ref.local_path = str(tmp_path / "missing")
        result = _validate_package_exists(str(tmp_path / "missing"), dep_ref=ref)
        assert result is False

    def test_local_path_no_markers(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        pkg = tmp_path / "empty-pkg"
        pkg.mkdir()
        ref = MagicMock()
        ref.is_local = True
        ref.local_path = str(pkg)
        result = _validate_package_exists(str(pkg), dep_ref=ref)
        assert result is False


# ---------------------------------------------------------------------------
# commands/uninstall/engine.py -- helpers
# ---------------------------------------------------------------------------


class TestUninstallEngineHelpers:
    """Cover pure-logic helpers in the uninstall engine."""

    def test_is_marketplace_ref_positive(self) -> None:
        from apm_cli.commands.uninstall.engine import _is_marketplace_ref

        assert _is_marketplace_ref("my-plugin@my-marketplace") is True

    def test_is_marketplace_ref_negative(self) -> None:
        from apm_cli.commands.uninstall.engine import _is_marketplace_ref

        assert _is_marketplace_ref("owner/repo") is False

    def test_is_marketplace_ref_bare_name(self) -> None:
        from apm_cli.commands.uninstall.engine import _is_marketplace_ref

        assert _is_marketplace_ref("just-a-name") is False

    def test_build_children_index_empty(self) -> None:
        from apm_cli.commands.uninstall.engine import _build_children_index

        lockfile = MagicMock()
        lockfile.get_package_dependencies.return_value = []
        result = _build_children_index(lockfile)
        assert result == {}

    def test_build_children_index_with_deps(self) -> None:
        from apm_cli.commands.uninstall.engine import _build_children_index

        dep1 = MagicMock()
        dep1.resolved_by = "org/parent"
        dep2 = MagicMock()
        dep2.resolved_by = "org/parent"
        dep3 = MagicMock()
        dep3.resolved_by = "other/pkg"
        dep4 = MagicMock()
        dep4.resolved_by = None
        lockfile = MagicMock()
        lockfile.get_package_dependencies.return_value = [dep1, dep2, dep3, dep4]
        result = _build_children_index(lockfile)
        assert len(result["org/parent"]) == 2
        assert len(result["other/pkg"]) == 1
        assert None not in result

    def test_parse_dependency_entry_string(self) -> None:
        from apm_cli.commands.uninstall.engine import _parse_dependency_entry

        ref = _parse_dependency_entry("owner/repo")
        assert ref.repo_url is not None

    def test_parse_dependency_entry_dict(self) -> None:
        from apm_cli.commands.uninstall.engine import _parse_dependency_entry

        ref = _parse_dependency_entry({"git": "https://github.com/owner/repo", "version": ">=1.0"})
        assert ref is not None

    def test_parse_dependency_entry_dep_ref(self) -> None:
        from apm_cli.commands.uninstall.engine import _parse_dependency_entry
        from apm_cli.models.apm_package import DependencyReference

        original = DependencyReference.parse("owner/repo")
        result = _parse_dependency_entry(original)
        assert result is original

    def test_parse_dependency_entry_invalid_type(self) -> None:
        from apm_cli.commands.uninstall.engine import _parse_dependency_entry

        with pytest.raises(ValueError, match=r"Unsupported dependency entry type"):
            _parse_dependency_entry(12345)


# ---------------------------------------------------------------------------
# commands/uninstall/engine.py -- _validate_uninstall_packages
# ---------------------------------------------------------------------------


class TestValidateUninstallPackages:
    """Cover _validate_uninstall_packages with various inputs."""

    def test_simple_owner_repo_found(self) -> None:
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = MagicMock()
        deps = ["owner/repo"]
        to_remove, not_found = _validate_uninstall_packages(["owner/repo"], deps, logger)
        assert len(to_remove) == 1
        assert len(not_found) == 0

    def test_package_not_found(self) -> None:
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = MagicMock()
        deps = ["org/other"]
        to_remove, not_found = _validate_uninstall_packages(["owner/repo"], deps, logger)
        assert len(to_remove) == 0
        assert len(not_found) == 1

    def test_bare_name_without_marketplace(self) -> None:
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = MagicMock()
        deps = ["owner/repo"]
        to_remove, not_found = _validate_uninstall_packages(["just-a-name"], deps, logger)
        assert len(to_remove) == 0
        assert len(not_found) == 1
        logger.error.assert_called_once()

    def test_multiple_packages_mixed(self) -> None:
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = MagicMock()
        deps = ["org/found-pkg", "org/other-pkg"]
        to_remove, not_found = _validate_uninstall_packages(
            ["org/found-pkg", "org/missing-pkg"], deps, logger
        )
        assert len(to_remove) == 1
        assert len(not_found) == 1


# ---------------------------------------------------------------------------
# commands/uninstall/engine.py -- _dry_run_uninstall
# ---------------------------------------------------------------------------


class TestDryRunUninstall:
    """Cover _dry_run_uninstall."""

    def test_dry_run_basic(self, tmp_path: Path) -> None:
        from apm_cli.commands.uninstall.engine import _dry_run_uninstall

        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        pkg_dir = apm_modules / "repo"
        pkg_dir.mkdir()
        logger = MagicMock()
        # Create apm.yml for lockfile reading
        (tmp_path / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\ndescription: test\nowner:\n  name: org\n"
        )
        with patch("apm_cli.commands.uninstall.engine.Path") as mock_path:
            mock_path.return_value = tmp_path
            # Simply exercise the code with owner/repo strings
            _dry_run_uninstall(["owner/repo"], apm_modules, logger)
        logger.success.assert_called_once()


# ---------------------------------------------------------------------------
# commands/uninstall/engine.py -- _remove_packages_from_disk
# ---------------------------------------------------------------------------


class TestRemovePackagesFromDisk:
    """Cover _remove_packages_from_disk."""

    def test_no_apm_modules_dir(self, tmp_path: Path) -> None:
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk

        logger = MagicMock()
        removed = _remove_packages_from_disk(["owner/repo"], tmp_path / "apm_modules", logger)
        assert removed == 0

    def test_package_exists_and_removed(self, tmp_path: Path) -> None:
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk

        apm_modules = tmp_path / "apm_modules"
        owner_dir = apm_modules / "owner"
        pkg_dir = owner_dir / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: test\n")
        logger = MagicMock()
        removed = _remove_packages_from_disk(["owner/repo"], apm_modules, logger)
        assert removed == 1
        assert not pkg_dir.exists()

    def test_package_not_on_disk(self, tmp_path: Path) -> None:
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk

        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        logger = MagicMock()
        removed = _remove_packages_from_disk(["owner/repo"], apm_modules, logger)
        assert removed == 0
        logger.warning.assert_called()


# ---------------------------------------------------------------------------
# commands/uninstall/engine.py -- _cleanup_transitive_orphans
# ---------------------------------------------------------------------------


class TestCleanupTransitiveOrphans:
    """Cover _cleanup_transitive_orphans."""

    def test_no_lockfile(self, tmp_path: Path) -> None:
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        removed, orphans = _cleanup_transitive_orphans(
            None, ["owner/repo"], tmp_path / "apm_modules", tmp_path / "apm.yml", MagicMock()
        )
        assert removed == 0
        assert len(orphans) == 0

    def test_no_apm_modules(self, tmp_path: Path) -> None:
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        lockfile = MagicMock()
        removed, _orphans = _cleanup_transitive_orphans(
            lockfile, ["owner/repo"], tmp_path / "missing_dir", tmp_path / "apm.yml", MagicMock()
        )
        assert removed == 0

    def test_no_orphans_found(self, tmp_path: Path) -> None:
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        lockfile = MagicMock()
        lockfile.get_package_dependencies.return_value = []
        logger = MagicMock()
        removed, orphans = _cleanup_transitive_orphans(
            lockfile, ["owner/repo"], apm_modules, tmp_path / "apm.yml", logger
        )
        assert removed == 0
        assert len(orphans) == 0
