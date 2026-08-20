"""Object-form registry dependency parsing (design §3.2).

Split out of ``reference.py`` to keep that file under the repo's file-length
guardrail. ``parse_registry_object_entry`` takes the ``DependencyReference``
class as a parameter (rather than importing it) so this module has no
dependency on ``reference.py`` and can't create an import cycle.
"""

from __future__ import annotations

import re
from typing import Any

from ...utils.github_host import default_host
from ...utils.path_security import validate_path_segments
from .subsets import parse_skill_subset, parse_target_subset

_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
_ID_SEGMENT_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


def parse_registry_object_entry(dependency_reference_cls: Any, entry: dict) -> Any:
    """Parse the object-form registry entry per §3.2.

    Required keys:
        id:       <owner>/<repo>   # package identity at the registry
        version:  <any-string>      # opaque version string; registry resolves it

    Optional:
        registry: <name>           # routes to named registry; omit to use default
        path:     prompts/foo.md   # virtual sub-path; omit to install the whole package
        alias:    <name>           # same meaning as in other object forms
        skills:   [x, y, z]        # same meaning as in other object forms
        targets:  [x, y, z]        # same meaning as in other object forms
    """
    from ...deps.registry.feature_gate import require_package_registry_enabled

    require_package_registry_enabled("Object-form registry dependencies")

    _registry_raw = entry.get("registry")
    registry_name: str | None = None
    if _registry_raw is not None:
        if not isinstance(_registry_raw, str) or not _registry_raw.strip():
            raise ValueError(
                "Object-form registry entry: 'registry' must be a non-empty "
                "string (the name of an entry in the apm.yml registries: block)"
            )
        registry_name = _registry_raw.strip()

    pkg_id = entry.get("id")
    if not isinstance(pkg_id, str) or not pkg_id.strip():
        raise ValueError(
            "Object-form registry entry: 'id' is required and must be a "
            "non-empty 'owner/repo' string"
        )
    pkg_id = pkg_id.strip()
    if "/" not in pkg_id:
        raise ValueError(f"Object-form registry entry: 'id' must be 'owner/repo', got {pkg_id!r}")

    raw_path = entry.get("path")
    sub_path: str | None = None
    if raw_path is not None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(
                "Object-form registry entry: 'path' must be a non-empty string "
                "when provided (e.g. 'prompts/review.prompt.md')"
            )
        sub_path = raw_path.strip().strip("/").replace("\\", "/").strip("/")
        validate_path_segments(sub_path, context="path")

    version = entry.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Object-form registry entry: 'version' is required")
    version = version.strip()

    alias = entry.get("alias")
    if alias is not None:
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("'alias' field must be a non-empty string")
        alias = alias.strip()
        if not _ALIAS_PATTERN.match(alias):
            raise ValueError(
                f"Invalid alias: {alias}. Aliases can only contain "
                f"letters, numbers, dots, underscores, and hyphens"
            )

    skills_raw = entry.get("skills")
    skill_subset = parse_skill_subset(skills_raw) if skills_raw is not None else None

    targets_raw = entry.get("targets")
    target_subset = parse_target_subset(targets_raw) if targets_raw is not None else None

    # Reject any unknown keys to catch typos early.
    known = {"registry", "id", "path", "version", "alias", "skills", "targets"}
    unknown = set(entry.keys()) - known
    if unknown:
        raise ValueError(
            f"Object-form registry entry has unknown fields: "
            f"{sorted(unknown)}. Known fields: {sorted(known)}"
        )

    owner_segments = pkg_id.split("/")
    validate_path_segments(pkg_id, context="registry id")
    for seg in owner_segments:
        if not _ID_SEGMENT_PATTERN.match(seg):
            raise ValueError(f"Invalid registry id segment: {seg!r} in {pkg_id!r}")

    return dependency_reference_cls(
        repo_url=pkg_id,
        host=default_host(),
        reference=version,
        virtual_path=sub_path,
        is_virtual=sub_path is not None,
        alias=alias,
        source="registry",
        registry_name=registry_name,
        skill_subset=skill_subset,
        target_subset=target_subset,
    )
