"""Shared implementation for ``apm add``/``apm remove`` and their legacy
``apm marketplace add``/``apm marketplace remove`` aliases.

This module is the single source of truth for marketplace registration /
unregistration mechanics. Both the top-level ``apm add``/``apm remove``
commands (issue #1075) and the legacy ``apm marketplace`` subcommands
delegate here so behavior cannot drift between them.

Design notes:

* Single-source operations return an integer exit code (0 success, non-zero
  failure) and never call ``sys.exit`` directly. This lets the multi-source
  loop in :func:`do_add_sources` continue past per-source failures.
* Security-class failures (path traversal during repo parsing, etc.) are
  raised as :class:`PathTraversalError` so the multi-source loop can fail
  closed - a malicious manifest must NOT keep getting attempted.
* When ``invoked_as_legacy`` is True and the operation succeeded, callers
  emit a one-line stderr tip pointing users at the new top-level command.
"""

from __future__ import annotations

import traceback

import click

from ...core.command_logger import CommandLogger
from ...marketplace.aliasing import is_valid_alias as _is_valid_alias
from ...utils.console import STATUS_SYMBOLS
from ...utils.path_security import PathTraversalError
from .._helpers import _is_interactive


def _emit_legacy_tip(new_command: str) -> None:
    """Print a single deprecation hint pointing at the new top-level command."""
    click.echo(
        f"{STATUS_SYMBOLS['info']} Tip: this is now available as a top-level command -- `{new_command}`.",
        err=True,
    )


def _add_single(repo, name, branch, host, verbose) -> int:
    """Register a single marketplace.

    Returns:
        0 on success, 1 on a non-security failure.

    Raises:
        PathTraversalError: when the repo argument contains a path-traversal
            sequence. Re-raised so the multi-source loop can fail closed.
    """
    logger = CommandLogger("marketplace-add", verbose=verbose)
    try:
        from ...marketplace.client import _auto_detect_path, fetch_marketplace
        from ...marketplace.models import MarketplaceSource
        from ...marketplace.registry import add_marketplace
        from ...utils.github_host import default_host, is_valid_fqdn

        if "/" not in repo:
            logger.error(
                f"Invalid format: '{repo}'. Use 'OWNER/REPO' (e.g., 'acme-org/plugin-marketplace')"
            )
            return 1

        parts = repo.split("/")
        if len(parts) == 3 and parts[0] and parts[1] and parts[2]:
            if not is_valid_fqdn(parts[0]):
                logger.error(
                    f"Invalid host: '{parts[0]}'. Use 'OWNER/REPO' or 'HOST/OWNER/REPO' format."
                )
                return 1
            if host and host != parts[0]:
                logger.error(f"Conflicting host: --host '{host}' vs '{parts[0]}' in argument.")
                return 1
            host = parts[0]
            owner, repo_name = parts[1], parts[2]
        elif len(parts) == 2 and parts[0] and parts[1]:
            owner, repo_name = parts[0], parts[1]
        else:
            logger.error(f"Invalid format: '{repo}'. Expected 'OWNER/REPO'")
            return 1

        if host is not None:
            normalized_host = host.strip().lower()
            if not is_valid_fqdn(normalized_host):
                logger.error(
                    f"Invalid host: '{host}'. Expected a valid host FQDN "
                    f"(for example, 'github.com')."
                )
                return 1
            resolved_host = normalized_host
        else:
            resolved_host = default_host()

        if name is not None and not _is_valid_alias(name):
            logger.error(
                f"Invalid marketplace name: '{name}'. "
                f"Names must only contain letters, digits, '.', '_', and '-' "
                f"(required for 'apm install plugin@marketplace' syntax)."
            )
            return 1

        probe_source = MarketplaceSource(
            name=name or repo_name,
            owner=owner,
            repo=repo_name,
            branch=branch,
            host=resolved_host,
        )
        detected_path = _auto_detect_path(probe_source)

        if detected_path is None:
            logger.error(
                f"No marketplace.json found in '{owner}/{repo_name}'. "
                f"Checked: marketplace.json, .github/plugin/marketplace.json, "
                f".claude-plugin/marketplace.json"
            )
            return 1

        fetch_source = MarketplaceSource(
            name=name or repo_name,
            owner=owner,
            repo=repo_name,
            branch=branch,
            host=resolved_host,
            path=detected_path,
        )
        manifest = fetch_marketplace(fetch_source, force_refresh=True)
        plugin_count = len(manifest.plugins)

        manifest_name = (manifest.name or "").strip()
        if name is not None:
            display_name = name
            alias_source = "--name flag"
        elif manifest_name and _is_valid_alias(manifest_name):
            display_name = manifest_name
            alias_source = f"manifest.name ('{manifest_name}')"
        else:
            display_name = repo_name
            if manifest_name and not _is_valid_alias(manifest_name):
                logger.warning(
                    f"Manifest declares name '{manifest_name}' which is not a "
                    f"valid alias (must match [a-zA-Z0-9._-]+). "
                    f"Falling back to repo name."
                )
                alias_source = f"repo name (manifest.name '{manifest_name}' invalid)"
            else:
                alias_source = "repo name (manifest.name missing)"

        assert _is_valid_alias(display_name), (  # noqa: S101
            f"Resolved marketplace alias '{display_name}' failed validation"
        )

        logger.start(f"Registering marketplace '{display_name}'...", symbol="gear")
        logger.verbose_detail(f"    Repository: {owner}/{repo_name}")
        logger.verbose_detail(f"    Branch: {branch}")
        if resolved_host != "github.com":
            logger.verbose_detail(f"    Host: {resolved_host}")
        logger.verbose_detail(f"    Detected path: {detected_path}")
        logger.verbose_detail(f"    Alias source: {alias_source}")

        source = MarketplaceSource(
            name=display_name,
            owner=owner,
            repo=repo_name,
            branch=branch,
            host=resolved_host,
            path=detected_path,
        )
        add_marketplace(source)

        logger.success(
            f"Marketplace '{display_name}' registered ({plugin_count} plugins)",
            symbol="check",
        )
        if manifest.description:
            logger.verbose_detail(f"    {manifest.description}")

        if name is None and display_name != repo_name:
            logger.progress(
                f"Install plugins with: apm install <plugin>@{display_name}",
                symbol="info",
            )

        return 0

    except PathTraversalError:
        # Re-raise so a multi-source batch fails closed on this source.
        raise
    except Exception as e:
        logger.error(f"Failed to register marketplace: {e}")
        if verbose:
            logger.progress(traceback.format_exc(), symbol="info")
        return 1


