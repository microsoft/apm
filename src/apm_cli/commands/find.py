"""``apm find`` -- trace a materialized file back to its contributing package(s)."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..constants import APM_LOCK_FILENAME
from ..deps.lockfile import LockedDependency, LockFile
from ..deps.why_walker import compute_why
from ..utils.console import _rich_error

# The on-disk lockfile is always the YAML variant.
_APM_LOCK_YAML = APM_LOCK_FILENAME + ".yaml"

# Sentinel key used for workspace-owned files (local_deployed_files).
_WORKSPACE_KEY = "."


# ---------------------------------------------------------------------------
# Reverse index builder
# ---------------------------------------------------------------------------


def build_reverse_index(lockfile: LockFile) -> dict[str, list[str]]:
    """Build a reverse mapping of deployed file path -> list of owner package keys.

    Iterates all :class:`LockedDependency` entries and their ``deployed_files``
    lists, plus ``lockfile.local_deployed_files`` (keyed to ``"."``).  Returns
    a dict where each key is the path string exactly as it appears in the
    lockfile and each value is the ordered list of package unique keys that
    claim that path.

    Multi-contributor files (e.g. AGENTS.md) naturally accumulate multiple
    owners -- the index stores them in the order they are encountered, which
    is deterministic (``get_all_dependencies()`` returns deps sorted by depth
    then repo_url).
    """
    index: dict[str, list[str]] = {}

    for dep in lockfile.get_all_dependencies():
        key = dep.get_unique_key()
        for file_path in dep.deployed_files:
            if file_path not in index:
                index[file_path] = []
            if key not in index[file_path]:
                index[file_path].append(key)

    for file_path in lockfile.local_deployed_files:
        if file_path not in index:
            index[file_path] = []
        if _WORKSPACE_KEY not in index[file_path]:
            index[file_path].append(_WORKSPACE_KEY)

    return index


# ---------------------------------------------------------------------------
# Origin formatting
# ---------------------------------------------------------------------------


def _format_origin(dep: LockedDependency) -> str:
    """Return a human-readable ASCII origin string for *dep*.

    Priority:
    1. OCI registry: resolved_url starting with ``oci://``
    2. Local source: use local_path
    3. Git ref: resolved_ref (first truthy)
    4. Git tag: resolved_tag
    5. Git commit: resolved_commit (first 12 chars)
    6. Fallback: repo_url
    """
    if dep.resolved_url and dep.resolved_url.startswith("oci://"):
        return dep.resolved_url
    if dep.source == "local" and dep.local_path:
        return dep.local_path
    if dep.resolved_ref:
        ref_part = dep.resolved_ref
        if dep.repo_url:
            return f"{dep.repo_url}@{ref_part}"
        return ref_part
    if dep.resolved_tag:
        tag_part = dep.resolved_tag
        if dep.repo_url:
            return f"{dep.repo_url}@{tag_part}"
        return tag_part
    if dep.resolved_commit:
        commit = dep.resolved_commit[:12]
        if dep.repo_url:
            return f"{dep.repo_url}@{commit}"
        return commit
    return dep.repo_url


# ---------------------------------------------------------------------------
# Why-path rendering
# ---------------------------------------------------------------------------


def _render_why(lockfile: LockFile, dep: LockedDependency) -> str:
    """Render the root-to-target chain for *dep* as a multi-line ASCII string."""
    result = compute_why(lockfile, dep)
    lines: list[str] = []
    for path in result.paths:
        chain_parts = []
        for edge in path.chain:
            if edge.parent_key is None:
                chain_parts.append(f"apm.yml -> {edge.child_key}")
            else:
                chain_parts.append(edge.child_key)
        lines.append(" -> ".join(chain_parts))
    return "\n".join(lines) if lines else dep.repo_url


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def _lookup_in_index(query: str, index: dict[str, list[str]]) -> list[str] | None:
    """Return the list of owner keys for *query*, or None if not found.

    Checks:
    1. Exact match on the normalized query string.
    2. Prefix match: if a key in the index ends with "/" and the query starts
       with that prefix, the entry owns the file. When multiple directory
       entries match (overlapping prefixes), the most specific (longest)
       prefix wins.
    """
    # Normalize forward slashes only -- no os.sep conversion needed here
    # because deployed_files entries are always stored as POSIX paths.
    normalized = query.replace("\\", "/")

    if normalized in index:
        return index[normalized]

    best_match: list[str] | None = None
    best_prefix_len = -1

    for entry, owners in index.items():
        if entry.endswith("/") and normalized.startswith(entry):
            if len(entry) > best_prefix_len:
                best_prefix_len = len(entry)
                best_match = owners
        elif normalized.endswith("/") and entry.startswith(normalized):
            if len(normalized) > best_prefix_len:
                best_prefix_len = len(normalized)
                best_match = owners

    return best_match


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("find")
@click.argument("file_path")
@click.option(
    "--source",
    "show_source",
    is_flag=True,
    help="Append resolved origin (oci/git/local) to each package name.",
)
@click.option(
    "--path",
    "show_path",
    is_flag=True,
    help="Print full root-to-target dependency chain (like apm deps why).",
)
@click.pass_context
def find(ctx: click.Context, file_path: str, show_source: bool, show_path: bool) -> None:
    """Trace a materialized file back to its contributing package(s).

    FILE_PATH is a relative path to a file deployed by an installed package.

    Exit 0 if the file is tracked; non-zero if it is unknown.
    """
    cwd = Path.cwd()
    lockfile_path = cwd / _APM_LOCK_YAML

    if not lockfile_path.exists():
        _rich_error(
            f"No lockfile found at {_APM_LOCK_YAML}. Run 'apm install' first.",
            symbol="error",
        )
        sys.exit(2)

    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        _rich_error(
            f"Could not read {_APM_LOCK_YAML}. The file may be corrupt.",
            symbol="error",
        )
        sys.exit(2)

    index = build_reverse_index(lockfile)

    # Normalize the query to a POSIX-style relative path.
    normalized_query = file_path.replace("\\", "/").lstrip("/")
    # Strip a leading "./" prefix common in shell tab-completion output.
    if normalized_query.startswith("./"):
        normalized_query = normalized_query[2:]

    owner_keys = _lookup_in_index(normalized_query, index)

    if not owner_keys:
        _rich_error(
            f"'{file_path}' is not tracked by any installed package in {_APM_LOCK_YAML}.",
            symbol="error",
        )
        sys.exit(1)

    for dep_key in owner_keys:
        if dep_key == _WORKSPACE_KEY:
            label = "."
            if show_source:
                click.echo(f"{label}  (workspace)")
            else:
                click.echo(label)
            continue

        dep = lockfile.get_dependency(dep_key)
        if dep is None:
            click.echo(dep_key)
            continue

        if show_path:
            chain_str = _render_why(lockfile, dep)
            click.echo(f"{dep.repo_url}")
            for line in chain_str.splitlines():
                click.echo(f"  {line}")
        elif show_source:
            origin = _format_origin(dep)
            click.echo(f"{dep.repo_url}  {origin}")
        else:
            click.echo(dep.repo_url)
