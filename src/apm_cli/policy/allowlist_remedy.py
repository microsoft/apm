"""The ``apm uninstall`` next step of a failed ``dependency-allowlist`` check (#4570).

``apm audit --ci`` offers ``apm uninstall <entries>`` only when this module
proves that the uninstall clears every denied dependency without breaking
another policy rule:

* each entry is selected exactly by the uninstall selection owner and is a
  shell-safe ASCII word;
* a dependency the current apm.yml declares is removed directly, whatever
  depth its lock row recorded; a transitive one goes with the direct
  dependencies the lock traces it to;
* the uninstall orphan owner removes every denied transitive dependency and
  no surviving package still reaches a denied one (forward reachability);
* no removed entry, through its ``replace:`` source, satisfies a package the
  policy requires;
* uninstall's pre-mutation lockfile guard accepts ``apm.lock.yaml`` (not a
  symlink, not written by a newer APM);
* uninstall's path preflight accepts every recorded path it would remove;
* every deployed file the uninstall would delete has deployment evidence
  (a user-edited file makes uninstall refuse), judged by uninstall's own
  candidate and refusal owner;
* uninstall's survivor preflight resolves the project's org policy (it
  rediscovers it, whatever policy this audit was given).

The proof reads the live ``apm_modules/`` the suggested uninstall acts on,
never audit's scratch replay; without it the advice is admin-only, as is
anything else it cannot prove.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.scope import manifest_file

if TYPE_CHECKING:
    from ..deps.lockfile import LockFile
    from ..models.apm_package import APMPackage, DependencyReference
    from ..models.dependency.selection import ManifestSelectionIndex
    from .models import CheckResult
    from .schema import ApmPolicy, DependencyPolicy

# One shell word every supported shell passes through unchanged.
_SHELL_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*")


@dataclass(frozen=True)
class AllowlistProject:
    """The project state an uninstall proposal is proven against."""

    project_root: Path
    modules_dir: Path
    lock: LockFile | None
    production: tuple[object, ...]  # raw apm.yml dependencies.apm entries
    development: tuple[object, ...]  # raw apm.yml devDependencies.apm entries
    policy: DependencyPolicy
    # Unique key of each production declaration -> its replace:-effective ref,
    # the reference the required-packages check judges.
    effective: Mapping[str, DependencyReference]
    # Manifest fields the policy requires, and the keys of apm.yml's
    # devDependencies block (uninstall drops ``apm`` once it is empty).
    required_fields: frozenset[str] = frozenset()
    dev_section_keys: frozenset[str] = frozenset()
    # apm.yml's default_host, which scopes shorthand as uninstall parses it (#5007).
    manifest_host: Mapping[str, object] = field(default_factory=dict)


class AdviceWithheld(Exception):
    """An uninstall the proof would otherwise offer stops; *reason* says why (#5008)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def allowlist_project(
    project_root: Path,
    manifest: APMPackage,
    effective: Sequence[DependencyReference],
    lock: LockFile | None,
    raw_yml: dict | None,
    policy: ApmPolicy,
) -> AllowlistProject:
    """Collect the project state the allow-list next step is proven against.

    The proof reads the live ``apm_modules/`` the suggested uninstall acts on,
    never audit's scratch replay. *effective* is the ``replace:`` projection of
    the production declarations, in declaration order.
    """

    def _entries(section: str) -> tuple[object, ...]:
        block = (raw_yml or {}).get(section)
        entries = block.get("apm") if isinstance(block, dict) else None
        return tuple(entries) if isinstance(entries, list) else ()

    declared = manifest.get_apm_dependencies()
    dev_block = (raw_yml or {}).get("devDependencies")
    return AllowlistProject(
        project_root=project_root,
        modules_dir=project_root / "apm_modules",
        lock=lock,
        production=_entries("dependencies"),
        development=_entries("devDependencies"),
        policy=policy.dependencies,
        effective=(
            {dep.get_unique_key(): ref for dep, ref in zip(declared, effective, strict=True)}
            if len(declared) == len(effective)
            else {}
        ),
        required_fields=frozenset(policy.manifest.required_fields),
        dev_section_keys=frozenset(dev_block) if isinstance(dev_block, dict) else frozenset(),
        manifest_host={"default_host": (raw_yml or {}).get("default_host")},
    )


