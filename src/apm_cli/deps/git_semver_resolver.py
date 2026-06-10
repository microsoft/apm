"""Resolve semver ranges on git-source dependencies against repo tags.

This module is the git-source counterpart of the registry/marketplace
semver resolvers.  When an author writes ``acme/foo#^1.2.0`` (string
shorthand) or ``ref: ^1.2.0`` (object form) in ``apm.yml``, the install
pipeline calls :class:`GitSemverResolver` to map the constraint to a
concrete tag on the remote.

Resolution algorithm
--------------------
1. Call :meth:`apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs`
   to enumerate the remote's refs (cached for 5 minutes).
2. Filter tag refs through each tag pattern in order
   (``DEFAULT_TAG_PATTERNS``: ``v{version}`` then ``{name}--v{version}``,
   followed by a bare ``{version}`` fallback).
3. Discard pre-release versions unless ``include_prerelease=True``.
4. Of all remaining candidates across all patterns, pick the highest
   :class:`~apm_cli.marketplace.semver.SemVer` that satisfies the
   constraint.

The module purposefully avoids any new transport or cache code -- it
composes ``RefResolver`` (transport + cache), ``tag_pattern.build_tag_regex``
(pattern matching) and ``marketplace.semver`` (version arithmetic).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from ..marketplace.ref_resolver import RefResolver, RemoteRef
from ..marketplace.semver import SemVer, parse_semver, satisfies_range
from ..marketplace.tag_pattern import build_tag_regex

__all__ = [
    "DEFAULT_TAG_PATTERNS",
    "FALLBACK_BARE_PATTERN",
    "GitSemverResolution",
    "GitSemverResolver",
    "NoMatchingTagError",
    "iter_semver_tags",
    "utc_now_iso",
]

# Default tag-pattern fallback order.
#
# The two-pattern default mirrors the conventions APM already commits to
# elsewhere:
#
# * ``v{version}`` -- universal lockstep convention (most projects).
# * ``{name}--v{version}`` -- Claude Code / PR #1422 per-package convention
#   used by multi-marketplace repos.
# * ``{name}-v{version}`` -- common single-dash per-package convention
#   accepted by marketplace tag-pattern helpers. The semver regex still
#   requires a full version, so branch-like names such as ``pkg-v1`` do not match.
DEFAULT_TAG_PATTERNS: tuple[str, ...] = (
    "v{version}",
    "{name}--v{version}",
    "{name}-v{version}",
)

# Bare-version fallback (``1.2.3`` literal, no prefix).
#
# Only consulted when neither default pattern produced a match.  Without
# this fallback, an author who tags releases as ``1.2.3`` (no leading
# ``v``) and writes ``ref: 1.2.3`` would see ``NoMatchingTagError``
# despite the tag existing -- because ``1.2.3`` parses as a semver
# *range* (exact-version match) and would never round-trip through the
# branch/literal-tag path.  Tested by
# ``resolve_bare_version_tag_falls_through_to_third_pattern``.
FALLBACK_BARE_PATTERN: str = "{version}"

_REFS_TAGS_PREFIX = "refs/tags/"


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (no microseconds).

    Centralised so :class:`GitSemverResolution` and tests share a single
    spelling.  Kept out of :mod:`datetime` import sites to make the
    timestamp easy to stub in tests.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class NoMatchingTagError(Exception):
    """Raised when no tag on the remote satisfies the requested constraint.

    Carries an actionable hint -- the resolver lists the patterns that
    were tried and (when available) the highest version it did find so
    authors can widen the range or pin a literal tag.
    """

    def __init__(self, summary: str, hint: str) -> None:
        super().__init__(f"{summary}\n{hint}" if hint else summary)
        self.summary = summary
        self.hint = hint


@dataclass(frozen=True)
class GitSemverResolution:
    """One git-source semver resolution result.

    Attributes
    ----------
    constraint:
        The original spec the author wrote (e.g. ``"^1.2.0"``).  Preserved
        verbatim into the lockfile so ``apm install`` is reproducible
        and ``apm update`` knows what to widen.
    resolved_version:
        Concrete version string the constraint resolved to (e.g. ``"1.5.3"``).
    resolved_tag:
        Concrete tag name on the remote (e.g. ``"v1.5.3"``).
    resolved_sha:
        40-char hex SHA the tag points to.
    matched_pattern:
        The tag pattern that produced the winning tag (one of
        ``DEFAULT_TAG_PATTERNS`` or ``FALLBACK_BARE_PATTERN``).
    resolved_at:
        ISO-8601 UTC timestamp captured at resolution time.  Surfaced in
        the lockfile so audits can answer "when was this tag picked?"
        without re-running ``git ls-remote``.
    """

    constraint: str
    resolved_version: str
    resolved_tag: str
    resolved_sha: str
    matched_pattern: str
    resolved_at: str


def iter_semver_tags(
    refs: Sequence[RemoteRef],
    *,
    package_name: str,
    patterns: Sequence[str],
) -> list[tuple[SemVer, str, str, str]]:
    """Yield ``(version, tag_name, sha, pattern)`` for tags matching any pattern.

    Each tag may match multiple patterns; in that case it appears once
    per matching pattern.  Callers downstream pick the highest version
    across all matches, so duplicates resolve to the same concrete tag
    even when patterns overlap.

    ``{name}`` placeholders in *patterns* are expanded with the literal
    *package_name* (regex-escaped) before regex compilation, so that
    a pattern like ``{name}--v{version}`` matches only tags scoped to
    the expected package (e.g. ``some-skills--v1.2.0``) and does not
    accept tags belonging to other packages (e.g. ``otherpkg--v9.9.9``)
    in repositories that publish multiple ``{name}--v{version}`` tag
    families. See issue #1488 review thread.

    Non-tag refs (``refs/heads/*``) and peeled-tag refs are skipped.
    """
    expanded = [pat.replace("{name}", package_name) for pat in patterns]
    compiled = [(pat, build_tag_regex(exp)) for pat, exp in zip(patterns, expanded, strict=True)]
    out: list[tuple[SemVer, str, str, str]] = []
    for ref in refs:
        if not ref.name.startswith(_REFS_TAGS_PREFIX):
            continue
        tag_name = ref.name[len(_REFS_TAGS_PREFIX) :]
        for pattern, rx in compiled:
            # ``build_tag_regex`` receives a pattern where any ``{name}``
            # has already been substituted with the literal package_name,
            # so the resulting regex is scoped to this package's tag
            # family and never accepts tags belonging to a sibling
            # package in the same repository.
            m = rx.match(tag_name)
            if not m:
                continue
            try:
                version_str = m.group("version")
            except (IndexError, KeyError):
                continue
            v = parse_semver(version_str)
            if v is None:
                continue
            out.append((v, tag_name, ref.sha, pattern))
            # Allow same tag to match multiple patterns; the picker
            # de-duplicates by (version, sha).
    return out


class GitSemverResolver:
    """Resolve a semver constraint against a remote's git tags.

    Composition only: holds a :class:`RefResolver` for transport + cache
    and applies the pattern + semver logic on top.  Stateless besides
    the underlying ref cache.

    Parameters
    ----------
    ref_resolver:
        The :class:`RefResolver` to use for ``git ls-remote`` calls.
    include_prerelease:
        When ``True``, prerelease versions are eligible.  Defaults to
        ``False`` (mirrors the marketplace resolver's default).
    """

    def __init__(
        self,
        ref_resolver: RefResolver,
        *,
        include_prerelease: bool = False,
    ) -> None:
        self._ref_resolver = ref_resolver
        self._include_prerelease = include_prerelease

    @property
    def include_prerelease(self) -> bool:
        """Whether prerelease versions are eligible for resolution."""
        return self._include_prerelease

    def resolve(
        self,
        *,
        owner_repo: str,
        package_name: str,
        constraint: str,
        tag_patterns: Sequence[str] = DEFAULT_TAG_PATTERNS,
        now_iso: str | None = None,
    ) -> GitSemverResolution:
        """Resolve *constraint* to a concrete tag on ``owner_repo``.

        Parameters
        ----------
        owner_repo:
            ``"owner/repo"`` string (no host, no ``.git`` suffix).
        package_name:
            Package name used to expand ``{name}`` placeholders in
            patterns.  Conventionally the trailing path segment of
            ``owner_repo``.
        constraint:
            Semver range (e.g. ``"^1.2.0"``, ``"~2.1"``, ``">=1.0 <2.0"``)
            or exact version (``"1.2.3"``).
        tag_patterns:
            Ordered tag patterns to try.  Defaults to
            :data:`DEFAULT_TAG_PATTERNS`.  A bare-version fallback
            (:data:`FALLBACK_BARE_PATTERN`) is appended automatically
            only when no candidate matches the user-supplied patterns
            (see the "Risks" discussion in issue #1488).
        now_iso:
            Override for the ISO-8601 ``resolved_at`` field; tests use
            this to keep assertions deterministic.

        Returns
        -------
        GitSemverResolution
            Constraint + winning tag + version + SHA + matched pattern.

        Raises
        ------
        NoMatchingTagError
            When no tag on the remote satisfies the constraint after
            trying the default patterns and the bare-version fallback.
        """
        refs = self._ref_resolver.list_remote_refs(owner_repo)
        primary_patterns = tuple(tag_patterns)
        # Two-pass: try the author-supplied patterns first; only if they
        # find zero candidates do we widen to the bare-version fallback.
        # That preserves the "literal 1.2.3 tag" edge case without
        # silently promoting bare numeric tags to first-class status.
        winner = self._pick_best(refs, package_name, primary_patterns, constraint)
        if winner is None:
            fallback_patterns = (FALLBACK_BARE_PATTERN,)
            winner = self._pick_best(refs, package_name, fallback_patterns, constraint)
        if winner is None:
            raise NoMatchingTagError(
                summary=self._format_no_match_summary(
                    owner_repo=owner_repo,
                    constraint=constraint,
                    refs=refs,
                ),
                hint=self._format_no_match_hint(constraint=constraint),
            )
        version, tag_name, sha, pattern = winner
        return GitSemverResolution(
            constraint=constraint,
            resolved_version=self._render_version(version),
            resolved_tag=tag_name,
            resolved_sha=sha,
            matched_pattern=pattern,
            resolved_at=now_iso or utc_now_iso(),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pick_best(
        self,
        refs: Sequence[RemoteRef],
        package_name: str,
        patterns: Sequence[str],
        constraint: str,
    ) -> tuple[SemVer, str, str, str] | None:
        """Return the highest-version candidate or ``None``."""
        candidates = iter_semver_tags(refs, package_name=package_name, patterns=patterns)
        if not candidates:
            return None
        filtered: list[tuple[SemVer, str, str, str]] = []
        for cand in candidates:
            version = cand[0]
            if version.is_prerelease and not self._include_prerelease:
                continue
            if not satisfies_range(version, constraint):
                continue
            filtered.append(cand)
        if not filtered:
            return None
        # Sort by SemVer ordering; the last entry is the winner.
        filtered.sort(key=lambda t: t[0])
        return filtered[-1]

    @staticmethod
    def _render_version(version: SemVer) -> str:
        """Render a :class:`SemVer` back into its canonical string form."""
        base = f"{version.major}.{version.minor}.{version.patch}"
        if version.prerelease:
            base = f"{base}-{version.prerelease}"
        if version.build_meta:
            base = f"{base}+{version.build_meta}"
        return base

    @staticmethod
    def _format_no_match_summary(
        *,
        owner_repo: str,
        constraint: str,
        refs: Sequence[RemoteRef],
    ) -> str:
        tags = [
            r.name[len(_REFS_TAGS_PREFIX) :] for r in refs if r.name.startswith(_REFS_TAGS_PREFIX)
        ]
        if not tags:
            return f"No tags on {owner_repo} satisfy {constraint!r}. The remote has no tag refs."
        sample = ", ".join(sorted(tags)[:5])
        return (
            f"No tags on {owner_repo} satisfy {constraint!r}. "
            f"Tags considered: {sample}"
            f"{' (and more)' if len(tags) > 5 else ''}."
        )

    @staticmethod
    def _format_no_match_hint(*, constraint: str) -> str:
        return (
            "Hint: widen the range, pin a literal tag with "
            f"'ref: <tag>' instead of '{constraint}', or fall back to "
            "a branch / SHA ref."
        )
