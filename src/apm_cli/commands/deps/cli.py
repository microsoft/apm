"""APM dependency management CLI commands."""

import shutil
import sys
from pathlib import Path

import click

# Import existing APM components
from ...constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ...core.command_logger import CommandLogger
from ...core.target_detection import TargetParamType
from ...deps.lockfile import LockedDependency
from ...models.apm_package import APMPackage
from .._helpers import (
    _expand_with_ancestors,
    _standalone_installed_packages,
)
from ._cli_ops import _show_scope_deps, _update_impl
from ._utils import (
    _absolute_local_display,
    _count_primitives,
    _get_package_display_info,
    _is_absolute_local_path,
    _is_nested_under_package,
    _logical_local_display,
    _walk_tree_children,
)
from .why import why as _why_cmd

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _format_primitive_counts(primitives):
    """Format primitive type counts into a comma-separated summary string."""
    parts = []
    for ptype, count in primitives.items():
        if count > 0:
            parts.append(f"{count} {ptype}")
    return ", ".join(parts)


def _deps_list_source_label(
    host: str | None,
    *,
    is_local: bool = False,
    lockfile_source: str | None = None,
) -> str:
    """Map host / local flags to the ``apm deps list`` Source column."""
    from ...utils.github_host import is_azure_devops_hostname, is_gitlab_hostname

    if is_local or lockfile_source == "local":
        return "local"
    if lockfile_source == "registry":
        return "registry"
    if host and is_azure_devops_hostname(host):
        return "azure-devops"
    if host and is_gitlab_hostname(host):
        return "gitlab"
    return "github"


def _dep_display_name(dep: LockedDependency) -> str:
    """Get display name for a locked dependency (key@version).

    Local deps render a logical, portable identity instead of the lockfile
    unique key. For anchored transitive local deps the unique key is an
    absolute ``local:/...`` slot (see build_dependency_unique_key), which would
    leak the host filesystem path into user-facing tree output. Prefer the
    declared relative ``local_path`` (``../pkg``); fall back to the logical
    ``repo_url`` (``_local/pkg``) when the path is absent or itself absolute
    (``/Users/...``, ``~/...``, ``C:\\...``). Remote deps keep their canonical
    unique key.
    """
    if dep.source == "local":
        if dep.local_path and not _is_absolute_local_path(dep.local_path):
            key = dep.local_path
        elif dep.local_path:
            key = _absolute_local_display(dep.local_path, dep.repo_url)
        else:
            key = dep.repo_url
    else:
        key = dep.get_unique_key()
    version = (
        dep.version
        or (dep.resolved_commit[:7] if dep.resolved_commit else None)
        or dep.resolved_ref
        or "latest"
    )
    return f"{key}@{version}"


def _add_tree_children(
    parent_branch,
    parent_key: str,
    children_map: dict[str, list[LockedDependency]],
) -> None:
    """Add every transitive dependency to a Rich tree without a depth limit."""
    branches = {(): parent_branch}
    for child_dep, path, _, is_circular in _walk_tree_children(parent_key, children_map):
        child_name = _dep_display_name(child_dep)
        suffix = " (circular)" if is_circular else ""
        child_branch = branches[path[:-1]].add(f"[dim]{child_name}{suffix}[/dim]")
        if not is_circular:
            branches[path] = child_branch


def _echo_tree_children(
    parent_key: str,
    children_map: dict[str, list[LockedDependency]],
    prefix: str = "",
) -> None:
    """Render every transitive dependency without Rich or a depth limit."""
    for child_dep, _, last_flags, is_circular in _walk_tree_children(parent_key, children_map):
        indentation = "".join("    " if is_last else "|   " for is_last in last_flags[:-1])
        child_prefix = "+-- " if last_flags[-1] else "|-- "
        suffix = " (circular)" if is_circular else ""
        click.echo(f"{prefix}{indentation}{child_prefix}{_dep_display_name(child_dep)}{suffix}")


# ---------------------------------------------------------------------------
# Data resolution — deps list
# ---------------------------------------------------------------------------


