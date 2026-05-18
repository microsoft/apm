"""Dependency tree building and rendering helpers.

Extracted from sections/__init__ to keep that module under 400 lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import click

from ....constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ....models.apm_package import APMPackage
from .._utils import (
    _add_tree_children,
    _count_primitives,
    _dep_display_name,
    _format_primitive_counts,
    _get_package_display_info,
    _is_nested_under_package,
)


@dataclass(frozen=True, slots=True)
class _TreeRenderContext:
    """Rendering context for dependency tree output."""

    logger: object
    console: object
    has_rich: bool


def _build_tree_result(project_name: str, apm_modules_path: Path) -> dict:
    return {
        "project_name": project_name,
        "apm_modules_path": apm_modules_path,
        "source": "directory",
        "direct": [],
        "children_map": {},
        "scanned_packages": [],
        "has_modules": apm_modules_path.exists(),
    }


def _load_project_name(apm_dir: Path) -> str:
    try:
        apm_yml_path = apm_dir / APM_YML_FILENAME
        if apm_yml_path.exists():
            return APMPackage.from_apm_yml(apm_yml_path).name
    except Exception:
        pass
    return "my-project"


def _load_lockfile_tree_data(apm_dir: Path, result: dict) -> dict | None:
    try:
        from ....deps.lockfile import LockFile, get_lockfile_path

        lockfile_path = get_lockfile_path(apm_dir)
        if not lockfile_path.exists():
            return None
        lockfile = LockFile.read(lockfile_path)
        if not lockfile:
            return None
        lockfile_deps = lockfile.get_package_dependencies()
        if not lockfile_deps:
            return None
        result["source"] = "lockfile"
        result["direct"] = [dep for dep in lockfile_deps if dep.depth <= 1]
        children_map: dict[str, list] = {}
        for dep in [item for item in lockfile_deps if item.depth > 1]:
            children_map.setdefault(dep.resolved_by or "", []).append(dep)
        result["children_map"] = children_map
        return result
    except Exception:
        return None


def _should_include_scanned_candidate(candidate: Path, apm_modules_path: Path) -> bool:
    if not candidate.is_dir() or candidate.name.startswith("."):
        return False
    has_apm = (candidate / APM_YML_FILENAME).exists()
    has_skill = (candidate / SKILL_MD_FILENAME).exists()
    if not has_apm and not has_skill:
        return False
    rel_parts = candidate.relative_to(apm_modules_path).parts
    if len(rel_parts) < 2 or ".apm" in rel_parts:
        return False
    return not (has_skill and not has_apm and _is_nested_under_package(candidate, apm_modules_path))


def _scan_tree_packages(apm_modules_path: Path) -> list[dict]:
    scanned = []
    for candidate in sorted(apm_modules_path.rglob("*")):
        if not _should_include_scanned_candidate(candidate, apm_modules_path):
            continue
        info = _get_package_display_info(candidate)
        scanned.append(
            {
                "display_name": info["display_name"],
                "primitives": _count_primitives(candidate),
            }
        )
    return scanned


def _build_dep_tree(apm_dir: Path) -> dict:
    """Build dependency tree data from lockfile or directory scan."""
    apm_modules_path = apm_dir / APM_MODULES_DIR
    result = _build_tree_result(_load_project_name(apm_dir), apm_modules_path)
    lockfile_result = _load_lockfile_tree_data(apm_dir, result)
    if lockfile_result is not None:
        return lockfile_result
    if apm_modules_path.exists():
        result["scanned_packages"] = _scan_tree_packages(apm_modules_path)
    return result


def _render_rich_lockfile_tree(tree_data: dict, ctx: _TreeRenderContext) -> None:
    from rich.tree import Tree

    root_tree = Tree(f"[bold cyan]{tree_data['project_name']}[/bold cyan] (local)")
    direct = tree_data["direct"]
    if not direct:
        root_tree.add("[dim]No dependencies installed[/dim]")
        ctx.console.print(root_tree)
        return
    apm_modules_path = tree_data["apm_modules_path"]
    children_map = tree_data["children_map"]
    for dep in direct:
        branch = root_tree.add(f"[green]{_dep_display_name(dep)}[/green]")
        install_path = apm_modules_path / dep.get_unique_key()
        if install_path.exists():
            prim_summary = _format_primitive_counts(_count_primitives(install_path))
            if prim_summary:
                branch.add(f"[dim]{prim_summary}[/dim]")
        _add_tree_children(branch, dep.repo_url, children_map, ctx.has_rich)
    ctx.console.print(root_tree)


def _render_plain_lockfile_tree(tree_data: dict) -> None:
    click.echo(f"{tree_data['project_name']} (local)")
    direct = tree_data["direct"]
    if not direct:
        click.echo("+-- No dependencies installed")
        return
    children_map = tree_data["children_map"]
    for index, dep in enumerate(direct):
        is_last = index == len(direct) - 1
        click.echo(f"{'+-- ' if is_last else '|-- '}{_dep_display_name(dep)}")
        sub_prefix = "    " if is_last else "|   "
        kids = children_map.get(dep.repo_url, [])
        for child_index, child in enumerate(kids):
            child_prefix = "+-- " if child_index == len(kids) - 1 else "|-- "
            click.echo(f"{sub_prefix}{child_prefix}{_dep_display_name(child)}")


def _render_rich_scanned_tree(tree_data: dict, ctx: _TreeRenderContext) -> None:
    from rich.tree import Tree

    root_tree = Tree(f"[bold cyan]{tree_data['project_name']}[/bold cyan] (local)")
    if not tree_data["has_modules"]:
        root_tree.add("[dim]No dependencies installed[/dim]")
    else:
        for pkg in tree_data["scanned_packages"]:
            branch = root_tree.add(f"[green]{pkg['display_name']}[/green]")
            prim_summary = _format_primitive_counts(pkg["primitives"])
            if prim_summary:
                branch.add(f"[dim]{prim_summary}[/dim]")
    ctx.console.print(root_tree)


def _render_plain_scanned_tree(tree_data: dict) -> None:
    click.echo(f"{tree_data['project_name']} (local)")
    if not tree_data["has_modules"]:
        click.echo("+-- No dependencies installed")


def _build_tree_render_context(logger: object) -> _TreeRenderContext:
    try:
        from rich.console import Console

        return _TreeRenderContext(logger=logger, console=Console(), has_rich=True)
    except ImportError:
        return _TreeRenderContext(logger=logger, console=None, has_rich=False)


def _render_tree_output(tree_data: dict, ctx: _TreeRenderContext) -> None:
    if tree_data["source"] == "lockfile":
        if ctx.has_rich:
            _render_rich_lockfile_tree(tree_data, ctx)
            return
        _render_plain_lockfile_tree(tree_data)
        return
    if ctx.has_rich:
        _render_rich_scanned_tree(tree_data, ctx)
        return
    _render_plain_scanned_tree(tree_data)