def with_allowlist_next_step(
    checks: list[CheckResult],
    source_deps: Sequence[DependencyReference],
    policy: ApmPolicy,
    policy_source: str | None,
    project_inputs: tuple,
) -> list[CheckResult]:
    """Attach the catalog next step to a failed ``dependency-allowlist`` check (#4570).

    Only under ``enforcement: block``; under warn the audit reports the
    violation without a next step. The ``apm uninstall`` option is offered
    only when :func:`uninstall_selectors` proves it clears every denied
    dependency; otherwise the advice is admin-only. *project_inputs* is
    ``(project_root, manifest, apm_deps, lock, raw_yml)``; *policy_source*
    (the discovered source, e.g. ``org:contoso/.github``) is named in it.
    """
    from dataclasses import replace

    from ..core.remediation import dependency_not_allowed_advice
    from .discovery import _redact_policy_ref
    from .matcher import dependency_policy_subject

    failed = next(
        (c for c in checks if c.name == "dependency-allowlist" and not c.passed and c.subjects),
        None,
    )
    if policy.enforcement != "block" or failed is None:
        return checks
    project = allowlist_project(*project_inputs, policy)
    refs = {subject.ref for subject in failed.subjects}
    denied = [dep for dep in source_deps if dependency_policy_subject(dep) in refs]
    try:
        selectors, withheld = uninstall_selectors(denied, project), None
    except AdviceWithheld as exc:
        selectors, withheld = [], exc.reason
    remedy, sentence = dependency_not_allowed_advice(
        selectors,
        len(failed.subjects),
        _redact_policy_ref(policy_source) if policy_source else None,
        withheld,
    )
    advised = replace(failed, next_step=sentence, remediations=(remedy,) if remedy else ())
    return [advised if check is failed else check for check in checks]


def uninstall_selectors(
    denied: Sequence[DependencyReference], project: AllowlistProject
) -> list[str]:
    """Return the ``apm uninstall`` arguments that clear *denied*, or ``[]`` if unproven.

    The proof parses declarations in apm.yml's ``default_host`` scope, as
    uninstall does (#5007). Raises :class:`AdviceWithheld` when an uninstall
    would stop for a reason worth naming.
    """
    from ..models.dependency.manifest_host import manifest_host_scope_for

    if project.lock is None or not denied or not project.modules_dir.is_dir():
        return []
    try:
        scope = manifest_host_scope_for(project.manifest_host)
    except ValueError:
        return []
    with scope:
        return _proven_selectors(denied, project)


def _proven_selectors(
    denied: Sequence[DependencyReference], project: AllowlistProject
) -> list[str]:
    from ..models.dependency.selection import ManifestSelectionIndex, parse_dependency_entry

    declared: dict[str, object] = {}
    for entry in (*project.production, *project.development):
        try:
            declared.setdefault(parse_dependency_entry(entry).get_unique_key(), entry)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    roots = _removal_roots(denied, declared, project.lock)
    if roots is None:
        return []
    selection = ManifestSelectionIndex([*project.production, *project.development], project.lock)
    selectors = [_selector(key, declared[key], selection) for key in roots]
    if (
        not all(selectors)
        or _removes_required(roots, declared, project)
        or _empties_required_dev_dependencies(roots, project)
    ):
        return []
    denied_keys = {dep.get_unique_key() for dep in denied}
    if not _uninstall_clears(denied_keys, [declared[key] for key in roots], project):
        return []
    return [selector for selector in selectors if selector]


