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

from apm_cli.utils.path_security import (
    safe_rmtree,
)

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


def _copy_local_package(dep_ref, install_path, base_dir, *, project_root, logger):
    """Copy a local package to apm_modules/.

    Args:
        dep_ref: DependencyReference with is_local=True.
        install_path: Target path under apm_modules/.
        base_dir: Directory used to resolve a relative ``dep_ref.local_path``.
            For direct deps from the root project this is the project root;
            for transitive deps it is the source directory of the package
            whose apm.yml declared *dep_ref* (#857). Must NOT be confused
            with ``project_root`` -- the anchoring base and the security
            containment boundary are deliberately distinct concerns.
        project_root: Project root, threaded through for symmetry with the
            anchoring story but NOT used as a hard containment boundary
            here. The actual untrusted-source boundary lives upstream in
            :mod:`apm_cli.deps.apm_resolver` (``_try_load_dependency_package``
            dual-rejects any local_path declared by a remote parent before
            this function ever runs). Enforcing project-root containment
            *here* would also block legitimate sibling layouts -- e.g.
            ``apm install ../pkg-a`` from a monorepo workspace -- which
            users explicitly opt into. Kept on the signature so callers
            keep the security model in mind and so a future tightening
            (e.g. opt-in strict mode) has a hook.
        logger: Required CommandLogger for structured output. Callers must
            thread one in; we no longer fall back to a bare console helper
            (#940 SR4) because doing so masked logger threading bugs in
            transitive call stacks.

    Returns:
        install_path on success, None on failure.

    Notes:
        We deliberately do NOT call ``validate_path_segments`` on
        ``dep_ref.local_path``: that helper rejects ``..`` segments, which
        would break the legitimate ``../sibling`` pattern this PR enables.
        The untrusted-source boundary is the resolver-level dual-reject
        of remote-parent local_paths; everything reaching this function
        comes from a parent the user already trusts (their own manifest,
        a CLI arg, or another local package they explicitly added).
    """
    import shutil

    # project_root retained on signature for future strict-mode hook (see
    # docstring); not consumed in the current copy path.
    _ = project_root

    # PR #1111 review C1: ``ctx.logger`` is allowed to be None
    # (``run_install_pipeline(logger=None)`` is a public, documented entry
    # point). Without this guard the unconditional ``logger.error(...)``
    # calls below would AttributeError for any local dep when a caller
    # does not thread an InstallLogger through. Defaulting to the rich-
    # console-backed ``NullCommandLogger`` keeps the error visible to the
    # user while preserving the documented "logger is required" contract
    # for callers that DO thread one in (their logger wins).
    if logger is None:
        from apm_cli.core.null_logger import NullCommandLogger

        logger = NullCommandLogger()

    local = Path(dep_ref.local_path).expanduser()
    # Anchor on the *declaring* package's directory (#857). For direct deps
    # from the root, ``base_dir`` IS ``project_root`` so behavior is
    # unchanged. For transitive deps, ``base_dir`` is the parent package's
    # source dir. Absolute paths bypass anchoring.
    if not local.is_absolute():  # noqa: SIM108
        local = (base_dir / local).resolve()
    else:
        local = local.resolve()

    if not local.is_dir():
        logger.error(f"Local package path does not exist: {dep_ref.local_path}")
        return None
    from apm_cli.utils.helpers import find_plugin_json

    if (
        not (local / "apm.yml").exists()
        and not (local / "SKILL.md").exists()
        and find_plugin_json(local) is None
    ):
        logger.error(
            "Local package is not a valid APM package "
            f"(no apm.yml, SKILL.md, or plugin.json): {dep_ref.local_path}"
        )
        return None

    # Ensure parent exists and clean target (always re-copy for local deps)
    install_path.parent.mkdir(parents=True, exist_ok=True)
    if install_path.exists():
        # install_path is already validated by get_install_path() (Layer 2),
        # but use safe_rmtree for defense-in-depth.
        apm_modules_dir = install_path.parent.parent  # _local/<name> -> apm_modules
        safe_rmtree(install_path, apm_modules_dir)

    # SECURITY: symlinks=True preserves in-package symlinks rather than
    # dereferencing them. This is INTENTIONAL: a package author who ships a
    # symlink owns the consequences. The link is inert in apm_modules; any
    # consumer tool that follows it is responsible for its own sandboxing.
    # SECURITY: TOCTOU window between local.resolve() above and copytree
    # here. An attacker with write access to the source tree could swap the
    # directory for a symlink in this gap; but such an attacker can already
    # modify deployed files directly, so the mitigation cost (atomic dir
    # operations) outweighs the marginal risk. Future hardening should land
    # at this site.
    shutil.copytree(local, install_path, dirs_exist_ok=False, symlinks=True)
    return install_path
