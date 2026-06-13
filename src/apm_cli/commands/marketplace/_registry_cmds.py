"""Registry-management Click commands for the marketplace group.

Extracted from ``marketplace/__init__.py`` to keep that module under 800 lines.
Contains: ``add``, ``list_cmd``, ``browse``, ``update``, ``remove`` plus their
private helpers.  All names are re-exported from the package ``__init__`` so
existing import paths keep working.

These commands are imported at the *bottom* of ``__init__.py`` (after
``marketplace``, ``_parse_marketplace_source``, and ``_is_valid_alias`` are
defined), so module-scope ``from . import ...`` is safe - the same pattern used
by the existing ``check``, ``outdated``, and ``publish`` sibling modules.
"""

from __future__ import annotations

import re
import sys
import traceback
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import click

from ...utils.path_security import PathTraversalError
from . import (
    _is_valid_alias,
    _parse_marketplace_source,
    marketplace,
)

# ---------------------------------------------------------------------------
# Constants and helpers used only by the registry commands
# ---------------------------------------------------------------------------

# Host-trust classification is owned by AuthResolver.classify_host (see
# core/auth.py). The marketplace command layer routes through it so that the
# credential-leakage guard at registration time uses the same single source of
# truth as the fetch-time guard in marketplace/client.py.


def _mkt_get_console():
    """Route to marketplace._get_console so test patches apply."""
    from apm_cli.commands import marketplace as _m

    return _m._get_console()


def _mkt_is_interactive():
    """Route to ``marketplace._is_interactive`` so test patches apply."""
    from apm_cli.commands import marketplace as _m

    return _m._is_interactive()


_TRUSTED_MARKETPLACE_HOST_KINDS = ("github", "ghe_cloud", "ghes", "gitlab")


def _check_gitignore_for_marketplace_json(log):
    """Warn if .gitignore contains a rule that would ignore marketplace outputs."""
    gitignore_path = Path.cwd() / ".gitignore"
    if not gitignore_path.exists():
        return

    try:
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    patterns = {
        "marketplace.json",
        "**/marketplace.json",
        "/marketplace.json",
        ".claude-plugin/marketplace.json",
        ".agents/plugins/marketplace.json",
        "*.json",
    }
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in patterns:
            log.warning(
                "Your .gitignore ignores marketplace.json. "
                "Track apm.yml plus generated marketplace files such as "
                ".claude-plugin/marketplace.json and .agents/plugins/marketplace.json. "
                "Remove the .gitignore rule or add explicit unignore entries.",
                symbol="warning",
            )
            return


def _marketplace_add_unsupported_host_error(
    resolved_host: str,
    quoted_repo: str,
    quoted_host: str,
    host_kind: str,
) -> str:
    """User-facing error when ``apm marketplace add`` rejects the resolved host.

    *quoted_repo* and *quoted_host* must already be ``shlex.quote``-safe for
    shell copy-paste (see call sites).
    """
    if host_kind == "ado":
        return (
            f"Host '{resolved_host}' is not supported for marketplace registration.\n"
            "APM marketplaces must be hosted on GitHub, GitHub Enterprise, or GitLab."
        )
    return (
        f"Host '{resolved_host}' is not supported.\n"
        "Supported marketplace hosts: github.com, *.ghe.com, "
        "GitHub Enterprise Server (configure GITHUB_HOST), "
        "and GitLab (gitlab.com or self-managed via GITLAB_HOST or APM_GITLAB_HOSTS).\n\n"
        "To use GitHub Enterprise Server on this host:\n"
        f"  export GITHUB_HOST={quoted_host}\n"
        "Then re-run:\n"
        f"  apm marketplace add {quoted_repo}\n\n"
        "To use self-managed GitLab on this host:\n"
        f"  export GITLAB_HOST={quoted_host}\n"
        "(or list the host in APM_GITLAB_HOSTS for multiple instances.)\n"
        "Then re-run:\n"
        f"  apm marketplace add {quoted_repo}\n"
    )


def _split_source_fragment_ref(source: str) -> tuple[str, str]:
    """Split an HTTPS git URL #ref fragment from the URL stored in the registry."""
    raw = (source or "").strip()
    if not raw.lower().startswith("https://"):
        return raw, ""
    parsed = urlsplit(raw)
    if not parsed.fragment:
        return raw, ""
    clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return clean_url, parsed.fragment


