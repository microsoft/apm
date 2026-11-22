"""Which deployed paths the active install targets govern.

Split from ``install/manifest_reconcile.py`` (its CI line budget); the
reconciler and the Claude plugin phase share these predicates.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile


def install_governance(
    targets: list[TargetProfile],
    *,
    bounded: bool = False,
) -> tuple[set[str], set[str]]:
    """Return ``(file_prefixes, uri_schemes)`` governed by *targets*.

    Dedicated target roots govern their full subtree. The shared ``.agents``
    root is partitioned by primitive subdirectory so one active target cannot
    claim a declared sibling's files (for example, Copilot's
    ``.agents/skills`` versus Antigravity's ``.agents/rules``).

    ``bounded`` partitions dedicated roots the same way. Ownership tests need
    the whole root, but a tree walk does not: a root such as ``.claude`` also
    holds session transcripts and other data APM never deploys.

    ``uri_schemes`` is the set of lockfile URI schemes used by dynamic /
    user-machine targets (``copilot-app`` -> ``copilot-app-db://``,
    ``copilot-cowork`` -> ``cowork://``).
    """
    file_prefixes: set[str] = set()
    uri_schemes: set[str] = set()
    from apm_cli.integration.targets import target_lockfile_uri_schemes

    for target in targets or []:
        target_schemes = target_lockfile_uri_schemes(target)
        if target_schemes:
            uri_schemes.update(target_schemes)
            continue
        root = str(getattr(target, "root_dir", None) or "").rstrip("/")
        shared_root = root == ".agents"
        if root and not shared_root and not bounded:
            file_prefixes.add(root + "/")
        primitives = getattr(target, "primitives", None)
        if isinstance(primitives, dict):
            for mapping in primitives.values():
                deploy_root = getattr(mapping, "deploy_root", None)
                base = str(deploy_root or root or "").rstrip("/")
                if not base or (base != ".agents" and not bounded):
                    continue
                subdir = getattr(mapping, "subdir", None)
                if subdir:
                    file_prefixes.add(f"{base}/{str(subdir).strip('/')}/")
                    continue
                extension = getattr(mapping, "extension", None)
                if extension:
                    file_prefixes.add(f"{base}/{str(extension).strip('/')}")
                else:
                    # Compatibility for minimal TargetProfile stand-ins.
                    file_prefixes.add(f"{base}/")
        if root and (shared_root or bounded):
            for generated in getattr(target, "generated_files", ()) or ():
                file_prefixes.add(f"{root}/{str(generated).lstrip('/')}")
    return file_prefixes, uri_schemes


def is_governed_by_install(path: str, file_prefixes: set[str], uri_schemes: set[str]) -> bool:
    """Return ``True`` if *path* is owned by the current install's targets.

    File paths are matched by top-level directory; scheme URIs (e.g.
    ``copilot-app-db://``, ``cowork://``) are matched by their scheme.
    """
    if "://" in path:
        scheme = path.split("://", 1)[0] + "://"
        return scheme in uri_schemes
    return any(
        path.startswith(prefix) if prefix.endswith("/") else path == prefix
        for prefix in file_prefixes
    )


def target_retired(
    name: str,
    *,
    active_targets: Iterable[TargetProfile],
    declared_targets: Iterable[TargetProfile] | None,
) -> bool:
    """Whether contraction retires target *name*: apm.yml declares targets, and
    neither they nor this run's active targets name it."""
    if declared_targets is None:
        return False
    return name not in {profile.name for profile in (*active_targets, *declared_targets)}


def retired_native_roots(
    lockfile: Any, active_targets: Iterable[TargetProfile], declared_targets: Any
) -> dict[str, str]:
    """``{root: "claude"}`` for each native Claude plugin root of a retired claude (#4744)."""
    from apm_cli.native_plugins.record import lock_native_plugins

    if not target_retired(
        "claude", active_targets=active_targets, declared_targets=declared_targets
    ):
        return {}
    return {record.root: "claude" for record in lock_native_plugins(lockfile).values()}
