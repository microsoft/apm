"""Regression traps for interrupted resolution and denied recovery operations."""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from filelock import FileLock, Timeout

from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.install.transaction import InstallTransaction
from apm_cli.utils.path_security import safe_rmtree
from scripts.windows_native_symlink_probe_entry import probe_native_context
from tests.utils.resolution_failure_probe import hold_path, release_directory

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]


@pytest.mark.parametrize("parent_first", [False, True], ids=["child-first", "parent-first"])
@pytest.mark.parametrize("child_existed", [True, False], ids=["prior-child", "new-child"])
def test_nested_replacement_denied_restore_retry_preserves_original_child_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_first: bool,
    child_existed: bool,
    record_property: Callable[[str, object], None],
    native_denial: bool = False,
) -> None:
    """Denied nested recovery must not consume backups needed by a later retry."""
    modules = tmp_path / "apm_modules"
    parent = modules / "owner" / "repo"
    child = parent / "plugins" / "tool"
    sibling = parent / "plugins" / "sibling"
    unrelated = modules / "other" / "package"
    original = {
        parent: b"original parent",
        sibling: b"original sibling",
        unrelated: b"original unrelated",
    }
    if child_existed:
        original[child] = b"original child\x00\r\n"
    for path, content in original.items():
        path.mkdir(parents=True, exist_ok=True)
        (path / "marker").write_bytes(content)
    staging = ResolutionStagingSession(modules)
    order = (parent, child) if parent_first else (child, parent)
    for path in (unrelated, sibling, *order):
        candidate = staging.prepare_replacement(path)
        candidate.mkdir()
        (candidate / "marker").write_bytes(b"replacement")
        if path == parent:
            nested = candidate / "plugins" / "tool"
            nested.mkdir(parents=True)
            (nested / "marker").write_bytes(b"parent replacement child")
        staging.publish_replacement(candidate)

    denied_path = order[-1]
    denied_backup = staging._backups[denied_path]
    replace = Path.replace
    denied = False

    def deny_first_restore(source: Path, target: Path) -> Path:
        nonlocal denied
        if source == denied_backup and not denied:
            denied = True
            raise PermissionError("nested restoration obstruction")
        return replace(source, target)

    if native_denial:
        context = probe_native_context(
            tmp_path / "native-context", os.environ["APM_WINDOWS_NATIVE_EXPECTED_SID"]
        )
        record_property("native_context", json.dumps(context, sort_keys=True))
        assert denied_backup is not None
        held = hold_path(denied_backup)
        try:
            issues = staging.rollback()
        finally:
            release_directory(held)
        assert any("[WinError 32]" in reason for _, reason in issues)
        record_property(
            "native_recovery_issues",
            json.dumps([(str(path), reason) for path, reason in issues]),
        )
    else:
        monkeypatch.setattr(Path, "replace", deny_first_restore)
        issues = staging.rollback()
        assert denied
        assert any("nested restoration obstruction" in reason for _, reason in issues)
    assert any("deferred recovery" in reason for _, reason in issues)
    assert (unrelated / "marker").read_bytes() == original[unrelated]
    assert staging.rollback() == []
    for path, content in original.items():
        assert (path / "marker").read_bytes() == content
    if not child_existed:
        assert not child.exists()
    assert not staging._staging_root.exists()


def test_denied_nested_restore_defers_ancestor_relocation_until_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ancestor relocation must not invalidate a retained child's journal path."""
    modules = tmp_path / "apm_modules"
    original_owner = modules / "original"
    moved_owner = modules / "moved"
    original_child = original_owner / "child"
    original_child.mkdir(parents=True)
    (original_child / "marker").write_bytes(b"original child")
    independent_source = modules / "separate-original"
    independent_destination = modules / "separate-moved"
    independent_source.mkdir()
    (independent_source / "marker").write_bytes(b"independent")
    staging = ResolutionStagingSession(modules)
    staging.relocate_path(independent_source, independent_destination)
    staging.relocate_path(original_owner, moved_owner)
    child = moved_owner / "child"
    candidate = staging.prepare_replacement(child)
    candidate.mkdir()
    (candidate / "marker").write_bytes(b"replacement")
    staging.publish_replacement(candidate)
    backup = staging._backups[child]
    replace = Path.replace

    def denied_restore(source: Path, target: Path) -> Path:
        if source == backup:
            raise PermissionError("nested restore before relocation")
        return replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", denied_restore)
        issues = staging.rollback()
    assert any("nested restore before relocation" in reason for _, reason in issues)
    assert (independent_source / "marker").read_bytes() == b"independent"
    assert not independent_destination.exists()
    assert not original_owner.exists()
    assert staging.rollback() == []
    assert (original_child / "marker").read_bytes() == b"original child"
    assert not moved_owner.exists()


