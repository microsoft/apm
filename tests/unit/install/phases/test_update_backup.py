"""Unit tests for the ``apm update`` stage-and-restore backup mechanism.

Regression coverage for the bug where a declined confirmation, a
non-interactive abort (no TTY, no ``--yes``), or ``--dry-run`` left
``apm_modules/`` already advanced to the new version while
``apm.lock.yaml`` stayed on the old one -- because ``download_callback``
materialises the new content to disk during resolve, before the plan-
confirmation gate ever runs. See ``update_backup.py`` for the mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from apm_cli.install.phases.update_backup import (
    _sanitize_backup_name,
    purge_cached_semver_paths_for_update,
    restore_update_backups,
)


@dataclass
class _FakeDep:
    """Minimal stand-in for the parts of DependencyReference this module uses."""

    key: str
    ref_kind: str | None = "semver"
    is_local: bool = False
    artifactory_prefix: str | None = None
    install_rel: str | None = None  # relative path under apm_modules_dir; defaults to key

    def get_unique_key(self) -> str:
        return self.key

    def get_install_path(self, apm_modules_dir: Path) -> Path:
        return apm_modules_dir / (self.install_rel or self.key)


def _write(path: Path, content: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "marker.txt").write_text(content, encoding="utf-8")


def _read(path: Path) -> str | None:
    marker = path / "marker.txt"
    return marker.read_text(encoding="utf-8") if marker.exists() else None


class TestSanitizeBackupName:
    """Sanitizing alone is not injective -- distinct dep keys that differ only
    in a character the sanitizer collapses (``/``, ``_``, ``:``) must not map
    to the same backup directory name, or one dep's backup could silently
    overwrite another's."""

    def test_distinct_keys_produce_distinct_names(self) -> None:
        names = {
            _sanitize_backup_name("owner/repo"),
            _sanitize_backup_name("owner_repo"),
            _sanitize_backup_name("owner:repo"),
        }
        assert len(names) == 3

    def test_same_key_is_deterministic(self) -> None:
        assert _sanitize_backup_name("owner/repo") == _sanitize_backup_name("owner/repo")

    def test_name_is_filesystem_safe(self) -> None:
        import re

        name = _sanitize_backup_name("github.com/owner/repo#v1.0.0")
        assert re.fullmatch(r"[A-Za-z0-9._-]+", name)


