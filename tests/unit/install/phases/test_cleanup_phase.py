"""Unit tests for apm_cli.install.phases.cleanup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.install.phases import cleanup


def _make_ctx(
    *,
    existing_lockfile=None,
    only_packages: bool = False,
    intended_dep_keys: set | None = None,
    package_deployed_files: dict | None = None,
    project_root: Path | None = None,
) -> MagicMock:
    """Build a minimal MagicMock InstallContext for cleanup phase tests."""
    ctx = MagicMock()
    ctx.existing_lockfile = existing_lockfile
    ctx.only_packages = only_packages
    ctx.intended_dep_keys = intended_dep_keys if intended_dep_keys is not None else set()
    ctx.package_deployed_files = (
        package_deployed_files if package_deployed_files is not None else {}
    )
    ctx.project_root = project_root or Path("/fake/project")
    ctx.targets = []
    ctx.diagnostics = MagicMock()
    ctx.diagnostics.count_for_package.return_value = 0
    ctx.logger = MagicMock()
    return ctx


def _make_lockfile(deps: dict) -> MagicMock:
    """Build a minimal lockfile mock with the given {key: dep_mock} dict."""
    lf = MagicMock()
    lf.dependencies = deps
    lf.get_dependency.side_effect = lambda key: deps.get(key)
    return lf


def _make_orphan_dep(deployed_files: list[str], file_hashes: dict | None = None) -> MagicMock:
    dep = MagicMock()
    dep.deployed_files = deployed_files
    dep.deployed_file_hashes = file_hashes or {}
    return dep


# ---------------------------------------------------------------------------
# Block 1: no existing lockfile → both cleanup blocks skipped entirely
# ---------------------------------------------------------------------------


class TestNoExistingLockfile:
    def test_no_orphan_no_stale_without_lockfile(self):
        """When existing_lockfile is None, nothing should be deleted."""
        ctx = _make_ctx(existing_lockfile=None)
        cleanup.run(ctx)
        ctx.logger.orphan_cleanup.assert_not_called()
        ctx.logger.stale_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# Block 2: only_packages=True → orphan cleanup skipped, stale may run
# ---------------------------------------------------------------------------


class TestOnlyPackagesSkipsOrphanCleanup:
    def test_orphan_cleanup_skipped_when_only_packages(self):
        """only_packages=True should prevent orphan cleanup even with lockfile."""
        orphan_dep = _make_orphan_dep(["file.md"])
        lf = _make_lockfile({"some-pkg": orphan_dep})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=True,
            intended_dep_keys=set(),
        )
        cleanup.run(ctx)
        ctx.logger.orphan_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# Block 3: orphan cleanup — SELF_KEY skipped
# ---------------------------------------------------------------------------


class TestOrphanCleanupSelfKeySkipped:
    def test_self_key_dep_not_cleaned_up(self):
        """Dependency with key '.' (_SELF_KEY) must be skipped."""
        self_dep = _make_orphan_dep(["file.md"])
        lf = _make_lockfile({".": self_dep})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=False,
            intended_dep_keys=set(),
        )
        with patch("apm_cli.install.phases.cleanup.remove_stale_deployed_files") as mock_rm:
            cleanup.run(ctx)
        # remove_stale_deployed_files should NOT be called for self-entry
        mock_rm.assert_not_called()


# ---------------------------------------------------------------------------
# Block 4: orphan cleanup — key in intended_dep_keys → skipped
# ---------------------------------------------------------------------------


class TestOrphanCleanupIntendedKeySkipped:
    def test_intended_dep_not_orphaned(self):
        """Packages still in intended_dep_keys must not be deleted."""
        still_present = _make_orphan_dep(["readme.md"])
        lf = _make_lockfile({"my-pkg": still_present})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=False,
            intended_dep_keys={"my-pkg"},
        )
        with patch("apm_cli.install.phases.cleanup.remove_stale_deployed_files") as mock_rm:
            cleanup.run(ctx)
        mock_rm.assert_not_called()


# ---------------------------------------------------------------------------
# Block 5: orphan cleanup — no deployed_files → skipped
# ---------------------------------------------------------------------------


class TestOrphanCleanupNoDeployedFiles:
    def test_dep_with_no_deployed_files_skipped(self):
        """Orphan deps with empty deployed_files are skipped."""
        empty_dep = _make_orphan_dep([])
        lf = _make_lockfile({"old-pkg": empty_dep})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=False,
            intended_dep_keys=set(),
        )
        with patch("apm_cli.install.phases.cleanup.remove_stale_deployed_files") as mock_rm:
            cleanup.run(ctx)
        mock_rm.assert_not_called()


# ---------------------------------------------------------------------------
# Block 6: orphan cleanup — full run with remove_stale_deployed_files
# ---------------------------------------------------------------------------


class TestOrphanCleanupFullRun:
    def test_orphan_removed_and_logger_called(self, tmp_path):
        """Orphan dep with deployed_files triggers removal and logger calls."""
        orphan_dep = _make_orphan_dep(["a/b.md"], file_hashes={"a/b.md": "abc123"})
        lf = _make_lockfile({"removed-pkg": orphan_dep})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=False,
            intended_dep_keys=set(),
            project_root=tmp_path,
        )

        mock_result = MagicMock()
        mock_result.deleted = ["a/b.md"]
        mock_result.deleted_targets = []
        mock_result.skipped_user_edit = []

        with (
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ),
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)

        ctx.logger.orphan_cleanup.assert_called_once_with(1)

    def test_orphan_cleanup_calls_cleanup_empty_parents_when_deleted_targets(self, tmp_path):
        """cleanup_empty_parents is called when deleted_targets is non-empty."""
        orphan_dep = _make_orphan_dep(["x/y.md"])
        lf = _make_lockfile({"orphaned": orphan_dep})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=False,
            intended_dep_keys=set(),
            project_root=tmp_path,
        )

        fake_target = tmp_path / "x"
        mock_result = MagicMock()
        mock_result.deleted = ["x/y.md"]
        mock_result.deleted_targets = [fake_target]
        mock_result.skipped_user_edit = []

        with (
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ),
            patch(
                "apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"
            ) as mock_cep,
        ):
            cleanup.run(ctx)

        mock_cep.assert_called_once_with([fake_target], tmp_path)

    def test_orphan_cleanup_logs_skipped_user_edit(self, tmp_path):
        """skipped_user_edit entries trigger logger.cleanup_skipped_user_edit."""
        orphan_dep = _make_orphan_dep(["docs/edited.md"])
        lf = _make_lockfile({"old-doc": orphan_dep})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=False,
            intended_dep_keys=set(),
            project_root=tmp_path,
        )

        mock_result = MagicMock()
        mock_result.deleted = []
        mock_result.deleted_targets = []
        mock_result.skipped_user_edit = ["docs/edited.md"]

        with (
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ),
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)

        ctx.logger.cleanup_skipped_user_edit.assert_called_once_with("docs/edited.md", "old-doc")

    def test_orphan_no_logger_no_crash(self, tmp_path):
        """When logger is None, orphan cleanup should not raise."""
        orphan_dep = _make_orphan_dep(["file.md"])
        lf = _make_lockfile({"gone": orphan_dep})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=False,
            intended_dep_keys=set(),
            project_root=tmp_path,
        )
        ctx.logger = None  # no logger

        mock_result = MagicMock()
        mock_result.deleted = ["file.md"]
        mock_result.deleted_targets = []
        mock_result.skipped_user_edit = []

        with (
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ),
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)  # must not raise


# ---------------------------------------------------------------------------
# Block 7: stale-file cleanup — no package_deployed_files → block skipped
# ---------------------------------------------------------------------------


class TestStaleCleanupNoPackageDeployedFiles:
    def test_stale_cleanup_skipped_when_no_deployed_files_dict(self):
        lf = _make_lockfile({})
        ctx = _make_ctx(existing_lockfile=lf, package_deployed_files={})
        cleanup.run(ctx)
        ctx.logger.stale_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# Block 8: stale-file cleanup — package has errors → skipped
# ---------------------------------------------------------------------------


class TestStaleCleanupErrorPackageSkipped:
    def test_package_with_error_diagnostic_skipped(self):
        lf = _make_lockfile({"pkg-with-error": _make_orphan_dep(["old.md"])})
        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"pkg-with-error"},  # not an orphan
            package_deployed_files={"pkg-with-error": ["new.md"]},
        )
        ctx.diagnostics.count_for_package.return_value = 1  # has errors

        with patch("apm_cli.install.phases.cleanup.remove_stale_deployed_files") as mock_rm:
            cleanup.run(ctx)

        mock_rm.assert_not_called()


# ---------------------------------------------------------------------------
# Block 9: stale-file cleanup — prev_dep not found → skipped
# ---------------------------------------------------------------------------


class TestStaleCleanupNoPrevDep:
    def test_new_package_skipped_no_prev_dep(self):
        lf = MagicMock()
        lf.dependencies = {"new-pkg": _make_orphan_dep(["file.md"])}
        lf.get_dependency.return_value = None  # new package, not in old lockfile

        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"new-pkg"},  # not an orphan
            package_deployed_files={"new-pkg": ["file.md"]},
        )

        with patch("apm_cli.install.phases.cleanup.remove_stale_deployed_files") as mock_rm:
            cleanup.run(ctx)

        mock_rm.assert_not_called()


# ---------------------------------------------------------------------------
# Block 10: stale-file cleanup — no stale files → skipped
# ---------------------------------------------------------------------------


class TestStaleCleanupNoStaleFiles:
    def test_no_stale_files_skips_removal(self):
        prev_dep = _make_orphan_dep(["readme.md"])
        lf = _make_lockfile({"my-pkg": prev_dep})
        lf.get_dependency.return_value = prev_dep

        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"my-pkg"},  # not an orphan
            package_deployed_files={"my-pkg": ["readme.md"]},
        )

        with (
            patch("apm_cli.install.phases.cleanup.detect_stale_files", return_value=[]),
            patch("apm_cli.install.phases.cleanup.remove_stale_deployed_files") as mock_rm,
        ):
            cleanup.run(ctx)

        mock_rm.assert_not_called()


# ---------------------------------------------------------------------------
# Block 11: stale-file cleanup — full stale run
# ---------------------------------------------------------------------------


class TestStaleCleanupFullRun:
    def test_stale_files_removed_and_logger_called(self, tmp_path):
        prev_dep = _make_orphan_dep(["old.md", "new.md"], file_hashes={"old.md": "abc"})
        lf = _make_lockfile({"my-pkg": prev_dep})
        lf.get_dependency.return_value = prev_dep

        new_deployed = ["new.md"]
        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"my-pkg"},  # not an orphan
            package_deployed_files={"my-pkg": new_deployed},
            project_root=tmp_path,
        )

        mock_result = MagicMock()
        mock_result.failed = []
        mock_result.deleted = ["old.md"]
        mock_result.deleted_targets = []
        mock_result.skipped_user_edit = []

        with (
            patch("apm_cli.install.phases.cleanup.detect_stale_files", return_value=["old.md"]),
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ),
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)

        ctx.logger.stale_cleanup.assert_called_once_with("my-pkg", 1)

    def test_stale_failed_paths_reinserted_into_deployed(self, tmp_path):
        """Files that failed deletion are re-added to new_deployed."""
        prev_dep = _make_orphan_dep(["old.md"])
        lf = _make_lockfile({"pkg": prev_dep})
        lf.get_dependency.return_value = prev_dep

        new_deployed = []
        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"pkg"},  # not an orphan
            package_deployed_files={"pkg": new_deployed},
            project_root=tmp_path,
        )

        mock_result = MagicMock()
        mock_result.failed = ["old.md"]
        mock_result.deleted = []
        mock_result.deleted_targets = []
        mock_result.skipped_user_edit = []

        with (
            patch("apm_cli.install.phases.cleanup.detect_stale_files", return_value=["old.md"]),
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ),
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)

        # failed paths must be re-inserted for retry
        assert "old.md" in new_deployed

    def test_stale_cleanup_empty_parents_called(self, tmp_path):
        prev_dep = _make_orphan_dep(["dir/old.md"])
        lf = _make_lockfile({"pkg": prev_dep})
        lf.get_dependency.return_value = prev_dep

        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"pkg"},  # not an orphan
            package_deployed_files={"pkg": []},
            project_root=tmp_path,
        )

        fake_target = tmp_path / "dir"
        mock_result = MagicMock()
        mock_result.failed = []
        mock_result.deleted = ["dir/old.md"]
        mock_result.deleted_targets = [fake_target]
        mock_result.skipped_user_edit = []

        with (
            patch("apm_cli.install.phases.cleanup.detect_stale_files", return_value=["dir/old.md"]),
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ),
            patch(
                "apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"
            ) as mock_cep,
        ):
            cleanup.run(ctx)

        mock_cep.assert_called_once_with([fake_target], tmp_path)

    def test_stale_cleanup_logs_skipped_user_edit(self, tmp_path):
        prev_dep = _make_orphan_dep(["hand-edited.md"])
        lf = _make_lockfile({"pkg": prev_dep})
        lf.get_dependency.return_value = prev_dep

        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"pkg"},  # not an orphan
            package_deployed_files={"pkg": []},
            project_root=tmp_path,
        )

        mock_result = MagicMock()
        mock_result.failed = []
        mock_result.deleted = []
        mock_result.deleted_targets = []
        mock_result.skipped_user_edit = ["hand-edited.md"]

        with (
            patch(
                "apm_cli.install.phases.cleanup.detect_stale_files", return_value=["hand-edited.md"]
            ),
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ),
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)

        ctx.logger.cleanup_skipped_user_edit.assert_called_once_with("hand-edited.md", "pkg")

    def test_stale_cleanup_no_logger_no_crash(self, tmp_path):
        prev_dep = _make_orphan_dep(["old.md"])
        lf = _make_lockfile({"pkg": prev_dep})
        lf.get_dependency.return_value = prev_dep

        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"pkg"},  # not an orphan
            package_deployed_files={"pkg": []},
            project_root=tmp_path,
        )
        ctx.logger = None

        mock_result = MagicMock()
        mock_result.failed = []
        mock_result.deleted = ["old.md"]
        mock_result.deleted_targets = []
        mock_result.skipped_user_edit = []

        with (
            patch("apm_cli.install.phases.cleanup.detect_stale_files", return_value=["old.md"]),
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ),
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)  # must not raise
