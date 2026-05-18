"""Package dependency resolution for deps list command."""

from __future__ import annotations

from pathlib import Path

from ....constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ....models.apm_package import APMPackage
from ..._helpers import _expand_with_ancestors, _standalone_installed_packages
from .._utils import _count_primitives, _deps_list_source_label, _is_nested_under_package


def _record_manifest_declared_source(declared_sources: dict, dep) -> None:
    """Add one declared dependency source from apm.yml."""
    repo_parts = dep.repo_url.split("/")
    source = _deps_list_source_label(dep.host, is_local=dep.is_local)
    if not dep.is_virtual:
        if dep.is_azure_devops() and len(repo_parts) >= 3:
            declared_sources[f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}"] = source
        elif len(repo_parts) >= 2:
            declared_sources[f"{repo_parts[0]}/{repo_parts[1]}"] = source
        return
    if dep.is_virtual_subdirectory() and dep.virtual_path:
        if dep.is_azure_devops() and len(repo_parts) >= 3:
            declared_sources[
                f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}/{dep.virtual_path}"
            ] = source
        elif len(repo_parts) >= 2:
            declared_sources[f"{repo_parts[0]}/{repo_parts[1]}/{dep.virtual_path}"] = source
        return
    package_name = dep.get_virtual_package_name()
    if dep.is_azure_devops() and len(repo_parts) >= 3:
        declared_sources[f"{repo_parts[0]}/{repo_parts[1]}/{package_name}"] = source
    elif len(repo_parts) >= 2:
        declared_sources[f"{repo_parts[0]}/{package_name}"] = source


def _load_lockfile_declared_sources(
    apm_dir, declared_sources: dict, insecure_lock_deps: dict
) -> None:
    """Merge declared sources inferred from the lockfile."""
    from ....deps.lockfile import LockFile, get_lockfile_path

    lockfile_path = get_lockfile_path(apm_dir)
    if not lockfile_path.exists():
        return
    lockfile = LockFile.read(lockfile_path)
    for dep in lockfile.dependencies.values():
        dep_key = dep.get_unique_key()
        if dep_key and dep_key not in declared_sources:
            declared_sources[dep_key] = _deps_list_source_label(
                dep.host, lockfile_source=dep.source
            )
        if getattr(dep, "is_insecure", False):
            insecure_lock_deps[dep_key] = dep


def _load_declared_sources(apm_dir):
    """Load declared dependencies from apm.yml and return declared_sources dict."""
    declared_sources = {}  # dep_path -> 'github' | 'gitlab' | 'azure-devops' | 'local'
    insecure_lock_deps = {}

    try:
        apm_yml_path = apm_dir / APM_YML_FILENAME
        if apm_yml_path.exists():
            project_package = APMPackage.from_apm_yml(apm_yml_path)
            for dep in project_package.get_apm_dependencies():
                _record_manifest_declared_source(declared_sources, dep)
    except Exception:
        pass  # Continue without orphan detection if apm.yml parsing fails

    try:
        _load_lockfile_declared_sources(apm_dir, declared_sources, insecure_lock_deps)
    except Exception:
        pass  # Continue without lockfile if it can't be read

    return declared_sources, insecure_lock_deps


def _scan_installed_candidates(apm_modules_path):
    """Scan for installed package candidates and return list of (candidate, name, has_apm_yml, has_skill_md)."""
    scanned_candidates = []
    for candidate in apm_modules_path.rglob("*"):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        has_apm_yml = (candidate / APM_YML_FILENAME).exists()
        has_skill_md = (candidate / SKILL_MD_FILENAME).exists()
        if not has_apm_yml and not has_skill_md:
            continue
        rel_parts = candidate.relative_to(apm_modules_path).parts
        if len(rel_parts) < 2:
            continue
        # Skip sub-skills inside .apm/ directories -- they belong to the parent package
        if ".apm" in rel_parts:
            continue

        # Skip skill sub-dirs nested inside another package (e.g. plugin
        # skills/ directories that are deployment artifacts, not packages).
        if (
            has_skill_md
            and not has_apm_yml
            and _is_nested_under_package(candidate, apm_modules_path)
        ):
            continue
        scanned_candidates.append((candidate, "/".join(rel_parts), has_apm_yml, has_skill_md))

    return scanned_candidates


