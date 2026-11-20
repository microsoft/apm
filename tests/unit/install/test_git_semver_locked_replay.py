"""Download strategy replays a locked git semver resolution from disk."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-git-semver-preflight-resolution")]


def _make_dep_ref(*, reference="^1.2.0", source="github", is_local=False, artifactory_prefix=None):
    """Build a minimal git-source DependencyReference for tests."""
    return DependencyReference(
        host="github.com",
        repo_url="acme/widget",
        reference=reference,
        source=source,
        is_local=is_local,
        artifactory_prefix=artifactory_prefix,
    )


class TestDownloadStrategyReplaysLockedSemver:
    """#3382: a semver dep already in apm_modules skips the resolve callback,
    so integrate must replay the locked tag instead of resolving the raw
    range as a literal ref (which branch-drift healing then re-downloads)."""

    def _lockfile(self, dep, *, constraint="^1.2.0", tag="v1.5.3"):
        lockfile = LockFile()
        lockfile.dependencies[dep.get_unique_key()] = LockedDependency(
            host="github.com",
            repo_url="acme/widget",
            source="git",
            resolved_ref=tag,
            resolved_commit="a" * 40,
            version="1.5.3",
            constraint=constraint,
            resolved_tag=tag,
            resolved_at="2025-01-15T12:00:00Z",
        )
        return lockfile

    def _ctx(self, lockfile, *, update_refs=False):
        ctx = MagicMock()
        ctx.existing_lockfile = lockfile
        ctx.update_refs = update_refs
        ctx.refresh = False
        ctx.git_semver_resolutions = {}
        ctx.pre_downloaded_keys = set()
        ctx.callback_downloaded = {}
        ctx.callback_content_hashes = {}
        ctx.content_hash_verified_deps = set()
        ctx.expected_hash_change_deps = set()
        ctx.downloader.resolve_git_reference.side_effect = Exception("no remote")
        return ctx

    def _resolved_refs(self, ctx):
        return [call.args[0].reference for call in ctx.downloader.resolve_git_reference.mock_calls]

    @pytest.mark.req("req-lk-009")
    def test_unchanged_constraint_replays_locked_tag(self, tmp_path):
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        dep = _make_dep_ref(reference="^1.2.0", source="git")
        ctx = self._ctx(self._lockfile(dep))

        _resolve_download_strategy(ctx, dep, tmp_path / "missing")

        assert self._resolved_refs(ctx) == ["v1.5.3"]
        assert dep.reference == "v1.5.3"
        resolution = ctx.git_semver_resolutions[dep.get_unique_key()]
        assert resolution.constraint == "^1.2.0"
        assert resolution.resolved_tag == "v1.5.3"
        assert resolution.resolved_sha == "a" * 40

    @pytest.mark.parametrize("tag", ["v1.5.3", "1.5.3"], ids=("v-prefixed", "bare-version"))
    def test_replayed_tag_is_not_reported_as_ref_drift(self, tmp_path, tag):
        """A bare ``1.5.3`` tag is itself a semver string; drift must still be
        judged against the declared constraint, not the replayed tag."""
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        dep = _make_dep_ref(reference="^1.2.0", source="git")
        ctx = self._ctx(self._lockfile(dep, tag=tag))

        _, _, _, ref_changed = _resolve_download_strategy(ctx, dep, tmp_path / "missing")

        assert dep.reference == tag
        assert ref_changed is False
        assert ctx.expected_hash_change_deps == set()

    @pytest.mark.parametrize(
        ("update_refs", "constraint"),
        [(True, "^1.2.0"), (False, "^1.0.0")],
        ids=("update", "constraint-changed"),
    )
    @pytest.mark.req("req-lk-009")
    def test_update_or_changed_constraint_does_not_replay(self, tmp_path, update_refs, constraint):
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        dep = _make_dep_ref(reference="^1.2.0", source="git")
        ctx = self._ctx(self._lockfile(dep, constraint=constraint), update_refs=update_refs)

        _resolve_download_strategy(ctx, dep, tmp_path / "missing")

        assert dep.reference == "^1.2.0"
        assert ctx.git_semver_resolutions == {}
