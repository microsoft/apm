"""apm outdated reuses the ref cache across duplicate dependencies."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-git-ref-freshness")]


def _tag(name: str, sha: str = "abc123") -> RemoteRef:
    return RemoteRef(name=name, ref_type=GitReferenceType.TAG, commit_sha=sha)


# ---------------------------------------------------------------------------
# Dedup on OutdatedRefCache directly
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# End-to-end through ``apm outdated``: dedup AND correctness preserved
# ---------------------------------------------------------------------------
_PATCH_LOCKFILE = "apm_cli.deps.lockfile.LockFile"
_PATCH_GET_LOCKFILE_PATH = "apm_cli.deps.lockfile.get_lockfile_path"
_PATCH_MIGRATE = "apm_cli.deps.lockfile.migrate_lockfile_if_needed"
_PATCH_DOWNLOADER = "apm_cli.deps.github_downloader.GitHubPackageDownloader"
_PATCH_AUTH = "apm_cli.core.auth.AuthResolver"
_PATCH_GET_APM_DIR = "apm_cli.core.scope.get_apm_dir"


def _locked(repo_url, virtual_path, resolved_ref):
    from apm_cli.deps.lockfile import LockedDependency

    return LockedDependency(
        repo_url=repo_url,
        resolved_ref=resolved_ref,
        resolved_commit="deadbee",
        virtual_path=virtual_path,
        is_virtual=True,
    )


def _make_lockfile(deps_dict):
    from apm_cli.deps.lockfile import LockFile

    lf = LockFile()
    lf.dependencies = deps_dict
    return lf


def test_outdated_command_dedupes_and_preserves_correctness(tmp_path):
    """Two same-repo virtual deps -> one ls-remote, and outdated stays accurate.

    Locked at v1.0.0 while upstream's highest tag is v2.0.0: both rows must
    report ``outdated`` (a stale/misapplied cache would wrongly say
    ``up-to-date``), while the shared upstream is listed exactly once.
    """
    from click.testing import CliRunner

    from apm_cli.cli import cli

    deps = {
        "org/mono#packages/a": _locked("org/mono", "packages/a", "v1.0.0"),
        "org/mono#packages/b": _locked("org/mono", "packages/b", "v1.0.0"),
    }

    inner = MagicMock()
    # Same upstream repo -> one listing; highest tag is v2.0.0 (newer).
    inner.list_remote_refs.return_value = [_tag("v1.0.0"), _tag("v2.0.0")]

    cwd = os.getcwd()
    with (
        patch(_PATCH_MIGRATE),
        patch(_PATCH_GET_LOCKFILE_PATH, return_value=tmp_path / "apm.lock.yaml"),
        patch(_PATCH_GET_APM_DIR, return_value=tmp_path),
        patch(_PATCH_LOCKFILE) as mock_lf_cls,
        patch(_PATCH_DOWNLOADER, return_value=inner),
        patch(_PATCH_AUTH),
    ):
        mock_lf_cls.read.return_value = _make_lockfile(deps)
        try:
            os.chdir(tmp_path)
            # -j 0 => sequential, so the dedup count is deterministic.
            result = CliRunner().invoke(cli, ["outdated", "-j", "0"])
        finally:
            os.chdir(cwd)

    assert result.exit_code == 0, result.output
    # Dedup: one shared upstream listed exactly once (would be 2 un-wrapped).
    assert inner.list_remote_refs.call_count == 1
    # Correctness: both same-repo deps still reported outdated (v1 -> v2).
    assert "outdated" in result.output.lower()
    assert "up-to-date" not in result.output.lower()