def _removal_roots(
    denied: Sequence[DependencyReference], declared: dict[str, object], lock: LockFile
) -> list[str] | None:
    """Map each denied dependency to the declared entries whose removal drops it."""
    from ..integration.mcp_config_view import DirectDependencyIndex

    index = DirectDependencyIndex(lock)
    roots: dict[str, None] = {}
    for dep in denied:
        key = dep.get_unique_key()
        try:
            found = (key,) if key in declared else tuple(sorted(index.roots(key) or ()))
        except RecursionError:
            # A chain deeper than the interpreter's recursion limit: no advice.
            return None
        if not found or any(root not in declared for root in found):
            return None
        roots.update(dict.fromkeys(found))
    return list(roots)


def _selector(key: str, entry: object, selection: ManifestSelectionIndex) -> str | None:
    """Return a shell-safe identifier the uninstall selection maps to exactly *entry*."""
    from ..models.dependency.selection import DependencySelectionStatus, parse_dependency_entry

    candidates = (key, entry) if isinstance(entry, str) else (key,)
    for candidate in dict.fromkeys(candidates):
        if not _SHELL_SAFE.fullmatch(candidate):
            continue
        selected_entry = selection.select(candidate)
        if selected_entry.status is not DependencySelectionStatus.MATCHED:
            continue
        try:
            selected = parse_dependency_entry(selected_entry.manifest_entry).get_unique_key()
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if selected == key:
            return candidate
    return None


def _removes_required(
    roots: Sequence[str], declared: dict[str, object], project: AllowlistProject
) -> bool:
    """Whether removing *roots* drops a package the policy requires.

    Judged on each root's declared and ``replace:``-effective reference, as
    the required-packages check sees it.
    """
    policy = project.policy
    if not policy.effective_require:
        return False
    from ..models.dependency.selection import parse_dependency_entry
    from .matcher import DependencyPolicyIndex

    refs = [parse_dependency_entry(declared[key]) for key in roots]
    refs += [project.effective[key] for key in roots if key in project.effective]
    removed = DependencyPolicyIndex.from_dependencies(refs)
    return any(
        removed.find_name(required.split("#", 1)[0]) is not None
        for required in policy.effective_require
    )


def _empties_required_dev_dependencies(roots: Sequence[str], project: AllowlistProject) -> bool:
    """Whether removing *roots* leaves a policy-required ``devDependencies`` empty.

    Uninstall drops ``devDependencies.apm`` once its last entry goes, so the
    block is empty unless it holds another key; ``dependencies`` keeps
    ``apm: []`` and stays non-empty.
    """
    if "devDependencies" not in project.required_fields or not project.development:
        return False
    if project.dev_section_keys - {"apm"}:
        return False
    from ..models.dependency.selection import parse_dependency_entry

    removed = set(roots)
    for entry in project.development:
        try:
            if parse_dependency_entry(entry).get_unique_key() not in removed:
                return False
        except (ValueError, TypeError, AttributeError, KeyError):
            return False
    return True


