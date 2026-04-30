"""OneDrive-backed Cowork skills directory resolution and lockfile path translation.

Cowork skills are deployed to the user's OneDrive ``Documents/Cowork/skills/``
directory so that Microsoft 365 Copilot can discover them.  This module owns:

1. **Resolution** -- locating the OneDrive mount point on macOS and Windows,
   and mapping it to ``<mount>/Documents/Cowork/skills/``.  The
   ``APM_COPILOT_COWORK_SKILLS_DIR`` environment variable overrides automatic detection
   for CI or non-standard layouts.

2. **Lockfile translation** -- APM's lockfile pipeline expects paths relative
   to ``project_root``.  Cowork paths are absolute (outside the project tree),
   so we encode them as ``cowork://skills/<rel-posix>`` in the lockfile and
   translate back to absolute paths at filesystem-I/O boundaries.

All filesystem input passes through ``validate_path_segments`` and
``ensure_path_within`` from ``apm_cli.utils.path_security``.  No ad-hoc
traversal checks.

Design note
-----------
This module is pure-stdlib and does **not** import any third-party library.
It is always importable but functionally inert until the ``cowork``
experimental flag is enabled by the caller.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COWORK_URI_SCHEME: str = "cowork://"
"""Synthetic URI prefix used in lockfile entries for cowork deployments."""

COWORK_LOCKFILE_PREFIX: str = "cowork://skills/"
"""Full prefix for skill entries in the lockfile (scheme + skills segment)."""

_ONEDRIVE_GLOB: str = "OneDrive*"
"""Glob pattern for OneDrive mount directories under ``~/Library/CloudStorage/``.

macOS creates directories named ``OneDrive - TenantName`` (with spaces), so
the glob must NOT restrict to ``OneDrive-*``.
"""

_COWORK_SUBDIR: str = "Documents/Cowork"
"""Relative path from the OneDrive mount root to the Cowork directory."""

_COPILOT_COWORK_SKILLS_SUBDIR: str = "Documents/Cowork/skills"
"""Relative path from the OneDrive mount root to the skills directory."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CoworkResolutionError(Exception):
    """Raised when OneDrive resolution fails with an actionable diagnostic.

    Callers should format the ``str(err)`` via ``CommandLogger.error()``
    so the user sees the message with an ``[x]`` symbol.
    """


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_copilot_cowork_skills_dir() -> Path | None:
    """Locate the Cowork skills directory on the current machine.

    Resolution order:

    1. ``APM_COPILOT_COWORK_SKILLS_DIR`` environment variable (highest priority).
    2. ``copilot_cowork_skills_dir`` from ``~/.apm/config.json`` (via ``apm config``).
    3. Platform auto-detection:
       - macOS: ``~/Library/CloudStorage/OneDrive*/``.
       - Windows: ``%ONEDRIVECOMMERCIAL%``, then ``%ONEDRIVE%``.
       - Linux: no default lookup (returns ``None``).

    Returns ``None`` when no OneDrive mount is found (target unavailable).

    Raises:
        CoworkResolutionError: When multiple OneDrive tenants are detected
            on macOS and ``APM_COPILOT_COWORK_SKILLS_DIR`` is not set.  The exception
            message lists the candidates and instructs the user to set the
            env var.
    """
    # --- env-var override ---
    env_override = os.environ.get("APM_COPILOT_COWORK_SKILLS_DIR")
    if env_override:
        from apm_cli.utils.path_security import (
            PathTraversalError,
            validate_path_segments,
        )

        try:
            validate_path_segments(env_override, context="APM_COPILOT_COWORK_SKILLS_DIR")
        except PathTraversalError as exc:
            raise CoworkResolutionError(
                f"APM_COPILOT_COWORK_SKILLS_DIR contains a traversal sequence: {exc}"
            ) from exc
        return Path(env_override).expanduser().resolve()

    # --- persisted config value ---
    from apm_cli.config import get_copilot_cowork_skills_dir

    config_value = get_copilot_cowork_skills_dir()
    if config_value:
        from apm_cli.utils.path_security import (
            PathTraversalError,
            validate_path_segments,
        )

        try:
            validate_path_segments(config_value, context="copilot_cowork_skills_dir config")
        except PathTraversalError as exc:
            raise CoworkResolutionError(
                f"copilot_cowork_skills_dir config contains a traversal sequence: {exc}"
            ) from exc
        return Path(config_value).expanduser().resolve()

    # --- Windows auto-detection ---
    if sys.platform == "win32":
        from apm_cli.utils.path_security import (
            PathTraversalError,
            validate_path_segments,
        )

        for _env_name in ("ONEDRIVECOMMERCIAL", "ONEDRIVE"):
            _win_root = os.environ.get(_env_name, "")
            if _win_root:
                _win_skills = Path(_win_root) / _COPILOT_COWORK_SKILLS_SUBDIR
                try:
                    validate_path_segments(str(_win_skills), context=f"{_env_name} env var")
                except PathTraversalError as exc:
                    raise CoworkResolutionError(
                        f"{_env_name} contains a traversal sequence: {exc}"
                    ) from exc
                return _win_skills.resolve()
        return None

    # --- macOS auto-detection ---
    cloud_storage = Path.home() / "Library" / "CloudStorage"
    if not cloud_storage.is_dir():
        return None

    candidates = sorted(cloud_storage.glob(_ONEDRIVE_GLOB))
    if not candidates:
        return None

    if len(candidates) > 1:
        listing = "\n".join(f"  - {c}" for c in candidates)
        raise CoworkResolutionError(
            f"Multiple OneDrive mounts detected:\n{listing}\n"
            f"Set APM_COPILOT_COWORK_SKILLS_DIR to the desired skills directory, e.g.:\n"
            f"  export APM_COPILOT_COWORK_SKILLS_DIR="
            f'"{candidates[0] / _COPILOT_COWORK_SKILLS_SUBDIR}"'
        )

    return candidates[0] / _COPILOT_COWORK_SKILLS_SUBDIR


