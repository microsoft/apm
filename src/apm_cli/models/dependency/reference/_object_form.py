"""Object-form (dict-style) dependency entry parsing.

Module-level helpers that are attached as class/static methods on
``DependencyReference`` in ``core.py``.  Extracted from ``parsing.py`` to
keep that module within the project's 500-line file budget.

The functions follow the same pattern as the rest of the ``reference/``
package: they are plain module-level functions decorated with
``@staticmethod`` / ``@classmethod`` so that Python's descriptor protocol
attaches them correctly when ``core.py`` does::

    DependencyReference.<name> = <name>

None of these helpers import ``DependencyReference`` directly — ``cls`` is
supplied at call-time by the descriptor protocol.
"""

import re

from ....utils.path_security import validate_path_segments


@staticmethod
def _normalize_parent_repo_decl_path(raw: str) -> str:
    """Normalize ``path`` for ``git: parent`` to a single canonical relative path."""
    s = raw.strip().replace("\\", "/").strip()
    s = s.strip("/")
    segments = [seg for seg in s.split("/") if seg]
    if not segments:
        raise ValueError("'path' field must be a non-empty string")
    normalized = "/".join(segments)
    validate_path_segments(normalized, context="path")
    return normalized


@classmethod
def _parse_object_local_path(cls, local_raw) -> "DependencyReference":
    """Validate and parse a path-only (no git) object-style entry."""
    if not isinstance(local_raw, str) or not local_raw.strip():
        raise ValueError("'path' field must be a non-empty string")
    local = local_raw.strip()
    if not cls.is_local_path(local):
        raise ValueError(
            "Object-style dependency must have a 'git' field, "
            "or 'path' must be a local filesystem path "
            "(starting with './', '../', '/', or '~')"
        )
    return cls.parse(local)


@classmethod
def _parse_object_parent(cls, entry: dict) -> "DependencyReference":
    """Parse a ``git: parent`` monorepo inheritance entry."""
    path_raw = entry.get("path")
    if path_raw is None:
        raise ValueError("Object-style dependency with git: 'parent' requires a 'path' field")
    if not isinstance(path_raw, str) or not path_raw.strip():
        raise ValueError("'path' field must be a non-empty string")
    normalized_path = cls._normalize_parent_repo_decl_path(path_raw)

    ref_override = entry.get("ref")
    alias_override = entry.get("alias")
    reference: str | None = None
    if ref_override is not None:
        if not isinstance(ref_override, str) or not ref_override.strip():
            raise ValueError("'ref' field must be a non-empty string")
        reference = ref_override.strip()

    alias_val: str | None = None
    if alias_override is not None:
        if not isinstance(alias_override, str) or not alias_override.strip():
            raise ValueError("'alias' field must be a non-empty string")
        alias_override = alias_override.strip()
        if not re.match(r"^[a-zA-Z0-9._-]+$", alias_override):
            raise ValueError(
                f"Invalid alias: {alias_override}. Aliases can only contain letters, numbers, dots, underscores, and hyphens"
            )
        alias_val = alias_override

    return cls(
        repo_url="_parent",
        host=None,
        reference=reference,
        alias=alias_val,
        virtual_path=normalized_path,
        is_virtual=True,
        is_parent_repo_inheritance=True,
    )


def _validate_skills_override(skills_raw) -> list[str]:
    """Validate and return sorted list of skill names from skills: override.

    Args:
        skills_raw: Raw value from entry.get("skills")

    Returns:
        Sorted list of validated, deduplicated skill names

    Raises:
        ValueError: If skills_raw is invalid format
    """
    if not isinstance(skills_raw, list):
        raise ValueError("'skills' field must be a list of skill names")
    if len(skills_raw) == 0:
        raise ValueError(
            "skills: must contain at least one name; "
            "remove the field to install all skills in the bundle."
        )
    seen: set = set()
    validated: list = []
    for name in skills_raw:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each entry in 'skills' must be a non-empty string")
        name = name.strip()
        validate_path_segments(name, context="skills/<name>")
        if name not in seen:
            seen.add(name)
            validated.append(name)
    return sorted(validated)


@classmethod
def _parse_object_git_overrides(cls, dep, entry: dict, sub_path: str | None) -> None:
    """Apply ref, alias, sub-path, and skills overrides from an object entry (in-place)."""
    ref_override = entry.get("ref")
    if ref_override is not None:
        if not isinstance(ref_override, str) or not ref_override.strip():
            raise ValueError("'ref' field must be a non-empty string")
        dep.reference = ref_override.strip()

    alias_override = entry.get("alias")
    if alias_override is not None:
        if not isinstance(alias_override, str) or not alias_override.strip():
            raise ValueError("'alias' field must be a non-empty string")
        alias_override = alias_override.strip()
        if not re.match(r"^[a-zA-Z0-9._-]+$", alias_override):
            raise ValueError(
                f"Invalid alias: {alias_override}. Aliases can only contain letters, numbers, dots, underscores, and hyphens"
            )
        dep.alias = alias_override

    if sub_path:
        dep.virtual_path = sub_path
        dep.is_virtual = True

    # Parse skills: field (SKILL_BUNDLE subset selection)
    skills_raw = entry.get("skills")
    if skills_raw is not None:
        dep.skill_subset = _validate_skills_override(skills_raw)


@classmethod
def parse_from_dict(cls, entry: dict) -> "DependencyReference":
    """Parse an object-style dependency entry from apm.yml.

    Supports the Cargo-inspired object format:

        - git: https://gitlab.com/acme/coding-standards.git
          path: instructions/security
          ref: v2.0

        - git: git@bitbucket.org:team/rules.git
          path: prompts/review.prompt.md

    Also supports local path entries:

        - path: ./packages/my-shared-skills

    Args:
        entry: Dictionary with 'git' or 'path' (required), plus optional fields

    Returns:
        DependencyReference: Parsed dependency reference

    Raises:
        ValueError: If the entry is missing required fields or has invalid format
    """
    # Support dict-form local path: { path: ./local/dir }
    if "path" in entry and "git" not in entry:
        return cls._parse_object_local_path(entry["path"])

    if "git" not in entry:
        raise ValueError("Object-style dependency must have a 'git' or 'path' field")

    git_url = entry["git"]
    if not isinstance(git_url, str) or not git_url.strip():
        raise ValueError("'git' field must be a non-empty string")

    # Monorepo parent inheritance (literal ``git: parent`` only; resolver expands)
    if git_url == "parent":
        return cls._parse_object_parent(entry)

    # Regular git URL with optional path/ref/alias/skills overrides
    allow_insecure = entry.get("allow_insecure", False)
    if not isinstance(allow_insecure, bool):
        raise ValueError("'allow_insecure' field must be a boolean")

    sub_path = entry.get("path")
    if sub_path is not None:
        if not isinstance(sub_path, str) or not sub_path.strip():
            raise ValueError("'path' field must be a non-empty string")
        sub_path = sub_path.strip().strip("/")
        sub_path = sub_path.replace("\\", "/").strip().strip("/")
        validate_path_segments(sub_path, context="path")

    dep = cls.parse(git_url)
    dep.allow_insecure = allow_insecure
    cls._parse_object_git_overrides(dep, entry, sub_path)
    return dep