class TestPurgeCachedSemverPathsForUpdate:
    def test_no_backup_root_deletes_outright(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        dep = _FakeDep("owner/repo")
        _write(dep.get_install_path(modules), "old")

        backups = purge_cached_semver_paths_for_update(
            all_apm_deps=[dep], apm_modules_dir=modules, logger=None
        )

        assert backups == {}
        assert not dep.get_install_path(modules).exists()

    def test_backup_root_moves_content_aside(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep = _FakeDep("owner/repo")
        _write(dep.get_install_path(modules), "old")

        backups = purge_cached_semver_paths_for_update(
            all_apm_deps=[dep],
            apm_modules_dir=modules,
            logger=None,
            backup_root=backup_root,
        )

        assert not dep.get_install_path(modules).exists()
        assert "owner/repo" in backups
        assert _read(backups["owner/repo"]) == "old"

    def test_skips_non_semver_deps(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep = _FakeDep("owner/repo", ref_kind="literal")
        _write(dep.get_install_path(modules), "old")

        backups = purge_cached_semver_paths_for_update(
            all_apm_deps=[dep],
            apm_modules_dir=modules,
            logger=None,
            backup_root=backup_root,
        )

        assert backups == {}
        assert dep.get_install_path(modules).exists()

    def test_skips_local_deps(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep = _FakeDep("owner/repo", is_local=True)
        _write(dep.get_install_path(modules), "old")

        backups = purge_cached_semver_paths_for_update(
            all_apm_deps=[dep],
            apm_modules_dir=modules,
            logger=None,
            backup_root=backup_root,
        )

        assert backups == {}
        assert dep.get_install_path(modules).exists()

    def test_skips_artifactory_deps(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep = _FakeDep("owner/repo", artifactory_prefix="proxy")
        _write(dep.get_install_path(modules), "old")

        backups = purge_cached_semver_paths_for_update(
            all_apm_deps=[dep],
            apm_modules_dir=modules,
            logger=None,
            backup_root=backup_root,
        )

        assert backups == {}
        assert dep.get_install_path(modules).exists()

    def test_missing_install_path_is_a_noop(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep = _FakeDep("owner/repo")  # never created on disk

        backups = purge_cached_semver_paths_for_update(
            all_apm_deps=[dep],
            apm_modules_dir=modules,
            logger=None,
            backup_root=backup_root,
        )

        assert backups == {}


class TestRestoreUpdateBackups:
    def _ctx(self, *, modules_dir, deps, downloaded, backups):
        return SimpleNamespace(
            apm_modules_dir=modules_dir,
            deps_to_install=deps,
            callback_downloaded=downloaded,
            update_backups=backups,
        )

    def test_keep_new_discards_backup_for_downloaded_dep(self, tmp_path: Path) -> None:
        """Committed update: new content stays, backup is discarded."""
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep = _FakeDep("owner/repo")
        _write(backup_root / "owner_repo", "old")
        _write(dep.get_install_path(modules), "new")

        ctx = self._ctx(
            modules_dir=modules,
            deps=[dep],
            downloaded={"owner/repo": None},
            backups={"owner/repo": backup_root / "owner_repo"},
        )

        restore_update_backups(ctx, keep_new=True)

        assert _read(dep.get_install_path(modules)) == "new"
        assert not (backup_root / "owner_repo").exists()

    def test_declined_restores_original_content(self, tmp_path: Path) -> None:
        """Declined/aborted/dry-run: new content is reverted to the backup."""
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep = _FakeDep("owner/repo")
        _write(backup_root / "owner_repo", "old")
        _write(dep.get_install_path(modules), "new")

        ctx = self._ctx(
            modules_dir=modules,
            deps=[dep],
            downloaded={"owner/repo": None},
            backups={"owner/repo": backup_root / "owner_repo"},
        )

        restore_update_backups(ctx, keep_new=False)

        assert _read(dep.get_install_path(modules)) == "old"
        assert not (backup_root / "owner_repo").exists()

    def test_declined_removes_freshly_added_dep_with_no_backup(self, tmp_path: Path) -> None:
        """A dep newly downloaded this run (no prior backup) never existed before --
        declining must remove it outright, not leave it half-installed."""
        modules = tmp_path / "apm_modules"
        dep = _FakeDep("owner/new-dep")
        _write(dep.get_install_path(modules), "new")

        ctx = self._ctx(
            modules_dir=modules,
            deps=[dep],
            downloaded={"owner/new-dep": None},
            backups={},
        )

        restore_update_backups(ctx, keep_new=False)

        assert not dep.get_install_path(modules).exists()

    def test_committed_but_not_downloaded_still_restores(self, tmp_path: Path) -> None:
        """A dep purged for re-resolution but whose callback never actually ran
        (e.g. an earlier failure aborted the graph walk) must not end up with an
        empty install path even though the overall run is committed."""
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep = _FakeDep("owner/repo")
        _write(backup_root / "owner_repo", "old")
        # Note: install path was purged and NOT re-created -- simulates the
        # callback never reaching this dep.

        ctx = self._ctx(
            modules_dir=modules,
            deps=[dep],
            downloaded={},  # callback never ran for this dep
            backups={"owner/repo": backup_root / "owner_repo"},
        )

        restore_update_backups(ctx, keep_new=True)

        assert _read(dep.get_install_path(modules)) == "old"

    def test_no_backups_and_keep_new_is_a_noop(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        ctx = self._ctx(modules_dir=modules, deps=[], downloaded={}, backups={})

        # Must not raise even though nothing was ever staged.
        restore_update_backups(ctx, keep_new=True)

    def test_empty_backup_root_dir_is_removed_after_restore(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep = _FakeDep("owner/repo")
        _write(backup_root / "owner_repo", "old")
        _write(dep.get_install_path(modules), "new")

        ctx = self._ctx(
            modules_dir=modules,
            deps=[dep],
            downloaded={"owner/repo": None},
            backups={"owner/repo": backup_root / "owner_repo"},
        )

        restore_update_backups(ctx, keep_new=False)

        assert not backup_root.exists()

    def test_multiple_deps_mixed_outcome(self, tmp_path: Path) -> None:
        """One dep committed, one dep declined-alongside via keep_new=False path,
        exercised together to check no cross-talk between entries."""
        modules = tmp_path / "apm_modules"
        backup_root = tmp_path / ".apm-update-backup"
        dep_a = _FakeDep("owner/a")
        dep_b = _FakeDep("owner/b")
        _write(backup_root / "owner_a", "old-a")
        _write(backup_root / "owner_b", "old-b")
        _write(dep_a.get_install_path(modules), "new-a")
        _write(dep_b.get_install_path(modules), "new-b")

        ctx = self._ctx(
            modules_dir=modules,
            deps=[dep_a, dep_b],
            downloaded={"owner/a": None, "owner/b": None},
            backups={"owner/a": backup_root / "owner_a", "owner/b": backup_root / "owner_b"},
        )

        restore_update_backups(ctx, keep_new=False)

        assert _read(dep_a.get_install_path(modules)) == "old-a"
        assert _read(dep_b.get_install_path(modules)) == "old-b"
