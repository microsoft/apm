"""Post-uninstall forward dependency reachability (canonical owner).

Single owner for the question: "given the direct dependencies that
survive an ``apm uninstall`` edit, which locked transitive dependencies
are still genuinely reachable from them, per their own real manifests?"

This exists because :mod:`apm_cli.commands.uninstall.engine`'s backward
orphan-candidate scan (``_build_children_index``, keyed by the
single-valued ``LockedDependency.resolved_by``) cannot tell a genuine
orphan from a shared/diamond transitive dependency that a SURVIVING
package still declares -- ``resolved_by`` only ever records the first
parent that introduced a dependency, never every parent that legitimately
depends on it. Rather than widen that durable, first-wins lockfile field
(large blast radius across :mod:`apm_cli.deps.why_walker`,
:mod:`apm_cli.deps.path_anchoring`, ``ci_checks.py``, ``mcp_integrator.py``
-- all single-parent consumers today), this module recomputes forward
reachability on demand, offline, from the canonical manifest/lock graph.

Completeness contract
----------------------
:func:`compute_forward_reachable_keys` returns a :class:`ReachabilityResult`
whose ``complete`` flag is ``False`` if ANY node touched during the walk
(a survivor, or a candidate orphan whose own on-disk anchor could not be
established) has a manifest that is missing, unreadable, malformed, or
whose resolved location is unsafe (a corrupt/cyclic/ambiguous local
anchor chain, or a path that escapes ``apm_modules/``). An unexplored
subtree is NOT a safe stand-in for "no children" -- it could be exactly
the branch that reaches a candidate orphan, so treating it as a leaf
would silently allow an incorrect deletion. Callers MUST treat
``complete is False`` as "no rescue information is trustworthy this run"
and preserve every candidate orphan rather than partially trust
``reachable`` -- see
:func:`apm_cli.commands.uninstall.engine._compute_actual_orphans`, the
single call site that enforces this policy for both ``apm uninstall`` and
its ``--dry-run`` preview.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.deps.path_anchoring import LocalResolutionError, resolve_local_dep_dir
from apm_cli.models.apm_package import APMPackage

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.models.apm_package import DependencyReference


@dataclass(frozen=True)
class ReachabilityResult:
    """Outcome of a forward reachability walk from surviving direct deps.

    Attributes:
        reachable: Lockfile unique keys confirmed forward-reachable from
            ``direct_refs``. Only meaningful when ``complete`` is True.
        complete: False if any node touched during the walk (a survivor,
            or a candidate orphan whose own anchor was needed for
            matching) could not be safely resolved and read. Callers MUST
            NOT partially trust ``reachable`` when this is False.
        unverifiable: ``(identifier, reason)`` pairs describing every node
            that could not be resolved/read, for diagnostics.
        reachable_via: For each key in ``reachable`` that was matched
            during the walk (i.e. every rescued candidate orphan -- not
            necessarily every incidental survivor key that also happens
            to end up in ``reachable``), the ``(parent_repo_url,
            local_path)`` of the surviving node whose manifest edge
            actually led to it: the immediate parent's own
            ``repo_url`` (suitable for a direct write into that entry's
            ``LockedDependency.resolved_by``), and -- for a LOCAL match
            only -- the literal ``local_path`` string declared by THAT
            parent's manifest edge (``None`` for a remote match, which
            has no anchored ``local_path`` concept). Repairing
            ``resolved_by`` alone is not sufficient for local deps: a
            transitive local dep's ``local_path`` is interpreted
            relative to whichever directory ``resolved_by`` currently
            names (see ``apm_cli.deps.path_anchoring``), so a stale
            ``local_path`` captured from a DIFFERENT (now-removed)
            parent could resolve to the wrong directory if only
            ``resolved_by`` were repointed. Callers that persist a
            rescue (see
            ``apm_cli.commands.uninstall.engine._cleanup_transitive_
            orphans``) MUST write back both fields together, never
            ``resolved_by`` alone.
    """

    reachable: frozenset[str]
    complete: bool
    unverifiable: tuple[tuple[str, str], ...]
    reachable_via: dict[str, tuple[str, str | None]] = field(default_factory=dict)


def _join_local(anchor: Path, local_path: str) -> Path:
    """Resolve *local_path* against *anchor*; absolute paths bypass it.

    A plain relative-path join, distinct from (and much smaller than) the
    ``resolved_by``-chain walk :func:`resolve_local_dep_dir` performs --
    that chain-walk is the canonical, non-duplicated piece; this is just
    "join a manifest-declared relative path onto a directory we already
    resolved while visiting its declaring parent."
    """
    raw = Path(local_path).expanduser()
    return raw.resolve() if raw.is_absolute() else (anchor / raw).resolve()


def _build_local_dir_index(
    lockfile: LockFile,
    project_root: Path,
    candidate_orphans: frozenset[str],
    unverifiable: list[tuple[str, str]],
) -> dict[Path, str]:
    """Map each LOCAL candidate orphan's real directory to its lockfile key.

    Only candidate orphans are resolved here (not the whole lockfile) so
    an unrelated, already-settled entry elsewhere in a large lockfile
    cannot spuriously mark this decision incomplete. Any
    :class:`LocalResolutionError` (corrupt / ambiguous / cyclic
    ``resolved_by`` chain) is recorded in *unverifiable*, since we then
    cannot rule out that some survivor reaches that entry.
    """
    index: dict[Path, str] = {}
    for key in candidate_orphans:
        dep = lockfile.get_dependency(key)
        if dep is None or dep.source != "local":
            continue
        try:
            index[resolve_local_dep_dir(dep, lockfile, project_root)] = key
        except LocalResolutionError as exc:
            unverifiable.append((key, str(exc)))
    return index


def compute_forward_reachable_keys(
    lockfile: LockFile,
    project_root: Path,
    apm_modules_dir: Path,
    direct_refs: list[DependencyReference],
    candidate_orphans: frozenset[str],
) -> ReachabilityResult:
    """Walk forward from surviving direct deps to find reachable lock keys.

    Args:
        lockfile: The current (in-memory) lockfile. Local anchor chains
            may still reference just-removed packages at this point in
            the uninstall flow; that is safe -- see the module docstring.
        project_root: Anchor for root-declared (direct) local deps.
        apm_modules_dir: Base directory for resolving remote/registry
            install paths (containment-checked internally by
            ``get_install_path``).
        direct_refs: The project's surviving direct dependency refs,
            freshly parsed from the (post-edit, for a real uninstall; or
            pre-edit minus the removed packages, for ``--dry-run``)
            apm.yml.
        candidate_orphans: Lockfile keys the backward orphan-candidate
            scan flagged for possible removal. Narrows how much work this
            walk needs to do (and how much can go wrong) -- see
            ``_build_local_dir_index``.

    Returns:
        A :class:`ReachabilityResult`. See its docstring and the module
        docstring for the completeness contract.
    """
    unverifiable: list[tuple[str, str]] = []
    local_dir_index = _build_local_dir_index(
        lockfile, project_root, candidate_orphans, unverifiable
    )

    reachable: set[str] = set()
    reachable_via: dict[str, tuple[str, str | None]] = {}
    visited: set[str] = set()  # resolved local dirs (as str) or remote unique keys
    # Each queue entry also carries the repo_url of the node whose manifest
    # edge introduced it (None for the seed direct_refs, which have no
    # parent in this graph) -- this is how a rescued candidate's immediate,
    # currently-valid parent is recovered for the resolved_by/local_path
    # repair (see ReachabilityResult.reachable_via).
    queue: deque[tuple[DependencyReference, Path | None, str | None]] = deque(
        (ref, project_root, None) for ref in direct_refs
    )

    while queue:
        ref, anchor, parent_repo_url = queue.popleft()

        if ref.is_local and ref.local_path:
            real_dir = _join_local(anchor, ref.local_path) if anchor else None
            if real_dir is None:
                continue
            visit_key = str(real_dir)
            if visit_key in visited:
                continue
            visited.add(visit_key)
            matched_key = local_dir_index.get(real_dir)
            if matched_key is not None:
                reachable.add(matched_key)
                if parent_repo_url is not None:
                    reachable_via.setdefault(matched_key, (parent_repo_url, ref.local_path))
            manifest_dir = real_dir
        else:
            try:
                install_path = ref.get_install_path(apm_modules_dir)
            except ValueError as exc:
                # Unresolved marketplace ref or an install path that
                # escapes apm_modules_dir (PathTraversalError, a ValueError
                # subclass) -- either way this node's subtree is unknown.
                unverifiable.append((ref.get_canonical_dependency_string(), str(exc)))
                continue
            visit_key = ref.get_unique_key()
            if visit_key in visited:
                continue
            visited.add(visit_key)
            if visit_key in lockfile.dependencies:
                reachable.add(visit_key)
                if parent_repo_url is not None:
                    # Remote deps don't use anchored local_path -- only
                    # resolved_by needs repairing for these.
                    reachable_via.setdefault(visit_key, (parent_repo_url, None))
            manifest_dir = install_path

        try:
            package = APMPackage.from_apm_yml(manifest_dir / "apm.yml", source_path=manifest_dir)
        except (FileNotFoundError, ValueError) as exc:
            # Missing or malformed manifest: this subtree's true children
            # are unknown. Do NOT treat it as childless -- record it and
            # let the caller preserve every candidate this run instead of
            # trusting a partial walk.
            unverifiable.append((visit_key, str(exc)))
            continue

        for child_ref in package.get_apm_dependencies():
            # manifest_dir anchors a LOCAL child's own relative local_path;
            # remote children ignore the anchor (they resolve via
            # get_install_path instead), so propagating it unconditionally
            # is safe and keeps this loop uniform for both source types.
            # ref.repo_url is this node's own identity, becoming the
            # recorded immediate parent for each child it declares.
            queue.append((child_ref, manifest_dir, ref.repo_url))

    return ReachabilityResult(
        reachable=frozenset(reachable),
        complete=not unverifiable,
        unverifiable=tuple(unverifiable),
        reachable_via=reachable_via,
    )
