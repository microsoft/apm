"""Tests for git-semver wiring in the install resolve phase and drift detection.

Covers issue #1488:

- ``_maybe_resolve_git_semver`` correctly routes git-source semver-range deps
  through ``GitSemverResolver`` and falls back to lockfile replay when the
  constraint is unchanged.
- ``drift.detect_ref_change`` does not report drift when the manifest
  carries a semver range and the lockfile holds the resolved tag, as long
  as the constraint matches the locked constraint.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest  # noqa: F401

from apm_cli.deps.git_semver_resolver import GitSemverResolution
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.drift import detect_ref_change
from apm_cli.install.phases.resolve import _maybe_resolve_git_semver
from apm_cli.models.dependency.reference import DependencyReference


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


class TestMaybeResolveGitSemver:
    def test_returns_none_for_local_dep(self):
        dep = _make_dep_ref(is_local=True)
        assert (
            _maybe_resolve_git_semver(dep_ref=dep, existing_lockfile=None, update_refs=False)
            is None
        )

    def test_returns_none_for_registry_dep(self):
        dep = _make_dep_ref(source="registry")
        assert (
            _maybe_resolve_git_semver(dep_ref=dep, existing_lockfile=None, update_refs=False)
            is None
        )

    def test_returns_none_for_proxy_dep(self):
        dep = _make_dep_ref(artifactory_prefix="my-prefix")
        assert (
            _maybe_resolve_git_semver(dep_ref=dep, existing_lockfile=None, update_refs=False)
            is None
        )

    def test_returns_none_for_literal_ref(self):
        dep = _make_dep_ref(reference="v1.2.3")
        assert (
            _maybe_resolve_git_semver(dep_ref=dep, existing_lockfile=None, update_refs=False)
            is None
        )

    def test_returns_none_for_none_ref(self):
        dep = _make_dep_ref(reference=None)
        assert (
            _maybe_resolve_git_semver(dep_ref=dep, existing_lockfile=None, update_refs=False)
            is None
        )

    def test_lockfile_replay_on_unchanged_constraint(self):
        """When lockfile already records the same constraint, replay without network."""
        dep = _make_dep_ref(reference="^1.2.0")
        lockfile = LockFile()
        lockfile.dependencies[dep.get_unique_key()] = LockedDependency(
            host="github.com",
            repo_url="acme/widget",
            source="github",
            resolved_ref="v1.5.3",
            resolved_commit="a" * 40,
            version="1.5.3",
            constraint="^1.2.0",
            resolved_tag="v1.5.3",
            resolved_at="2025-01-15T12:00:00Z",
        )

        # Patch RefResolver / GitSemverResolver so any accidental network
        # call would blow up the test loudly.
        with patch("apm_cli.marketplace.ref_resolver.RefResolver") as rr_mock:
            resolution = _maybe_resolve_git_semver(
                dep_ref=dep, existing_lockfile=lockfile, update_refs=False
            )
            assert rr_mock.called is False

        assert isinstance(resolution, GitSemverResolution)
        assert resolution.constraint == "^1.2.0"
        assert resolution.resolved_tag == "v1.5.3"
        assert resolution.resolved_version == "1.5.3"
        assert resolution.resolved_sha == "a" * 40

    def test_fresh_resolution_when_update_refs(self):
        """With --update, ignore lockfile and call out to RefResolver."""
        dep = _make_dep_ref(reference="^1.2.0")
        lockfile = LockFile()
        lockfile.dependencies[dep.get_unique_key()] = LockedDependency(
            host="github.com",
            repo_url="acme/widget",
            source="github",
            resolved_ref="v1.5.3",
            resolved_commit="a" * 40,
            version="1.5.3",
            constraint="^1.2.0",
            resolved_tag="v1.5.3",
            resolved_at="2025-01-15T12:00:00Z",
        )

        fresh = GitSemverResolution(
            constraint="^1.2.0",
            resolved_version="1.6.0",
            resolved_tag="v1.6.0",
            resolved_sha="b" * 40,
            matched_pattern="v{version}",
            resolved_at="2025-02-01T00:00:00Z",
        )
        with patch("apm_cli.deps.git_semver_resolver.GitSemverResolver") as resolver_cls:
            instance = MagicMock()
            instance.resolve.return_value = fresh
            resolver_cls.return_value = instance

            resolution = _maybe_resolve_git_semver(
                dep_ref=dep, existing_lockfile=lockfile, update_refs=True
            )

        assert resolution is fresh

    def test_lockfile_replay_skipped_when_constraint_changed(self):
        """If the manifest constraint differs from the locked constraint,
        replay is skipped and a fresh resolution kicks in."""
        dep = _make_dep_ref(reference="^2.0.0")  # manifest bumped
        lockfile = LockFile()
        lockfile.dependencies[dep.get_unique_key()] = LockedDependency(
            host="github.com",
            repo_url="acme/widget",
            source="github",
            resolved_ref="v1.5.3",
            resolved_commit="a" * 40,
            version="1.5.3",
            constraint="^1.2.0",  # stale
            resolved_tag="v1.5.3",
            resolved_at="2025-01-15T12:00:00Z",
        )

        fresh = GitSemverResolution(
            constraint="^2.0.0",
            resolved_version="2.1.0",
            resolved_tag="v2.1.0",
            resolved_sha="c" * 40,
            matched_pattern="v{version}",
            resolved_at="2025-02-01T00:00:00Z",
        )
        with patch("apm_cli.deps.git_semver_resolver.GitSemverResolver") as resolver_cls:
            instance = MagicMock()
            instance.resolve.return_value = fresh
            resolver_cls.return_value = instance

            resolution = _maybe_resolve_git_semver(
                dep_ref=dep, existing_lockfile=lockfile, update_refs=False
            )

        assert resolution is fresh


class TestDriftDetectRefChangeForSemver:
    """``detect_ref_change`` must not report drift when the manifest carries
    a semver range and the lockfile's recorded constraint is identical --
    even though ``dep_ref.reference`` (``^1.2.0``) differs from the locked
    ``resolved_ref`` (``v1.5.3``)."""

    def _locked(self, *, constraint, resolved_ref="v1.5.3"):
        return LockedDependency(
            host="github.com",
            repo_url="acme/widget",
            source="github",
            resolved_ref=resolved_ref,
            resolved_commit="a" * 40,
            version="1.5.3",
            constraint=constraint,
            resolved_tag=resolved_ref,
            resolved_at="2025-01-15T12:00:00Z",
        )

    def test_no_drift_when_constraint_unchanged(self):
        dep = _make_dep_ref(reference="^1.2.0")
        locked = self._locked(constraint="^1.2.0")
        assert detect_ref_change(dep, locked, update_refs=False) is False

    def test_drift_when_constraint_changed(self):
        dep = _make_dep_ref(reference="^2.0.0")
        locked = self._locked(constraint="^1.2.0")
        assert detect_ref_change(dep, locked, update_refs=False) is True

    def test_no_false_drift_against_literal_locked_tag(self):
        """The classic bug: ``^1.2.0`` != ``v1.5.3`` substring-comparison
        used to trip a false drift. With the new branch, equal constraint
        wins regardless of resolved_ref."""
        dep = _make_dep_ref(reference="^1.2.0")
        locked = self._locked(constraint="^1.2.0", resolved_ref="v1.5.3")
        assert detect_ref_change(dep, locked, update_refs=False) is False
