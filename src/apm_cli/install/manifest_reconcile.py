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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile


def install_governance(targets: list[TargetProfile]) -> tuple[set[str], set[str]]:
    """Return ``(file_roots, uri_schemes)`` governed by *targets*.

    ``file_roots`` is the set of top-level managed directory names -- each
    target's ``root_dir`` plus every primitive ``deploy_root`` override (the
    ``copilot`` target routes its ``skills`` primitive to ``.agents`` via
    ``deploy_root`` even though ``root_dir`` is ``.github``).

    ``uri_schemes`` is the set of lockfile URI schemes used by dynamic /
    user-machine targets (``copilot-app`` -> ``copilot-app-db://``,
    ``copilot-cowork`` -> ``cowork://``).
    """
    from apm_cli.integration.copilot_app_db import COPILOT_APP_URI_SCHEME
    from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

    file_roots: set[str] = set()
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
        if root:
            file_roots.add(str(root).split("/", 1)[0])
        primitives = getattr(target, "primitives", None)
        if isinstance(primitives, dict):
            for mapping in primitives.values():
                deploy_root = getattr(mapping, "deploy_root", None)
                if deploy_root:
                    file_roots.add(str(deploy_root).split("/", 1)[0])
    return file_roots, uri_schemes


def is_governed_by_install(path: str, file_roots: set[str], uri_schemes: set[str]) -> bool:
    """Return ``True`` if *path* is owned by the current install's targets.

    File paths are matched by top-level directory; scheme URIs (e.g.
    ``copilot-app-db://``, ``cowork://``) are matched by their scheme.
    """
    if "://" in path:
        scheme = path.split("://", 1)[0] + "://"
        return scheme in uri_schemes
    top = path.split("/", 1)[0]
    return top in file_roots


def union_preserving(
    current_files: list[str],
    current_hashes: dict[str, str],
    prior_files: list[str],
    prior_hashes: dict[str, str],
    targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None = None,
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
    ``None`` (auto-detect or ``--target``-only consumers -- no declared
    universe to check against) the legacy preserve-all behaviour is kept so a
    genuine multi-target deploy is never clobbered (issue #1716).
    """
    file_roots, uri_schemes = install_governance(targets)
    allowed_roots: set[str] | None = None
    allowed_schemes: set[str] | None = None
    if declared_targets is not None:
        d_roots, d_schemes = install_governance(declared_targets)
        # Active targets are always legitimate (this run selected them), so a
        # ``--target`` that reaches outside the declared set is still honoured.
        allowed_roots = file_roots | d_roots
        allowed_schemes = uri_schemes | d_schemes
    current_set = set(current_files or ())
    merged_hashes = dict(current_hashes or {})
    preserved: list[str] = []
    for path in prior_files or ():
        if path in current_set:
            continue
        if is_governed_by_install(path, file_roots, uri_schemes):
            continue
        if (
            allowed_roots is not None
            and allowed_schemes is not None
            and not is_governed_by_install(path, allowed_roots, allowed_schemes)
        ):
            # Ghost: governed by no target the consumer declares. Drop it.
            continue
        preserved.append(path)
        if prior_hashes and path in prior_hashes:
            merged_hashes[path] = prior_hashes[path]
    return list(current_files or ()) + preserved, merged_hashes
