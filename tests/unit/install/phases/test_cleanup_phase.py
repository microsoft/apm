"""Unit tests for apm_cli.install.phases.cleanup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.install.phases import cleanup
from apm_cli.integration.cleanup import CleanupResult


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
    ctx.orphan_cleanup_retained = {}
    ctx.package_cleanup_retained = {}
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

    def test_orphan_cleanup_preserves_freshly_redeployed_paths(self, tmp_path):
        """A repaired dependency identity must not delete its new deployment."""
        orphan_dep = _make_orphan_dep(
            ["shared.md", "old-only.md"],
            file_hashes={
                "shared.md": "shared-hash",
                "old-only.md": "old-hash",
            },
        )
        lf = _make_lockfile({"tampered-identity": orphan_dep})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=False,
            intended_dep_keys={"canonical-identity"},
            package_deployed_files={"canonical-identity": ["shared.md"]},
            project_root=tmp_path,
        )
        mock_result = MagicMock(
            deleted=["old-only.md"],
            deleted_targets=[],
            skipped_user_edit=[],
        )

        with patch(
            "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
            return_value=mock_result,
        ) as mock_remove:
            cleanup.run(ctx)

        mock_remove.assert_called_once()
        assert mock_remove.call_args.args[0] == ["old-only.md"]

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

    def test_orphan_cleanup_refusal_retains_existing_hash_and_owner_key(self, tmp_path):
        """A failed orphan cleanup remains attributable without a new owner."""
        path = ".agents/skills/alpha/SKILL.md"
        orphan_dep = _make_orphan_dep([path], file_hashes={path: "sha256:original"})
        lf = _make_lockfile({"old-skill": orphan_dep})
        ctx = _make_ctx(
            existing_lockfile=lf,
            only_packages=False,
            intended_dep_keys=set(),
            project_root=tmp_path,
        )
        result = CleanupResult(skipped_user_edit=[path])

        with patch(
            "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
            return_value=result,
        ):
            cleanup.run(ctx)

        assert ctx.orphan_cleanup_retained == {"old-skill": {path: "sha256:original"}}

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
        mock_result.retained = ["old.md"]
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

    def test_stale_user_edited_and_unmanaged_paths_remain_tracked(self, tmp_path):
        """Cleanup refusals retain their existing ownership rows for later review."""
        user_edited = ".agents/skills/alpha/SKILL.md"
        unmanaged = ".agents/skills/alpha/extra.txt"
        prev_dep = _make_orphan_dep(
            [user_edited, unmanaged],
            file_hashes={user_edited: "sha256:original"},
        )
        lf = _make_lockfile({"pkg": prev_dep})
        lf.get_dependency.return_value = prev_dep
        new_deployed: list[str] = []
        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"pkg"},
            package_deployed_files={"pkg": new_deployed},
            project_root=tmp_path,
        )
        result = CleanupResult(
            skipped_user_edit=[user_edited],
            skipped_unmanaged=[unmanaged],
        )

        with (
            patch(
                "apm_cli.install.phases.cleanup.detect_stale_files",
                return_value=[user_edited, unmanaged],
            ),
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=result,
            ),
        ):
            cleanup.run(ctx)

        assert new_deployed == [user_edited, unmanaged]
        assert ctx.package_cleanup_retained == {
            "pkg": {
                user_edited: "sha256:original",
                unmanaged: None,
            }
        }

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


# ---------------------------------------------------------------------------
# Block 13: cross-package file protection (#1831)
# Files deployed by another package must not be removed as stale.
# ---------------------------------------------------------------------------


class TestCrossPackageProtection:
    """Stale cleanup must skip files still claimed by another package."""

    def test_shared_file_not_removed_when_other_package_deploys_it(self, tmp_path):
        """Issue #1831: if pkg-a no longer deploys shared.md but pkg-b still
        does, cleanup must NOT delete shared.md."""
        prev_dep_a = _make_orphan_dep(
            [".github/agents/shared.md", ".github/agents/only-a.md"],
            file_hashes={".github/agents/shared.md": "abc", ".github/agents/only-a.md": "def"},
        )
        lf = _make_lockfile({"pkg-a": prev_dep_a})
        lf.get_dependency.return_value = prev_dep_a

        # pkg-a no longer deploys either file; pkg-b still deploys shared.md
        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"pkg-a", "pkg-b"},
            package_deployed_files={
                "pkg-a": [],
                "pkg-b": [".github/agents/shared.md"],
            },
            project_root=tmp_path,
        )

        mock_result = MagicMock()
        mock_result.failed = []
        mock_result.deleted = [".github/agents/only-a.md"]
        mock_result.deleted_targets = []
        mock_result.skipped_user_edit = []

        with (
            patch(
                "apm_cli.install.phases.cleanup.detect_stale_files",
                return_value={".github/agents/shared.md", ".github/agents/only-a.md"},
            ),
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ) as mock_rm,
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)

        # remove_stale_deployed_files should only receive "only-a.md"
        # because shared.md is still claimed by pkg-b
        call_args = mock_rm.call_args
        stale_passed = call_args[0][0]
        assert ".github/agents/only-a.md" in stale_passed
        assert ".github/agents/shared.md" not in stale_passed
        ctx.logger.verbose_detail.assert_called_once_with(
            "Kept stale file .github/agents/shared.md for pkg-a; still deployed by another package"
        )

    def test_file_removed_when_no_other_package_claims_it(self, tmp_path):
        """Normal case: stale file not claimed by any other package is removed."""
        prev_dep = _make_orphan_dep(
            [".github/agents/old.md"],
            file_hashes={".github/agents/old.md": "abc"},
        )
        lf = _make_lockfile({"pkg-a": prev_dep})
        lf.get_dependency.return_value = prev_dep

        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"pkg-a", "pkg-b"},
            package_deployed_files={
                "pkg-a": [],
                "pkg-b": [".github/agents/other.md"],
            },
            project_root=tmp_path,
        )

        mock_result = MagicMock()
        mock_result.failed = []
        mock_result.deleted = [".github/agents/old.md"]
        mock_result.deleted_targets = []
        mock_result.skipped_user_edit = []

        with (
            patch(
                "apm_cli.install.phases.cleanup.detect_stale_files",
                return_value={".github/agents/old.md"},
            ),
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
                return_value=mock_result,
            ) as mock_rm,
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)

        # The file should be passed to removal since no other package claims it
        call_args = mock_rm.call_args
        stale_passed = call_args[0][0]
        assert ".github/agents/old.md" in stale_passed


# ---------------------------------------------------------------------------
# Block 14: legacy lockfile graceful skip (#1831)
# When prev_dep.deployed_files is empty (old APM version), stale cleanup
# is safely skipped and the new deployed_files is recorded for next run.
# ---------------------------------------------------------------------------


class TestLegacyLockfileGracefulSkip:
    """Legacy lockfiles with empty deployed_files skip stale cleanup safely."""

    def test_empty_prev_deployed_files_skips_cleanup(self, tmp_path):
        """When previous lockfile has empty deployed_files, no stale removal
        is attempted. The new deployed_files will be recorded for future diffs."""
        prev_dep = _make_orphan_dep([], file_hashes={})
        lf = _make_lockfile({"legacy-pkg": prev_dep})
        lf.get_dependency.return_value = prev_dep

        ctx = _make_ctx(
            existing_lockfile=lf,
            intended_dep_keys={"legacy-pkg"},
            package_deployed_files={
                "legacy-pkg": [".github/agents/new-file.md"],
            },
            project_root=tmp_path,
        )

        with (
            patch(
                "apm_cli.install.phases.cleanup.remove_stale_deployed_files",
            ) as mock_rm,
            patch("apm_cli.install.phases.cleanup.BaseIntegrator.cleanup_empty_parents"),
        ):
            cleanup.run(ctx)

        # No removal should be attempted since there are no previously
        # tracked files to compare against
        mock_rm.assert_not_called()
        ctx.logger.stale_cleanup.assert_not_called()