def _is_remote_marketplace_json_url(url: str) -> bool:
    """Return True when *url* names a hosted marketplace.json document."""
    from ...marketplace.models import url_names_remote_manifest

    return url_names_remote_manifest(url)


def _should_warn_unpinned_git_url(
    source: str,
    kind: str,
    is_direct_url: bool,
    fragment_ref: str,
    explicit_ref: bool,
) -> bool:
    """Return True when a git URL source uses the implicit mutable default ref."""
    if is_direct_url or fragment_ref or explicit_ref:
        return False
    return source.lower().startswith("https://") and kind in {"github", "gitlab", "git"}


def _local_source_points_to_file(source) -> bool:
    """Return True when a local marketplace source points directly to a file."""
    if source.kind != "local":
        return False
    try:
        return Path(source.local_path).expanduser().is_file()
    except OSError:
        return False


def _display_source_kind(kind: str, is_direct_url: bool) -> str:
    """Return a human-readable source kind for verbose CLI output."""
    if is_direct_url:
        return "hosted marketplace.json URL"
    labels = {
        "github": "GitHub repository",
        "gitlab": "GitLab repository",
        "git": "generic git repository",
        "local": "local filesystem path",
    }
    return labels.get(kind, kind)


def _default_alias_from_remote_url(url: str) -> str:
    """Derive a stable default alias for a direct remote marketplace.json URL."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "marketplace"
    host = (parsed.hostname or "marketplace").lower().split(":", 1)[0]
    path_segments = [seg for seg in (parsed.path or "").split("/") if seg]
    parent = ""
    if len(path_segments) >= 2 and path_segments[-1].lower() == "marketplace.json":
        parent = path_segments[-2]
    if parent:
        alias = f"{host}-{parent}"
        return re.sub(r"[^a-zA-Z0-9._-]", "_", alias).strip("._-") or host
    return host


def _default_alias_from_url(url: str) -> str:
    """Derive a default marketplace alias from a parsed URL.

    Strips ``.git`` suffix, trailing slashes, and uses the last
    path-segment.  For ``file://`` URLs the alias falls back to the
    final filesystem segment.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url) if "://" in url else None
    if parsed and parsed.path:
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    else:
        tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail or "marketplace"


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------

_ADD_EPILOG = """
\b
Examples:
  apm marketplace add owner/repo
  apm marketplace add github.com/owner/repo
  apm marketplace add https://github.com/owner/repo#v1.0.0
  apm marketplace add https://catalog.example.com/marketplace.json --name catalog
  apm marketplace add https://gitlab.com/group/repo
  apm marketplace add https://dev.azure.com/org/proj/_git/repo --name apm-mkt
  apm marketplace add git@gitea.example.com:org/repo.git --name custom
  apm marketplace add /srv/marketplaces/agent-forge --name agent-forge
"""


