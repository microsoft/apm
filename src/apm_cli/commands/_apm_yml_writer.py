"""Write-back helper for persisting skill subset selection in apm.yml.

Single helper ``set_skill_subset_for_entry`` is the one source of truth
for promoting entries to dict form and setting/clearing the ``skills:``
field.  Keeps write-back logic isolated and unit-testable.
"""

from pathlib import Path
from typing import List, Optional  # noqa: F401, UP035

from ..models.dependency.reference import DependencyReference
from ..utils.yaml_io import dump_yaml, load_yaml


def set_skill_subset_for_entry(
    manifest_path: Path,
    repo_url: str,
    subset: list[str] | None,
) -> bool:
    """Promote entry to dict form and set/clear skills: field.

    subset=None or empty list -> remove skills: from entry (reset to all).
    subset=[...] -> set skills: to sorted+deduped list.

    Returns True if file was modified.
    """
    data = load_yaml(manifest_path) or {}
    deps_section = data.get("dependencies", {})
    apm_deps = deps_section.get("apm", [])
    if not apm_deps:
        return False

    modified = False
    new_deps = []

    for entry in apm_deps:
        if _entry_matches(entry, repo_url):
            entry = _apply_subset(entry, subset)
            modified = True
        new_deps.append(entry)

    if not modified:
        return False

    deps_section["apm"] = new_deps
    data["dependencies"] = deps_section
    dump_yaml(data, manifest_path)
    return True


def _entry_matches(entry, repo_url: str) -> bool:
    """Check if an apm.yml entry matches the given repo_url."""
    try:
        if isinstance(entry, str):
            ref = DependencyReference.parse(entry)
        elif isinstance(entry, dict):
            ref = DependencyReference.parse_from_dict(entry)
        else:
            return False
        return ref.repo_url == repo_url
    except (ValueError, TypeError, AttributeError, KeyError):
        return False


def _apply_subset(entry, subset: list[str] | None):
    """Apply skill subset to an entry, promoting to dict form if needed."""
    # Parse current entry to get canonical info
    if isinstance(entry, str):
        ref = DependencyReference.parse(entry)
    elif isinstance(entry, dict):
        ref = DependencyReference.parse_from_dict(entry)
    else:
        return entry

    # Determine if we should set or clear
    if subset:
        ref.skill_subset = sorted(set(subset))
    else:
        ref.skill_subset = None

    return ref.to_apm_yml_entry()
