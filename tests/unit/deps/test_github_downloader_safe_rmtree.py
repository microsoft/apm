"""Validation cleanup removes scratch trees through the safe rmtree owner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps import github_downloader_validation as gdv
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference

pytestmark = [pytest.mark.component, pytest.mark.promise("P-LAW-filesystem.open_world_observation")]


def _make_subdir_dep(
    repo_url: str = "owner/repo",
    vpath: str = "skills/my-skill",
    ref: str | None = "main",
    host: str | None = None,
) -> DependencyReference:
    """Build a virtual-subdirectory ``DependencyReference`` for tests."""
    return DependencyReference(
        repo_url=repo_url,
        host=host,
        reference=ref,
        virtual_path=vpath,
        is_virtual=True,
    )


# ---------------------------------------------------------------------------
# Finding 7: path traversal rejection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Finding 6: fail-open close
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Finding 8: ADO bearer header injection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Mechanical guards
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Round-3 regression tests
# ---------------------------------------------------------------------------


class TestRound3SafeRmtreeNotRobustRmtreeDirect:
    """Round-3: cleanup MUST go through safe_rmtree (containment gate)."""

    def test_safe_rmtree_called_not_robust_rmtree_direct(self) -> None:
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/x", ref="main")
        winning = gdv.AttemptSpec("plain HTTPS w/ credential helper", "https://x", {})

        completed = MagicMock(stdout="040000 tree abc\tskills/x")
        with (
            patch("apm_cli.deps.github_downloader_validation.safe_rmtree") as safe_rm_mock,
            patch(
                "apm_cli.deps.github_downloader_validation.subprocess.run",
                return_value=completed,
            ),
        ):
            ok = gdv._path_exists_in_tree_at_ref(
                downloader,
                dep_ref,
                "skills/x",
                "main",
                log=lambda _m: None,
                winning_attempt=winning,
            )

        assert ok is True
        safe_rm_mock.assert_called_once()
        # Containment: first arg is the tmpdir, second is the base.
        call_args = safe_rm_mock.call_args.args
        assert len(call_args) == 2, "safe_rmtree must be called with (path, base_dir)"


# ---------------------------------------------------------------------------
# Issue #1094: probe order in SUBDIRECTORY case
# ---------------------------------------------------------------------------