@marketplace.command(help="Register a marketplace", epilog=_ADD_EPILOG)
@click.argument("source", metavar="SOURCE", required=True)
@click.option("--name", "-n", default=None, help="Display name (defaults to repo name)")
@click.option(
    "--ref",
    "-r",
    default=None,
    help="Git ref (branch, tag, or commit). Default: main. Applies to git-backed sources only.",
)
@click.option("--branch", "-b", default=None, help="Deprecated alias for --ref", hidden=True)
@click.option(
    "--host",
    default=None,
    help="Git host FQDN for OWNER/REPO shorthand (default: github.com)",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(source, name, ref, branch, host, verbose):
    """Register a marketplace.

    SOURCE accepts: OWNER/REPO shorthand, HOST/OWNER/REPO shorthand, a full
    HTTPS git URL with optional ``#ref`` (GitHub, GitLab, Azure DevOps,
    Gitea, Bitbucket Server, or any self-hosted git server), a hosted
    ``marketplace.json`` URL, an SSH URL (``git@host:org/repo.git``),
    a local filesystem path, or a ``file://`` URI.
    """
    from ...core.command_logger import CommandLogger

    log = CommandLogger("marketplace-add", verbose=verbose)
    try:
        from ...marketplace.client import _auto_detect_path, fetch_marketplace
        from ...marketplace.models import MarketplaceSource
        from ...marketplace.registry import add_marketplace
        from ...utils.github_host import is_valid_fqdn

        source_arg, fragment_ref = _split_source_fragment_ref(source)

        # --ref / --branch reconciliation. --branch stays as a hidden alias
        # for one release so legacy invocations keep working; passing multiple
        # ref sources is a hard error so we never silently pick one.
        explicit_ref = ref is not None or branch is not None
        if ref is not None and branch is not None:
            log.error(
                "--ref and --branch are mutually exclusive. Use --ref (--branch is a deprecated alias).",
                symbol="error",
            )
            sys.exit(1)
        if fragment_ref and explicit_ref:
            log.error(
                "Do not combine a git URL #ref with --ref or --branch. Use one ref source.",
                symbol="error",
            )
            sys.exit(1)
        effective_ref = fragment_ref or ref or branch or "main"

        try:
            url, kind, resolved_host = _parse_marketplace_source(source_arg, host)
        except PathTraversalError:
            log.error(
                f"Invalid source '{source}': contains a path-traversal sequence. "
                f"Remove '..', '.', or '~' from each path segment."
            )
            sys.exit(1)
        except ValueError as exc:
            log.error(str(exc))
            sys.exit(1)

        if host is not None and not is_valid_fqdn(host.strip().lower()):
            log.error(
                f"Invalid host: '{host}'. Expected a valid host FQDN (for example, 'github.com').",
                symbol="error",
            )
            sys.exit(1)

        is_direct_url = _is_remote_marketplace_json_url(url)

        if host is not None and is_direct_url:
            log.warning(
                "--host is ignored when SOURCE is a hosted marketplace.json URL.",
                symbol="warning",
            )
        elif host is not None and kind == "local":
            log.warning(
                "--host is ignored when SOURCE is a local filesystem path.",
                symbol="warning",
            )
        elif (
            host is not None
            and host.strip().lower() != (resolved_host or "").lower()
            and kind in ("git", "github", "gitlab")
            and (source_arg.startswith(("https://", "git@", "file://")))
        ):
            log.warning(
                "--host is ignored when SOURCE is a full URL.",
                symbol="warning",
            )

        # Trust gate is now scoped to kinds that would forward an APM token
        # via header injection. The subprocess git path (kind == "git")
        # never forwards GITHUB_APM_PAT / GITLAB_APM_PAT -- AuthResolver
        # only emits credentials matching the classified host. Local-kind
        # fetches use no credentials at all.
        if kind in ("github", "gitlab"):
            from ...core.auth import AuthResolver

            host_info = AuthResolver.classify_host(resolved_host or "")
            if host_info.kind not in _TRUSTED_MARKETPLACE_HOST_KINDS:
                import shlex as _shlex

                quoted_repo = _shlex.quote(source)
                quoted_host = _shlex.quote(resolved_host or "")
                log.error(
                    _marketplace_add_unsupported_host_error(
                        resolved_host or "", quoted_repo, quoted_host, host_info.kind
                    )
                )
                sys.exit(1)

        if name is not None and not _is_valid_alias(name):
            log.error(
                f"Invalid marketplace name: '{name}'. "
                f"Names must only contain letters, digits, '.', '_', and '-' "
                f"(required for 'apm install plugin@marketplace' syntax).",
                symbol="error",
            )
            sys.exit(1)

        # Surface progress before the slow probe + fetch (5-30s for generic-git)
        # so the user sees activity instead of staring at a blank terminal.
        provisional_label = name or (
            _default_alias_from_remote_url(url) if is_direct_url else _default_alias_from_url(url)
        )
        log.start(f"Registering marketplace '{provisional_label}'...", symbol="gear")
        if _should_warn_unpinned_git_url(
            source_arg, kind, is_direct_url, fragment_ref, explicit_ref
        ):
            log.warning(
                "Pin this git marketplace with a #ref (for example, "
                f"{source_arg}#v1.0.0) or --ref to avoid mutable branch updates.",
                symbol="warning",
            )

        # Probe for marketplace.json location. The probe source's name is a
        # placeholder -- _auto_detect_path only consults url/ref/path/kind.
        probe_name = provisional_label
        probe_source = MarketplaceSource(
            name=probe_name,
            url=url,
            ref="" if is_direct_url else effective_ref,
            path="" if is_direct_url else "marketplace.json",
        )
        if is_direct_url or _local_source_points_to_file(probe_source):
            detected_path = ""
        else:
            detected_path = _auto_detect_path(probe_source)

        if detected_path is None:
            log.error(
                f"No marketplace.json found in '{probe_source.display_source}'. "
                f"Checked: marketplace.json, .github/plugin/marketplace.json, "
                f".claude-plugin/marketplace.json",
                symbol="error",
            )
            sys.exit(1)

        fetch_source = MarketplaceSource(
            name=probe_name,
            url=url,
            ref="" if is_direct_url else effective_ref,
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
            display_name = probe_name
            if manifest_name and not _is_valid_alias(manifest_name):
                log.warning(
                    f"Manifest declares name '{manifest_name}' which is not a "
                    f"valid alias (must match [a-zA-Z0-9._-]+). "
                    f"Falling back to repo name.",
                    symbol="warning",
                )
                alias_source = f"derived name (manifest.name '{manifest_name}' invalid)"
            else:
                alias_source = "derived name (manifest.name missing)"

        assert _is_valid_alias(display_name), (  # noqa: S101
            f"Resolved marketplace alias '{display_name}' failed validation"
        )

        log.verbose_detail(f"    Source: {fetch_source.display_source}")
        log.verbose_detail(
            f"    Source type: {_display_source_kind(fetch_source.kind, is_direct_url)}"
        )
        if not is_direct_url:
            log.verbose_detail(f"    Ref: {effective_ref}")
        if detected_path:
            log.verbose_detail(f"    Detected path: {detected_path}")
        elif not is_direct_url:
            log.verbose_detail("    Detected path: direct local file")
        log.verbose_detail(f"    Alias source: {alias_source}")

        final_source = MarketplaceSource(
            name=display_name,
            url=url,
            ref="" if is_direct_url else effective_ref,
            path=detected_path,
        )
        add_marketplace(final_source)

        log.success(
            f"Marketplace '{display_name}' registered ({plugin_count} plugins)",
            symbol="check",
        )
        if manifest.description:
            log.verbose_detail(f"    {manifest.description}")

        if name is None and display_name != probe_name:
            log.progress(
                f"Install plugins with: apm install <plugin>@{display_name}",
                symbol="info",
            )

    except Exception as e:
        log.error(f"Failed to register marketplace: {e}")
        if verbose:
            log.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)


@marketplace.command(name="list", help="List registered marketplaces")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def list_cmd(verbose):
    """Show all registered marketplaces."""
    from ...core.command_logger import CommandLogger

    log = CommandLogger("marketplace-list", verbose=verbose)
    try:
        from ...marketplace.registry import get_registered_marketplaces

        sources = get_registered_marketplaces()

        if not sources:
            log.progress(
                "No marketplaces registered. Use 'apm marketplace add SOURCE' to register one "
                "(OWNER/REPO, HTTPS URL, SSH URL, or local path).",
                symbol="info",
            )
            return

        console = _mkt_get_console()
        if not console:
            log.progress(f"{len(sources)} marketplace(s) registered:", symbol="info")
            for s in sources:
                log.tree_item(f"  {s.name}  ({s.display_source})")
            return

        from rich.table import Table

        table = Table(
            title="Registered Marketplaces",
            show_header=True,
            header_style="bold cyan",
            border_style="cyan",
        )
        table.add_column("Name", style="bold white", no_wrap=True)
        table.add_column("Source", style="white")
        table.add_column("Ref", style="cyan")
        table.add_column("Path", style="dim")

        for s in sources:
            table.add_row(s.name, s.display_source, s.ref, s.path)

        console.print()
        console.print(table)
        log.progress(
            "Use 'apm marketplace browse <name>' to see plugins",
            symbol="info",
        )

    except Exception as e:
        log.error(f"Failed to list marketplaces: {e}")
        if verbose:
            log.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)


