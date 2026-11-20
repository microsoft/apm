"""Local-path package validation against real directories."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-single-file-package-materialization"),
]

# ---------------------------------------------------------------------------
# _is_tls_failure
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _log_tls_failure
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _local_path_failure_reason
# ---------------------------------------------------------------------------


class TestLocalPathFailureReason:
    def test_path_does_not_exist_returns_message(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        dep = MagicMock()
        dep.is_local = True
        dep.local_path = str(tmp_path / "nonexistent")
        reason = _local_path_failure_reason(dep)
        assert reason == (
            f"Local package path does not exist: {dep.local_path}. "
            "Restore the source or correct the dependency path, then rerun apm install."
        )

    def test_path_is_file_not_dir_returns_message(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        f = tmp_path / "file.txt"
        f.write_text("x", encoding="utf-8")
        dep = MagicMock()
        dep.is_local = True
        dep.local_path = str(f)
        reason = _local_path_failure_reason(dep)
        assert reason == (
            "Unsupported single-file dependency: file.txt. "
            "Use .prompt.md, .instructions.md, .agent.md, "
            "or a directory package with apm.yml for companion assets."
        )

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
        assert logger.method_calls == []

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


# ---------------------------------------------------------------------------
# _validate_package_exists: ADO enforce_only
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_package_exists: exception parse fallback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_package_exists: TLS failure path
# ---------------------------------------------------------------------------