def _resolve_scope_deps(apm_dir, logger, insecure_only=False):
    """Resolve installed packages and orphan status for a single scope.

    Returns ``(installed_packages, orphaned_packages)`` where
    *installed_packages* is a list of dicts and *orphaned_packages* is a
    list of name strings, or ``(None, None)`` when no ``apm_modules``
    directory exists.
    """
    from ...deps.lockfile import LockFile, get_lockfile_path

    apm_modules_path = apm_dir / APM_MODULES_DIR
    insecure_lock_deps = {}

    # Check if apm_modules exists
    if not apm_modules_path.exists():
        return None, None

    # Load project dependencies to check for orphaned packages
    # GitHub: owner/repo or owner/virtual-pkg-name (2 levels)
    # Azure DevOps: org/project/repo or org/project/virtual-pkg-name (3 levels)
    declared_sources = {}  # dep_path -> 'github' | 'gitlab' | 'azure-devops' | 'local'
    try:
        apm_yml_path = apm_dir / APM_YML_FILENAME
        if apm_yml_path.exists():
            project_package = APMPackage.from_apm_yml(apm_yml_path)
            for dep in project_package.get_apm_dependencies():
                # Build the expected installed package name
                repo_parts = dep.repo_url.split("/")
                source = _deps_list_source_label(
                    dep.host, is_local=dep.is_local, lockfile_source=dep.source
                )
                is_ado = dep.is_azure_devops() and len(repo_parts) >= 3
                is_gh = len(repo_parts) >= 2

                if not dep.is_virtual:
                    # Regular package: use full repo_url path
                    if is_ado:
                        declared_sources[f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}"] = (
                            source
                        )
                    elif is_gh:
                        declared_sources[f"{repo_parts[0]}/{repo_parts[1]}"] = source
                    continue

                if dep.is_virtual_subdirectory() and dep.virtual_path:
                    # Virtual subdirectory packages keep natural path structure.
                    if is_ado:
                        declared_sources[
                            f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}/{dep.virtual_path}"
                        ] = source
                    elif is_gh:
                        declared_sources[f"{repo_parts[0]}/{repo_parts[1]}/{dep.virtual_path}"] = (
                            source
                        )
                    continue

                # Virtual file/collection packages are flattened.
                package_name = dep.get_virtual_package_name()
                if is_ado:
                    declared_sources[f"{repo_parts[0]}/{repo_parts[1]}/{package_name}"] = source
                elif is_gh:
                    declared_sources[f"{repo_parts[0]}/{package_name}"] = source
    except Exception:
        pass  # Continue without orphan detection if apm.yml parsing fails

    # Also load lockfile deps to avoid false orphan flags on transitive deps.
    # Local transitive deps are installed into hashed physical slots
    # (``_local/<hash>/pkg``) to prevent sibling collisions (#2155), but their
    # logical identity is the un-hashed lockfile ``repo_url`` (``_local/pkg``).
    # Key orphan/insecure state on the logical id and remember each physical
    # slot -> logical id so the scan below can report the logical name instead
    # of leaking the hash slot into user-facing output.
    physical_to_logical: dict[str, str] = {}
    try:
        lockfile_path = get_lockfile_path(apm_dir)
        if lockfile_path.exists():
            lockfile = LockFile.read(lockfile_path)
            for dep in lockfile.dependencies.values():
                # Local deps: key on the logical lockfile identity
                # (``repo_url``) and map the physical install slot back to it.
                if dep.source == "local":
                    install_path = dep.to_dependency_ref().get_install_path(apm_modules_path)
                    physical_key = install_path.relative_to(apm_modules_path).as_posix()
                    dep_key = dep.repo_url
                    physical_to_logical[physical_key] = dep_key
                else:
                    dep_key = dep.get_canonical_dependency_string()
                if dep_key and dep_key not in declared_sources:
                    declared_sources[dep_key] = _deps_list_source_label(
                        dep.host,
                        lockfile_source=dep.source,
                    )
                if getattr(dep, "is_insecure", False):
                    insecure_lock_deps[dep_key] = dep
    except Exception:
        pass  # Continue without lockfile if it can't be read

    # Scan for installed packages in org-namespaced structure
    # Walks the tree to find directories containing apm.yml or SKILL.md,
    # handling GitHub (2-level), ADO (3-level), and subdirectory (4+ level) packages.
    # First pass: collect valid candidate paths for ancestor-aware orphan check.
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

        # Nested manifests are deployment artifacts owned by the parent package.
        if _is_nested_under_package(candidate, apm_modules_path):
            continue
        scanned_candidates.append((candidate, "/".join(rel_parts), has_apm_yml, has_skill_md))

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

    installed_packages = []
    orphaned_packages = []
    for candidate, org_repo_name, has_apm_yml, _has_skill_md in scanned_candidates:
        # Local transitive deps are scanned at their physical hash slot
        # (``_local/<hash>/pkg``); report and orphan-check against the
        # logical lockfile key (``_local/pkg``) instead of leaking the slot.
        # A genuinely orphaned slot has no lockfile entry, so it stays keyed by
        # its raw physical identity for correct detection. Only confirmed
        # orphans have their physical hash redacted; declared remote identities
        # that happen to match the slot shape remain unchanged.
        logical_name = physical_to_logical.get(org_repo_name, org_repo_name)
        is_orphaned = logical_name not in declared_with_ancestors
        display_name = _logical_local_display(logical_name) if is_orphaned else logical_name
        try:
            version = "unknown"
            if has_apm_yml:
                package = APMPackage.from_apm_yml(candidate / APM_YML_FILENAME)
                version = package.version or "unknown"
            primitives = _count_primitives(candidate)

            if is_orphaned:
                orphaned_packages.append(display_name)

            locked_dep = insecure_lock_deps.get(logical_name)
            installed_packages.append(
                {
                    "name": display_name,
                    "version": version,
                    "source": "orphaned"
                    if is_orphaned
                    else declared_sources.get(logical_name, "github"),
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
        except Exception:
            # The raised error (e.g. malformed-YAML ValueError) embeds the
            # absolute apm.yml path; never forward it. Emit a stable, actionable
            # public warning keyed on the hash-free display name.
            logger.warning(
                f"Failed to inspect package {display_name}. Check its package files and rerun."
            )

    if insecure_only:
        installed_packages = [pkg for pkg in installed_packages if pkg["is_insecure"]]

    return installed_packages, sorted(orphaned_packages)


@click.group(help="Manage APM package dependencies")
def deps():
    """APM dependency management commands."""
    pass


deps.add_command(_why_cmd)


@deps.command(name="list", help="List installed APM dependencies and their primitives")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="List user-scope dependencies (~/.apm/) instead of project",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show both project and user-scope dependencies",
)
@click.option(
    "--insecure",
    "insecure_only",
    is_flag=True,
    default=False,
    help="Show only installed dependencies locked to http:// sources",
)
def list_packages(global_, show_all, insecure_only):
    """Show all installed APM dependencies with context files and agent workflows."""
    logger = CommandLogger("deps-list")

    try:
        # Import Rich components with fallback
        import shutil

        from rich.console import Console

        term_width = shutil.get_terminal_size((120, 24)).columns
        console = Console(width=max(120, term_width))
        has_rich = True
    except ImportError:
        has_rich = False
        console = None

    try:
        from ...core.scope import InstallScope, get_apm_dir

        if show_all:
            # Show both scopes
            _show_scope_deps(
                "Project",
                get_apm_dir(InstallScope.PROJECT),
                logger,
                console,
                has_rich,
                insecure_only=insecure_only,
            )
            if console and has_rich:
                console.print()  # spacing between tables
            _show_scope_deps(
                "Global",
                get_apm_dir(InstallScope.USER),
                logger,
                console,
                has_rich,
                insecure_only=insecure_only,
            )
        elif global_:
            _show_scope_deps(
                "Global",
                get_apm_dir(InstallScope.USER),
                logger,
                console,
                has_rich,
                insecure_only=insecure_only,
            )
        else:
            _show_scope_deps(
                "Project",
                get_apm_dir(InstallScope.PROJECT),
                logger,
                console,
                has_rich,
                insecure_only=insecure_only,
            )
    except Exception as e:
        logger.error(f"Error listing dependencies: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Data resolution — deps tree
# ---------------------------------------------------------------------------


def _build_dep_tree(apm_dir):
    """Build dependency tree data from lockfile or directory scan.

    Returns a dict describing the tree structure::

        {
            'project_name': str,
            'apm_modules_path': Path,
            'source': 'lockfile' | 'directory',
            'direct': [dep, ...],           # lockfile mode only
            'children_map': {url: [dep]},   # lockfile mode only
            'scanned_packages': [{...}],    # directory fallback only
            'has_modules': bool,
        }
    """
    apm_modules_path = apm_dir / APM_MODULES_DIR

    # Load project info
    project_name = "my-project"
    try:
        apm_yml_path = apm_dir / APM_YML_FILENAME
        if apm_yml_path.exists():
            root_package = APMPackage.from_apm_yml(apm_yml_path)
            project_name = root_package.name
    except Exception:
        pass

    result = {
        "project_name": project_name,
        "apm_modules_path": apm_modules_path,
        "source": "directory",
        "direct": [],
        "children_map": {},
        "unresolved": [],
        "scanned_packages": [],
        "has_modules": apm_modules_path.exists(),
    }

    # Try to load lockfile for accurate tree with depth/parent info
    try:
        from ...deps.lockfile import LockFile, get_lockfile_path

        lockfile_path = get_lockfile_path(apm_dir)
        if lockfile_path.exists():
            lockfile = LockFile.read(lockfile_path)
            if lockfile:
                lockfile_deps = lockfile.get_package_dependencies()
                if lockfile_deps:
                    result["source"] = "lockfile"
                    result["direct"] = [d for d in lockfile_deps if d.depth <= 1]
                    transitive = [d for d in lockfile_deps if d.depth > 1]
                    children_map: dict[str, list[LockedDependency]] = {}
                    unresolved = []
                    parents_by_unique_key: dict[tuple[int, str], list[LockedDependency]] = {}
                    parents_by_repo_url: dict[tuple[int, str], list[LockedDependency]] = {}
                    for candidate in lockfile_deps:
                        parents_by_unique_key.setdefault(
                            (candidate.depth, candidate.get_unique_key()), []
                        ).append(candidate)
                        parents_by_repo_url.setdefault(
                            (candidate.depth, candidate.repo_url), []
                        ).append(candidate)
                    for dep in transitive:
                        parent_key = dep.resolved_by or ""
                        parent_depth = dep.depth - 1
                        parent_candidates = parents_by_unique_key.get(
                            (parent_depth, parent_key), []
                        )
                        if not parent_candidates:
                            parent_candidates = parents_by_repo_url.get(
                                (parent_depth, parent_key), []
                            )
                        if len(parent_candidates) == 1:
                            parent_key = parent_candidates[0].get_unique_key()
                        else:
                            unresolved.append(dep)
                            continue
                        if parent_key not in children_map:
                            children_map[parent_key] = []
                        children_map[parent_key].append(dep)
                    result["children_map"] = children_map
                    result["unresolved"] = unresolved
                    return result
    except Exception:
        pass

    # Fallback: scan apm_modules directory (no lockfile)
    if not apm_modules_path.exists():
        return result

    scanned = []
    for candidate in sorted(apm_modules_path.rglob("*")):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        has_apm = (candidate / APM_YML_FILENAME).exists()
        has_skill = (candidate / SKILL_MD_FILENAME).exists()
        if not has_apm and not has_skill:
            continue
        rel_parts = candidate.relative_to(apm_modules_path).parts
        if len(rel_parts) < 2:
            continue
        if ".apm" in rel_parts:
            continue
        if _is_nested_under_package(candidate, apm_modules_path):
            continue
        info = _get_package_display_info(candidate)
        primitives = _count_primitives(candidate)
        scanned.append(
            {
                "display_name": info["display_name"],
                "primitives": primitives,
            }
        )
    result["scanned_packages"] = scanned
    return result


@deps.command(help="Show the full dependency tree")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Show user-scope dependency tree (~/.apm/)",
)
def tree(global_):
    """Display dependencies in hierarchical tree format using lockfile."""
    logger = CommandLogger("deps-tree")

    try:
        # Import Rich components with fallback
        from rich.console import Console
        from rich.tree import Tree

        console = Console()
        has_rich = True
    except ImportError:
        has_rich = False
        console = None

    try:
        from ...core.scope import InstallScope, get_apm_dir

        scope = InstallScope.USER if global_ else InstallScope.PROJECT
        apm_dir = get_apm_dir(scope)

        tree_data = _build_dep_tree(apm_dir)
        project_name = tree_data["project_name"]
        apm_modules_path = tree_data["apm_modules_path"]

        if tree_data["source"] == "lockfile":
            direct = tree_data["direct"]
            children_map = tree_data["children_map"]
            unresolved = tree_data.get("unresolved", [])

            if has_rich:
                root_tree = Tree(f"[bold cyan]{project_name}[/bold cyan] (local)")
                if not direct:
                    root_tree.add("[dim]No dependencies installed[/dim]")
                else:
                    for dep in direct:
                        display = _dep_display_name(dep)
                        install_key = dep.get_unique_key()
                        install_path = apm_modules_path / install_key
                        branch = root_tree.add(f"[green]{display}[/green]")
                        if install_path.exists():
                            prim_summary = _format_primitive_counts(_count_primitives(install_path))
                            if prim_summary:
                                branch.add(f"[dim]{prim_summary}[/dim]")
                        _add_tree_children(branch, dep.get_unique_key(), children_map)
                    for dep in unresolved:
                        display = _dep_display_name(dep)
                        branch = root_tree.add(
                            f"[yellow]{display} (could not place in tree; "
                            "run apm install to resolve)[/yellow]"
                        )
                        _add_tree_children(branch, dep.get_unique_key(), children_map)
                console.print(root_tree)
            else:
                click.echo(f"{project_name} (local)")
                if not direct:
                    click.echo("+-- No dependencies installed")
                else:
                    for i, dep in enumerate(direct):
                        is_last = i == len(direct) - 1 and not unresolved
                        prefix = "+-- " if is_last else "|-- "
                        display = _dep_display_name(dep)
                        click.echo(f"{prefix}{display}")
                        sub_prefix = "    " if is_last else "|   "
                        _echo_tree_children(dep.get_unique_key(), children_map, sub_prefix)
                    for i, dep in enumerate(unresolved):
                        is_last = i == len(unresolved) - 1
                        prefix = "+-- " if is_last else "|-- "
                        click.echo(
                            f"{prefix}{_dep_display_name(dep)} "
                            "(could not place in tree; "
                            "run apm install to resolve)"
                        )
                        sub_prefix = "    " if is_last else "|   "
                        _echo_tree_children(dep.get_unique_key(), children_map, sub_prefix)
        # Fallback: scan apm_modules directory (no lockfile)
        elif has_rich:
            root_tree = Tree(f"[bold cyan]{project_name}[/bold cyan] (local)")
            if not tree_data["has_modules"]:
                root_tree.add("[dim]No dependencies installed[/dim]")
            else:
                for pkg in tree_data["scanned_packages"]:
                    branch = root_tree.add(f"[green]{pkg['display_name']}[/green]")
                    prim_summary = _format_primitive_counts(pkg["primitives"])
                    if prim_summary:
                        branch.add(f"[dim]{prim_summary}[/dim]")
            console.print(root_tree)
        else:
            click.echo(f"{project_name} (local)")
            if not tree_data["has_modules"]:
                click.echo("+-- No dependencies installed")

    except Exception as e:
        logger.error(f"Error showing dependency tree: {e}")
        sys.exit(1)


@deps.command(help="Remove all APM dependencies")
@click.option(
    "--dry-run", is_flag=True, default=False, help="Show what would be removed without removing"
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
def clean(dry_run: bool, yes: bool):
    """Remove entire apm_modules/ directory."""
    logger = CommandLogger("deps-clean")

    project_root = Path(".")
    apm_modules_path = project_root / APM_MODULES_DIR

    if not apm_modules_path.exists():
        logger.progress("No apm_modules/ directory found - already clean")
        return

    # Count actual installed packages (not just top-level dirs like org namespaces or _local)
    from ._utils import _scan_installed_packages

    packages = _scan_installed_packages(apm_modules_path)
    package_count = len(packages)

    if dry_run:
        logger.progress(f"Dry run: would remove apm_modules/ ({package_count} package(s))")
        for pkg in sorted(packages):
            logger.progress(f"  - {pkg}")
        return

    logger.warning(
        f"This will remove the entire apm_modules/ directory ({package_count} package(s))"
    )

    # Confirmation prompt (skip if --yes provided)
    if not yes:
        try:
            from rich.prompt import Confirm

            confirm = Confirm.ask("Continue?")
        except ImportError:
            confirm = click.confirm("Continue?")

        if not confirm:
            logger.progress("Operation cancelled")
            return

    try:
        shutil.rmtree(apm_modules_path)
        logger.success("Successfully removed apm_modules/ directory")
    except Exception as e:
        logger.error(f"Error removing apm_modules/: {e}")
        sys.exit(1)


@deps.command(
    help="DEPRECATED: use 'apm update' instead (strict superset). Update APM dependencies to latest refs"
)
@click.argument("packages", nargs=-1)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed update information")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite locally-authored files on collision",
)
@click.option(
    "--target",
    "-t",
    type=TargetParamType(),
    default=None,
    help="Target platform (comma-separated). Values: copilot, claude, cursor, opencode, codex, gemini, antigravity, windsurf, kiro, agent-skills, all. 'agent-skills' deploys to .agents/skills/ (cross-client). 'antigravity' (alias 'agy') deploys to .agents/ and is explicit-only -- not part of 'all'. 'all' = copilot+claude+cursor+opencode+codex+gemini+windsurf+kiro (excludes agent-skills and antigravity); combine with 'agent-skills' or 'antigravity' to add them. 'copilot-cowork' is also accepted when the copilot-cowork experimental flag is enabled (run 'apm experimental enable copilot-cowork').",
)
@click.option(
    "--parallel-downloads",
    type=int,
    default=4,
    show_default=True,
    help="Max concurrent package downloads (0 to disable parallelism)",
)
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Update user-scope dependencies (~/.apm/)",
)
@click.option(
    "--legacy-skill-paths",
    "legacy_skill_paths",
    is_flag=True,
    default=False,
    help=(
        "Deploy skill files to per-client paths (e.g. .cursor/skills/) instead of "
        "the shared .agents/skills/ directory. Compatibility flag for projects that "
        "need per-client skill layouts."
    ),
)
def update(packages, verbose, force, target, parallel_downloads, global_, legacy_skill_paths):
    """Update APM dependencies to latest git refs.

    Re-resolves git references (branches/tags) to their current SHAs,
    downloads updated content, re-integrates primitives, and regenerates
    the lockfile.

    \b
    Examples:
        apm deps update                    # Update all packages
        apm deps update org/repo           # Update one package
        apm deps update org/a org/b        # Update specific packages
        apm deps update --verbose          # Show detailed progress
    """
    _update_impl(packages, verbose, force, target, parallel_downloads, global_, legacy_skill_paths)


@deps.command(
    help="Show detailed package information (alias for 'apm view PACKAGE' for installed packages; prefer 'apm view' in new scripts)"
)
@click.argument("package", required=True)
def info(package: str):
    """Show detailed information about a specific package including context files and workflows."""
    from ..view import display_package_info, resolve_package_path

    logger = CommandLogger("deps-info")

    project_root = Path(".")
    apm_modules_path = project_root / APM_MODULES_DIR

    if not apm_modules_path.exists():
        logger.error("No apm_modules/ directory found")
        logger.progress("Run 'apm install' to install dependencies first")
        sys.exit(1)

    package_path = resolve_package_path(package, apm_modules_path, logger)
    display_package_info(package, package_path, logger, project_root=project_root)