@marketplace.command(help="Browse plugins in a marketplace")
@click.argument("name", required=True)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def browse(name, verbose):
    """Show available plugins in a marketplace."""
    from ...core.command_logger import CommandLogger

    log = CommandLogger("marketplace-browse", verbose=verbose)
    try:
        from ...marketplace.client import fetch_marketplace
        from ...marketplace.registry import get_marketplace_by_name

        source = get_marketplace_by_name(name)
        log.start(f"Fetching plugins from '{name}'...", symbol="search")

        manifest = fetch_marketplace(source, force_refresh=True)

        if not manifest.plugins:
            log.warning(f"Marketplace '{name}' has no plugins")
            return

        console = _mkt_get_console()
        if not console:
            log.success(f"{len(manifest.plugins)} plugin(s) in '{name}':", symbol="check")
            for p in manifest.plugins:
                desc = f" -- {p.description}" if p.description else ""
                log.tree_item(f"  {p.name}{desc}")
            log.progress(f"Install: apm install <plugin-name>@{name}", symbol="info")
            return

        from rich.table import Table

        table = Table(
            title=f"Plugins in '{name}'",
            show_header=True,
            header_style="bold cyan",
            border_style="cyan",
        )
        table.add_column("Plugin", style="bold white", no_wrap=True)
        table.add_column("Description", style="white", ratio=1)
        table.add_column("Version", style="cyan", justify="center")
        table.add_column("Install", style="green")

        for p in manifest.plugins:
            desc = p.description or "--"
            ver = p.version or "--"
            table.add_row(p.name, desc, ver, f"{p.name}@{name}")

        console.print()
        console.print(table)
        log.progress(
            f"Install a plugin: apm install <plugin-name>@{name}",
            symbol="info",
        )

    except Exception as e:
        log.error(f"Failed to browse marketplace: {e}")
        if verbose:
            log.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)


