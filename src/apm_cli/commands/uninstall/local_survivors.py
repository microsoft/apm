"""What survives an uninstall: the surviving direct declarations, the surviving
local declarations that share a removed package's install slot, and what a
surviving package ships."""

from __future__ import annotations

from pathlib import Path

from ...models.apm_package import DependencyReference
from ...models.dependency.selection import (
    parse_dependency_entry as _parse_dependency_entry,
)
from ...utils.path_security import PathTraversalError


def _surviving_local_refs_at_install_path(
    package: object,
    surviving_dependencies: list[object],
    apm_modules_dir: Path,
) -> list[DependencyReference]:
    """Return surviving local declarations that share a package install slot."""
    try:
        removed_ref = _parse_dependency_entry(package)
        removed_path = removed_ref.get_install_path(apm_modules_dir)
    except (ValueError, TypeError, AttributeError, KeyError, PathTraversalError):
        return []

    survivors: list[DependencyReference] = []
    for entry in surviving_dependencies:
        try:
            survivor = _parse_dependency_entry(entry)
            if survivor.is_local and survivor.get_install_path(apm_modules_dir) == removed_path:
                survivors.append(survivor)
        except (ValueError, TypeError, AttributeError, KeyError, PathTraversalError):
            continue
    return survivors


def survivor_ships_hooks(lockfile: object, key: str, modules_dir: Path) -> bool:
    """Whether the materialized copy of the surviving locked package *key* carries hook files."""
    from ...integration.hook_integrator import HookIntegrator

    dependency = (getattr(lockfile, "dependencies", None) or {}).get(key)
    if dependency is None:
        return False
    try:
        install_path = dependency.to_dependency_ref().get_install_path(modules_dir)
    except (ValueError, PathTraversalError):
        return False
    return install_path.is_dir() and bool(HookIntegrator().find_hook_files(install_path))


def read_survivor_direct_refs(apm_yml_path, packages_to_remove):
    """Parse apm.yml's direct APM dependencies, excluding *packages_to_remove*.

    Seeds the forward reachability walk (see
    :func:`apm_cli.deps.reachability.compute_forward_reachable_keys`) with
    the project's SURVIVING direct dependencies. For the real uninstall
    path and ``--dry-run`` preview, *apm_yml_path* is still pre-edit.
    Identity subtraction turns the pre-edit dependency list into the
    correct post-removal survivor set, mirroring the identity
    matching ``_validate_uninstall_packages`` already uses.

    Returns ``(production_refs, dev_refs)`` so the walk can apply install's
    ``is_dev`` rule. Returns empty lists -- never raises -- if *apm_yml_path* is ``None``,
    missing, or malformed; the project's own manifest is a Step-1/3
    concern here, not part of this walk's fail-closed contract, which
    covers the manifests visited DURING the walk, not this seed step.
    The single survivor projection: ``apm audit --ci``'s allow-list advice
    (#4570) proves its uninstall against the same survivors.
    """
    if apm_yml_path is None:
        return [], []

    removed_identities = set()
    for pkg in packages_to_remove:
        try:
            removed_identities.add(_parse_dependency_entry(pkg).get_identity())
        except (ValueError, TypeError, AttributeError, KeyError):
            continue

    import yaml

    from ...utils.yaml_io import load_yaml

    try:
        data = load_yaml(apm_yml_path) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return [], []

    refs: dict[str, list] = {"dependencies": [], "devDependencies": []}
    for section_name, section_refs in refs.items():
        section = data.get(section_name) if isinstance(data, dict) else None
        # ``devDependencies: null`` (accepted by the manifest parser) declares nothing.
        entries = section.get("apm") if isinstance(section, dict) else None
        for dep_entry in entries if isinstance(entries, list) else ():
            try:
                ref = _parse_dependency_entry(dep_entry)
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
            if ref.get_identity() not in removed_identities:
                section_refs.append(ref)
    return refs["dependencies"], refs["devDependencies"]


def _reintegration_error_detail(
    dependency: DependencyReference,
    error: Exception,
) -> str:
    """Return verbose failure detail without exposing a local declaration."""
    error_type = type(error).__name__
    if dependency.is_local:
        return error_type
    return f"{error_type}: {error}"