def test_three_level_recovery_preserves_bytes_across_successive_denials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial progress across retries must preserve the remaining nested journal."""
    modules = tmp_path / "apm_modules"
    root = modules / "owner" / "repo"
    middle = root / "nested"
    leaf = middle / "leaf"
    paths = (root, middle, leaf)
    original = {path: f"original {index}".encode() for index, path in enumerate(paths)}
    for path, content in original.items():
        path.mkdir(parents=True, exist_ok=True)
        (path / "marker").write_bytes(content)
    staging = ResolutionStagingSession(modules)
    for path in reversed(paths):
        candidate = staging.prepare_replacement(path)
        candidate.mkdir()
        (candidate / "marker").write_bytes(b"replacement")
        staging.publish_replacement(candidate)
    replace = Path.replace
    denied_backup: Path | None = None

    def deny_selected_backup(source: Path, target: Path) -> Path:
        if source == denied_backup:
            raise PermissionError("successive nested obstruction")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", deny_selected_backup)
    for path in paths:
        denied_backup = staging._backups[path]
        assert denied_backup is not None and denied_backup.exists()
        issues = staging.rollback()
        assert any("successive nested obstruction" in reason for _, reason in issues)
    denied_backup = None
    assert staging.rollback() == []
    for path, content in original.items():
        assert (path / "marker").read_bytes() == content
    assert not staging._staging_root.exists()


def test_failed_directory_removal_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readonly fallback must not swallow a persistent removal denial."""
    staging = tmp_path / "staging"
    staging.mkdir()
    original_rmdir = os.rmdir

    def denied_rmdir(path: str, *args: object, **kwargs: object) -> None:
        if Path(path) == staging:
            raise PermissionError(errno.EACCES, "directory removal obstruction", str(path))
        original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", denied_rmdir)
    with pytest.raises(PermissionError, match="directory removal obstruction"):
        safe_rmtree(staging, tmp_path)
    assert staging.is_dir()


def test_cleanup_denial_preserves_original_failure_and_restores_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second filesystem failure must not replace the failed install's cause."""
    manifest = tmp_path / "apm.yml"
    original = b"name: original\r\nversion: 1.0.0\r\n"
    manifest.write_bytes(original)
    logger = MagicMock()
    transaction = InstallTransaction(
        manifest_path=manifest,
        apm_modules_dir=tmp_path / "apm_modules",
        validation=None,
        logger=logger,
        acquire_lock=False,
    )
    candidate = transaction.resolution.prepare_replacement(
        tmp_path / "apm_modules" / "owner" / "package"
    )
    candidate.mkdir()
    (candidate / "partial").write_bytes(b"downloaded candidate")
    manifest.write_bytes(b"name: modified\n")

    def denied_cleanup() -> None:
        raise PermissionError("cleanup obstruction")

    monkeypatch.setattr(transaction.resolution, "_remove_staging_root", denied_cleanup)
    with pytest.raises(RuntimeError, match="original activation obstruction"):
        with transaction:
            raise RuntimeError("original activation obstruction")

    assert manifest.read_bytes() == original
    assert (candidate / "partial").read_bytes() == b"downloaded candidate"
    diagnostics = " ".join(str(arg) for call in logger.method_calls for arg in call.args)
    assert "cleanup obstruction" in diagnostics
    assert str(candidate.parent.parent) in diagnostics