@marketplace.command(help="Refresh marketplace cache")
@click.argument("name", required=False, default=None)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def update(name, verbose):
    """Refresh cached marketplace data (one or all)."""
    from ...core.command_logger import CommandLogger

    log = CommandLogger("marketplace-update", verbose=verbose)
    try:
        from ...marketplace.client import clear_marketplace_cache, fetch_marketplace
        from ...marketplace.registry import (
            get_marketplace_by_name,
            get_registered_marketplaces,
        )

        if name:
            source = get_marketplace_by_name(name)
            log.start(f"Refreshing marketplace '{name}'...", symbol="gear")
            clear_marketplace_cache(source=source)
            manifest = fetch_marketplace(source, force_refresh=True)
            log.success(
                f"Marketplace '{name}' updated ({len(manifest.plugins)} plugins)",
                symbol="check",
            )
        else:
            sources = get_registered_marketplaces()
            if not sources:
                log.progress("No marketplaces registered.", symbol="info")
                return
            log.start(f"Refreshing {len(sources)} marketplace(s)...", symbol="gear")
            for s in sources:
                try:
                    clear_marketplace_cache(source=s)
                    manifest = fetch_marketplace(s, force_refresh=True)
                    log.tree_item(f"  {s.name} ({len(manifest.plugins)} plugins)")
                except Exception as exc:
                    log.warning(f"  {s.name}: {exc}")
                    if verbose:
                        log.progress(traceback.format_exc(), symbol="info")
            log.success("Marketplace cache refreshed", symbol="check")

    except Exception as e:
        log.error(f"Failed to update marketplace: {e}")
        if verbose:
            log.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)


@marketplace.command(help="Remove a registered marketplace")
@click.argument("name", required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(name, yes, verbose):
    """Unregister a marketplace."""
    from ...core.command_logger import CommandLogger

    log = CommandLogger("marketplace-remove", verbose=verbose)
    try:
        from ...marketplace.client import clear_marketplace_cache
        from ...marketplace.registry import get_marketplace_by_name, remove_marketplace

        source = get_marketplace_by_name(name)

        if not yes:
            if not _mkt_is_interactive():
                log.error(
                    "Use --yes to skip confirmation in non-interactive mode",
                    symbol="error",
                )
                sys.exit(1)
            confirmed = click.confirm(
                f"Remove marketplace '{source.name}' ({source.display_source})?",
                default=False,
            )
            if not confirmed:
                log.progress("Cancelled", symbol="info")
                return

        remove_marketplace(name)
        clear_marketplace_cache(source=source)
        log.success(f"Marketplace '{name}' removed", symbol="check")

    except Exception as e:
        log.error(f"Failed to remove marketplace: {e}")
        if verbose:
            log.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)
