"""Dataclass parameter objects for skill integrator functions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SkillPromoteOpts:
    """Optional arguments for :func:`_promote_sub_skills`."""

    warn: bool = True
    owned_by: dict[str, str] | None = None
    diagnostics: Any = None
    managed_files: Any = None
    force: bool = False
    project_root: Path | None = None
    logger: Any = None
    name_filter: Any = None  # set | None


@dataclass(frozen=True, slots=True)
class SkillOpts:
    """Optional arguments for skill integration functions.

    Used by ``_integrate_native_skill``, ``_integrate_skill_bundle``,
    and ``integrate_package_skill``.
    """

    diagnostics: Any = None
    managed_files: Any = None
    force: bool = False
    logger: Any = None
    targets: Any = None
    skill_subset: Any = None


@dataclass(frozen=True, slots=True)
class SkillCollisionOpts:
    """Optional arguments for native skill collision warnings."""

    current_key: str | None = None
    lockfile_native_owners: dict[str, str] | None = None
    diagnostics: Any = None
    logger: Any = None
