"""Write-back helpers for persisting dependency subset selection in apm.yml.

The helpers promote entries to dict form and set/clear the ``skills:`` or
``targets:`` fields.  Keeping write-back logic isolated makes it unit-testable.
"""

from pathlib import Path

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
    return _set_subset_for_entry(manifest_path, repo_url, "skills", subset)


def set_target_subset_for_entry(
    manifest_path: Path,
    repo_url: str,
    subset: list[str] | None,
) -> bool:
    """Promote entry to dict form and set/clear targets: field.

    subset=None or empty list -> remove targets: from entry (reset to all).
    subset=[...] -> set targets: to sorted+deduped lowercase list.

    Returns True if file was modified.
    """
    return _set_subset_for_entry(manifest_path, repo_url, "targets", subset)


def _set_subset_for_entry(
    manifest_path: Path,
    repo_url: str,
    field: str,
    subset: list[str] | None,
) -> bool:
    """Promote a matching entry to dict form and set/clear one subset field."""
    data = load_yaml(manifest_path) or {}
    deps_section = data.get("dependencies")
    if deps_section is None:
        deps_section = {}
    if not isinstance(deps_section, dict):
        raise ValueError(
            f"Invalid 'dependencies' in {manifest_path}: expected a mapping "
            f"with 'apm:' key, got {type(deps_section).__name__}. "
            "Use the structured format:\n"
            "  dependencies:\n"
            "    apm:\n"
            "      - owner/repo"
        )
    apm_deps = deps_section.get("apm", [])
    if not apm_deps:
        return False

    modified = False
    new_deps = []

    for entry in apm_deps:
        if _entry_matches(entry, repo_url):
            entry = _apply_subset(entry, field, subset)
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


def _apply_subset(entry, field: str, subset: list[str] | None):
    """Apply a dependency subset field, promoting to dict form if needed."""
    # Parse current entry to get canonical info
    if isinstance(entry, str):
        ref = DependencyReference.parse(entry)
    elif isinstance(entry, dict):
        ref = DependencyReference.parse_from_dict(entry)
    else:
        return entry

    # Determine if we should set or clear
    if field == "skills":
        ref.skill_subset = sorted(set(subset)) if subset else None
    elif field == "targets":
        ref.target_subset = sorted({name.strip().lower() for name in subset}) if subset else None
    else:
        raise ValueError(f"Unsupported dependency subset field: {field}")

    return ref.to_apm_yml_entry()
