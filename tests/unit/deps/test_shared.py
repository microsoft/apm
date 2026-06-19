"""Unit tests for apm_cli.deps._shared."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps._shared import _validate_and_load_package

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep_ref(repo_url: str = "github.com/owner/repo") -> MagicMock:
    dep_ref = MagicMock()
    dep_ref.repo_url = repo_url
    dep_ref.to_github_url.return_value = f"https://{repo_url}"
    return dep_ref


def _make_valid_result(package: MagicMock) -> MagicMock:
    result = MagicMock()
    result.is_valid = True
    result.package = package
    result.errors = []
    return result


def _make_invalid_result(errors: list[str]) -> MagicMock:
    result = MagicMock()
    result.is_valid = False
    result.package = None
    result.errors = errors
    return result


# ---------------------------------------------------------------------------
# _validate_and_load_package
# ---------------------------------------------------------------------------


class TestValidateAndLoadPackage:
    """Tests for _validate_and_load_package."""

    def test_returns_package_on_success(self, tmp_path: Path) -> None:
        package = MagicMock()
        package.source = None
        dep_ref = _make_dep_ref()
        validation_result = _make_valid_result(package)

        result = _validate_and_load_package(validation_result, tmp_path, dep_ref)

        assert result is package

    def test_sets_package_source_to_github_url(self, tmp_path: Path) -> None:
        package = MagicMock()
        package.source = None
        dep_ref = _make_dep_ref()
        dep_ref.to_github_url.return_value = "https://github.com/owner/repo"
        validation_result = _make_valid_result(package)

        _validate_and_load_package(validation_result, tmp_path, dep_ref)

        assert package.source == "https://github.com/owner/repo"

    def test_raises_on_invalid_result(self, tmp_path: Path) -> None:
        dep_ref = _make_dep_ref("github.com/owner/bad-pkg")
        validation_result = _make_invalid_result(["Missing apm.yml", "Invalid structure"])

        with pytest.raises(RuntimeError, match="Invalid APM package"):
            _validate_and_load_package(validation_result, tmp_path, dep_ref)

    def test_error_message_contains_errors(self, tmp_path: Path) -> None:
        dep_ref = _make_dep_ref("github.com/o/r")
        validation_result = _make_invalid_result(["Error one", "Error two"])

        with pytest.raises(RuntimeError) as exc_info:
            _validate_and_load_package(validation_result, tmp_path, dep_ref)

        assert "Error one" in str(exc_info.value)
        assert "Error two" in str(exc_info.value)

    def test_removes_target_path_on_invalid_result(self, tmp_path: Path) -> None:
        # Create target directory that should be cleaned up
        target = tmp_path / "pkg"
        target.mkdir()
        (target / "file.txt").write_text("content", encoding="utf-8")

        dep_ref = _make_dep_ref()
        validation_result = _make_invalid_result(["Bad"])

        with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree:
            with pytest.raises(RuntimeError):
                _validate_and_load_package(validation_result, target, dep_ref)

        mock_rmtree.assert_called_once_with(target, ignore_errors=True)

    def test_does_not_remove_path_when_not_exists(self, tmp_path: Path) -> None:
        # Target path does not exist
        target = tmp_path / "nonexistent"
        dep_ref = _make_dep_ref()
        validation_result = _make_invalid_result(["Bad"])

        with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree:
            with pytest.raises(RuntimeError):
                _validate_and_load_package(validation_result, target, dep_ref)

        mock_rmtree.assert_not_called()

    def test_raises_when_valid_but_no_package(self, tmp_path: Path) -> None:
        result = MagicMock()
        result.is_valid = True
        result.package = None
        dep_ref = _make_dep_ref("github.com/owner/empty")

        with pytest.raises(RuntimeError, match="no package metadata found"):
            _validate_and_load_package(result, tmp_path, dep_ref)

    def test_error_message_contains_repo_url(self, tmp_path: Path) -> None:
        dep_ref = _make_dep_ref("github.com/owner/repo")
        validation_result = _make_invalid_result(["Bad"])

        with pytest.raises(RuntimeError) as exc_info:
            _validate_and_load_package(validation_result, tmp_path, dep_ref)

        assert "github.com/owner/repo" in str(exc_info.value)
