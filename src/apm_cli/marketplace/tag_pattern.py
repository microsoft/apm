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
    "build_tag_regex",
    "render_tag",
]

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


def build_tag_regex(pattern: str) -> re.Pattern[str]:
    """Return a compiled regex that captures ``{version}`` from a tag.

    Literal text in *pattern* is escaped so that special regex characters
    (e.g. dots, parens) are matched verbatim.  ``{version}`` becomes a
    named capture group ``(?P<version>...)`` matching a semver-like
    string.  ``{name}`` becomes a non-capturing wildcard ``[^/]+``.

    Parameters
    ----------
    pattern:
        Tag pattern string, e.g. ``"v{version}"``.

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
    escaped = escaped.replace(re.escape(_sentinel_name), r"[^/]+")

    return re.compile(r"^" + escaped + r"$")
