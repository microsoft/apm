"""lockfile-write-authority owner: when apm.lock.yaml reaches disk, and by whom (#4121)."""

from __future__ import annotations

import os
from pathlib import Path

import click
import pytest

from apm_cli.deps.lockfile_write_authority import (
    LockfileCommitError,
    LockfileWriter,
    after_lockfile_commit,
    flush_lockfile_writes,
    has_staged_lockfile,
    lockfile_exists,
    lockfile_write_scope,
    observe_lockfile_writers,
    provisional_lockfile_writes,
    remove_lockfile,
    restore_lockfile_bytes,
    stage_lockfile,
    staged_lockfile_text,
)
from apm_cli.install.errors import PolicyViolationError

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-lockfile-write-authority")]

DOC_A = "lockfile_version: '4'\ndependencies: []\n"
DOC_B = "lockfile_version: '4'\ndependencies:\n- repo_url: acme/x\n"


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    return tmp_path / "apm.lock.yaml"


# -- outside a writer --------------------------------------------------------------


def test_outside_a_writer_nothing_is_staged(lock: Path) -> None:
    assert stage_lockfile(lock, DOC_A) is False
    assert has_staged_lockfile(lock) is None
    assert staged_lockfile_text(lock) is None
    lock.write_text(DOC_B, encoding="utf-8")
    assert staged_lockfile_text(lock) == DOC_B
    assert lockfile_exists(lock) is True


def test_outside_a_writer_remove_and_restore_hit_disk(lock: Path) -> None:
    lock.write_text(DOC_A, encoding="utf-8")
    remove_lockfile(lock)
    assert not lock.exists()
    remove_lockfile(lock)  # missing is fine
    restore_lockfile_bytes(lock, DOC_B.encode())
    assert lock.read_bytes() == DOC_B.encode()


def test_outside_a_writer_after_commit_runs_at_once_and_flush_is_a_no_op(lock: Path) -> None:
    ran: list[str] = []
    after_lockfile_commit(lambda: ran.append("now"))
    flush_lockfile_writes()
    assert ran == ["now"]
    assert not lock.exists()


# -- staging inside a writer -----------------------------------------------------


def test_a_writer_stages_then_commits_once_on_success(lock: Path) -> None:
    ran: list[str] = []
    with lockfile_write_scope(LockfileWriter.INSTALL):
        assert stage_lockfile(lock, DOC_A) is True
        assert stage_lockfile(lock, DOC_B) is True
        assert not lock.exists()
        assert has_staged_lockfile(lock) is True
        assert staged_lockfile_text(lock) == DOC_B
        assert lockfile_exists(lock) is True
        after_lockfile_commit(lambda: ran.append(lock.read_text(encoding="utf-8")))
        assert ran == []
    assert lock.read_text(encoding="utf-8") == DOC_B
    assert ran == [DOC_B]


def test_a_writer_that_stages_nothing_writes_nothing(lock: Path) -> None:
    ran: list[str] = []
    with lockfile_write_scope(LockfileWriter.LOCK):
        assert has_staged_lockfile(lock) is None
        after_lockfile_commit(lambda: ran.append("now"))
        assert ran == ["now"]
    assert not lock.exists()


def _other_spellings(lock: Path) -> list[Path]:
    """Spellings of *lock* that only an absolute, normalized key folds together."""
    nested = lock.parent / "nested"
    nested.mkdir(exist_ok=True)
    spellings = [nested / ".." / lock.name]
    # A relative spelling exists only on the working directory's drive (Windows).
    if Path.cwd().drive == lock.drive:
        spellings.append(Path(os.path.relpath(lock)))
    return spellings


def test_staged_reads_are_keyed_by_absolute_path(lock: Path) -> None:
    for spelling in _other_spellings(lock):
        with lockfile_write_scope(LockfileWriter.INSTALL):
            stage_lockfile(spelling, DOC_A)
            assert staged_lockfile_text(lock) == DOC_A
            assert has_staged_lockfile(lock) is True
        assert lock.read_text(encoding="utf-8") == DOC_A
        lock.unlink()


