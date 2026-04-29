"""Marketplace package subgroup helpers and click wiring."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import click

from ....core.command_logger import CommandLogger
from ....marketplace.errors import (
    GitLsRemoteError,
    MarketplaceYmlError,
    OfflineMissError,
)
from ..._helpers import _is_interactive

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _yml_path() -> Path:
    """Return the canonical ``marketplace.yml`` path in CWD."""
    return Path.cwd() / "marketplace.yml"

def _ensure_yml_exists(logger: CommandLogger) -> Path:
    """Return the yml path or exit with guidance if it does not exist."""
    path = _yml_path()
    if not path.exists():
        logger.error(
            "No marketplace.yml found. "
            "Run 'apm marketplace init' to scaffold one.",
            symbol="error",
        )
        sys.exit(1)
    return path

def _parse_tags(raw: str | None) -> list[str] | None:
    """Split a comma-separated tag string into a list, or return None."""
    if raw is None:
        return None
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    return parts if parts else None

def _verify_source(logger: CommandLogger, source: str) -> None:
    """Run ``git ls-remote`` against *source* to verify reachability."""
    from ....marketplace.ref_resolver import RefResolver

    resolver = RefResolver()
    try:
        resolver.list_remote_refs(source)
    except GitLsRemoteError as exc:
        logger.error(
            f"Source '{source}' is not reachable: {exc}",
            symbol="error",
        )
        sys.exit(2)
    except OfflineMissError:
        logger.warning(
            f"Cannot verify source '{source}' (offline / no cache).",
            symbol="warning",
        )

def _resolve_ref(
    logger: CommandLogger,
    source: str,
    ref: str | None,
    version: str | None,
    no_verify: bool,
) -> str | None:
    """Resolve *ref* to a concrete SHA when it is mutable.

    Returns the (possibly resolved) ref string, or ``None`` when
    *version* is set (version-based pinning, no ref needed).
    """
    from ....marketplace.ref_resolver import RefResolver

    # Version-based - no ref resolution needed.
    if version is not None:
        return None

    # Already a concrete SHA - store as-is.
    if ref is not None and _SHA_RE.match(ref):
        return ref

    # HEAD (explicit or implicit) requires network access.
    is_head = ref is None or ref.upper() == "HEAD"
    if is_head:
        if no_verify:
            logger.error(
                "Cannot resolve HEAD ref without network access. "
                "Provide an explicit --ref SHA.",
                symbol="error",
            )
            sys.exit(2)
        if ref is not None:
            logger.warning(
                "'HEAD' is a mutable ref. Resolving to current SHA for safety.",
                symbol="warning",
            )
        resolver = RefResolver()
        try:
            sha = resolver.resolve_ref_sha(source, "HEAD")
        except GitLsRemoteError as exc:
            logger.error(
                f"Failed to resolve HEAD for '{source}': {exc}",
                symbol="error",
            )
            sys.exit(2)
        logger.progress(
            f"Resolved HEAD to {sha[:12]}",
            symbol="info",
        )
        return sha

    # Non-HEAD, non-SHA ref - check whether it is a branch name.
    resolver = RefResolver()
    try:
        remote_refs = resolver.list_remote_refs(source)
    except (GitLsRemoteError, OfflineMissError):
        # Cannot verify - store as-is but warn the user.
        logger.warning(
            f"Could not verify ref '{ref}' for '{source}' (network unavailable). "
            "Storing unresolved -- run with network access to pin a concrete SHA.",
            symbol="warning",
        )
        return ref

    for remote_ref in remote_refs:
        if remote_ref.name == f"refs/heads/{ref}":
            if no_verify:
                logger.error(
                    "Cannot resolve branch ref without network access. "
                    "Provide an explicit --ref SHA.",
                    symbol="error",
                )
                sys.exit(2)
            logger.warning(
                f"'{ref}' is a branch (mutable ref). "
                "Resolving to current SHA for safety.",
                symbol="warning",
            )
            logger.progress(
                f"Resolved {ref} to {remote_ref.sha[:12]}",
                symbol="info",
            )
            return remote_ref.sha

    # Not a branch - tag or unknown ref; store as-is.
    return ref

@click.group(help="Manage packages in marketplace.yml (add, set, remove)")
def package():
    """Add, update, or remove packages in marketplace.yml."""
    from .. import _require_authoring_flag

    _require_authoring_flag()



from .add import add  # noqa: E402
from .remove import remove  # noqa: E402
from .set import set_cmd  # noqa: E402

__all__ = [
    "package",
    "add",
    "set_cmd",
    "remove",
    "_SHA_RE",
    "_yml_path",
    "_ensure_yml_exists",
    "_parse_tags",
    "_verify_source",
    "_resolve_ref",
]

