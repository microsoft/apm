"""Unmanaged-files governance check (split out of ``policy_checks``).

Houses Check 16 -- the single unified unmanaged-files report -- plus its
classification, deny-conflict, formatting, and symlink-guard helpers. Split
into its own module to keep ``policy_checks`` within the repository
file-length guardrail. Re-exported from ``policy_checks`` for backward
compatibility (tests and callers import ``_check_unmanaged_files`` from there).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .models import CheckResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..deps.lockfile import LockFile
    from .schema import UnmanagedFilesPolicy

_DEFAULT_GOVERNANCE_DIRS = [
    ".github/agents",
    ".github/instructions",
    ".github/hooks",
    ".cursor/rules",
    ".claude",
    ".opencode",
    ".kiro",
]

_MAX_UNMANAGED_SCAN_FILES = 10_000

# Appended once to a non-empty unmanaged-files report so a flagged file is
# self-resolving: the reader learns how to track it or how to suppress it.
_UNMANAGED_NEXT_ACTION = (
    "Next: run 'apm install <ref>' to track a flagged file, "
    "or add a glob to unmanaged_files.exclude to suppress it."
)


def _classify_primitive_type(rel_path: str) -> str | None:
    """Lazily classify an already-flagged unmanaged file by APM convention.

    Called ONLY on files already flagged as unmanaged -- never on the whole
    tree -- so a user can triage skill / agent / instruction / mcp artifacts.
    Returns ``None`` when the path matches no known primitive convention.
    """
    posix = rel_path.replace("\\", "/").lower()
    name = posix.rsplit("/", 1)[-1]
    segments = posix.split("/")
    # Explicit filename conventions win first (most specific signal).
    _filename_rules: tuple[tuple[Callable[[str], bool], str], ...] = (
        (lambda n: n.endswith(".instructions.md"), "instruction"),
        (lambda n: n.endswith(".agent.md"), "agent"),
        (lambda n: n == "mcp.json" or n.endswith(".mcp.json"), "mcp"),
        (lambda n: n == "skill.md", "skill"),
    )
    for predicate, label in _filename_rules:
        if predicate(name):
            return label
    # Directory-segment hints next (less specific). MCP is narrowed to a
    # dedicated ``.mcp/`` root -- a directory merely named ``mcp`` is not an
    # MCP config and must not mislabel files under it.
    _segment_rules: tuple[tuple[str, str], ...] = (
        ("instructions", "instruction"),
        ("agents", "agent"),
        ("skills", "skill"),
        (".mcp", "mcp"),
    )
    for segment, label in _segment_rules:
        if segment in segments:
            return label
    return None


def _unmanaged_deny_conflict(
    rel_path: str,
    dependency_deny: tuple[str, ...] | None,
    mcp_deny: tuple[str, ...] | None,
) -> str | None:
    """Return the deny pattern an unmanaged file conflicts with, or ``None``.

    Surfaces APM's OWN deny policy as a human-resolve conflict: the dependency
    side is defaults-inclusive (``dependencies.effective_deny``) and the MCP
    side is the raw ``mcp.deny`` -- mirroring the deny-list checks exactly.
    Routes through the same ``first_matching_pattern`` matcher the deny-list
    checks use -- never a second matcher.
    """
    from .matcher import first_matching_pattern

    name = rel_path.rsplit("/", 1)[-1]
    for patterns in (dependency_deny, mcp_deny):
        hit = first_matching_pattern(rel_path, patterns)
        if hit is None:
            # Fall back to the basename so a deny glob written against a bare
            # filename (e.g. 'mcp.json') still surfaces the conflict.
            hit = first_matching_pattern(name, patterns)
        if hit is not None:
            return hit
    return None


def _format_unmanaged_detail(
    rel_path: str,
    primitive_type: str | None,
    deny_hit: str | None,
) -> str:
    """Render one enriched, ASCII-only finding line for an unmanaged file."""
    label = f"{rel_path} [type: {primitive_type}]" if primitive_type else rel_path
    reasons = ["not tracked in apm.lock.yaml"]
    if deny_hit:
        reasons.append(f"matches deny rule ({deny_hit})")
    return f"{label} -- {'; '.join(reasons)}"


def _symlink_escapes_workspace(path: Path, project_root: Path) -> bool:
    """Return True if *path* is a symlink resolving outside *project_root*.

    Guards the traversal so a symlink pointing out of the workspace is never
    followed (no traversal bomb); broken or looping links also count as
    escaping and are skipped.
    """
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        return False
    except (OSError, RuntimeError, ValueError):
        return True


def _check_unmanaged_files(
    project_root: Path,
    lock: LockFile | None,
    policy: UnmanagedFilesPolicy,
    *,
    dependency_deny: tuple[str, ...] | None = None,
    mcp_deny: tuple[str, ...] | None = None,
) -> CheckResult:
    """Check 16: surface files in governance dirs not tracked in apm.lock.yaml.

    This is the ONE unified unmanaged-files report. Each flagged file is
    enriched in-place (within this single scan) with a factual reason, a lazy
    primitive-type classification, and -- where it matches APM's own
    ``dependencies.deny`` / ``mcp.deny`` -- a deny-conflict note for a human to
    resolve. Paths matching ``policy.exclude`` are suppressed. This is drift /
    divergence visibility, not supply-chain-attack prevention.
    """
    from .matcher import first_matching_pattern

    if policy.effective_action == "ignore":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message="Unmanaged files check disabled (action: ignore)",
        )

    dirs = policy.directories if policy.directories else _DEFAULT_GOVERNANCE_DIRS
    exclude = policy.exclude or ()

    # Build set of deployed files AND directory prefixes from lockfile
    deployed: set[str] = set()
    deployed_dir_prefixes: list[str] = []
    if lock:
        for _key, dep in lock.dependencies.items():
            for f in dep.deployed_files:
                cleaned = f.rstrip("/")
                deployed.add(cleaned)
                if f.endswith("/"):
                    deployed_dir_prefixes.append(cleaned + "/")

    dir_prefix_tuple = tuple(deployed_dir_prefixes)

    details: list[str] = []
    unmanaged_count = 0
    files_scanned = 0
    cap_hit = False
    for gov_dir in dirs:
        dir_path = project_root / gov_dir
        if not dir_path.exists() or not dir_path.is_dir():
            continue
        # os.walk(followlinks=False) never recurses INTO a directory symlink, so
        # a symlinked dir resolving outside the workspace is never traversed
        # (the house pattern from security/gate.py). File symlinks still appear
        # in the listing and are guarded individually below.
        for dirpath, _subdirs, filenames in os.walk(dir_path, followlinks=False):
            for fname in filenames:
                file_path = Path(dirpath) / fname
                # File-symlink guard: never follow a link out of the workspace.
                if file_path.is_symlink() and _symlink_escapes_workspace(file_path, project_root):
                    continue
                if not file_path.is_file():
                    continue
                files_scanned += 1
                if files_scanned > _MAX_UNMANAGED_SCAN_FILES:
                    cap_hit = True
                    break
                rel = file_path.relative_to(project_root).as_posix()
                if rel in deployed or (dir_prefix_tuple and rel.startswith(dir_prefix_tuple)):
                    continue
                if first_matching_pattern(rel, exclude) is not None:
                    continue
                primitive_type = _classify_primitive_type(rel)
                deny_hit = _unmanaged_deny_conflict(rel, dependency_deny, mcp_deny)
                details.append(_format_unmanaged_detail(rel, primitive_type, deny_hit))
                unmanaged_count += 1
            if cap_hit:
                break
        if cap_hit:
            break

    if cap_hit:
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message=(
                f"Scan capped at {_MAX_UNMANAGED_SCAN_FILES:,} files "
                "-- skipping unmanaged-files check"
            ),
            details=[
                f"Governance directories contain > {_MAX_UNMANAGED_SCAN_FILES:,} files; "
                "consider adding exclude patterns in the unmanaged_files policy"
            ],
        )

    if not details:
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message="No unmanaged files in governance directories",
        )

    # One report carries a single next-action hint after the per-file lines.
    details.append(_UNMANAGED_NEXT_ACTION)

    if policy.effective_action == "warn":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message=f"{unmanaged_count} unmanaged file(s) found (warn)",
            details=details,
        )

    return CheckResult(
        name="unmanaged-files",
        passed=False,
        message=f"{unmanaged_count} unmanaged file(s) in governance directories",
        details=details,
    )