def _uninstall_clears(
    denied_keys: set[str], removed: list[object], project: AllowlistProject
) -> bool:
    """Prove, through the orphan owner, that the uninstall drops every denied key.

    A denied transitive dependency must be among the uninstall's removed
    descendants. A denied entry removed directly must be one the surviving
    packages no longer reach (the owner's installed-path inspection).
    """
    from ..install.orphans import (
        InstalledPaths,
        OrphanOperation,
        ReachabilityProbe,
        decide_orphan_removal,
    )
    from ..utils.paths import portable_relpath

    lock = project.lock
    production, development = _survivors(removed, project)
    survivors = (*production, *development)
    roots = {key: ref for key, ref in _refs_by_key(removed).items() if key in denied_keys}
    # A removed entry the lock records as transitive is still another package's child.
    if any(getattr(lock.dependencies.get(key), "depth", None) != 1 for key in roots):
        return False
    probe = ReachabilityProbe(
        project_root=project.project_root,
        apm_modules_dir=project.modules_dir,
        direct_refs=tuple(production),
        dev_refs=tuple(development),
    )
    try:
        # A checkout outside apm_modules (a symlink) fails containment: no advice.
        paths = {
            key: portable_relpath(ref.get_install_path(project.modules_dir), project.modules_dir)
            for key, ref in roots.items()
        }
        transitive = decide_orphan_removal(
            operation=OrphanOperation.UNINSTALL,
            roots=removed,
            candidate_universe=lock,
            installed_paths=None,
            resolved_keys=None,
            intended=None,
            materialized=None,
            reachability=probe,
            deployment_claims=None,
        )
        direct = decide_orphan_removal(
            operation=OrphanOperation.INSPECT,
            roots=(),
            candidate_universe=lock,
            installed_paths=InstalledPaths(
                root=project.modules_dir, packages=frozenset(paths.values())
            ),
            resolved_keys=None,
            intended=survivors,
            materialized=None,
            reachability=ReachabilityProbe(
                project_root=project.project_root,
                apm_modules_dir=project.modules_dir,
                direct_refs=survivors,
                dev_refs=(),
            ),
            deployment_claims=None,
        )
    except (OSError, ValueError):
        return False
    if not set(paths.values()) <= set(direct.removed_paths):
        return False
    # A rescued root loses only its declaration: uninstall keeps its lock row
    # and subtree, so the denied dependency (or its descendant) stays.
    if transitive.rescued_roots or not denied_keys - set(roots) <= transitive.removed_keys:
        return False
    cleanup_keys = set(_refs_by_key(removed)) | set(transitive.removed_keys)
    linked = _final_symlinks(cleanup_keys, removed, project)
    if linked is None:
        return False
    if linked:
        raise AdviceWithheld(f"{linked[0]} is a symbolic link, which uninstall does not remove")
    return (
        _lockfile_writable(project)
        and _deployment_paths_validate(cleanup_keys, project)
        and _deletions_have_evidence(cleanup_keys, removed, project)
    ) and (_uninstall_policy_resolves(removed, project))


def _deployment_paths_validate(cleanup_keys: set[str], project: AllowlistProject) -> bool:
    """Whether uninstall's path preflight accepts every recorded path it would remove.

    ``apm uninstall`` stops before any change when the lock records a path APM
    does not manage (one outside the project, or not a deployable location).
    """
    from ..commands.uninstall.selection_shrink import uninstall_unvalidated_paths

    return not uninstall_unvalidated_paths(
        project.lock, cleanup_keys, project.project_root, user_scope=False
    )


def _final_symlinks(
    cleanup_keys: set[str], removed: list[object], project: AllowlistProject
) -> list[str] | None:
    """Final symlinks the uninstall would delete but cleanup keeps (#5008, #5035).

    Target cleanup keeps such a path, out of the project
    (``allow_final_symlink=False`` containment) or in it (a link carries no
    receipt or render evidence), so the uninstall would stop. Paths a
    shrinking survivor selection retires count too. A path a surviving
    dependency still claims is kept anyway and does not count, nor does a
    dangling in-project link, which cleanup counts as already gone, nor a
    retired-only hook link the hook sync unlinks.
    """
    from ..commands.uninstall.engine import project_shrunk_selections
    from ..commands.uninstall.selection_shrink import uninstall_deletion_candidates
    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..integration.cleanup import (
        final_link_handled,
        hook_sync_unlinks,
        managed_hook_prefixes,
    )

    root = project.project_root
    lock = project.lock
    try:
        shrunk = project_shrunk_selections(lock, removed, project.modules_dir, manifest_file(root))
        # Uninstall's own snapshot: canonical ledger hashes first, as the hook sync reads them.
        owners = set(cleanup_keys) | set(shrunk)
        hashes = DeploymentLedgerCodec.cleanup_snapshot(lock, owners).hashes
        # Removed owners' files go through stale cleanup; retired-only ones through the syncs.
        removed_paths = set(uninstall_deletion_candidates(lock, cleanup_keys))
        hook_prefixes = managed_hook_prefixes()

        def handled(path: str) -> bool:
            # The cleanup route decides first: the hook sync keeps its own absence rule.
            if path not in removed_paths and path.startswith(hook_prefixes):
                # The advised uninstall carries no --trust-lockfile consent.
                return hook_sync_unlinks(
                    path, root, recorded_hash=hashes.get(path), lockfile_consent=False
                )
            return final_link_handled(path, root)

        return [
            path
            for path in uninstall_deletion_candidates(lock, cleanup_keys, shrunk=shrunk)
            if "://" not in path and (root / path).is_symlink() and not handled(path)
        ]
    except (OSError, ValueError, RuntimeError):
        # An unreadable or looping link proves nothing: the advice is admin-only.
        return None


