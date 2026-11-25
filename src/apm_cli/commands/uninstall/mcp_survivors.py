"""Lock rows whose MCP servers survive an uninstall (#4896)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def surviving_package_keys(
    lockfile, apm_package, apm_modules_dir: Path, denied: Mapping[str, str], logger=None
) -> frozenset[str] | None:
    """Return the lock keys whose MCP servers stay, or ``None`` for every row.

    A direct row apm.yml no longer declares (removed by hand, say) is asked
    of the uninstall orphan owner as a removal root: unless a surviving
    package still reaches it, it and its unreachable descendants keep no
    shared server alive. A package the gates deny never stays. When the
    evidence cannot be read or the walk is incomplete, every row the gates
    allow stays, as before.
    """
    from apm_cli.core.scope import manifest_file
    from apm_cli.install.orphans import (
        OrphanOperation,
        ReachabilityProbe,
        decide_orphan_removal,
    )

    from .local_survivors import read_survivor_direct_refs

    for key, why in sorted(denied.items()):
        if logger is not None:
            logger.warning(f"{key} is no longer allowed ({why}); removing its MCP servers")
    allowed = frozenset(key for key in lockfile.dependencies if key not in denied)
    unchanged = allowed if denied else None
    try:
        if apm_package.package_path is None:
            return unchanged
        manifest_root = Path(apm_package.package_path)
        direct, dev = read_survivor_direct_refs(manifest_file(manifest_root), ())
        declared = {ref.get_unique_key() for ref in (*direct, *dev)}
        undeclared = {
            dep.get_unique_key(): dep.to_dependency_ref()
            for dep in lockfile.get_package_dependencies()
            if dep.resolved_by is None and dep.get_unique_key() not in declared
        }
        if not undeclared:
            return unchanged
        decision = decide_orphan_removal(
            operation=OrphanOperation.UNINSTALL,
            roots=list(undeclared.values()),
            candidate_universe=lockfile,
            installed_paths=None,
            resolved_keys=None,
            intended=None,
            materialized=None,
            reachability=ReachabilityProbe(
                manifest_root, apm_modules_dir, tuple(direct), tuple(dev)
            ),
            deployment_claims=None,
        )
    except Exception:
        return unchanged
    if decision.reachability is None or not decision.reachability.complete:
        # A survivor whose manifest cannot be read may still need the row.
        return unchanged
    gone = (set(undeclared) - decision.rescued_roots) | set(decision.removed_keys)
    return frozenset(key for key in allowed if key not in gone)
