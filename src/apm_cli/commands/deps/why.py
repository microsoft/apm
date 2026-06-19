"""``apm deps why <pkg>`` -- explain why a package is installed.

Inverts the lockfile dependency graph from a target package back to one or
more direct dependencies that pulled it in. The lockfile is the source of
truth -- no remote calls.

Output formats:

* Default: a plain-text indented tree (ASCII ``+--``), one chain per
  direct dependency that transitively required the target. Rich is
  intentionally not used here -- a list of N short root-to-leaf chains
  is not a single tree, and ``rich.tree`` would imply a hierarchy that
  does not exist.
* ``--json``: a machine-readable JSON document on stdout.

Exit codes:

* ``0`` -- target found and explained.
* ``1`` -- package not installed or query ambiguous.
* ``2`` -- no lockfile / project misconfiguration.
"""

from __future__ import annotations

import json
import sys

import click

from ...core.command_logger import CommandLogger
from ...core.scope import InstallScope, get_apm_dir
from ...deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
from ...deps.why_walker import (
    AmbiguousPackageError,
    PackageNotInstalledError,
    WhyEdge,
    WhyResult,
    compute_why,
    resolve_package_query,
)
from ...utils.console import set_console_stderr

# Exit codes
_EXIT_OK = 0
_EXIT_NOT_FOUND = 1
_EXIT_NO_LOCKFILE = 2


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_dep_label(dep) -> str:
    """Render ``repo_url@version`` for the human output header."""
    version = (
        dep.version
        or (dep.resolved_commit[:7] if dep.resolved_commit else None)
        or dep.resolved_ref
        or "unknown"
    )
    return f"{dep.repo_url}@{version}"


def _edge_annotation(edge: WhyEdge, is_root: bool) -> str:
    """Render the bracketed annotation for a single chain edge.

    Mirrors the design-pack convention:
    * Root edge (direct dep):   ``[constraint: ^1.2.0, declared in apm.yml]``
      or ``[declared in apm.yml]`` when constraint is unknown.
    * Inner edges:              ``[constraint: ^1.4.0]`` or empty.
    """
    parts: list[str] = []
    if edge.constraint:
        parts.append(f"constraint: {edge.constraint}")
    if is_root:
        parts.append("declared in apm.yml")
    if not parts:
        return ""
    return f"   [{', '.join(parts)}]"


def _render_human(result: WhyResult) -> str:
    """Render the human-readable explanation as a single string."""
    target = result.target
    header_kind = "direct dependency" if result.is_direct else "transitive"
    lines: list[str] = [f"[i] {_format_dep_label(target)}  ({header_kind})", ""]

    if result.is_direct:
        # Single trivial chain.
        edge = result.paths[0].chain[0]
        annotation = _edge_annotation(edge, is_root=True)
        lines.append(f"    {target.repo_url}{annotation}")
        return "\n".join(lines)

    for path in result.paths:
        for idx, edge in enumerate(path.chain):
            # Root is determined by the absence of a recorded parent, NOT by
            # position in the chain: compute_why() may emit truncated chains
            # (missing parent in lockfile, cycle break, depth-cap hit) where
            # the first element is NOT a real root. See PR #1495 review.
            is_root = edge.parent_key is None
            annotation = _edge_annotation(edge, is_root=is_root)
            prefix = "    " if idx == 0 else ("    " + " " * (idx - 1) + "+-- ")
            lines.append(f"{prefix}{edge.child_key}{annotation}")
        lines.append("")  # blank line between chains
    # Caller uses click.echo() which appends its own newline; do not add one
    # here or we emit a trailing blank line.
    return "\n".join(lines).rstrip()


def _render_json(result: WhyResult) -> str:
    """Render the JSON explanation."""
    target = result.target
    payload = {
        "package": {
            "repo_url": target.repo_url,
            "version": target.version or target.resolved_ref or target.resolved_commit,
            "source": target.source or "git",
            "is_direct": result.is_direct,
        },
        "paths": [
            {
                "chain": [
                    {
                        "repo_url": edge.child_key,
                        "constraint": edge.constraint,
                        # Directness is recorded by the walker as a missing
                        # parent_key, not by position: a corrupt or
                        # depth-capped chain may not start at a true root.
                        "is_direct": edge.parent_key is None,
                    }
                    for edge in path.chain
                ]
            }
            for path in result.paths
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Lockfile loading
# ---------------------------------------------------------------------------


def _load_lockfile(apm_dir, logger: CommandLogger) -> LockFile | None:
    """Load the lockfile for *apm_dir*; emit an error and return ``None``
    when missing or unreadable. Callers translate that into exit code 2.
    """
    migrate_lockfile_if_needed(apm_dir)
    lockfile_path = get_lockfile_path(apm_dir)
    if not lockfile_path.exists():
        logger.error("no apm.lock.yaml found in this project.")
        logger.info("Hint: run 'apm install' first.")
        return None
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        logger.error(f"could not read lockfile at {lockfile_path}")
        return None
    return lockfile


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


_HELP = (
    "Explain why a package is installed by walking the lockfile back to "
    "one or more direct dependencies that pulled it in.\n\n"
    "Examples:\n"
    "  apm deps why shared-utils\n"
    "  apm deps why acme-org/shared-utils --json\n"
    "  apm deps why shared-utils --global"
)


@click.command(name="why", help=_HELP)
@click.argument("package", required=True)
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Resolve against the user-scope lockfile (~/.apm/apm.lock.yaml).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON to stdout.",
)
def why(package: str, global_: bool, as_json: bool) -> None:
    """Entry point for ``apm deps why <pkg>``."""
    # Stream discipline: under --json, route ALL human-facing output to
    # stderr so that downstream tools (jq, scripts) can consume stdout
    # as a clean JSON document. Mirrors the convention established by
    # `apm pack --json` (commands/pack.py) and by npm / yarn / cargo.
    if as_json:
        set_console_stderr(True)

    logger = CommandLogger("deps-why")
    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    apm_dir = get_apm_dir(scope)

    lockfile = _load_lockfile(apm_dir, logger)
    if lockfile is None:
        if as_json:
            click.echo(json.dumps({"error": "no_lockfile"}), err=True)
        sys.exit(_EXIT_NO_LOCKFILE)

    try:
        target = resolve_package_query(lockfile, package)
    except AmbiguousPackageError as exc:
        if as_json:
            click.echo(
                json.dumps({"error": "ambiguous", "query": exc.query, "matches": exc.matches}),
                err=True,
            )
        else:
            logger.error(f"'{exc.query}' matches multiple packages:")
            for match in exc.matches:
                click.echo(f"  - {match}", err=True)
            logger.info("Hint: use the full owner/repo form.")
        sys.exit(_EXIT_NOT_FOUND)
    except PackageNotInstalledError as exc:
        if as_json:
            click.echo(
                json.dumps({"error": "not_installed", "query": exc.query}),
                err=True,
            )
        else:
            logger.error(f"'{exc.query}' is not installed (not in apm.lock.yaml).")
            logger.info("Hint: run 'apm deps list' to see installed packages.")
        sys.exit(_EXIT_NOT_FOUND)

    result = compute_why(lockfile, target)

    if as_json:
        click.echo(_render_json(result))
    else:
        click.echo(_render_human(result))

    sys.exit(_EXIT_OK)
