"""Marketplace upstream subgroup -- click wiring + helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import GitLsRemoteError, MarketplaceYmlError, OfflineMissError
from ...marketplace.yml_editor import (
    add_upstream_entry,
    list_upstream_entries,
    remove_upstream_entry,
)
from .plugin import _SHA_RE, _ensure_yml_exists


def _build_resolver(repo: str, host: str | None):
    """Build a ``RefResolver`` configured for the upstream's host + auth.

    Resolves the per-host token via ``AuthResolver`` so private upstream
    repos and GHE/GHES hosts work transparently. Owner is derived from
    *repo* (``owner/name``) so org-scoped credentials match.
    """
    from ...core.auth import AuthResolver
    from ...marketplace.ref_resolver import RefResolver

    target_host = host or "github.com"
    org = repo.split("/", 1)[0] if "/" in repo else None
    try:
        ctx = AuthResolver().resolve(target_host, org=org)
        token = ctx.token
    except Exception:
        token = None
    return RefResolver(host=target_host, token=token)


@click.group(help="Manage upstream marketplaces in authoring config")
def upstream():
    """Add, list, or remove upstream marketplaces in apm.yml."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verify_upstream_repo(logger: CommandLogger, repo: str, host: str | None) -> None:
    """Verify *repo* is reachable via ``git ls-remote``.

    Soft-warns on offline; hard-errors on definitive miss.
    """
    resolver = _build_resolver(repo, host)
    try:
        resolver.list_remote_refs(repo)
    except GitLsRemoteError as exc:
        logger.error(
            f"Upstream '{repo}' is not reachable: {exc}",
            symbol="error",
        )
        sys.exit(2)
    except OfflineMissError:
        logger.warning(
            f"Cannot verify upstream '{repo}' (offline / no cache).",
            symbol="warning",
        )


def _resolve_upstream_ref_to_sha(
    logger: CommandLogger,
    repo: str,
    ref: str,
    host: str | None,
) -> str:
    """Resolve a mutable ref (tag / branch) to a SHA via ls-remote.

    Returns *ref* unchanged when it already looks like a 40-char SHA or
    when offline. Hard-errors when ls-remote returns no matching ref.
    """
    if _SHA_RE.match(ref):
        return ref

    resolver = _build_resolver(repo, host)
    try:
        refs = resolver.list_remote_refs(repo)
    except OfflineMissError:
        logger.warning(
            f"Offline: cannot resolve ref '{ref}' for {repo}; storing as-is.",
            symbol="warning",
        )
        return ref
    except GitLsRemoteError as exc:
        logger.error(
            f"Failed to resolve ref '{ref}' for {repo}: {exc}",
            symbol="error",
        )
        sys.exit(2)

    for entry in refs:
        if entry.name in (
            f"refs/tags/{ref}",
            f"refs/heads/{ref}",
            ref,
        ):
            return entry.sha
    logger.error(
        f"Ref '{ref}' not found in {repo}.",
        symbol="error",
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# upstream add
# ---------------------------------------------------------------------------


@upstream.command(help="Register an upstream marketplace")
@click.argument("repo", required=True)
@click.option(
    "--alias", required=True, help="Local alias for the upstream (used to reference plugins)"
)
@click.option(
    "--ref",
    default=None,
    help="Pin to an immutable ref (40-char SHA or tag). Mutable refs are auto-resolved.",
)
@click.option("--branch", default=None, help="Track a mutable branch (requires --allow-head)")
@click.option(
    "--path",
    default=None,
    help="Manifest path inside the upstream repo (default: .claude-plugin/marketplace.json)",
)
@click.option("--host", default=None, help="Git host FQDN (default: github.com)")
@click.option("--allow-head", is_flag=True, help="Permit a mutable branch (HEAD-tracking)")
@click.option("--no-verify", is_flag=True, help="Skip remote reachability check")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(repo, alias, ref, branch, path, host, allow_head, no_verify, verbose):
    """Add an upstream marketplace entry to authoring config."""
    logger = CommandLogger("marketplace-upstream-add", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    if ref and branch:
        raise click.UsageError("--ref and --branch are mutually exclusive.")
    if not ref and not branch:
        raise click.UsageError("Specify either --ref (immutable) or --branch (with --allow-head).")
    if branch and not allow_head:
        raise click.UsageError(
            "--branch requires --allow-head to acknowledge the mutable-pin trade-off."
        )

    if not no_verify:
        _verify_upstream_repo(logger, repo, host)

    if ref is not None:
        ref = _resolve_upstream_ref_to_sha(logger, repo, ref, host)

    try:
        add_upstream_entry(
            Path(yml),
            alias=alias,
            repo=repo,
            ref=ref,
            branch=branch,
            path=path,
            host=host,
            allow_head=allow_head,
        )
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(
        f"Registered upstream '{alias}' -> {repo}",
        symbol="check",
    )
    logger.info(
        f"Next: 'apm marketplace package add --upstream {alias} --plugin <plugin-name>'",
        symbol="info",
    )


# ---------------------------------------------------------------------------
# upstream list
# ---------------------------------------------------------------------------


@upstream.command("list", help="List registered upstream marketplaces")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def list_cmd(verbose):
    """List upstream marketplaces."""
    logger = CommandLogger("marketplace-upstream-list", verbose=verbose)
    yml = _ensure_yml_exists(logger)
    entries = list_upstream_entries(Path(yml))

    if not entries:
        logger.info(
            "No upstream marketplaces registered. "
            "Run 'apm marketplace upstream add <repo> --alias <alias> --ref <sha>' to add one.",
            symbol="info",
        )
        return

    logger.info(f"{len(entries)} upstream(s) registered:", symbol="info")
    for entry in entries:
        alias = entry.get("alias", "<unknown>")
        repo = entry.get("repo", "<unknown>")
        host = entry.get("host", "github.com")
        pin = entry.get("ref") or entry.get("branch", "<unpinned>")
        head = " (HEAD-tracking)" if entry.get("allow_head") else ""
        # Per-entry rows are visual continuation under the count line
        # above; rendering them without the [i] prefix avoids the
        # double-symbol look that confused cli-logging review.
        logger.tree_item(f"  {alias} -> {host}/{repo} @ {pin}{head}")


# ---------------------------------------------------------------------------
# upstream remove
# ---------------------------------------------------------------------------


@upstream.command(help="Remove an upstream marketplace")
@click.argument("alias", required=True)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(alias, verbose):
    """Remove an upstream marketplace from authoring config."""
    logger = CommandLogger("marketplace-upstream-remove", verbose=verbose)
    yml = _ensure_yml_exists(logger)
    try:
        remove_upstream_entry(Path(yml), alias)
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    # Default success symbol is "sparkles" ([*]); [+] (check) reads as
    # "addition" in this codebase, which doesn't fit a removal action.
    logger.success(f"Removed upstream '{alias}'")


__all__ = ["add", "list_cmd", "remove", "upstream"]
