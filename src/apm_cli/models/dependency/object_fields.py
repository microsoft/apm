"""Shared helpers for object-form dependency fields."""

from __future__ import annotations

import re
from typing import Any

from .subsets import parse_skill_subset, parse_target_subset

_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


def parse_alias_override(raw: object) -> str | None:
    """Return a validated alias override from an object-form dependency."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("'alias' field must be a non-empty string")
    alias = raw.strip()
    if not _ALIAS_PATTERN.match(alias):
        raise ValueError(
            f"Invalid alias: {alias}. Aliases can only contain "
            "letters, numbers, dots, underscores, and hyphens"
        )
    return alias


def reject_unknown_fields(entry: dict, allowed: set[str], dependency_type: str) -> None:
    """Reject inert typos in object-form dependency declarations."""
    unknown = sorted(set(entry) - allowed)
    if unknown:
        fields = ", ".join(repr(field) for field in unknown)
        raise ValueError(f"Unsupported field(s) for {dependency_type} dependency: {fields}")


def apply_optional_dependency_fields(dep: Any, entry: dict) -> None:
    """Apply common alias, skills, and targets fields to a dependency."""
    alias = parse_alias_override(entry.get("alias"))
    if alias is not None:
        dep.alias = alias
    skills_raw = entry.get("skills")
    if skills_raw is not None:
        dep.skill_subset = parse_skill_subset(skills_raw)
    targets_raw = entry.get("targets")
    if targets_raw is not None:
        dep.target_subset = parse_target_subset(targets_raw)


def local_path_apm_yml_entry(
    local_path: str,
    alias: str | None,
    skill_subset: list[str] | None,
    target_subset: list[str] | None,
) -> dict[str, object]:
    """Build the dict form for a local path dependency with optional fields."""
    entry: dict[str, object] = {"path": local_path}
    if alias:
        entry["alias"] = alias
    if skill_subset:
        entry["skills"] = sorted(skill_subset)
    if target_subset:
        entry["targets"] = sorted(target_subset)
    return entry
