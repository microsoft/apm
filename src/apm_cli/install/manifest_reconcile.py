"""Target-scoped manifest reconciliation shared by lockfile build sites.

On-disk stale cleanup is target-scoped: it preserves files belonging to
OTHER deploy targets (``phases/cleanup.py``). The lockfile manifest must
reconcile with the same symmetry. An ``apm install`` only governs its own
targets' deploy roots and URI schemes, so manifest entries written by a
prior install for OTHER targets must be PRESERVED rather than clobbered.

Without this symmetry a multi-target deploy (e.g. the ``copilot`` target
writing ``.github/`` + ``.agents/skills/`` files, then a later
``copilot-app`` install writing DB-URI rows) leaves the committed lockfile
single-target: the surviving on-disk files become orphaned from the
manifest and escape every manifest-driven audit gate -- deployed-files-
present, content-integrity, and drift (issue #1716).

Two manifest blocks need this reconciliation:

* per-dependency ``deployed_files`` / ``deployed_file_hashes``
  (``phases/lockfile.py``), and
* project-root ``local_deployed_files`` / ``local_deployed_file_hashes``
  (``phases/post_deps_local.py``).

Both import :func:`union_preserving` so the behaviour stays identical.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector


def install_governance(targets: list[TargetProfile]) -> tuple[set[str], set[str]]:
    """Return ``(file_prefixes, uri_schemes)`` governed by *targets*.

    Dedicated target roots govern their full subtree. The shared ``.agents``
    root is partitioned by primitive subdirectory so one active target cannot
    claim a declared sibling's files (for example, Copilot's
    ``.agents/skills`` versus Antigravity's ``.agents/rules``).

    ``uri_schemes`` is the set of lockfile URI schemes used by dynamic /
    user-machine targets (``copilot-app`` -> ``copilot-app-db://``,
    ``copilot-cowork`` -> ``cowork://``).
    """
    from apm_cli.integration.copilot_app_db import COPILOT_APP_URI_SCHEME
    from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

    file_prefixes: set[str] = set()
    uri_schemes: set[str] = set()
    for target in targets or []:
        name = getattr(target, "name", None)
        if name == "copilot-app":
            uri_schemes.add(COPILOT_APP_URI_SCHEME)
            continue
        if name == "copilot-cowork":
            uri_schemes.add(COWORK_URI_SCHEME)
            continue
        root = getattr(target, "root_dir", None)
        if root and str(root).rstrip("/") != ".agents":
            file_prefixes.add(str(root).rstrip("/") + "/")
        primitives = getattr(target, "primitives", None)
        if isinstance(primitives, dict):
            for mapping in primitives.values():
                deploy_root = getattr(mapping, "deploy_root", None)
                base = str(deploy_root or root or "").rstrip("/")
                if base != ".agents":
                    continue
                subdir = getattr(mapping, "subdir", None)
                if subdir:
                    file_prefixes.add(f"{base}/{str(subdir).strip('/')}/")
                    continue
                extension = getattr(mapping, "extension", None)
                if extension:
                    file_prefixes.add(f"{base}/{str(extension).strip('/')}")
                else:
                    # Compatibility for minimal TargetProfile stand-ins.
                    file_prefixes.add(f"{base}/")
        if str(root or "").rstrip("/") == ".agents":
            for generated in getattr(target, "generated_files", ()) or ():
                file_prefixes.add(f".agents/{str(generated).lstrip('/')}")
    return file_prefixes, uri_schemes


def is_governed_by_install(path: str, file_prefixes: set[str], uri_schemes: set[str]) -> bool:
    """Return ``True`` if *path* is owned by the current install's targets.

    File paths are matched by top-level directory; scheme URIs (e.g.
    ``copilot-app-db://``, ``cowork://``) are matched by their scheme.
    """
    if "://" in path:
        scheme = path.split("://", 1)[0] + "://"
        return scheme in uri_schemes
    return any(
        path.startswith(prefix) if prefix.endswith("/") else path == prefix
        for prefix in file_prefixes
    )


def union_preserving(
    current_files: list[str],
    current_hashes: dict[str, str],
    prior_files: list[str],
    prior_hashes: dict[str, str],
    targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None = None,
    on_ghost_drop: Callable[[str], None] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Union the current install's manifest with preserved other-target entries.

    ``current_files`` / ``current_hashes`` describe what THIS install
    deployed (and thus governs). ``prior_files`` / ``prior_hashes`` come from
    the existing lockfile. Returns ``(files, hashes)`` -- the current entries
    plus any prior entries that belong to OTHER targets (not governed by this
    install). Entries the current install governs are authoritative, so a
    same-target reinstall still drops files removed from the package.

    ``declared_targets`` is the consumer's legitimate target universe --
    apm.yml-declared canonical targets plus the always-legitimate gated/dynamic
    targets -- independent of any ``--target`` narrowing (see
    ``phases.targets.declared_target_profiles``). When provided, a prior entry
    that belongs to NEITHER this install's targets NOR any of those targets is
    an inactive-target *ghost* (e.g. a dependency's
    package-declared ``windsurf`` paths the consumer never activates) and is
    DROPPED -- it can never be written on disk, so re-preserving it fails
    ``deployed-files-present`` forever on fresh checkouts (issue #2059). When
    An entry matching no registered target pattern is indeterminate and is
    preserved. When ``declared_targets`` is ``None`` (auto-detect or
    ``--target``-only consumers -- no declared universe to check against), the
    legacy preserve-all behaviour is kept so a genuine multi-target deploy is
    never clobbered (issue #1716).
    """
    file_prefixes, uri_schemes = install_governance(targets)
    allowed_prefixes: set[str] | None = None
    allowed_schemes: set[str] | None = None
    known_prefixes: set[str] | None = None
    known_schemes: set[str] | None = None
    if declared_targets is not None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        declared_prefixes, d_schemes = install_governance(declared_targets)
        known_prefixes, known_schemes = install_governance(list(KNOWN_TARGETS.values()))
        # Active targets are always legitimate (this run selected them), so a
        # ``--target`` that reaches outside the declared set is still honoured.
        allowed_prefixes = file_prefixes | declared_prefixes
        allowed_schemes = uri_schemes | d_schemes
    current_set = set(current_files or ())
    merged_hashes = dict(current_hashes or {})
    preserved: list[str] = []
    for path in prior_files or ():
        if path in current_set:
            continue
        if is_governed_by_install(path, file_prefixes, uri_schemes):
            continue
        if (
            allowed_prefixes is not None
            and allowed_schemes is not None
            and not is_governed_by_install(path, allowed_prefixes, allowed_schemes)
            and known_prefixes is not None
            and known_schemes is not None
            and is_governed_by_install(path, known_prefixes, known_schemes)
        ):
            # Ghost: attributable to a known target the consumer does not
            # declare. Unknown patterns are indeterminate and stay preserved.
            if on_ghost_drop is not None:
                on_ghost_drop(path)
            continue
        preserved.append(path)
        if prior_hashes and path in prior_hashes:
            merged_hashes[path] = prior_hashes[path]
    return list(current_files or ()) + preserved, merged_hashes


def declared_target_profiles(
    project_root: Path,
    *,
    user_scope: bool = False,
) -> list[TargetProfile] | None:
    """Resolve the target universe declared by a project manifest."""
    from apm_cli.core.apm_yml import CANONICAL_TARGETS, parse_targets_field
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.utils.yaml_io import load_yaml

    try:
        data = load_yaml(project_root / "apm.yml")
        names = parse_targets_field(data) if isinstance(data, dict) else None
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return None
    if not names:
        return None

    profiles: list[TargetProfile] = []
    for name in dict.fromkeys(names):
        profile = KNOWN_TARGETS.get(name)
        if profile is None:
            continue
        scoped = profile.for_scope(user_scope=user_scope)
        if scoped is not None:
            profiles.append(scoped)
    for name, profile in KNOWN_TARGETS.items():
        if name in CANONICAL_TARGETS:
            continue
        scoped = profile.for_scope(user_scope=user_scope)
        profiles.append(scoped if scoped is not None else profile)
    return profiles or None


def reconcile_deployed_block(
    *,
    project_root: Path,
    dep_key: str,
    current_files: list[str],
    current_hashes: dict[str, str],
    prior_files: list[str],
    prior_hashes: dict[str, str],
    active_targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None,
    diagnostics: DiagnosticCollector,
    on_ghost_drop: Callable[[str], None] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Reconcile one deployed-state block and safely remove dropped paths."""
    files, hashes = union_preserving(
        current_files,
        current_hashes,
        prior_files,
        prior_hashes,
        active_targets,
        declared_targets=declared_targets,
        on_ghost_drop=on_ghost_drop,
    )
    dropped = set(prior_files) - set(files)
    if not dropped:
        return files, hashes

    from apm_cli.integration.base_integrator import BaseIntegrator
    from apm_cli.integration.cleanup import remove_stale_deployed_files

    cleanup = remove_stale_deployed_files(
        dropped,
        project_root,
        dep_key=dep_key,
        targets=None,
        diagnostics=diagnostics,
        recorded_hashes=prior_hashes,
    )
    for path in cleanup.failed:
        if path not in files:
            files.append(path)
        if path in prior_hashes:
            hashes[path] = prior_hashes[path]
    if cleanup.deleted_targets:
        BaseIntegrator.cleanup_empty_parents(cleanup.deleted_targets, project_root)
    return files, hashes


def reconcile_deployed_state(
    *,
    project_root: Path,
    lockfile: LockFile,
    active_targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None,
    diagnostics: DiagnosticCollector,
) -> bool:
    """Prune undeclared-target ownership from every lockfile deployment block."""
    from apm_cli.deps.lockfile import _SELF_KEY
    from apm_cli.integration.targets import KNOWN_TARGETS

    allowed_targets = [*active_targets, *(declared_targets or [])]
    allowed_prefixes, allowed_schemes = install_governance(allowed_targets)
    known_prefixes, known_schemes = install_governance(list(KNOWN_TARGETS.values()))

    def _retained(files: list[str]) -> list[str]:
        if declared_targets is None:
            return list(files)
        return [
            path
            for path in files
            if not (
                is_governed_by_install(path, known_prefixes, known_schemes)
                and not is_governed_by_install(path, allowed_prefixes, allowed_schemes)
            )
        ]

    changed = False
    for dep_key, dependency in lockfile.dependencies.items():
        if dep_key == _SELF_KEY:
            continue
        prior_files = list(dependency.deployed_files)
        prior_hashes = dict(dependency.deployed_file_hashes)
        current_files = _retained(prior_files)
        current_hashes = {
            path: value for path, value in prior_hashes.items() if path in current_files
        }
        files, hashes = reconcile_deployed_block(
            project_root=project_root,
            dep_key=dep_key,
            current_files=current_files,
            current_hashes=current_hashes,
            prior_files=prior_files,
            prior_hashes=prior_hashes,
            active_targets=active_targets,
            declared_targets=declared_targets,
            diagnostics=diagnostics,
        )
        if files != prior_files or hashes != prior_hashes:
            dependency.deployed_files = files
            dependency.deployed_file_hashes = hashes
            changed = True

    prior_local = list(lockfile.local_deployed_files)
    prior_local_hashes = dict(lockfile.local_deployed_file_hashes)
    current_local = _retained(prior_local)
    current_local_hashes = {
        path: value for path, value in prior_local_hashes.items() if path in current_local
    }
    local_files, local_hashes = reconcile_deployed_block(
        project_root=project_root,
        dep_key="<local .apm/>",
        current_files=current_local,
        current_hashes=current_local_hashes,
        prior_files=prior_local,
        prior_hashes=prior_local_hashes,
        active_targets=active_targets,
        declared_targets=declared_targets,
        diagnostics=diagnostics,
    )
    if local_files != prior_local or local_hashes != prior_local_hashes:
        lockfile.local_deployed_files = local_files
        lockfile.local_deployed_file_hashes = local_hashes
        changed = True
    return changed


def reconcile_project_deployed_state(
    manifest_root: Path,
    *,
    explicit_target: str | list[str] | None,
    deploy_root: Path | None = None,
    lock_root: Path | None = None,
    user_scope: bool = False,
    verbose: bool = False,
) -> bool:
    """Reconcile and persist a project's deployed state after a command."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.integration.targets import active_targets, active_targets_user_scope
    from apm_cli.utils.diagnostics import DiagnosticCollector

    deploy_root = deploy_root or manifest_root
    lock_path = get_lockfile_path(lock_root or manifest_root)
    lockfile = LockFile.read(lock_path)
    if lockfile is None:
        return False
    declared = declared_target_profiles(manifest_root, user_scope=user_scope)
    if explicit_target is None and declared is not None:
        targets = declared
    elif user_scope:
        targets = active_targets_user_scope(explicit_target=explicit_target)
    else:
        targets = active_targets(deploy_root, explicit_target=explicit_target)
    changed = reconcile_deployed_state(
        project_root=deploy_root,
        lockfile=lockfile,
        active_targets=targets,
        declared_targets=declared,
        diagnostics=DiagnosticCollector(verbose=verbose),
    )
    if changed:
        lockfile.save(lock_path)
    return changed
