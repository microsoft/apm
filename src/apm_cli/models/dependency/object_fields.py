"""Shared helpers for object-form dependency fields."""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

from ...core.remediation import DEPENDENCY_ALIAS_INVALID
from ...utils.path_security import PathTraversalError, validate_path_segments
from ..extension_keys import is_extension_key
from .subsets import parse_skill_subset, parse_target_subset

_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
_REMOTE_GIT_DEPENDENCY_FIELDS = frozenset(
    {
        "alias",
        "allow_insecure",
        "git",
        "native",
        "path",
        "ref",
        "skills",
        "targets",
        "type",
    }
)
_PARENT_GIT_DEPENDENCY_FIELDS = frozenset({"alias", "git", "native", "path", "ref"})


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
    try:
        validate_path_segments(alias, context="dependency alias")
    except PathTraversalError:
        raise ValueError(
            f"Invalid dependency alias {alias!r}: '.' and '..' are reserved directory names. "
            + DEPENDENCY_ALIAS_INVALID.bind(
                fix="set alias to a name such as 'my-skill.v2' in apm.yml"
            ).sentence()
        ) from None
    return alias


def reject_unknown_fields(
    entry: dict,
    allowed: Collection[str],
    dependency_type: str,
) -> None:
    """Reject inert typos in object-form dependency declarations.

    Vendor-extension keys (req-ext-001) are ignored, never interpreted (#4260).
    """
    unknown = sorted(key for key in set(entry) - set(allowed) if not is_extension_key(key))
    if unknown:
        fields = ", ".join(repr(field) for field in unknown)
        raise ValueError(f"Unsupported field(s) for {dependency_type} dependency: {fields}")


def reject_unknown_git_fields(entry: dict, *, parent: bool) -> None:
    """Reject fields outside the canonical remote or parent Git vocabulary."""
    allowed = _PARENT_GIT_DEPENDENCY_FIELDS if parent else _REMOTE_GIT_DEPENDENCY_FIELDS
    dependency_type = "parent git" if parent else "git"
    unknown = set(entry) - set(allowed)
    if "version" in unknown:
        raise ValueError(
            "Git dependency field 'version' is unsupported; use 'ref' for a branch, tag, or commit"
        )
    reject_unknown_fields(entry, allowed, dependency_type)


def parse_native_route(entry: dict) -> bool | None:
    """Return a dependency entry's ``native:`` route choice (#4995); only booleans are valid.

    An absent key is ``None``; a present null is rejected like any non-boolean.
    """
    if "native" not in entry:
        return None
    raw = entry["native"]
    if not isinstance(raw, bool):
        raise ValueError(
            f"'native' field must be true or false, got {raw!r}; "
            "use native: false to deploy this dependency loose"
        )
    return raw


def apply_optional_dependency_fields(dep: Any, entry: dict) -> None:
    """Apply common alias, skills, targets and native fields to a dependency."""
    dep.native = parse_native_route(entry)
    alias = parse_alias_override(entry.get("alias"))
    if alias is not None:
        dep.alias = alias
    skills_raw = entry.get("skills")
    if skills_raw is not None:
        dep.skill_subset = parse_skill_subset(skills_raw)
    targets_raw = entry.get("targets")  # architecture-authority-exempt: dependency-target-subset
    if targets_raw is not None:
        dep.target_subset = parse_target_subset(targets_raw)


def add_selection_fields(entry: dict, dep: Any) -> None:
    """Write a dependency's skills, targets and native choices into its apm.yml entry."""
    if dep.skill_subset:
        entry["skills"] = sorted(dep.skill_subset)
    if dep.target_subset:
        entry["targets"] = sorted(dep.target_subset)
    if dep.native is not None:
        entry["native"] = dep.native


def local_path_apm_yml_entry(
    local_path: str,
    alias: str | None,
    skill_subset: list[str] | None,
    target_subset: list[str] | None,
    native: bool | None = None,
) -> dict[str, object]:
    """Build the dict form for a local path dependency with optional fields."""
    entry: dict[str, object] = {"path": local_path}
    if native is not None:
        entry["native"] = native
    if alias:
        entry["alias"] = alias
    if skill_subset:
        entry["skills"] = sorted(skill_subset)
    if target_subset:
        entry["targets"] = sorted(target_subset)
    return entry
