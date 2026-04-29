"""``apm marketplace package {add,set,remove}`` subgroup.

Lets maintainers programmatically manage package entries in
``marketplace.yml`` instead of hand-editing YAML.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import click

from ..core.command_logger import CommandLogger
from ..marketplace.errors import (
    GitLsRemoteError,
    MarketplaceYmlError,
    OfflineMissError,
)
from ._helpers import _is_interactive


# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _yml_path() -> Path:
    """Return the path to the active marketplace authoring config.

    Prefers ``apm.yml`` when it has a ``marketplace:`` block; falls back
    to legacy ``marketplace.yml`` otherwise.  Returns the apm.yml path
    when both files are absent (so callers can produce a consistent
    error message).
    """
    cwd = Path.cwd()
    apm_path = cwd / "apm.yml"
    legacy_path = cwd / "marketplace.yml"

    # Detect apm.yml with marketplace block.
    if apm_path.exists():
        try:
            import yaml
            text = apm_path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if isinstance(data, dict) and "marketplace" in data \
                    and data["marketplace"] is not None:
                return apm_path
        except (OSError, yaml.YAMLError):
            pass
    if legacy_path.exists():
        return legacy_path
    return apm_path


def _ensure_yml_exists(logger: CommandLogger) -> Path:
    """Return the yml path or exit with guidance if it does not exist."""
    cwd = Path.cwd()
    apm_path = cwd / "apm.yml"
    legacy_path = cwd / "marketplace.yml"

    # Hard error when both files are present.
    if apm_path.exists():
        try:
            import yaml
            data = yaml.safe_load(apm_path.read_text(encoding="utf-8"))
            has_block = isinstance(data, dict) and "marketplace" in data \
                and data["marketplace"] is not None
        except (OSError, yaml.YAMLError):
            has_block = False
        if has_block and legacy_path.exists():
            logger.error(
                "Both apm.yml (with a 'marketplace:' block) and "
                "marketplace.yml exist. Remove marketplace.yml or run "
                "'apm marketplace migrate --force' to consolidate.",
                symbol="error",
            )
            sys.exit(1)

    path = _yml_path()
    if not path.exists() or (
        path == apm_path and path.exists() and not _has_marketplace_block(path)
    ):
        logger.error(
            "No marketplace authoring config found. "
            "Run 'apm marketplace init' to scaffold one.",
            symbol="error",
        )
        sys.exit(1)
    return path


def _has_marketplace_block(apm_path: Path) -> bool:
    """Return True when *apm_path* has a populated ``marketplace:`` block."""
    try:
        import yaml
        data = yaml.safe_load(apm_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and "marketplace" in data and \
        data["marketplace"] is not None


def _parse_tags(raw: str | None) -> list[str] | None:
    """Split a comma-separated tag string into a list, or return None."""
    if raw is None:
        return None
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    return parts if parts else None


def _verify_source(logger: CommandLogger, source: str) -> None:
    """Run ``git ls-remote`` against *source* to verify reachability."""
    from ..marketplace.ref_resolver import RefResolver

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
    from ..marketplace.ref_resolver import RefResolver

    # Version-based — no ref resolution needed.
    if version is not None:
        return None

    # Already a concrete SHA — store as-is.
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

    # Non-HEAD, non-SHA ref — check whether it is a branch name.
    resolver = RefResolver()
    try:
        remote_refs = resolver.list_remote_refs(source)
    except (GitLsRemoteError, OfflineMissError):
        # Cannot verify — store as-is but warn the user.
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

    # Not a branch — tag or unknown ref; store as-is.
    return ref


# -------------------------------------------------------------------
# Click group
# -------------------------------------------------------------------


@click.group(help="Manage packages in marketplace.yml (add, set, remove)")
def package():
    """Add, update, or remove packages in marketplace.yml."""
    from ..commands.marketplace import _require_authoring_flag

    _require_authoring_flag()


# -------------------------------------------------------------------
# package add
# -------------------------------------------------------------------


@package.command(help="Add a package to marketplace.yml")
@click.argument("source")
@click.option("--name", default=None, help="Package name (default: repo name)")
@click.option("--version", default=None, help="Semver range (e.g. '>=1.0.0')")
@click.option(
    "--ref",
    default=None,
    help="Pin to a git ref (SHA, tag, or HEAD). Mutable refs are auto-resolved to SHA.",
)
@click.option("-s", "--subdir", default=None, help="Subdirectory inside source repo")
@click.option("--tag-pattern", default=None, help="Tag pattern (e.g. 'v{version}')")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option(
    "--include-prerelease", is_flag=True, help="Include prerelease versions"
)
@click.option("--no-verify", is_flag=True, help="Skip remote reachability check")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(
    source,
    name,
    version,
    ref,
    subdir,
    tag_pattern,
    tags,
    include_prerelease,
    no_verify,
    verbose,
):
    """Add a package entry to marketplace.yml."""
    from ..marketplace.yml_editor import add_plugin_entry

    logger = CommandLogger("marketplace-package-add", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    # --version and --ref are mutually exclusive.
    if version and ref:
        raise click.UsageError(
            "--version and --ref are mutually exclusive. "
            "Use --version for semver ranges or --ref for git refs."
        )

    parsed_tags = _parse_tags(tags)

    # Verify source reachability unless skipped.
    if not no_verify:
        _verify_source(logger, source)

    # Resolve mutable refs to concrete SHAs.
    ref = _resolve_ref(logger, source, ref, version, no_verify)

    try:
        resolved_name = add_plugin_entry(
            yml,
            source=source,
            name=name,
            version=version,
            ref=ref,
            subdir=subdir,
            tag_pattern=tag_pattern,
            tags=parsed_tags,
            include_prerelease=include_prerelease,
        )
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(
        f"Added package '{resolved_name}' from {source}",
        symbol="check",
    )


# -------------------------------------------------------------------
# package set
# -------------------------------------------------------------------


@package.command("set", help="Update a package entry in marketplace.yml")
@click.argument("name")
@click.option("--version", default=None, help="Semver range (e.g. '>=1.0.0')")
@click.option(
    "--ref",
    default=None,
    help="Pin to a git ref (SHA, tag, or HEAD). Mutable refs are auto-resolved to SHA.",
)
@click.option("--subdir", default=None, help="Subdirectory inside source repo")
@click.option("--tag-pattern", default=None, help="Tag pattern (e.g. 'v{version}')")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option(
    "--include-prerelease",
    is_flag=True,
    default=None,
    help="Include prerelease versions",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def set_cmd(
    name,
    version,
    ref,
    subdir,
    tag_pattern,
    tags,
    include_prerelease,
    verbose,
):
    """Update fields on an existing package entry."""
    from ..marketplace.yml_editor import update_plugin_entry

    logger = CommandLogger("marketplace-package-set", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    # --version and --ref are mutually exclusive.
    if version and ref:
        raise click.UsageError(
            "--version and --ref are mutually exclusive. "
            "Use --version for semver ranges or --ref for git refs."
        )

    # Resolve mutable refs to concrete SHAs.
    if ref is not None and not _SHA_RE.match(ref):
        from ..marketplace.yml_schema import load_marketplace_yml

        yml_data = load_marketplace_yml(yml)
        source = None
        for pkg in yml_data.packages:
            if pkg.name.lower() == name.lower():
                source = pkg.source
                break
        if source is None:
            logger.error(f"Package '{name}' not found", symbol="error")
            sys.exit(2)
        ref = _resolve_ref(logger, source, ref, version, no_verify=False)

    parsed_tags = _parse_tags(tags)

    fields = {}
    if version is not None:
        fields["version"] = version
    if ref is not None:
        fields["ref"] = ref
    if subdir is not None:
        fields["subdir"] = subdir
    if tag_pattern is not None:
        fields["tag_pattern"] = tag_pattern
    if parsed_tags is not None:
        fields["tags"] = parsed_tags
    if include_prerelease is not None:
        fields["include_prerelease"] = include_prerelease

    if not fields:
        logger.error(
            "No fields specified. Pass at least one option "
            "(e.g. --version, --ref, --subdir).",
            symbol="error",
        )
        sys.exit(1)

    try:
        update_plugin_entry(yml, name, **fields)
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(f"Updated package '{name}'", symbol="check")


# -------------------------------------------------------------------
# package remove
# -------------------------------------------------------------------


@package.command(help="Remove a package from marketplace.yml")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(name, yes, verbose):
    """Remove a package entry from marketplace.yml."""
    from ..marketplace.yml_editor import remove_plugin_entry

    logger = CommandLogger("marketplace-package-remove", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    # Confirmation gate.
    if not yes:
        if not _is_interactive():
            logger.error(
                "Use --yes to skip confirmation in non-interactive mode",
                symbol="error",
            )
            sys.exit(1)
        try:
            click.confirm(
                f"Remove package '{name}' from marketplace.yml?",
                abort=True,
            )
        except click.Abort:
            logger.progress("Cancelled.", symbol="info")
            return

    try:
        remove_plugin_entry(yml, name)
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(f"Removed package '{name}'", symbol="check")
