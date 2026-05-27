"""Semver-aware version constraint resolution for marketplace dependencies.

Resolves a semver range (e.g. ``~2.1.0``, ``^2.0``, ``>=1.4``) against
git tags on a marketplace repository using the ``{name}--v{version}``
naming convention from the Claude Code plugin dependency spec.

Reuses the existing infrastructure:

- :class:`~apm_cli.marketplace.ref_resolver.RefResolver` for ``git ls-remote``
- :func:`~apm_cli.marketplace.tag_pattern.build_tag_regex` for tag pattern matching
- :func:`~apm_cli.marketplace._shared.iter_semver_tags` for tag iteration
- :func:`~apm_cli.marketplace.semver.satisfies_range` for range filtering
"""

from __future__ import annotations

import logging
import re

from ._shared import iter_semver_tags
from .errors import NoMatchingVersionError
from .ref_resolver import RefResolver
from .semver import satisfies_range
from .tag_pattern import build_tag_regex

logger = logging.getLogger(__name__)

_SEMVER_RANGE_CHARS = re.compile(r"[~^<>=!]")
_BARE_SEMVER = re.compile(r"^\d+\.\d+\.\d+")

DEFAULT_TAG_PATTERN = "{name}--v{version}"


def is_semver_range(spec: str) -> bool:
    """Return True if *spec* contains semver range operators (``~``, ``^``, ``>=``, etc.)."""
    return bool(_SEMVER_RANGE_CHARS.search(spec))


def is_version_constraint(spec: str) -> bool:
    """Return True if *spec* is a semver range or bare version, not a raw git ref.

    Matches range operators (``~2.1.0``, ``^2.0``, ``>=1.4``) and bare
    version numbers (``2.1.0``).  Does NOT match branch names (``main``),
    tag names with prefixes (``v2.1.0``), or commit SHAs.
    """
    return bool(_SEMVER_RANGE_CHARS.search(spec) or _BARE_SEMVER.match(spec))


def resolve_version_constraint(
    plugin_name: str,
    owner_repo: str,
    version_range: str,
    *,
    tag_pattern: str = DEFAULT_TAG_PATTERN,
    host: str | None = None,
    token: str | None = None,
) -> tuple[str, str]:
    """Resolve a semver range to the highest matching git tag.

    Lists tags on *owner_repo*, filters to those matching *tag_pattern*
    with *plugin_name* substituted, then returns the highest version
    satisfying *version_range*.

    Args:
        plugin_name: Plugin name used in the tag prefix
            (e.g. ``"secrets-vault"`` for tags like ``secrets-vault--v2.1.0``).
        owner_repo: Repository in ``owner/repo`` format.
        version_range: Semver range expression (e.g. ``"~2.1.0"``).
        tag_pattern: Tag naming convention. Defaults to
            ``"{name}--v{version}"``.
        host: Git host for ``git ls-remote``. Defaults to github.com.
        token: Optional auth token for private repos.

    Returns:
        ``(tag_name, commit_sha)`` of the highest matching version.

    Raises:
        NoMatchingVersionError: No tag satisfies the range.
    """
    pinned_pattern = tag_pattern.replace("{name}", plugin_name)
    tag_rx = build_tag_regex(pinned_pattern)

    resolver = RefResolver(host=host, token=token)
    try:
        refs = resolver.list_remote_refs(owner_repo)
    finally:
        resolver.close()

    candidates: list[tuple] = []
    for sv, tag_name, sha in iter_semver_tags(refs, tag_rx):
        if sv.is_prerelease:
            continue
        if satisfies_range(sv, version_range):
            candidates.append((sv, tag_name, sha))

    if not candidates:
        raise NoMatchingVersionError(
            plugin_name,
            version_range,
            detail=f"pattern='{tag_pattern}', remote='{owner_repo}'",
        )

    candidates.sort(key=lambda c: c[0], reverse=True)
    _best_sv, best_tag, best_sha = candidates[0]

    logger.debug(
        "Version constraint '%s' for %s resolved to tag '%s' (sha=%s)",
        version_range,
        plugin_name,
        best_tag,
        best_sha[:12],
    )

    return best_tag, best_sha
