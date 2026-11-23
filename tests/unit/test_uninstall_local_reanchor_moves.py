"""An interrupted re-anchor materialization move restores every directory (#4999)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.commands.uninstall.local_reanchor import (
    move_materializations,
    restore_materializations,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I3", "P-INV-I1")]


def _package(root: Path, name: str) -> Path:
    path = root / "_local" / name
    path.mkdir(parents=True)
    (path / "apm.yml").write_text(f"name: {name}\n", encoding="utf-8")
    return path


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_an_interrupted_move_puts_every_moved_directory_back(tmp_path: Path) -> None:
    """The second move fails (its parent is a file): the first one is undone."""
    modules = tmp_path / "apm_modules"
    first, second = _package(modules, "a"), _package(modules, "b")
    (modules / "_local" / "blocker").write_text("not a directory", encoding="utf-8")
    before = _tree(modules)
    moves = (
        (first, modules / "_local" / "h1" / "a"),
        (second, modules / "_local" / "blocker" / "b"),
    )

    with pytest.raises(OSError):
        move_materializations(moves, modules)

    assert _tree(modules) == before
    assert not (modules / "_local" / "h1").exists()


def test_a_completed_move_is_restored_on_a_later_failure(tmp_path: Path) -> None:
    """A later step of the uninstall fails: restoring puts each directory back."""
    modules = tmp_path / "apm_modules"
    first = _package(modules, "a")
    before = _tree(modules)

    moved = move_materializations(((first, modules / "_local" / "h1" / "a"),), modules)
    assert (modules / "_local" / "h1" / "a" / "apm.yml").is_file()
    assert not first.exists()

    restore_materializations(moved, modules)
    assert _tree(modules) == before
    assert not (modules / "_local" / "h1").exists()


def test_a_move_outside_apm_modules_is_refused_before_any_move(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    first = _package(modules, "a")
    before = _tree(modules)

    with pytest.raises(ValueError, match="apm_modules"):
        move_materializations(((first, tmp_path / "elsewhere" / "a"),), modules)

    assert _tree(modules) == before
    assert not (tmp_path / "elsewhere").exists()


def test_a_failed_rename_removes_only_the_parents_it_created(tmp_path: Path) -> None:
    """mkdir succeeds, the rename fails: the parent it made goes, an existing one stays."""
    modules = tmp_path / "apm_modules"
    first = _package(modules, "a")
    (modules / "_local" / "kept-empty").mkdir()
    before = _tree(modules)
    moves = (
        (first, modules / "_local" / "kept-empty" / "a"),
        (modules / "_local" / "missing", modules / "_local" / "h2" / "x"),
    )

    with pytest.raises(OSError):
        move_materializations(moves, modules)

    assert _tree(modules) == before
    assert not (modules / "_local" / "h2").exists()
    assert (modules / "_local" / "kept-empty").is_dir()


def test_a_save_that_raises_after_the_move_rolls_everything_back(tmp_path: Path) -> None:
    """Binding raises after the move and the save: directory, apm.yml and lock restored."""
    from unittest.mock import MagicMock

    from apm_cli.commands.uninstall.cli import (
        _persist_lock_then_resync_native,
        _StillNeededRepair,
    )
    from apm_cli.commands.uninstall.engine import IntegrationCleanupOutcome
    from apm_cli.commands.uninstall.local_reanchor import (
        LocalReanchorPlan,
        snapshot_for_rollback,
    )
    from apm_cli.deps.lockfile import LockFile

    modules = tmp_path / "apm_modules"
    current = _package(modules, "e")
    manifest, lock = tmp_path / "apm.yml", tmp_path / "apm.lock.yaml"
    manifest.write_text("name: proj\n", encoding="utf-8")
    lock.write_text("lockfile_version: '4'\n", encoding="utf-8")
    plan = snapshot_for_rollback(
        LocalReanchorPlan(
            renames={"./vendor/e": "local:vendor/e"},
            moves=((current, modules / "_local" / "h1" / "e"),),
        ),
        (manifest, lock),
    )
    before = (_tree(modules), manifest.read_bytes(), lock.read_bytes())
    manifest.write_text("name: changed\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        _persist_lock_then_resync_native(
            lock_changed=True,
            persist_allowed=True,
            lockfile=LockFile(),
            lockfile_path=lock,
            logger=MagicMock(),
            integration_cleanup=IntegrationCleanupOutcome(
                counts={},
                deployed_files={},
                failed_paths=[],
                error_count=0,
                # The lock saves, then binding its receipt claims raises.
                materialization_session=MagicMock(
                    bind_committed_claims=MagicMock(side_effect=RuntimeError("binding failed"))
                ),
            ),
            deploy_root=tmp_path,
            modules_dir=modules,
            scope=None,
            native_before=None,
            still_needed_repairs=_StillNeededRepair({}, {}, {}, frozenset(), plan),
        )

    assert exc.value.code == 1
    assert (_tree(modules), manifest.read_bytes(), lock.read_bytes()) == before


def test_a_receipt_rename_that_raises_leaves_the_tree_as_it_was(
    tmp_path: Path, monkeypatch
) -> None:
    """The lock commits, then the receipt rename raises RuntimeError: everything goes back."""
    from unittest.mock import MagicMock

    from apm_cli.commands.uninstall.cli import (
        _persist_lock_then_resync_native,
        _StillNeededRepair,
    )
    from apm_cli.commands.uninstall.engine import IntegrationCleanupOutcome
    from apm_cli.commands.uninstall.local_reanchor import (
        LocalReanchorPlan,
        snapshot_for_rollback,
    )
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    modules = tmp_path / "apm_modules"
    current = _package(modules, "e")
    manifest, lock_path = tmp_path / "apm.yml", tmp_path / "apm.lock.yaml"
    manifest.write_text("name: proj\n", encoding="utf-8")
    lock_path.write_text("lockfile_version: '4'\n", encoding="utf-8")
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="acme/e", depth=2))
    (new_key,) = lock.dependencies
    plan = snapshot_for_rollback(
        LocalReanchorPlan(
            renames={"./vendor/e": new_key},
            moves=((current, modules / "_local" / "h1" / "e"),),
        ),
        (manifest, lock_path),
    )
    before = (_tree(modules), manifest.read_bytes(), lock_path.read_bytes())

    def _boom(*_args, **_kwargs):
        raise RuntimeError("receipt store unavailable")

    monkeypatch.setattr("apm_cli.core.deployment_reanchor.rename_receipt_owners", _boom)

    with pytest.raises(SystemExit) as exc:
        _persist_lock_then_resync_native(
            lock_changed=True,
            persist_allowed=True,
            lockfile=lock,
            lockfile_path=lock_path,
            logger=MagicMock(),
            integration_cleanup=IntegrationCleanupOutcome(
                counts={}, deployed_files={}, failed_paths=[], error_count=0
            ),
            deploy_root=tmp_path,
            modules_dir=modules,
            scope=None,
            native_before=None,
            still_needed_repairs=_StillNeededRepair({}, {}, {}, frozenset(), plan),
        )

    assert exc.value.code == 1
    assert (_tree(modules), manifest.read_bytes(), lock_path.read_bytes()) == before


class _InterruptedPath(type(Path())):
    """A directory whose move is cancelled (Ctrl-C) mid-rename."""

    def rename(self, target):
        raise KeyboardInterrupt


def test_an_interrupted_rename_cleans_up_before_cancelling(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    first, second = _package(modules, "a"), _package(modules, "b")
    before = _tree(modules)
    moves = (
        (first, modules / "_local" / "h1" / "a"),
        (_InterruptedPath(second), modules / "_local" / "h2" / "b"),
    )

    with pytest.raises(KeyboardInterrupt):
        move_materializations(moves, modules)

    assert _tree(modules) == before
    assert not (modules / "_local" / "h1").exists()
    assert not (modules / "_local" / "h2").exists()


def _reanchor_call(tmp_path: Path, **overrides):
    """Run the persist step of a re-anchor whose directory moves; return the roots to compare."""
    from unittest.mock import MagicMock

    from apm_cli.commands.uninstall.cli import (
        _persist_lock_then_resync_native,
        _StillNeededRepair,
    )
    from apm_cli.commands.uninstall.engine import IntegrationCleanupOutcome
    from apm_cli.commands.uninstall.local_reanchor import (
        LocalReanchorPlan,
        snapshot_for_rollback,
    )
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    modules = tmp_path / "apm_modules"
    current = _package(modules, "e")
    manifest, lock_path = tmp_path / "apm.yml", tmp_path / "apm.lock.yaml"
    manifest.write_text("name: proj\n", encoding="utf-8")
    lock_path.write_text("lockfile_version: '4'\n", encoding="utf-8")
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="acme/e", depth=2))
    (new_key,) = lock.dependencies
    plan = snapshot_for_rollback(
        LocalReanchorPlan(
            renames={"./vendor/e": new_key},
            moves=((current, modules / "_local" / "h1" / "e"),),
        ),
        (manifest, lock_path),
    )
    before = (_tree(modules), manifest.read_bytes(), lock_path.read_bytes())
    manifest.write_text("name: changed\n", encoding="utf-8")
    cleanup = IntegrationCleanupOutcome(
        counts={}, deployed_files={}, failed_paths=[], error_count=0, **overrides
    )

    def run() -> None:
        _persist_lock_then_resync_native(
            lock_changed=True,
            persist_allowed=True,
            lockfile=lock,
            lockfile_path=lock_path,
            logger=MagicMock(),
            integration_cleanup=cleanup,
            deploy_root=tmp_path,
            modules_dir=modules,
            scope=None,
            native_before=None,
            still_needed_repairs=_StillNeededRepair({}, {}, {}, frozenset(), plan),
        )

    def after():
        return (_tree(modules), manifest.read_bytes(), lock_path.read_bytes())

    return run, before, after


def test_an_interrupted_receipt_binding_restores_everything(tmp_path: Path) -> None:
    """Ctrl-C while binding claims after the save: dirs, apm.yml and lock all go back."""
    from unittest.mock import MagicMock

    session = MagicMock(bind_committed_claims=MagicMock(side_effect=KeyboardInterrupt))
    run, before, after = _reanchor_call(tmp_path, materialization_session=session)

    with pytest.raises(KeyboardInterrupt):
        run()

    assert after() == before


def test_an_interrupted_receipt_rename_restores_everything(tmp_path: Path, monkeypatch) -> None:
    """Ctrl-C while renaming receipt owners after the save: everything goes back."""

    def _interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("apm_cli.core.deployment_reanchor.rename_receipt_owners", _interrupt)
    run, before, after = _reanchor_call(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        run()

    assert after() == before


def test_a_missing_copy_refuses_even_when_the_install_path_does_not_change(
    tmp_path: Path,
) -> None:
    """Same alias on both edges, same install path, no copy: install would recreate it."""
    from types import SimpleNamespace

    from apm_cli.commands.uninstall.local_reanchor import (
        LocalReanchorRefused,
        _materialization_moves,
    )

    modules = tmp_path / "apm_modules"
    path = modules / "_local" / "e"
    ref = SimpleNamespace(get_install_path=lambda _modules: path)
    lockfile = SimpleNamespace(
        get_dependency=lambda _key: SimpleNamespace(to_dependency_ref=lambda: ref)
    )

    with pytest.raises(LocalReanchorRefused, match="is missing"):
        _materialization_moves(lockfile, {"./vendor/e": ref}, modules, lambda key: key)


class _InterruptedAfterRename(type(Path())):
    """The rename completes, then Ctrl-C arrives before the journal records it."""

    def rename(self, target):
        Path(self).rename(target)
        raise KeyboardInterrupt


def test_an_interrupt_right_after_a_rename_still_moves_it_back(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    first = _package(modules, "a")
    before = _tree(modules)

    with pytest.raises(KeyboardInterrupt):
        move_materializations(
            ((_InterruptedAfterRename(first), modules / "_local" / "h1" / "a"),), modules
        )

    assert _tree(modules) == before
    assert not (modules / "_local" / "h1").exists()