def do_add_sources(
    repos: tuple[str, ...],
    name: str | None,
    branch: str,
    host: str | None,
    verbose: bool,
    invoked_as_legacy: bool,
) -> int:
    """Register one or more marketplace sources.

    Returns the exit code the caller should exit with.

    Multi-source semantics:
    * Continue-on-error for non-security failures (404, parse error, etc.).
    * Fail-closed on security-class errors (path traversal): the batch
      aborts immediately and remaining sources are NOT attempted.
    * ``--name`` requires exactly one positional source.
    """
    if not repos:
        click.echo("Error: at least one OWNER/REPO is required.", err=True)
        return 2

    if name is not None and len(repos) > 1:
        click.echo(
            "Error: --name requires exactly one OWNER/REPO. "
            "Aliases are per-source; omit --name when registering multiple.",
            err=True,
        )
        return 2

    # Smart typo error: bare name without a slash, e.g. ``apm add cool-plugin``.
    # Only surface this in the top-level entry path (not legacy) so we don't
    # change existing legacy behavior.
    if not invoked_as_legacy:
        for r in repos:
            if "/" not in r:
                click.echo(
                    f"Error: '{r}' is not in OWNER/REPO format.\n"
                    f"  - To register a marketplace: apm add OWNER/REPO\n"
                    f"  - Did you mean to install a plugin? Try: apm install {r}",
                    err=True,
                )
                return 1

    if len(repos) == 1:
        rc = _add_single(repos[0], name, branch, host, verbose)
        if rc == 0 and invoked_as_legacy:
            _emit_legacy_tip("apm add")
        return rc

    # Multi-source path (only reachable from the top-level command).
    successes: list[str] = []
    failures: list[str] = []
    for r in repos:
        try:
            rc = _add_single(r, None, branch, host, verbose)
        except PathTraversalError as exc:
            click.echo(
                f"{STATUS_SYMBOLS['error']} Security error on '{r}': {exc}. "
                f"Aborting batch; remaining sources were not attempted.",
                err=True,
            )
            failures.append(r)
            # Note any unprocessed remaining repos in the summary.
            remaining = [x for x in repos if x not in successes and x not in failures]
            click.echo(
                f"Summary: {len(successes)} registered, {len(failures)} failed, "
                f"{len(remaining)} skipped.",
                err=True,
            )
            return 1
        if rc == 0:
            successes.append(r)
        else:
            failures.append(r)

    if failures:
        click.echo(
            f"Summary: {len(successes)} registered, {len(failures)} failed.",
            err=True,
        )
    else:
        click.echo(f"Summary: {len(successes)} registered.", err=True)
    return 0 if not failures else 1


def do_remove_source(
    name: str,
    yes: bool,
    verbose: bool,
    invoked_as_legacy: bool,
) -> int:
    """Unregister a marketplace by name.

    Returns the exit code the caller should exit with.
    """
    logger = CommandLogger("marketplace-remove", verbose=verbose)
    try:
        from ...marketplace.client import clear_marketplace_cache
        from ...marketplace.registry import get_marketplace_by_name, remove_marketplace

        source = get_marketplace_by_name(name)

        if not yes:
            if not _is_interactive():
                logger.error(
                    "Use --yes to skip confirmation in non-interactive mode",
                    symbol="error",
                )
                return 1
            confirmed = click.confirm(
                f"Remove marketplace '{source.name}' ({source.owner}/{source.repo})?",
                default=False,
            )
            if not confirmed:
                logger.progress("Cancelled", symbol="info")
                return 0

        remove_marketplace(name)
        clear_marketplace_cache(name, host=source.host)
        logger.success(f"Marketplace '{name}' removed", symbol="check")

        if invoked_as_legacy:
            _emit_legacy_tip("apm remove")
        return 0

    except Exception as e:
        logger.error(f"Failed to remove marketplace: {e}")
        if verbose:
            logger.progress(traceback.format_exc(), symbol="info")
        return 1


__all__ = ["do_add_sources", "do_remove_source"]
