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


def retired_native_harnesses(
    active_targets: Iterable[TargetProfile], declared_targets: Iterable[TargetProfile] | None
) -> frozenset[str]:
    """Native-plugin harnesses contraction retires, whose enable entries APM drops (#4874)."""
    from apm_cli.native_plugins.scopes import HARNESSES

    active = tuple(active_targets)
    declared = None if declared_targets is None else tuple(declared_targets)
    return frozenset(
        name
        for name in HARNESSES
        if target_retired(name, active_targets=active, declared_targets=declared)
    )


def _allowed_harnesses(locked: Any) -> frozenset[str]:
    """Harnesses a native lock row's format, scope and own ``targets:`` allow."""
    from apm_cli.native_plugins.scopes import record_harnesses

    harnesses = record_harnesses(locked.package_type, locked.native_plugin.marketplace_path)
    if locked.target_subset:
        harnesses &= frozenset(locked.target_subset)
    return harnesses


def retired_native_plugins(
    lockfile: Any, active_targets: Iterable[TargetProfile], declared_targets: Any
) -> dict[str, tuple[str, str]]:
    """``{dep_key: (root, harness)}`` for native plugins whose every harness is retired (#4744).

    A plugin another still-declared harness loads (#4559) keeps its record.
    """
    if declared_targets is None:
        return {}
    retired = {}
    for key, locked in lockfile.dependencies.items():
        record = locked.native_plugin
        if record is None:
            continue
        harnesses = _allowed_harnesses(locked)
        if harnesses and all(
            target_retired(name, active_targets=active_targets, declared_targets=declared_targets)
            for name in harnesses
        ):
            # Name a harness the lock's declaration recorded, so the #4517 precedence filter
            # in dropped_target_files keeps it; the retirement decision above is unchanged.
            recorded = harnesses & frozenset(getattr(lockfile, "manifest_targets", None) or ())
            retired[key] = (record.root, min(recorded or harnesses))
    return retired


def retired_native_declarations(
    lockfile: Any, active_targets: Iterable[TargetProfile], declared_targets: Any
) -> dict[str, str]:
    """``{settings file: harness}`` of APM enable entries target contraction retires (#4874).

    For a project-scope plugin another declared harness still loads: each
    retired harness that held APM's ``true`` (``native_declared``; an older
    row, its allowed harnesses), as ``registrar._owned_in`` retires it. The
    lock records what APM wrote, so no manifest-history filter applies. A
    plugin every harness of which is retired is :func:`retired_native_plugins`'.
    """
    from apm_cli.native_plugins.scopes import PROJECT_SETTINGS_RELATIVE, is_project_path

    if declared_targets is None:
        return {}
    active = tuple(active_targets)
    whole = retired_native_plugins(lockfile, active, declared_targets)
    found = {}
    for key, locked in lockfile.dependencies.items():
        record = locked.native_plugin
        if record is None or key in whole or not is_project_path(record.marketplace_path):
            continue
        wrote = locked.native_declared
        for harness in _allowed_harnesses(locked) if wrote is None else wrote:
            # native_declared is advisory: ignore a name that is no native harness.
            if harness in PROJECT_SETTINGS_RELATIVE and target_retired(
                harness, active_targets=active, declared_targets=declared_targets
            ):
                found[PROJECT_SETTINGS_RELATIVE[harness]] = harness
    return found