def test_failed_immediate_restore_keeps_both_errors_and_prior_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native denials are supplemented here by exact operation fault injection."""
    staging = ResolutionStagingSession(tmp_path / "apm_modules")
    live = tmp_path / "apm_modules" / "owner" / "package"
    live.mkdir(parents=True)
    (live / "marker").write_bytes(b"prior install")
    candidate = staging.prepare_replacement(live)
    candidate.mkdir()
    (candidate / "marker").write_bytes(b"candidate")
    replace = Path.replace

    def deny_activation_and_restore(path: Path, target: Path) -> Path:
        if path == candidate:
            raise PermissionError("activation obstruction")
        if path.parent.name == "backups":
            raise PermissionError("restoration obstruction")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", deny_activation_and_restore)
    with pytest.raises(OSError) as caught:
        staging.publish_replacement(candidate)

    message = str(caught.value)
    assert "activation obstruction" in message
    assert "restoration obstruction" in message
    backups = list(staging._staging_root.glob("backups/*/marker"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"prior install"
    monkeypatch.setattr(Path, "replace", replace)
    staging.rollback()
    assert (live / "marker").read_bytes() == b"prior install"


def test_prepared_replacement_holds_activity_lock_until_rollback(tmp_path: Path) -> None:
    """A candidate in use is not a lockless legacy root or abandoned backup."""
    staging = ResolutionStagingSession(tmp_path / "apm_modules")
    candidate = staging.prepare_replacement(tmp_path / "apm_modules" / "owner" / "package")
    candidate.mkdir()
    (candidate / "marker").write_bytes(b"active candidate")
    try:
        with pytest.raises(Timeout):
            with FileLock(str(staging._staging_lock_path)).acquire(timeout=0):
                pass
        other = ResolutionStagingSession(tmp_path / "apm_modules")
        assert other.remove_abandoned_roots() == []
        assert (candidate / "marker").read_bytes() == b"active candidate"
    finally:
        staging.rollback()
    assert not candidate.exists()


def test_worker_preparation_releases_activity_lock_in_completing_thread(tmp_path: Path) -> None:
    """Parallel resolution and main-thread completion share one activity lock."""
    staging = ResolutionStagingSession(tmp_path / "apm_modules")
    with ThreadPoolExecutor(max_workers=1) as executor:
        candidate = executor.submit(
            staging.prepare_replacement, tmp_path / "apm_modules" / "owner" / "package"
        ).result()
    candidate.mkdir()
    lock = staging._staging_lock
    assert lock is not None and lock.is_locked
    staging.rollback()
    assert not lock.is_locked
    assert not staging._staging_lock_path.exists()


def test_denied_rollback_preserves_backups_and_restores_other_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Later cleanup must not mistake an unlocked recovery backup for garbage."""
    from apm_cli.install import resolution_staging

    modules = tmp_path / "apm_modules"
    staging = ResolutionStagingSession(modules)
    paths = [modules / name for name in ("first", "second")]
    for path in paths:
        path.mkdir(parents=True)
        (path / "marker").write_bytes(b"prior")
        staging.prepare_path(path)
        path.mkdir()
        (path / "marker").write_bytes(b"candidate")
    remove = resolution_staging.safe_rmtree

    def deny_one(path: Path, root: Path) -> None:
        if path == paths[1]:
            raise PermissionError("locked replacement")
        remove(path, root)

    monkeypatch.setattr(resolution_staging, "safe_rmtree", deny_one)
    issues = staging.rollback()
    assert any("locked replacement" in reason for _, reason in issues)
    assert (paths[0] / "marker").read_bytes() == b"prior"
    backup = staging._backups[paths[1]]
    assert backup is not None
    assert (backup / "marker").read_bytes() == b"prior"
    staging._staging_lock_path.touch()
    later = ResolutionStagingSession(modules)
    assert later.remove_abandoned_roots()
    assert (backup / "marker").read_bytes() == b"prior"
    monkeypatch.setattr(resolution_staging, "safe_rmtree", remove)
    staging.rollback()
    assert (paths[1] / "marker").read_bytes() == b"prior"


