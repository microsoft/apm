"""Tag-pattern expansion and regex builder for marketplace version tags.

Marketplace entries may specify a ``tag_pattern`` (e.g. ``"v{version}"``
or ``"{name}-v{version}"``) that describes how git tags map to semver
versions.  This module provides two helpers:

* ``render_tag`` -- expand ``{name}`` and ``{version}`` placeholders
  into a concrete tag string.
* ``build_tag_regex`` -- compile a pattern into a regex that captures
  the ``{version}`` portion from an arbitrary tag.

The pattern engine is intentionally minimal: only ``{version}`` and
``{name}`` are recognised.  All other text is treated as literal.
"""

from __future__ import annotations

import re

__all__ = [
    "DEFAULT_TAG_PATTERNS",
    "build_tag_regex",
    "infer_tag_pattern",
    "infer_tag_pattern_from_refs",
    "is_version_tag_ref",
    "parse_tag_version",
    "render_tag",
]

# Common tag layouts tried when no explicit ``tag_pattern`` is configured.
DEFAULT_TAG_PATTERNS: tuple[str, ...] = (
    "v{version}",
    "{version}",
    "{name}_v{version}",
    "{name}--v{version}",
    "{name}-v{version}",
)

_PLAIN_SEMVER_RE = re.compile(
    r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

# Placeholders we recognise.
_PLACEHOLDER_VERSION = "{version}"
_PLACEHOLDER_NAME = "{name}"


def render_tag(pattern: str, *, name: str, version: str) -> str:
    """Expand ``{name}`` and ``{version}`` placeholders in *pattern*.

    Parameters
    ----------
    pattern:
        Tag pattern string, e.g. ``"v{version}"`` or ``"{name}-v{version}"``.
    name:
        Package name to substitute for ``{name}``.
    version:
        Version string (e.g. ``"1.2.3"``) to substitute for ``{version}``.

    Returns
    -------
    str
        The expanded tag string.
    """
    result = pattern.replace(_PLACEHOLDER_VERSION, version)
    result = result.replace(_PLACEHOLDER_NAME, name)
    return result


def build_tag_regex(pattern: str, *, name: str | None = None) -> re.Pattern[str]:
    """Return a compiled regex that captures ``{version}`` from a tag.

    Literal text in *pattern* is escaped so that special regex characters
    (e.g. dots, parens) are matched verbatim.  ``{version}`` becomes a
    named capture group ``(?P<version>...)`` matching a semver-like
    string.  ``{name}`` becomes a non-capturing wildcard ``[^/]+``, or
    the literal *name* when provided (for monorepo per-package tags).

    Parameters
    ----------
    pattern:
        Tag pattern string, e.g. ``"v{version}"``.
    name:
        When set and the pattern contains ``{name}``, match only this
        package name instead of any ``[^/]+`` segment.

    Returns
    -------
    re.Pattern[str]
        Compiled regex with a ``version`` named group.

    Examples
    --------
    >>> rx = build_tag_regex("v{version}")
    >>> m = rx.match("v1.2.3")
    >>> m.group("version")
    '1.2.3'
    """
    # Split pattern around placeholders, escape literal segments, then
    # rejoin with regex fragments.
    #
    # Strategy: replace placeholders with unique sentinels, escape the
    # whole string, then swap sentinels for regex fragments.
    _sentinel_version = "\x00VERSION\x00"
    _sentinel_name = "\x00NAME\x00"

    temp = pattern.replace(_PLACEHOLDER_VERSION, _sentinel_version)
    temp = temp.replace(_PLACEHOLDER_NAME, _sentinel_name)

    escaped = re.escape(temp)

    # Semver-like version capture: digits.digits.digits with optional
    # prerelease and build metadata.
    _VERSION_RX = (
        r"(?P<version>"
        r"\d+\.\d+\.\d+"
        r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        r")"
    )

    escaped = escaped.replace(re.escape(_sentinel_version), _VERSION_RX)
    name_rx = re.escape(name) if _PLACEHOLDER_NAME in pattern and name else r"[^/]+"
    escaped = escaped.replace(re.escape(_sentinel_name), name_rx)

    return re.compile(r"^" + escaped + r"$")


def infer_tag_pattern(tag: str, package_name: str = "") -> str | None:
    """Return the first default pattern that matches *tag*, or ``None``.

    When *package_name* is set, patterns containing ``{name}`` only match
    tags for that package (monorepo-safe).
    """
    for pattern in DEFAULT_TAG_PATTERNS:
        rx = (
            build_tag_regex(pattern, name=package_name)
            if _PLACEHOLDER_NAME in pattern and package_name
            else build_tag_regex(pattern)
        )
        if rx.match(tag):
            return pattern
    return None


def infer_tag_pattern_from_refs(refs: list, package_name: str = "") -> str | None:
    """Infer a tag pattern from the first semver-like tag in *refs*."""
    for remote_ref in refs:
        name = getattr(remote_ref, "name", "") or ""
        if name.startswith("refs/tags/"):
            name = name[len("refs/tags/") :]
        found = infer_tag_pattern(name, package_name)
        if found:
            return found
    return None


def parse_tag_version(tag: str, pattern: str, *, name: str | None = None) -> str | None:
    """Extract the semver substring from *tag* using *pattern*."""
    if _PLACEHOLDER_VERSION not in pattern:
        return None
    rx = (
        build_tag_regex(pattern, name=name)
        if _PLACEHOLDER_NAME in pattern and name
        else build_tag_regex(pattern)
    )
    match = rx.match(tag)
    if match is None:
        return None
    return match.groupdict().get("version")


def is_version_tag_ref(ref: str, package_name: str | None = None) -> bool:
    """Return True when *ref* names a version tag (plain or patterned)."""
    if not ref:
        return False
    if _PLAIN_SEMVER_RE.match(ref):
        return True
    return infer_tag_pattern(ref, package_name or "") is not None
