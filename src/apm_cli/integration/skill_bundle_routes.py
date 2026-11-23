"""The destinations the skill promotion writes for a package, without writing (#4940).

The audit drift replay serves a package from its render record only when
this mapping, built from the skill integrator's own route, promoted names and
admitted-member walk, equals the record's destinations.
"""

from __future__ import annotations

from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.integration.skill_integrator import SkillIntegrator


def verbatim_destinations(
    integrator: SkillIntegrator, package_info, project_root: Path, targets, source_plan=None
) -> dict[Path, Path] | None:
    """``{target_path: source}`` the sub-skill copy may write for *targets*, else ``None``.

    The same route, skill-root dedup, promoted name and admitted member
    walk the sub-skill and bundle promotion uses. Native skills and
    marketplace plugins are not mapped (``None``). Whether a member is a
    byte copy is proven by its bytes.
    """
    from apm_cli.integration.skill_support import _build_deployable_copy_ignore
    from apm_cli.models.apm_package import PackageType

    info = copy(package_info)
    info.install_path = Path(package_info.install_path).resolve()
    selection = integrator.selected_skill_sources(info)
    if selection.route == "none":
        return {}
    # Bundle and sub-skill routes promote each selected child the same way;
    # native skills and marketplace plugins have their own deploy paths.
    if selection.route not in ("subskills", "bundle") or (
        getattr(info, "package_type", None) is PackageType.MARKETPLACE_PLUGIN
    ):
        return None
    ignore = _build_deployable_copy_ignore(source_plan=source_plan)
    planned: dict[Path, Path] = {}
    seen_roots: set[Path] = set()
    for target in targets:
        if not target.supports("skills"):
            continue
        skills_root = project_root / target.skills_rel_root
        if skills_root.resolve() in seen_roots:
            continue
        seen_roots.add(skills_root.resolve())
        for name in sorted(selection.children):
            child = selection.children[name]
            if not child.is_dir() or not (child / "SKILL.md").exists():
                continue
            destination = skills_root / promoted_skill_name(child.name)
            for member in integrator.admitted_source_members(child, ignore):
                planned[destination / member.relative_to(child)] = member
    return planned


def promoted_skill_name(raw_name: str) -> str:
    """The directory name a promoted sub-skill deploys under."""
    from apm_cli.integration.skill_integrator import normalize_skill_name, validate_skill_name

    is_valid, _ = validate_skill_name(raw_name)
    return raw_name if is_valid else normalize_skill_name(raw_name)