def _deletions_have_evidence(
    cleanup_keys: set[str], removed: list[object], project: AllowlistProject
) -> bool:
    """Whether uninstall's deployment-evidence preflight would let these deletions through.

    ``apm uninstall`` refuses before any change when a deployed file it would
    delete (one the user edited, including a skill a shrinking survivor
    selection retires) has no deployment evidence; that uninstall would leave
    the audit red, so its advice is not offered. The candidates and the
    refusal come from uninstall's own owner.
    """
    from ..commands.uninstall.engine import project_shrunk_selections
    from ..commands.uninstall.selection_shrink import (
        uninstall_deletion_candidates,
        uninstall_evidence_refusals,
    )
    from ..integration.cleanup import DeploymentEvidence
    from ..utils.diagnostics import DiagnosticCollector

    lock = project.lock
    try:
        shrunk = project_shrunk_selections(
            lock, removed, project.modules_dir, manifest_file(project.project_root)
        )
        evidence = DeploymentEvidence.collect(
            project.project_root,
            user_scope=False,
            lockfile=lock,
            lockfile_root=project.project_root,
            diagnostics=DiagnosticCollector(),
            kept=(),
        )
        evidence.prepare(uninstall_deletion_candidates(lock, cleanup_keys, shrunk=shrunk))
        return not uninstall_evidence_refusals(evidence, lock, cleanup_keys, shrunk=shrunk)
    except (OSError, ValueError):
        return False


def _uninstall_policy_resolves(removed: list[object], project: AllowlistProject) -> bool:
    """Whether uninstall's survivor preflight resolves the org policy it reintegrates under.

    With surviving dependencies, ``apm uninstall`` rediscovers the project's
    org policy (``resolve_project_exec_allow_map``) and aborts before removing
    anything when the outcome blocks (for example
    ``policy.fetch_failure_default: block`` with no reachable policy),
    whatever policy this audit was given. Only the policy-outcome owner that
    raises that refusal is consulted: the audit never reads or migrates the
    user's executable consent.
    """
    if not any(_survivors(removed, project)):
        return True
    from ..core.scope import manifest_root
    from ..install.errors import PolicyViolationError
    from .outcome_routing import resolve_trust_policy

    try:
        resolve_trust_policy(manifest_root(project.project_root, user_scope=False), announce=False)
    except PolicyViolationError:
        return False
    return True


def _survivors(
    removed: list[object], project: AllowlistProject
) -> tuple[list[DependencyReference], list[DependencyReference]]:
    """The direct dependencies that survive removing *removed*, per section.

    Uninstall's own survivor projection, so the proof and the command agree.
    """
    from ..commands.uninstall.local_survivors import read_survivor_direct_refs

    return read_survivor_direct_refs(manifest_file(project.project_root), removed)


def _lockfile_writable(project: AllowlistProject) -> bool:
    """Whether uninstall's pre-mutation lockfile guard lets it rewrite the lock.

    ``apm uninstall`` refuses before any change when ``apm.lock.yaml`` is a
    symlink or was written by a newer APM.
    """
    from ..deps.lockfile import LockfileRefusal, get_lockfile_path, refuse_lockfile_before_mutation

    try:
        refuse_lockfile_before_mutation(
            project.lock, get_lockfile_path(project.project_root), would_mutate=True
        )
    except LockfileRefusal:
        return False
    return True


def _refs_by_key(entries: list[object]) -> dict[str, DependencyReference]:
    from ..models.dependency.selection import parse_dependency_entry

    refs = map(parse_dependency_entry, entries)
    return {ref.get_unique_key(): ref for ref in refs}
