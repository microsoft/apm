"""Repository configuration for transport-agnostic package resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml


@dataclass(frozen=True)
class RepositoryDefinition:
    """A configured package repository backend."""

    name: str
    type: str
    base: str
    priority: int = 0


DEFAULT_REPOSITORIES: List[RepositoryDefinition] = [
    RepositoryDefinition(name="github", type="git", base="https://github.com", priority=100),
    RepositoryDefinition(name="gitlab", type="git", base="https://gitlab.com", priority=90),
    RepositoryDefinition(name="ghcr", type="oci", base="ghcr.io/apm", priority=80),
]


def repositories_config_path() -> Path:
    """Return the repository config path."""
    return Path.home() / ".apm" / "repositories.yml"


def load_repositories() -> List[RepositoryDefinition]:
    """Load configured repositories, falling back to built-in defaults."""
    path = repositories_config_path()
    if not path.exists():
        return list(DEFAULT_REPOSITORIES)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return list(DEFAULT_REPOSITORIES)

    entries = raw.get("repositories")
    if not isinstance(entries, list):
        return list(DEFAULT_REPOSITORIES)

    repositories: List[RepositoryDefinition] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        type_ = entry.get("type")
        base = entry.get("base")
        priority = entry.get("priority", 0)
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(type_, str) or type_ not in ("git", "oci"):
            continue
        if not isinstance(base, str) or not base.strip():
            continue
        if not isinstance(priority, int):
            priority = 0
        repositories.append(
            RepositoryDefinition(
                name=name.strip(),
                type=type_,
                base=base.strip().rstrip("/"),
                priority=priority,
            )
        )

    if not repositories:
        return list(DEFAULT_REPOSITORIES)

    return sorted(repositories, key=lambda repo: repo.priority, reverse=True)
