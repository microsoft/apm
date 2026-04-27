"""Local-content integration: deploy primitives the user authored locally.

This module handles two related scenarios:

1. **Root project as implicit local package (#714)** -- when the project's own
   ``.apm/`` directory contains skills, instructions, agents, prompts, hooks,
   or commands, ``apm install`` deploys them to target directories exactly like
   dependency primitives.  ``_project_has_root_primitives`` and
   ``_has_local_apm_content`` detect this case.

2. **Local-path dependencies from apm.yml** -- ``_copy_local_package`` copies
   a locally-referenced package into ``apm_modules/`` so the downstream
   integration pipeline can treat it uniformly.

The orchestrator ``_integrate_local_content`` lives in
``apm_cli.install.services`` (the DI seam) and is re-exported from
``apm_cli.commands.install`` for backward-compatible patching. Tests should
patch the symbol at the import path used by the code under test rather than
assuming the implementation lives in the commands module.

Functions
---------
_project_has_root_primitives
    Return True when the project root contains a ``.apm/`` directory.
_has_local_apm_content
    Return True when ``.apm/`` contains at least one primitive file.
_copy_local_package
    Copy a local-path dependency into ``apm_modules/``.
"""

from pathlib import Path

from apm_cli.utils.console import _rich_error
from apm_cli.utils.path_security import safe_rmtree

# ---------------------------------------------------------------------------
# Root primitive detection helpers
# ---------------------------------------------------------------------------


def _project_has_root_primitives(project_root) -> bool:
    """Return True when *project_root* has a .apm/ directory of its own.

    Used to decide whether ``apm install`` should enter the integration
    pipeline even when no external APM dependencies are declared (#714).
    The integrators themselves determine whether the directory contains
    anything actionable, so we only check for the directory's existence.
    """
    from pathlib import Path as _Path

    root = _Path(project_root)
    return (root / ".apm").is_dir()


def _has_local_apm_content(project_root):
    """Check if the project has local .apm/ content worth integrating.

    Returns True if .apm/ exists and contains at least one primitive file
    in a recognized subdirectory (skills, instructions, agents/chatmodes,
    prompts, hooks, commands).
    """
    apm_dir = project_root / ".apm"
    if not apm_dir.is_dir():
        return False
    _PRIMITIVE_DIRS = (
        "skills",
        "instructions",
        "chatmodes",
        "agents",
        "prompts",
        "hooks",
        "commands",
    )
    for subdir_name in _PRIMITIVE_DIRS:
        subdir = apm_dir / subdir_name
        if subdir.is_dir() and any(p.is_file() for p in subdir.rglob("*")):
            return True
    return False


# ---------------------------------------------------------------------------
# Local-path dependency copy
# ---------------------------------------------------------------------------


def _copy_local_package(dep_ref, install_path, project_root, logger=None):
    """Copy a local package to apm_modules/.

    Args:
        dep_ref: DependencyReference with is_local=True
        install_path: Target path under apm_modules/
        project_root: Project root for resolving relative paths
        logger: Optional CommandLogger for structured output

    Returns:
        install_path on success, None on failure
    """
    import shutil

    local = Path(dep_ref.local_path).expanduser()
    if not local.is_absolute():  # noqa: SIM108
        local = (project_root / local).resolve()
    else:
        local = local.resolve()

    if not local.is_dir():
        msg = f"Local package path does not exist: {dep_ref.local_path}"
        if logger:
            logger.error(msg)
        else:
            _rich_error(msg)
        return None
    from apm_cli.utils.helpers import find_plugin_json

    if (
        not (local / "apm.yml").exists()
        and not (local / "SKILL.md").exists()
        and find_plugin_json(local) is None
    ):
        msg = (
            f"Local package is not a valid APM package "
            f"(no apm.yml, SKILL.md, or plugin.json): {dep_ref.local_path}"
        )
        if logger:
            logger.error(msg)
        else:
            _rich_error(msg)
        return None

    # Ensure parent exists and clean target (always re-copy for local deps)
    install_path.parent.mkdir(parents=True, exist_ok=True)
    if install_path.exists():
        # install_path is already validated by get_install_path() (Layer 2),
        # but use safe_rmtree for defense-in-depth.
        apm_modules_dir = install_path.parent.parent  # _local/<name> -> apm_modules
        safe_rmtree(install_path, apm_modules_dir)

    shutil.copytree(local, install_path, dirs_exist_ok=False, symlinks=True)
    return install_path
