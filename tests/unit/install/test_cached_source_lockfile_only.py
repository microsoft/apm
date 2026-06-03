"""Regression test for CachedDependencySource.acquire() lockfile_only guard.

Regression for #975 (PR #1639):
When ctx.lockfile_only=True and ctx.targets=[], the first early-return
guard was ``if not ctx.targets:`` which fired before installed_packages
was populated. The fix adds ``and not ctx.lockfile_only`` so the SHA
recording proceeds before the second guard returns without deploying.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.install.sources import CachedDependencySource


def _make_source(
    *,
    lockfile_only: bool,
    install_path: Path,
) -> CachedDependencySource:
    ctx = MagicMock()
    ctx.targets = []
    ctx.lockfile_only = lockfile_only
    ctx.logger = MagicMock()
    ctx.callback_downloaded = {}
    ctx.existing_lockfile = None
    ctx.dependency_graph = MagicMock()
    ctx.dependency_graph.dependency_tree.get_node.return_value = None
    ctx.registry_config = None
    ctx.git_semver_resolutions = {}
    ctx.installed_packages = []
    ctx.package_hashes = {}
    ctx.package_types = {}

    dep_ref = MagicMock()
    dep_ref.is_virtual = False
    dep_ref.is_local = False
    dep_ref.local_path = None
    dep_ref.repo_url = "https://github.com/owner/repo"
    dep_ref.reference = "v1.0.0"
    dep_ref.source = "github"

    dep_locked_chk = MagicMock()
    dep_locked_chk.resolved_commit = "abcd1234deadbeef1234"
    dep_locked_chk.registry_prefix = None

    return CachedDependencySource(
        ctx=ctx,
        dep_ref=dep_ref,
        install_path=install_path,
        dep_key="owner/repo@v1.0.0",
        resolved_ref=None,
        dep_locked_chk=dep_locked_chk,
        fetched_this_run=False,
    )


class TestCachedSourceLockfileOnly:
    def test_lockfile_only_populates_installed_packages(self, tmp_path: Path) -> None:
        """In lockfile_only mode, acquire() must record in installed_packages
        even when targets=[] (regression for the guard bug in #975)."""
        install_path = tmp_path / "pkg"
        install_path.mkdir()

        src = _make_source(lockfile_only=True, install_path=install_path)

        with (
            patch("apm_cli.models.validation.detect_package_type", return_value=(None, None)),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="fakehash"),
            patch(
                "apm_cli.install.sources._rebuild_cached_semver_resolution",
                return_value=None,
            ),
        ):
            src.acquire()

        assert len(src.ctx.installed_packages) == 1, (
            "lockfile_only=True with empty targets must populate installed_packages "
            "before the second early-return guard fires"
        )

    def test_non_lockfile_only_empty_targets_exits_before_installed_packages(
        self, tmp_path: Path
    ) -> None:
        """Without lockfile_only, empty targets must early-return before
        recording installed_packages (pre-existing behaviour verified)."""
        install_path = tmp_path / "pkg"
        install_path.mkdir()

        src = _make_source(lockfile_only=False, install_path=install_path)
        src.acquire()

        assert len(src.ctx.installed_packages) == 0, (
            "non-lockfile_only with empty targets should exit early "
            "without populating installed_packages"
        )
