"""Regression traps for orphan-pruning the lockfile on a depless manifest.

When every apm dependency is removed from ``apm.yml``, ``installed_packages``
is empty, so ``LockfileBuilder.build_and_save`` used to early-return before the
orphan-prune path (``_merge_existing``) ran -- leaving the lockfile listing deps
the manifest no longer declares. ``_has_orphan_lockfile_entries`` guards that
early-return so the builder falls through and rebuilds a lockfile matching the
manifest. Partial (``only_packages``) installs must still preserve unlisted
entries, so the guard is suppressed for them.
"""

from __future__ import annotations

from types import SimpleNamespace

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.context import InstallContext
from apm_cli.install.phases.lockfile import LockfileBuilder


def _existing_with(*repo_urls: str) -> LockFile:
    lf = LockFile()
    for url in repo_urls:
        lf.add_dependency(LockedDependency(repo_url=url, resolved_ref="1.0.0"))
    return lf


def _ctx(*, existing_lockfile, intended_dep_keys, only_packages=None):
    return SimpleNamespace(
        existing_lockfile=existing_lockfile,
        intended_dep_keys=set(intended_dep_keys),
        only_packages=only_packages,
        lockfile_only=False,
        installed_packages=[],
    )


class TestHasOrphanLockfileEntries:
    def test_orphan_detected_when_manifest_emptied(self) -> None:
        ctx = _ctx(
            existing_lockfile=_existing_with("acme/pkg"),
            intended_dep_keys=set(),  # manifest now declares nothing
        )
        assert LockfileBuilder(ctx)._has_orphan_lockfile_entries() is True

    def test_no_orphan_when_all_entries_intended(self) -> None:
        ctx = _ctx(
            existing_lockfile=_existing_with("acme/pkg", "acme/other"),
            intended_dep_keys={"acme/pkg", "acme/other"},
        )
        assert LockfileBuilder(ctx)._has_orphan_lockfile_entries() is False

    def test_partial_orphan_detected(self) -> None:
        ctx = _ctx(
            existing_lockfile=_existing_with("acme/pkg", "acme/dropped"),
            intended_dep_keys={"acme/pkg"},
        )
        assert LockfileBuilder(ctx)._has_orphan_lockfile_entries() is True

    def test_only_packages_install_never_reports_orphans(self) -> None:
        # Partial install intentionally preserves unlisted entries.
        ctx = _ctx(
            existing_lockfile=_existing_with("acme/pkg"),
            intended_dep_keys=set(),
            only_packages=["acme/pkg"],
        )
        assert LockfileBuilder(ctx)._has_orphan_lockfile_entries() is False

    def test_no_existing_lockfile_has_no_orphans(self) -> None:
        ctx = _ctx(existing_lockfile=None, intended_dep_keys=set())
        assert LockfileBuilder(ctx)._has_orphan_lockfile_entries() is False

    def test_self_key_is_not_treated_as_orphan(self) -> None:
        # The "." self-entry (local deployed files) is not a manifest dep and
        # must never trip the orphan guard on its own.
        from apm_cli.deps.lockfile import _SELF_KEY

        lf = LockFile()
        lf.dependencies[_SELF_KEY] = LockedDependency(repo_url=_SELF_KEY, resolved_ref="")
        ctx = _ctx(existing_lockfile=lf, intended_dep_keys=set())
        assert LockfileBuilder(ctx)._has_orphan_lockfile_entries() is False

    def test_build_and_save_prunes_orphans_when_manifest_is_empty(self, tmp_path) -> None:
        existing = _existing_with("acme/pkg")
        lockfile_path = tmp_path / "apm.lock.yaml"
        existing.write(lockfile_path)
        ctx = InstallContext(
            project_root=tmp_path,
            apm_dir=tmp_path,
            installed_packages=[],
            existing_lockfile=existing,
            intended_dep_keys=set(),
        )

        LockfileBuilder(ctx).build_and_save()

        written = LockFile.read(lockfile_path)
        assert written is not None
        assert written.get_package_dependencies() == []

    def test_empty_install_reconciles_target_files_before_early_return(self, monkeypatch) -> None:
        """Target contraction must not depend on installing a package this run."""
        existing = _existing_with("acme/pkg")
        ctx = _ctx(
            existing_lockfile=existing,
            intended_dep_keys={"acme/pkg"},
        )
        observed: list[LockFile | None] = []
        monkeypatch.setattr(
            LockfileBuilder,
            "_sync_cache_pin_markers_from_existing",
            lambda self: None,
        )
        monkeypatch.setattr(
            LockfileBuilder,
            "_reconcile_dropped_merge_hook_targets",
            lambda self: None,
        )
        monkeypatch.setattr(
            LockfileBuilder,
            "_sync_cache_pin_markers_from_disk",
            lambda self: None,
        )
        monkeypatch.setattr(
            LockfileBuilder,
            "_reconcile_target_deployed_files",
            lambda self, lockfile: observed.append(lockfile),
        )

        LockfileBuilder(ctx).build_and_save()

        assert observed == [existing]
