"""Shared helpers for marketplace tag-version iteration.

Extracted here to avoid duplicate-code violations between
``marketplace.builder`` and ``commands.marketplace``.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .semver import SemVer


def iter_semver_tags(
    refs: list,
    tag_rx,
) -> Generator[tuple[SemVer, str, str], None, None]:
    """Yield ``(SemVer, tag_name, sha)`` for every remote ref that:

    - is a git tag (``refs/tags/…``),
    - matches *tag_rx* (must expose a ``version`` named group), and
    - whose captured version string parses as a valid :class:`SemVer`.

    Prerelease filtering and range filtering are left to the caller.

    Args:
        refs: Iterable of remote-ref objects (each with ``.name`` and
            ``.sha`` attributes).
        tag_rx: Compiled regular expression with a ``version`` named group.

    Yields:
        Triples of ``(sv, tag_name, sha)`` in iteration order.
    """
    from .semver import parse_semver

    for remote_ref in refs:
        if not remote_ref.name.startswith("refs/tags/"):
            continue
        tag_name = remote_ref.name[len("refs/tags/") :]
        m = tag_rx.match(tag_name)
        if not m:
            continue
        sv = parse_semver(m.group("version"))
        if sv is None:
            continue
        yield sv, tag_name, remote_ref.sha
