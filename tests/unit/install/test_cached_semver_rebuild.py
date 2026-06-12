"""Tests for the cached git-semver resolution rebuild gate (PR #1496).

Regression-trap for the PR #1496 Copilot review thread: previously the
``CachedDependencySource.acquire`` path only checked
``dep_locked_chk.constraint`` and back-filled the other fields with
empty strings, which would propagate an incomplete
``GitSemverResolution`` into ``InstalledPackage`` and cause the
lockfile to be rewritten with empty ``version`` / ``resolved_tag`` /
``resolved_commit`` (and an empty ``resolved_ref``).

The fix lives in ``_rebuild_cached_semver_resolution``: it returns
``None`` unless ALL required fields (constraint, version,
resolved_tag, resolved_commit) are present.
"""

from __future__ import annotations

import pytest

from apm_cli.deps.git_semver_resolver import GitSemverResolution
from apm_cli.deps.lockfile import LockedDependency
from apm_cli.install.sources import _rebuild_cached_semver_resolution


def _make_locked(
    *,
    constraint: str | None = "^1.2.0",
    version: str | None = "1.5.3",
    resolved_tag: str | None = "v1.5.3",
    resolved_commit: str | None = "c" * 40,
    resolved_at: str | None = "2024-06-15T12:00:00+00:00",
) -> LockedDependency:
    return LockedDependency(
        repo_url="acme/some-skills",
        host="github.com",
        port=None,
        registry_prefix=None,
        resolved_commit=resolved_commit,
        resolved_ref=resolved_tag,
        version=version,
        local_path="apm_modules/some-skills",
        depth=1,
        resolved_by=None,
        is_dev=False,
        constraint=constraint,
        resolved_tag=resolved_tag,
        resolved_at=resolved_at,
    )


class TestRebuildCachedSemverResolution:
    def test_returns_resolution_when_all_required_fields_present(self) -> None:
        locked = _make_locked()
        result = _rebuild_cached_semver_resolution(locked)
        assert isinstance(result, GitSemverResolution)
        assert result.constraint == "^1.2.0"
        assert result.resolved_version == "1.5.3"
        assert result.resolved_tag == "v1.5.3"
        assert result.resolved_sha == "c" * 40

    def test_returns_none_when_dep_is_none(self) -> None:
        assert _rebuild_cached_semver_resolution(None) is None

    def test_returns_none_when_constraint_missing(self) -> None:
        assert _rebuild_cached_semver_resolution(_make_locked(constraint=None)) is None

    @pytest.mark.parametrize(
        "missing_field",
        ["version", "resolved_tag", "resolved_commit"],
    )
    def test_returns_none_when_any_required_field_missing(self, missing_field: str) -> None:
        """Mutation-trap: if any required field is missing, rebuild aborts.

        If the gate is loosened (e.g. back to ``and dep_locked_chk.constraint``
        only), each of these cases would return a ``GitSemverResolution``
        with empty strings -- which is exactly the bug the PR #1496 review
        thread called out.
        """
        kwargs: dict[str, str | None] = {missing_field: None}
        locked = _make_locked(**kwargs)
        assert _rebuild_cached_semver_resolution(locked) is None

    def test_returns_none_when_required_field_is_empty_string(self) -> None:
        """Empty strings are as harmful as ``None`` -- truthiness check guards both."""
        locked = _make_locked(resolved_tag="")
        assert _rebuild_cached_semver_resolution(locked) is None

    def test_missing_resolved_at_is_tolerated(self) -> None:
        """``resolved_at`` is not required: it's an audit field, not a trust anchor."""
        locked = _make_locked(resolved_at=None)
        result = _rebuild_cached_semver_resolution(locked)
        assert isinstance(result, GitSemverResolution)
        assert result.resolved_at == ""