def test_an_unstaged_path_in_a_writer_reads_disk(lock: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.lock.yaml"
    other.write_text(DOC_B, encoding="utf-8")
    with lockfile_write_scope(LockfileWriter.INSTALL):
        stage_lockfile(lock, DOC_A)
        assert has_staged_lockfile(other) is None
        assert staged_lockfile_text(other) == DOC_B
        assert lockfile_exists(other) is True
        assert lockfile_exists(tmp_path / "missing.yaml") is False


def test_a_removed_lockfile_stays_removed_for_the_rest_of_the_command(lock: Path) -> None:
    lock.write_text(DOC_A, encoding="utf-8")
    with lockfile_write_scope(LockfileWriter.UNINSTALL):
        stage_lockfile(lock, DOC_B)
        remove_lockfile(lock)
        assert not lock.exists()
        assert has_staged_lockfile(lock) is False
        assert staged_lockfile_text(lock) is None
        assert lockfile_exists(lock) is False
    assert not lock.exists()


def test_a_removal_opens_the_scope_and_runs_after_commit_actions(lock: Path) -> None:
    lock.write_text(DOC_A, encoding="utf-8")
    ran: list[bool] = []
    with lockfile_write_scope(LockfileWriter.PRUNE):
        remove_lockfile(lock)
        after_lockfile_commit(lambda: ran.append(lock.exists()))
        assert ran == []
    assert ran == [False]


def test_a_restore_discards_what_was_staged(lock: Path) -> None:
    with lockfile_write_scope(LockfileWriter.INSTALL):
        stage_lockfile(lock, DOC_B)
        restore_lockfile_bytes(lock, DOC_A.encode())
        assert has_staged_lockfile(lock) is None
        assert staged_lockfile_text(lock) == DOC_A
    assert lock.read_text(encoding="utf-8") == DOC_A


def test_restore_inside_a_writer_needs_nothing_staged_for_that_path(
    lock: Path, tmp_path: Path
) -> None:
    with lockfile_write_scope(LockfileWriter.INSTALL):
        stage_lockfile(tmp_path / "other.yaml", DOC_B)
        restore_lockfile_bytes(lock, DOC_A.encode())
    assert lock.read_text(encoding="utf-8") == DOC_A


def test_reads_alone_never_turn_a_failure_into_a_commit(lock: Path) -> None:
    with pytest.raises(RuntimeError), lockfile_write_scope(LockfileWriter.INSTALL):
        with provisional_lockfile_writes():
            stage_lockfile(lock, DOC_A)
        staged_lockfile_text(lock)
        lockfile_exists(lock)
        raise RuntimeError("failed before changing state")
    assert not lock.exists()


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_a_new_lockfile_gets_the_umask_mode_not_a_private_one(lock: Path) -> None:
    previous = os.umask(0o022)
    try:
        with lockfile_write_scope(LockfileWriter.INSTALL):
            stage_lockfile(lock, DOC_A)
    finally:
        os.umask(previous)
    assert lock.stat().st_mode & 0o777 == 0o644


def test_restore_keeps_bytes_exactly(lock: Path) -> None:
    restore_lockfile_bytes(lock, b"a: 1\r\nb: 2\r\n")
    assert lock.read_bytes() == b"a: 1\r\nb: 2\r\n"


def test_a_flush_commits_early_and_staging_continues(lock: Path) -> None:
    ran: list[str] = []
    with lockfile_write_scope(LockfileWriter.INSTALL):
        stage_lockfile(lock, DOC_A)
        after_lockfile_commit(lambda: ran.append("first"))
        flush_lockfile_writes()
        assert lock.read_text(encoding="utf-8") == DOC_A
        assert ran == ["first"]
        assert has_staged_lockfile(lock) is None
        stage_lockfile(lock, DOC_B)
        assert lock.read_text(encoding="utf-8") == DOC_A
    assert lock.read_text(encoding="utf-8") == DOC_B
    assert ran == ["first"]


def test_commits_follow_the_order_of_the_latest_write(tmp_path: Path) -> None:
    first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
    order: list[str] = []

    def record(path: Path, *_args: object, **_kwargs: object) -> None:
        order.append(Path(path).name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("apm_cli.utils.atomic_io.atomic_write_text", record)
        with lockfile_write_scope(LockfileWriter.INSTALL):
            stage_lockfile(first, DOC_A)
            stage_lockfile(second, DOC_A)
            stage_lockfile(first, DOC_B)
    assert order == ["b.yaml", "a.yaml"]


def test_an_unchanged_document_is_not_rewritten(lock: Path) -> None:
    on_disk = "lockfile_version: '4'   # keep this comment\ndependencies: []\n"
    lock.write_text(on_disk, encoding="utf-8")
    with lockfile_write_scope(LockfileWriter.INSTALL):
        stage_lockfile(lock, DOC_A)
    assert lock.read_text(encoding="utf-8") == on_disk


def test_a_commit_refuses_a_symlinked_lockfile(lock: Path, tmp_path: Path) -> None:
    target = tmp_path / "real.yaml"
    target.write_text(DOC_B, encoding="utf-8")
    lock.symlink_to(target)
    with pytest.raises(OSError), lockfile_write_scope(LockfileWriter.INSTALL):
        stage_lockfile(lock, DOC_A)
    assert target.read_text(encoding="utf-8") == DOC_B


def test_a_failed_commit_names_the_lockfile(lock: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr("apm_cli.utils.atomic_io.atomic_write_text", fail)
    ran: list[str] = []
    with pytest.raises(LockfileCommitError) as raised:
        with lockfile_write_scope(LockfileWriter.INSTALL):
            stage_lockfile(lock, DOC_A)
            after_lockfile_commit(lambda: ran.append("never"))
    assert str(raised.value) == "Could not generate apm.lock.yaml: read-only"
    assert ran == []


# -- commit or discard on failure ---------------------------------------------------


class _Refusal(RuntimeError):
    refuses_commit = True


def _wrapped(error: BaseException, *, cause: bool) -> BaseException:
    try:
        try:
            raise error
        except BaseException as inner:
            if cause:
                raise RuntimeError("rendered") from inner
            raise RuntimeError("rendered")  # noqa: B904 - exercise __context__
    except RuntimeError as outer:
        return outer


@pytest.mark.parametrize(
    ("error", "commits"),
    [
        (RuntimeError("deploy failed"), True),
        (SystemExit(1), True),
        (click.exceptions.Exit(1), True),
        (KeyboardInterrupt(), False),
        (_Refusal("blocked"), False),
        (PolicyViolationError("blocked"), False),
        (_wrapped(_Refusal("blocked"), cause=True), False),
        (_wrapped(_Refusal("blocked"), cause=False), False),
        (_wrapped(ValueError("other"), cause=True), True),
    ],
)
def test_a_failed_command_commits_only_unrefused_state_changes(
    lock: Path, error: BaseException, commits: bool
) -> None:
    with pytest.raises(type(error)), lockfile_write_scope(LockfileWriter.INSTALL):
        stage_lockfile(lock, DOC_A)
        raise error
    assert lock.exists() is commits


def _commits_within(error: BaseException, seconds: float = 0.2) -> bool:
    """Whether a staged write commits after *error*; fails instead of hanging on a cycle."""
    import threading

    from apm_cli.deps.lockfile_write_authority import _commits

    result: list[bool] = []
    worker = threading.Thread(target=lambda: result.append(_commits(error)), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "walking the cause chain did not terminate"
    return result[0]


def test_a_cause_cycle_terminates_and_still_commits() -> None:
    error = RuntimeError("a")
    other = RuntimeError("b")
    error.__cause__ = other
    other.__context__ = error
    assert _commits_within(error) is True


def test_a_refusal_behind_a_self_cycle_is_still_found() -> None:
    error = RuntimeError("wrapper")
    error.__cause__ = _Refusal("blocked")
    error.__context__ = error
    assert _commits_within(error) is False


def test_a_failure_before_any_write_commits_nothing(lock: Path) -> None:
    with pytest.raises(RuntimeError), lockfile_write_scope(LockfileWriter.INSTALL):
        raise RuntimeError("refused early")
    assert not lock.exists()


def test_provisional_corrections_alone_do_not_commit_on_failure(lock: Path) -> None:
    with pytest.raises(RuntimeError), lockfile_write_scope(LockfileWriter.INSTALL):
        with provisional_lockfile_writes():
            stage_lockfile(lock, DOC_A)
        raise RuntimeError("failed before changing state")
    assert not lock.exists()


def test_provisional_corrections_commit_with_a_later_state_change(
    lock: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other.yaml"
    with pytest.raises(RuntimeError), lockfile_write_scope(LockfileWriter.INSTALL):
        with provisional_lockfile_writes():
            stage_lockfile(lock, DOC_A)
        stage_lockfile(other, DOC_B)
        raise RuntimeError("failed after changing state")
    assert lock.read_text(encoding="utf-8") == DOC_A
    assert other.read_text(encoding="utf-8") == DOC_B


def test_provisional_corrections_commit_on_success(lock: Path) -> None:
    with lockfile_write_scope(LockfileWriter.INSTALL):
        with provisional_lockfile_writes():
            stage_lockfile(lock, DOC_A)
    assert lock.read_text(encoding="utf-8") == DOC_A


# -- writers --------------------------------------------------------------------------


def test_only_registered_writers_open_a_scope() -> None:
    with pytest.raises(TypeError, match="is not a registered lockfile writer"):
        with lockfile_write_scope("install"):  # type: ignore[arg-type]
            pass


def test_a_nested_writer_joins_the_outer_scope(lock: Path) -> None:
    with observe_lockfile_writers() as seen:
        with lockfile_write_scope(LockfileWriter.UPDATE):
            with lockfile_write_scope(LockfileWriter.INSTALL):
                stage_lockfile(lock, DOC_A)
            assert not lock.exists()
            stage_lockfile(lock, DOC_B)
    assert lock.read_text(encoding="utf-8") == DOC_B
    assert seen.declared == [LockfileWriter.UPDATE]
    assert seen.opened == [LockfileWriter.UPDATE]


def test_a_scope_opens_once_at_its_first_write(lock: Path) -> None:
    with observe_lockfile_writers() as seen:
        with lockfile_write_scope(LockfileWriter.INSTALL):
            staged_lockfile_text(lock)
            lockfile_exists(lock)
            has_staged_lockfile(lock)
            after_lockfile_commit(lambda: None)
            flush_lockfile_writes()
            restore_lockfile_bytes(lock, DOC_A.encode())
            assert seen.opened == []
            stage_lockfile(lock, DOC_A)
            stage_lockfile(lock, DOC_B)
            remove_lockfile(lock)
    assert seen.opened == [LockfileWriter.INSTALL]