# ---------------------------------------------------------------------------
# Lockfile translation
# ---------------------------------------------------------------------------


def to_lockfile_path(absolute: Path, cowork_root: Path) -> str:
    """Encode an absolute cowork path as a ``cowork://`` lockfile entry.

    Args:
        absolute: Absolute path to a deployed file or directory inside
            the cowork skills tree.
        cowork_root: The resolved cowork skills root (from
            ``resolve_copilot_cowork_skills_dir()``).

    Returns:
        A string like ``cowork://skills/my-skill/SKILL.md``.

    Raises:
        ``PathTraversalError`` if *absolute* escapes *cowork_root*.
    """
    from apm_cli.utils.path_security import ensure_path_within

    # Validate containment -- raises PathTraversalError on violation.
    resolved = ensure_path_within(absolute, cowork_root)
    rel = resolved.relative_to(cowork_root.resolve())
    return f"{COWORK_URI_SCHEME}skills/{rel.as_posix()}"


def from_lockfile_path(lockfile_path: str, cowork_root: Path) -> Path:
    """Decode a ``cowork://`` lockfile entry to an absolute ``Path``.

    Args:
        lockfile_path: A string like ``cowork://skills/my-skill/SKILL.md``.
        cowork_root: The resolved cowork skills root.

    Returns:
        Absolute ``Path`` under *cowork_root*.

    Raises:
        ``PathTraversalError`` if the decoded path escapes *cowork_root*.
        ``ValueError`` if *lockfile_path* does not start with the cowork
        URI scheme.
    """
    from apm_cli.utils.path_security import (
        ensure_path_within,
        validate_path_segments,
    )

    if not lockfile_path.startswith(COWORK_URI_SCHEME):
        raise ValueError(f"Not a cowork lockfile path: {lockfile_path!r}")

    # Strip scheme to get the relative portion (e.g. "skills/my-skill/SKILL.md").
    rel_posix = lockfile_path[len(COWORK_URI_SCHEME) :]

    # Pre-parse traversal rejection.
    validate_path_segments(rel_posix, context="cowork lockfile path")

    # The lockfile stores "skills/<name>/..." but cowork_root already
    # points to the skills directory, so we must strip the leading
    # "skills/" segment to avoid double-nesting.
    _skills_prefix = "skills/"
    if rel_posix.startswith(_skills_prefix):
        rel_posix = rel_posix[len(_skills_prefix) :]

    candidate = cowork_root / rel_posix
    # Re-validate containment after path construction.
    return ensure_path_within(candidate, cowork_root)


def is_cowork_path(lockfile_path: str) -> bool:
    """Return ``True`` if *lockfile_path* uses the ``cowork://`` scheme."""
    return lockfile_path.startswith(COWORK_URI_SCHEME)
