"""Marketplace plugin subgroup helpers and click wiring."""

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
    parts = [tag.strip() for tag in raw.split(",") if tag.strip()]
    return parts if parts else None


def _verify_source(logger: CommandLogger, source: str) -> None:
    """Run ``git ls-remote`` against *source* to verify reachability."""
    from ....marketplace.ref_resolver import RefResolver

    resolver = RefResolver()
    try:
        resolver.list_remote_refs(source)
    except GitLsRemoteError as exc:
        logger.error(f"Source '{source}' is not reachable: {exc}", symbol="error")
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
    """Resolve *ref* to a concrete SHA when it is mutable."""
    from ....marketplace.ref_resolver import RefResolver

    if version is not None:
        return None

    if ref is not None and _SHA_RE.match(ref):
        return ref

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
            logger.error(f"Failed to resolve HEAD for '{source}': {exc}", symbol="error")
            sys.exit(2)
        logger.progress(f"Resolved HEAD to {sha[:12]}", symbol="info")
        return sha

    resolver = RefResolver()
    try:
        remote_refs = resolver.list_remote_refs(source)
    except (GitLsRemoteError, OfflineMissError):
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
            logger.progress(f"Resolved {ref} to {remote_ref.sha[:12]}", symbol="info")
            return remote_ref.sha

    return ref


@click.group(help="Manage plugins in marketplace.yml (add, set, remove)")
def plugin() -> None:
    """Add, update, or remove packages in marketplace.yml."""
    pass


from .add import add  # noqa: E402
from .remove import remove  # noqa: E402
from .set import set_cmd  # noqa: E402

__all__ = [
    "plugin",
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