def test_commit_cleanup_failure_never_restores_a_partially_deleted_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disposal after successful validation must not reopen the rollback journal."""
    from apm_cli.models.results import InstallResult

    logger = MagicMock()
    transaction = InstallTransaction(
        manifest_path=tmp_path / "apm.yml",
        apm_modules_dir=tmp_path / "apm_modules",
        validation=None,
        logger=logger,
        acquire_lock=False,
    )
    live = transaction.apm_modules_dir / "package"
    live.mkdir(parents=True)
    (live / "marker").write_bytes(b"prior")
    (live / "other").write_bytes(b"prior additional data")
    staged = transaction.resolution.prepare_replacement(live)
    staged.mkdir()
    (staged / "marker").write_bytes(b"validated replacement")
    transaction.resolution.publish_replacement(staged)
    backup = transaction.resolution._backups[live]
    assert backup is not None

    def partially_remove() -> None:
        (backup / "marker").unlink()
        raise PermissionError("committed backup cleanup denied")

    monkeypatch.setattr(transaction.resolution, "_remove_staging_root", partially_remove)
    result = transaction.commit(InstallResult())
    transaction.rollback()
    assert result.committed
    assert (live / "marker").read_bytes() == b"validated replacement"
    assert (backup / "other").read_bytes() == b"prior additional data"
    assert "committed backup cleanup denied" in logger.warning.call_args.args[0]


def test_manifest_restoration_denial_retains_byte_exact_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denied atomic replacement must not discard the only original manifest."""
    manifest = tmp_path / "apm.yml"
    original = b"name: original\r\nversion: 1.0.0\r\n"
    manifest.write_bytes(original)
    logger = MagicMock()
    transaction = InstallTransaction(
        manifest_path=manifest,
        apm_modules_dir=tmp_path / "apm_modules",
        validation=None,
        logger=logger,
        acquire_lock=False,
    )
    manifest.write_bytes(b"name: changed\n")

    def denied_replace(source: str, destination: Path) -> None:
        raise PermissionError("manifest is locked")

    monkeypatch.setattr(os, "replace", denied_replace)
    with pytest.raises(RuntimeError, match="original install failure"):
        with transaction:
            raise RuntimeError("original install failure")
    snapshots = list(tmp_path.glob("apm-restore-*"))
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == original
    warning = logger.warning.call_args.args[0]
    assert "manifest is locked" in warning
    assert str(snapshots[0]) in warning


def test_denied_relocation_restore_continues_remaining_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denied relocation does not hide the error or stop independent recovery."""
    modules = tmp_path / "apm_modules"
    staging = ResolutionStagingSession(modules)
    sources = [modules / name for name in ("first", "second")]
    destinations = [modules / name for name in ("moved-first", "moved-second")]
    for source, destination in zip(sources, destinations, strict=True):
        source.mkdir(parents=True)
        (source / "marker").write_bytes(b"prior")
        staging.relocate_path(source, destination)
    replace = Path.replace

    def deny_one(path: Path, target: Path) -> Path:
        if path == destinations[1]:
            raise PermissionError("relocation restore denied")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", deny_one)
    issues = staging.rollback()
    assert any("relocation restore denied" in reason for _, reason in issues)
    assert (sources[0] / "marker").read_bytes() == b"prior"
    assert (destinations[1] / "marker").read_bytes() == b"prior"
    monkeypatch.setattr(Path, "replace", replace)
    assert staging.rollback() == []
    assert (sources[1] / "marker").read_bytes() == b"prior"


def test_partial_directory_removal_finishes_independent_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report the denied child after removing independent writable children."""
    root = tmp_path / "partial"
    root.mkdir()
    for name in ("first", "second"):
        (root / name).write_bytes(b"preserve")
    denied_names: list[str] = []
    unlink = os.unlink

    def deny_one(path: str, *args: object, **kwargs: object) -> None:
        name = Path(path).name
        if not denied_names:
            denied_names.append(name)
        if name == denied_names[0]:
            raise PermissionError(errno.EACCES, "child removal denied", str(path))
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", deny_one)
    with pytest.raises(PermissionError, match="child removal denied"):
        safe_rmtree(root, tmp_path)
    root.chmod(0o700)
    denied = root / denied_names[0]
    denied.chmod(0o600)
    assert denied.read_bytes() == b"preserve"
    assert list(root.iterdir()) == [denied]
