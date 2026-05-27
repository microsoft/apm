"""Tests for ``apm_cli.deps.git_semver_resolver``."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.deps.git_semver_resolver import (
    DEFAULT_TAG_PATTERNS,
    GitSemverResolution,
    GitSemverResolver,
    NoMatchingTagError,
    iter_semver_tags,
)
from apm_cli.marketplace.ref_resolver import RemoteRef

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _refs_from(*tags_with_sha: tuple[str, str]) -> list[RemoteRef]:
    """Build ``RemoteRef`` objects from ``(tag_name, sha)`` tuples."""
    return [RemoteRef(name=f"refs/tags/{name}", sha=sha) for name, sha in tags_with_sha]


def _ref_resolver_returning(refs: list[RemoteRef]) -> MagicMock:
    """Build a stub ``RefResolver`` whose ``list_remote_refs`` returns *refs*."""
    rr = MagicMock()
    rr.list_remote_refs.return_value = list(refs)
    return rr


_FROZEN_NOW = "2024-06-15T12:00:00+00:00"


# ---------------------------------------------------------------------------
# GitSemverResolver.resolve()
# ---------------------------------------------------------------------------


class TestResolveCaretRange:
    """Caret range resolves to the highest matching tag."""

    def test_resolve_caret_range_picks_highest_matching_tag(self) -> None:
        refs = _refs_from(
            ("v1.0.0", "a" * 40),
            ("v1.2.0", "b" * 40),
            ("v1.5.3", "c" * 40),
            ("v2.0.0", "d" * 40),
        )
        resolver = GitSemverResolver(_ref_resolver_returning(refs))

        result = resolver.resolve(
            owner_repo="acme/some-skills",
            package_name="some-skills",
            constraint="^1.2.0",
            now_iso=_FROZEN_NOW,
        )

        assert isinstance(result, GitSemverResolution)
        assert result.constraint == "^1.2.0"
        assert result.resolved_version == "1.5.3"
        assert result.resolved_tag == "v1.5.3"
        assert result.resolved_sha == "c" * 40
        assert result.matched_pattern == "v{version}"
        assert result.resolved_at == _FROZEN_NOW


class TestPatternFallback:
    """Pattern-list fallthrough behaviour."""

    def test_resolve_falls_through_to_secondary_pattern_when_primary_misses(self) -> None:
        # Only the {name}--v{version} convention is used; primary v{version} produces nothing.
        refs = _refs_from(
            ("some-skills--v1.2.0", "a" * 40),
            ("some-skills--v1.4.0", "b" * 40),
            ("unrelated-tag", "c" * 40),
        )
        resolver = GitSemverResolver(_ref_resolver_returning(refs))

        result = resolver.resolve(
            owner_repo="acme/some-skills",
            package_name="some-skills",
            constraint="^1.0.0",
        )

        assert result.resolved_version == "1.4.0"
        assert result.resolved_tag == "some-skills--v1.4.0"
        assert result.matched_pattern == "{name}--v{version}"

    def test_resolve_prefers_higher_version_when_both_patterns_match(self) -> None:
        # Both v{version} and {name}--v{version} match different versions.
        # The picker chooses the highest version regardless of pattern order.
        refs = _refs_from(
            ("v1.2.0", "a" * 40),
            ("some-skills--v1.5.3", "b" * 40),
        )
        resolver = GitSemverResolver(_ref_resolver_returning(refs))

        result = resolver.resolve(
            owner_repo="acme/some-skills",
            package_name="some-skills",
            constraint="^1.0.0",
        )

        assert result.resolved_version == "1.5.3"
        assert result.resolved_tag == "some-skills--v1.5.3"
        assert result.matched_pattern == "{name}--v{version}"


class TestPrereleaseHandling:
    """Prerelease tags are excluded by default and opt-in via flag."""

    def test_resolve_excludes_prereleases_by_default(self) -> None:
        refs = _refs_from(
            ("v1.5.3", "a" * 40),
            ("v2.0.0-beta.1", "b" * 40),
        )
        resolver = GitSemverResolver(_ref_resolver_returning(refs))

        result = resolver.resolve(
            owner_repo="acme/foo",
            package_name="foo",
            constraint=">=1.0.0",
        )

        assert result.resolved_version == "1.5.3"
        assert result.resolved_tag == "v1.5.3"

    def test_resolve_includes_prereleases_when_flag_set(self) -> None:
        refs = _refs_from(
            ("v1.5.3", "a" * 40),
            ("v2.0.0-beta.1", "b" * 40),
        )
        resolver = GitSemverResolver(
            _ref_resolver_returning(refs),
            include_prerelease=True,
        )

        result = resolver.resolve(
            owner_repo="acme/foo",
            package_name="foo",
            constraint=">=1.0.0",
        )

        assert result.resolved_version == "2.0.0-beta.1"
        assert result.resolved_tag == "v2.0.0-beta.1"


class TestNoMatch:
    """Error path: no matching tag."""

    def test_resolve_no_matching_tag_raises_with_actionable_hint(self) -> None:
        refs = _refs_from(
            ("v0.9.0", "a" * 40),
            ("v1.0.0", "b" * 40),
        )
        resolver = GitSemverResolver(_ref_resolver_returning(refs))

        with pytest.raises(NoMatchingTagError) as excinfo:
            resolver.resolve(
                owner_repo="acme/foo",
                package_name="foo",
                constraint="^2.0.0",
            )

        err = excinfo.value
        # The exception carries both a summary (what we looked at) and a hint
        # (what the user can do). Both must be present and actionable.
        assert "acme/foo" in err.summary
        assert "^2.0.0" in err.summary
        assert "v0.9.0" in err.summary or "v1.0.0" in err.summary
        assert err.hint
        assert "pin" in err.hint.lower() or "widen" in err.hint.lower()

    def test_resolve_no_tags_at_all_raises_with_empty_remote_hint(self) -> None:
        resolver = GitSemverResolver(_ref_resolver_returning([]))

        with pytest.raises(NoMatchingTagError) as excinfo:
            resolver.resolve(
                owner_repo="acme/foo",
                package_name="foo",
                constraint="^1.0.0",
            )

        # When zero tag refs are present we still surface a usable message.
        assert "no tag refs" in excinfo.value.summary.lower()


class TestRefResolverCaching:
    """Two resolves against the same remote reuse the underlying cache."""

    def test_resolve_uses_ref_resolver_cache_on_second_call(self) -> None:
        refs = _refs_from(
            ("v1.0.0", "a" * 40),
            ("v1.2.0", "b" * 40),
        )
        rr = _ref_resolver_returning(refs)
        # Resolver delegates caching to the ref_resolver. Verify the resolver
        # never reaches past list_remote_refs (i.e. we don't shortcircuit
        # the cache contract by calling resolve_ref_sha directly).
        resolver = GitSemverResolver(rr)

        resolver.resolve(
            owner_repo="acme/foo",
            package_name="foo",
            constraint="^1.0.0",
        )
        resolver.resolve(
            owner_repo="acme/foo",
            package_name="foo",
            constraint="^1.0.0",
        )

        # list_remote_refs is the only network-ish call we make.
        # The real RefResolver caches it; here we just assert we
        # consistently route through that single entry point.
        assert rr.list_remote_refs.call_count == 2
        rr.list_remote_refs.assert_called_with("acme/foo")
        # Crucially we do NOT call resolve_ref_sha — that would bypass
        # the cache for our concrete-tag lookup.
        rr.resolve_ref_sha.assert_not_called()


class TestBareVersionTagFallback:
    """Regression trap: literal ``1.2.3`` tag with no ``v`` prefix is resolvable."""

    def test_resolve_bare_version_tag_falls_through_to_third_pattern(self) -> None:
        # The only tag uses the bare-version convention. Default patterns
        # (``v{version}`` and ``{name}--v{version}``) would miss; the
        # bare-version fallback must catch it.
        refs = _refs_from(
            ("1.2.3", "a" * 40),
            ("1.5.0", "b" * 40),
        )
        resolver = GitSemverResolver(_ref_resolver_returning(refs))

        result = resolver.resolve(
            owner_repo="acme/bare",
            package_name="bare",
            constraint="^1.0.0",
        )

        assert result.resolved_version == "1.5.0"
        assert result.resolved_tag == "1.5.0"
        assert result.matched_pattern == "{version}"

    def test_resolve_v_prefixed_wins_over_bare_when_both_present(self) -> None:
        # When both conventions exist, the primary v{version} pattern
        # matches first and the bare fallback is never consulted.
        # This is intentional: bare-version is a *fallback*, not a
        # competing default.
        refs = _refs_from(
            ("v1.5.3", "a" * 40),
            ("1.5.3", "b" * 40),
        )
        resolver = GitSemverResolver(_ref_resolver_returning(refs))

        result = resolver.resolve(
            owner_repo="acme/mixed",
            package_name="mixed",
            constraint="^1.0.0",
        )

        assert result.resolved_tag == "v1.5.3"
        assert result.matched_pattern == "v{version}"


# ---------------------------------------------------------------------------
# iter_semver_tags  (internal but worth its own surface)
# ---------------------------------------------------------------------------


class TestIterSemverTags:
    """The internal tag iterator skips non-tag refs and invalid versions."""

    def test_skips_branch_refs(self) -> None:
        refs = [
            RemoteRef(name="refs/heads/main", sha="a" * 40),
            RemoteRef(name="refs/tags/v1.0.0", sha="b" * 40),
        ]
        out = iter_semver_tags(refs, package_name="foo", patterns=DEFAULT_TAG_PATTERNS)
        assert len(out) == 1
        assert out[0][1] == "v1.0.0"

    def test_skips_non_semver_tags(self) -> None:
        refs = [
            RemoteRef(name="refs/tags/release-candidate", sha="a" * 40),
            RemoteRef(name="refs/tags/v1.0.0", sha="b" * 40),
        ]
        out = iter_semver_tags(refs, package_name="foo", patterns=DEFAULT_TAG_PATTERNS)
        tags = [t[1] for t in out]
        assert tags == ["v1.0.0"]

    def test_name_placeholder_scoped_to_package_name(self) -> None:
        """``{name}--v{version}`` must only match tags for the requested package.

        Regression-trap for PR #1496 review thread: previously
        ``iter_semver_tags`` accepted ``package_name`` but never used it,
        leaving ``{name}`` as a wildcard (``[^/]+``).  In a repo that
        publishes multiple ``{name}--v{version}`` tag families, the
        resolver could then accept a sibling package's tag (e.g.
        ``otherpkg--v9.9.9``) when asked to resolve ``mypkg``.
        """
        refs = _refs_from(
            ("mypkg--v1.0.0", "a" * 40),
            ("mypkg--v1.2.0", "b" * 40),
            ("otherpkg--v9.9.9", "c" * 40),
        )
        out = iter_semver_tags(
            refs,
            package_name="mypkg",
            patterns=("{name}--v{version}",),
        )
        tags = sorted(t[1] for t in out)
        assert tags == ["mypkg--v1.0.0", "mypkg--v1.2.0"]
        assert "otherpkg--v9.9.9" not in tags

    def test_resolver_does_not_pick_sibling_package_tag(self) -> None:
        """Highest-version picker must ignore tags belonging to other packages."""
        refs = _refs_from(
            ("mypkg--v1.0.0", "a" * 40),
            ("otherpkg--v9.9.9", "c" * 40),
        )
        resolver = GitSemverResolver(_ref_resolver_returning(refs))

        result = resolver.resolve(
            owner_repo="acme/mypkg",
            package_name="mypkg",
            constraint=">=1.0.0",
            tag_patterns=("{name}--v{version}",),
        )

        assert result.resolved_tag == "mypkg--v1.0.0"
        assert result.resolved_version == "1.0.0"
