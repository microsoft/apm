"""Installation scope resolution for APM packages.

Defines where packages are deployed based on scope:

- **project** (default): Deploy to the current working directory.
  Manifest, lockfile, and modules live at the project root.
- **user**: Deploy to user-level directories (``~/.claude/``, etc.).
  Manifest, lockfile, and modules live under ``~/.apm/``.

User-scope support varies by target -- see ``TargetProfile.user_supported``
in ``apm_cli.integration.targets`` for the canonical registry.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import List  # noqa: F401, UP035

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_APM_DIR = ".apm"
"""Directory under ``$HOME`` for user-scope metadata."""


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class InstallScope(Enum):
    """Controls where packages are deployed."""

    PROJECT = "project"
    USER = "user"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def get_deploy_root(scope: InstallScope) -> Path:
    """Return the root used to construct deployment paths.

    For project scope this is ``Path.cwd()``.
    For user scope this is ``Path.home()`` so that integrators produce
    paths like ``~/.claude/commands/``.
    """
    if scope is InstallScope.USER:
        return Path.home()
    return Path.cwd()


def get_apm_dir(scope: InstallScope) -> Path:
    """Return the directory that holds APM metadata (manifest, lockfile, modules).

    * Project scope: ``<cwd>/``
    * User scope: ``~/.apm/``
    """
    if scope is InstallScope.USER:
        return Path.home() / USER_APM_DIR
    return Path.cwd()


def get_modules_dir(scope: InstallScope) -> Path:
    """Return the ``apm_modules`` directory for *scope*."""
    from ..constants import APM_MODULES_DIR

    return get_apm_dir(scope) / APM_MODULES_DIR


def get_manifest_path(scope: InstallScope) -> Path:
    """Return the ``apm.yml`` path for *scope*."""
    from ..constants import APM_YML_FILENAME

    return get_apm_dir(scope) / APM_YML_FILENAME


def get_lockfile_dir(scope: InstallScope) -> Path:
    """Return the directory containing the lockfile for *scope*."""
    return get_apm_dir(scope)


def ensure_user_dirs() -> Path:
    """Create ``~/.apm/`` and ``~/.apm/apm_modules/`` if they do not exist.

    Returns the user APM root (``~/.apm/``).
    """
    from ..constants import APM_MODULES_DIR

    user_root = Path.home() / USER_APM_DIR
    user_root.mkdir(parents=True, exist_ok=True)
    (user_root / APM_MODULES_DIR).mkdir(exist_ok=True)
    return user_root


# ---------------------------------------------------------------------------
# Per-target user-scope helpers
#
# These functions query ``KNOWN_TARGETS`` in ``targets.py`` for user-scope
# metadata.  No parallel registry is needed -- TargetProfile carries
# ``user_supported``, ``user_root_dir``, and ``unsupported_user_primitives``.
# ---------------------------------------------------------------------------


def get_unsupported_targets() -> list[str]:
    """Return target names that do not support user-scope deployment."""
    from ..integration.targets import KNOWN_TARGETS

    return [name for name, profile in KNOWN_TARGETS.items() if profile.user_supported is False]


def warn_unsupported_user_scope() -> str:
    """Return a warning message listing targets that lack user-scope support.

    Returns an empty string when all targets are fully supported.

    The message distinguishes three categories:

    * **fully supported** -- ``user_supported is True``
    * **partially supported** -- ``user_supported == "partial"``
    * **not supported** -- ``user_supported is False``

    When some targets have ``unsupported_user_primitives``, a second line
    is added listing those primitives per target.
    """
    from ..integration.targets import KNOWN_TARGETS

    fully_supported = [name for name, p in KNOWN_TARGETS.items() if p.user_supported is True]
    partially_supported = [
        name for name, p in KNOWN_TARGETS.items() if p.user_supported == "partial"
    ]
    unsupported = [name for name, p in KNOWN_TARGETS.items() if p.user_supported is False]

    if not unsupported and not partially_supported:
        return ""

    parts: list[str] = []

    supported_names = ", ".join(fully_supported)
    parts.append(f"User-scope primitives are fully supported by {supported_names}.")

    if partially_supported:
        partial_names = ", ".join(partially_supported)
        parts[0] += f" Partially supported: {partial_names}."

    if unsupported:
        unsupported_names = ", ".join(unsupported)
        parts[0] += f" Targets without native user-level support: {unsupported_names}"

    # Collect per-target unsupported primitives
    unsupported_prims: list[str] = []
    for name, profile in KNOWN_TARGETS.items():
        prims = profile.unsupported_user_primitives
        if prims:
            unsupported_prims.append(f"{name} ({', '.join(prims)})")
    if unsupported_prims:
        parts.append("Some primitives are not supported: " + "; ".join(unsupported_prims))

    return "\n".join(parts)
