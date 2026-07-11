"""Unit coverage for the canonical install transaction owner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.command_logger import _ValidationOutcome
from apm_cli.install.transaction import InstallTransaction
from apm_cli.models.results import InstallDisposition, InstallResult
from apm_cli.utils.path_security import PathTraversalError


def _transaction(
    tmp_path: Path,
    validation: _ValidationOutcome | None = None,
) -> InstallTransaction:
    manifest = tmp_path / "apm.yml"
    manifest.write_bytes(b"name: fixture\r\nversion: 1.0.0\r\n")
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    return InstallTransaction(
        manifest_path=manifest,
        apm_modules_dir=modules,
        validation=validation,
        logger=MagicMock(),
    )


def test_total_validation_failure_rolls_back(tmp_path: Path) -> None:
    """An all-invalid batch is a failed validation, not a successful no-op."""
    transaction = _transaction(
        tmp_path,
        _ValidationOutcome(valid=[], invalid=[("bad", "not found")]),
    )
    transaction.manifest_path.write_text("changed\n", encoding="ascii")

    result = transaction.validation_result()

    assert result is not None
    assert result.disposition is InstallDisposition.VALIDATION_FAILED
    assert result.exit_code == 1
    assert result.committed is False
    assert transaction.manifest_path.read_bytes() == b"name: fixture\r\nversion: 1.0.0\r\n"


def test_mixed_validation_commits_as_partial_success(tmp_path: Path) -> None:
    """A batch with survivors commits and keeps the existing warning policy."""
    transaction = _transaction(
        tmp_path,
        _ValidationOutcome(
            valid=[("good", False)],
            invalid=[("bad", "not found")],
        ),
    )

    result = transaction.commit(InstallResult(installed_count=1))

    assert result.disposition is InstallDisposition.PARTIAL_SUCCESS
    assert result.exit_code == 0
    assert result.committed is True


@pytest.mark.parametrize(
    "disposition",
    [InstallDisposition.CANCELLED, InstallDisposition.DRY_RUN],
)
def test_non_mutating_dispositions_remain_uncommitted(
    tmp_path: Path,
    disposition: InstallDisposition,
) -> None:
    """Cancellation and dry-run results remain non-mutating and uncommitted."""
    transaction = _transaction(tmp_path)
    result = InstallResult(disposition=disposition)

    transaction.rollback()

    assert result.exit_code == 0
    assert result.committed is False


def test_success_commit_finalizes_resolution(tmp_path: Path) -> None:
    """A successful install finalizes the resolution journal."""
    transaction = _transaction(tmp_path)
    package = transaction.apm_modules_dir / "new-package"
    transaction.resolution.prepare_path(package)
    package.mkdir()

    result = transaction.commit(InstallResult(installed_count=1))

    assert result.disposition is InstallDisposition.SUCCESS
    assert result.committed is True
    assert package.is_dir()
    assert not (transaction.apm_modules_dir / ".apm-resolution-staging").exists()


def test_manifest_restore_is_byte_exact(tmp_path: Path) -> None:
    """Rollback restores the exact bytes captured before validation."""
    transaction = _transaction(tmp_path)
    original = transaction.manifest_path.read_bytes()
    transaction.manifest_path.write_bytes(b"name: changed\n")

    transaction.rollback()

    assert transaction.manifest_path.read_bytes() == original


def test_rollback_removes_new_path_and_restores_existing_path(tmp_path: Path) -> None:
    """Rollback affects only paths prepared by this resolution attempt."""
    transaction = _transaction(tmp_path)
    new_path = transaction.apm_modules_dir / "new"
    existing_path = transaction.apm_modules_dir / "existing"
    existing_path.mkdir()
    (existing_path / "marker").write_text("original", encoding="ascii")

    transaction.resolution.prepare_path(new_path)
    new_path.mkdir()
    transaction.resolution.prepare_path(existing_path)
    existing_path.mkdir()
    (existing_path / "marker").write_text("replacement", encoding="ascii")
    transaction.rollback()

    assert not new_path.exists()
    assert (existing_path / "marker").read_text(encoding="ascii") == "original"


def test_concurrent_duplicate_prepare_is_idempotent(tmp_path: Path) -> None:
    """Concurrent duplicate prepares preserve one original snapshot."""
    transaction = _transaction(tmp_path)
    package = transaction.apm_modules_dir / "package"
    package.mkdir()
    (package / "marker").write_text("original", encoding="ascii")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(transaction.resolution.prepare_path, [package] * 32))
    package.mkdir(exist_ok=True)
    (package / "marker").write_text("replacement", encoding="ascii")
    transaction.rollback()

    assert (package / "marker").read_text(encoding="ascii") == "original"


def test_commit_only_after_cycle_validation(tmp_path: Path) -> None:
    """The resolution journal is finalized only after graph validation."""
    transaction = _transaction(tmp_path)
    cycle_validated = False
    original_commit = transaction.resolution.commit

    def checked_commit() -> None:
        assert cycle_validated
        original_commit()

    transaction.resolution.commit = checked_commit
    cycle_validated = True

    transaction.commit(InstallResult())


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), SystemExit(2), KeyboardInterrupt()],
)
def test_context_rolls_back_base_exceptions(tmp_path: Path, error: BaseException) -> None:
    """Exceptions, process exits, and interruptions all restore staged paths."""
    transaction = _transaction(tmp_path)
    package = transaction.apm_modules_dir / "package"

    with pytest.raises(type(error)):
        with transaction:
            transaction.resolution.prepare_path(package)
            package.mkdir()
            raise error

    assert not package.exists()


def test_fail_rolls_back_and_preserves_error(tmp_path: Path) -> None:
    """Failure returns a structured non-zero result after rollback."""
    transaction = _transaction(tmp_path)
    error = RuntimeError("failed")

    result = transaction.fail(error)

    assert result.disposition is InstallDisposition.FAILED
    assert result.exit_code == 1
    assert result.error is error
    assert result.committed is False


def test_no_cleanup_outside_apm_modules(tmp_path: Path) -> None:
    """The resolution journal rejects paths outside its owned root."""
    transaction = _transaction(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_text("keep", encoding="ascii")

    with pytest.raises(PathTraversalError):
        transaction.resolution.prepare_path(outside)
    transaction.rollback()

    assert (outside / "marker").read_text(encoding="ascii") == "keep"


def test_positional_url_total_failure_exits_one(tmp_path: Path, monkeypatch) -> None:
    """The Click boundary maps a structured total validation failure to 1."""
    (tmp_path / "apm.yml").write_text("name: test\nversion: 1.0.0\n", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    outcome = _ValidationOutcome(
        valid=[],
        invalid=[("https://example.invalid/missing", "not found")],
    )

    with patch(
        "apm_cli.commands.install._validate_and_add_packages_to_apm_yml",
        return_value=([], outcome),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", "https://example.invalid/missing"],
        )

    assert result.exit_code == 1