def _compute_orphan_set(apm_dir, apm_modules_path, scanned_candidates, declared_sources):
    """Compute the set of paths that should not be considered orphaned (includes ancestors)."""
    from ....deps.lockfile import LockFile, get_lockfile_path

    # Precompute expected paths + ancestors for O(1) orphan checks.
    # Mirror prune.py / _check_orphaned_packages: pass the standalone
    # installed paths (lockfile-membership + apm.yml fallback) so a
    # genuinely orphaned ``owner/repo`` package is not masked when a
    # sibling subdirectory dep shares the same install root.
    try:
        try:
            lockfile_path_for_check = get_lockfile_path(apm_dir)
            lockfile_for_check = (
                LockFile.read(lockfile_path_for_check) if lockfile_path_for_check.exists() else None
            )
        except Exception:
            lockfile_for_check = None
        scanned_names = [name for _c, name, _h, _s in scanned_candidates]
        standalone_installed_for_check = _standalone_installed_packages(
            scanned_names, apm_modules_path, lockfile=lockfile_for_check
        )
    except Exception:
        standalone_installed_for_check = []
    declared_with_ancestors = _expand_with_ancestors(
        declared_sources.keys(), standalone_installed_for_check
    )
    return declared_with_ancestors


def _build_package_list(
    scanned_candidates, declared_with_ancestors, declared_sources, insecure_lock_deps, logger
):
    """Build the installed_packages and orphaned_packages lists from scanned candidates."""
    installed_packages = []
    orphaned_packages = []
    for candidate, org_repo_name, has_apm_yml, _has_skill_md in scanned_candidates:
        try:
            version = "unknown"
            if has_apm_yml:
                package = APMPackage.from_apm_yml(candidate / APM_YML_FILENAME)
                version = package.version or "unknown"
            primitives = _count_primitives(candidate)

            is_orphaned = org_repo_name not in declared_with_ancestors
            if is_orphaned:
                orphaned_packages.append(org_repo_name)

            locked_dep = insecure_lock_deps.get(org_repo_name)
            installed_packages.append(
                {
                    "name": org_repo_name,
                    "version": version,
                    "source": "orphaned"
                    if is_orphaned
                    else declared_sources.get(org_repo_name, "github"),
                    "primitives": primitives,
                    "path": str(candidate),
                    "is_orphaned": is_orphaned,
                    "is_insecure": locked_dep is not None,
                    "insecure_via": (
                        f"via {locked_dep.resolved_by}"
                        if locked_dep and locked_dep.resolved_by
                        else "direct"
                    ),
                }
            )
        except Exception as e:
            logger.warning(f"Failed to read package {org_repo_name}: {e}")

    return installed_packages, sorted(orphaned_packages)


def _resolve_scope_deps(apm_dir, logger, insecure_only=False):
    """Resolve installed packages and orphan status for a single scope.

    Returns ``(installed_packages, orphaned_packages)`` where
    *installed_packages* is a list of dicts and *orphaned_packages* is a
    list of name strings, or ``(None, None)`` when no ``apm_modules``
    directory exists.
    """
    apm_modules_path = apm_dir / APM_MODULES_DIR

    # Check if apm_modules exists
    if not apm_modules_path.exists():
        return None, None

    # Load project dependencies to check for orphaned packages
    # GitHub: owner/repo or owner/virtual-pkg-name (2 levels)
    # Azure DevOps: org/project/repo or org/project/virtual-pkg-name (3 levels)
    declared_sources, insecure_lock_deps = _load_declared_sources(apm_dir)

    # Scan for installed packages in org-namespaced structure
    # Walks the tree to find directories containing apm.yml or SKILL.md,
    # handling GitHub (2-level), ADO (3-level), and subdirectory (4+ level) packages.
    # First pass: collect valid candidate paths for ancestor-aware orphan check.
    scanned_candidates = _scan_installed_candidates(apm_modules_path)

    # Compute orphan set (paths that should not be considered orphaned)
    declared_with_ancestors = _compute_orphan_set(
        apm_dir, apm_modules_path, scanned_candidates, declared_sources
    )

    # Build package lists
    installed_packages, orphaned_packages = _build_package_list(
        scanned_candidates, declared_with_ancestors, declared_sources, insecure_lock_deps, logger
    )

    if insecure_only:
        installed_packages = [pkg for pkg in installed_packages if pkg["is_insecure"]]

    return installed_packages, orphaned_packages
