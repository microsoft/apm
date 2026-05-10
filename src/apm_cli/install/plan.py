"""Update plan: structured diff between current lockfile and fresh resolution.

This module is the support library for the ``apm update`` command (#1203).
It also provides the structural-satisfaction check used by
``apm install --frozen``.

Two responsibilities:

1. ``build_update_plan`` -- pure comparison of an old :class:`LockFile`
   against a list of freshly-resolved :class:`DependencyReference`
   objects (post-resolve, pre-download).  Produces an
   :class:`UpdatePlan` of immutable :class:`PlanEntry` records, each
   capturing one dependency's before/after state.

2. ``render_plan_text`` -- ASCII rendering of an :class:`UpdatePlan`
   suitable for terminal display, using the bracket-status convention
   from ``.github/instructions/encoding.instructions.md``.

3. ``lockfile_satisfies_manifest`` -- structural check: does a lockfile
   carry an entry for every direct dependency declared in the manifest?
   Used to enforce ``--frozen`` without running the resolve phase.

Design notes
------------
* No I/O.  No network.  Every function in this module is pure --
  testable in isolation by feeding fixture lockfiles and dep lists.
* Frozen dataclasses (``PlanEntry``, ``UpdatePlan``) so callers can
  freely pass them across phase boundaries without aliasing risk.
* The ``deployed_files`` shown in a plan entry are taken from the
  EXISTING lockfile (i.e. what is currently on disk).  We do not yet
  know the post-update file list at the plan checkpoint, since
  integration has not run.  Showing the "files at risk" surface is
  honest enough for P0; a richer post-download diff is a P1+
  enhancement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apm_cli.utils.console import STATUS_SYMBOLS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.models.dependency.reference import DependencyReference


_ACTION_UPDATE = "update"
_ACTION_ADD = "add"
_ACTION_REMOVE = "remove"
_ACTION_UNCHANGED = "unchanged"


def _dep_ref_key(dep: DependencyReference) -> str:
    """Compute the unique key for a manifest dependency.

    Mirrors :meth:`LockedDependency.get_unique_key` so that manifest and
    lockfile entries can be matched 1:1 without round-tripping through
    a full resolution.

    Local refs use ``local_path``; virtual subdirectory refs use
    ``repo_url/virtual_path``; everything else is keyed on ``repo_url``.
    """
    if getattr(dep, "is_local", False) and dep.local_path:
        return dep.local_path
    if getattr(dep, "is_virtual", False) and dep.virtual_path:
        return f"{dep.repo_url}/{dep.virtual_path}"
    return dep.repo_url


def _short_sha(commit: str | None, length: int = 7) -> str:
    """Render a commit SHA (or placeholder) in short form.

    Returns ``-`` when no commit is available, so the diff shows a
    visible delta even on the "before" or "after" side of an
    add / remove entry.
    """
    if not commit:
        return "-"
    return commit[:length]


@dataclass(frozen=True)
class PlanEntry:
    """One dependency's before/after state in an :class:`UpdatePlan`.

    ``action`` is one of ``"update"``, ``"add"``, ``"remove"``, or
    ``"unchanged"`` -- mutually exclusive.  Callers should use the
    ``has_changes`` property rather than comparing strings.

    ``deployed_files`` reflects the existing lockfile only -- see module
    docstring for rationale.
    """

    dep_key: str
    action: str
    display_name: str = ""

    old_resolved_ref: str | None = None
    old_resolved_commit: str | None = None
    old_content_hash: str | None = None

    new_resolved_ref: str | None = None
    new_resolved_commit: str | None = None

    deployed_files: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return self.action != _ACTION_UNCHANGED

    @property
    def short_old_commit(self) -> str:
        return _short_sha(self.old_resolved_commit)

    @property
    def short_new_commit(self) -> str:
        return _short_sha(self.new_resolved_commit)


@dataclass(frozen=True)
class UpdatePlan:
    """Structured diff between current lockfile and a fresh resolution."""

    entries: tuple[PlanEntry, ...] = ()

    @property
    def has_changes(self) -> bool:
        return any(e.has_changes for e in self.entries)

    @property
    def changed_entries(self) -> tuple[PlanEntry, ...]:
        return tuple(e for e in self.entries if e.has_changes)

    @property
    def summary_counts(self) -> dict[str, int]:
        counts = {
            _ACTION_UPDATE: 0,
            _ACTION_ADD: 0,
            _ACTION_REMOVE: 0,
            _ACTION_UNCHANGED: 0,
        }
        for e in self.entries:
            counts[e.action] = counts.get(e.action, 0) + 1
        return counts


def _display_name(dep_key: str, locked: LockedDependency | None) -> str:
    """Pick a short, human-friendly label for a dep entry.

    Prefers ``repo_url`` (with ``virtual_path`` suffix when present)
    from the locked record, since the manifest reference may be a bare
    shorthand without the resolved host.
    """
    if locked is not None:
        name = locked.repo_url
        if getattr(locked, "virtual_path", None):
            name = f"{name}/{locked.virtual_path}"
        return name
    return dep_key


def build_update_plan(
    old_lockfile: LockFile | None,
    resolved_deps: Iterable[DependencyReference],
) -> UpdatePlan:
    """Compare an existing lockfile against freshly-resolved deps.

    Args:
        old_lockfile: Current on-disk lockfile, or None when the project
            has never been installed before.
        resolved_deps: Output of the resolve phase -- each
            ``DependencyReference`` carries a ``resolved_reference``
            populated by the resolver.  Typically
            ``InstallContext.deps_to_install``.

    Returns:
        A frozen :class:`UpdatePlan` summarising the diff.
    """
    old_entries: dict[str, LockedDependency] = {}
    if old_lockfile is not None:
        from apm_cli.deps.lockfile import _SELF_KEY

        old_entries = {
            key: dep for key, dep in old_lockfile.dependencies.items() if key != _SELF_KEY
        }

    seen_keys: set[str] = set()
    plan_entries: list[PlanEntry] = []

    for dep in resolved_deps:
        key = _dep_ref_key(dep)
        seen_keys.add(key)
        old = old_entries.get(key)
        new_ref, new_commit = _extract_new_ref_and_commit(dep)

        if old is None:
            plan_entries.append(
                PlanEntry(
                    dep_key=key,
                    action=_ACTION_ADD,
                    display_name=_display_name(key, None) or dep.repo_url,
                    new_resolved_ref=new_ref,
                    new_resolved_commit=new_commit,
                )
            )
            continue

        old_ref = old.resolved_ref
        old_commit = old.resolved_commit
        deployed = tuple(old.deployed_files)

        if (old_commit or None) == (new_commit or None) and (old_ref or None) == (new_ref or None):
            plan_entries.append(
                PlanEntry(
                    dep_key=key,
                    action=_ACTION_UNCHANGED,
                    display_name=_display_name(key, old),
                    old_resolved_ref=old_ref,
                    old_resolved_commit=old_commit,
                    old_content_hash=old.content_hash,
                    new_resolved_ref=new_ref,
                    new_resolved_commit=new_commit,
                    deployed_files=deployed,
                )
            )
            continue

        plan_entries.append(
            PlanEntry(
                dep_key=key,
                action=_ACTION_UPDATE,
                display_name=_display_name(key, old),
                old_resolved_ref=old_ref,
                old_resolved_commit=old_commit,
                old_content_hash=old.content_hash,
                new_resolved_ref=new_ref,
                new_resolved_commit=new_commit,
                deployed_files=deployed,
            )
        )

    for key, old in old_entries.items():
        if key in seen_keys:
            continue
        plan_entries.append(
            PlanEntry(
                dep_key=key,
                action=_ACTION_REMOVE,
                display_name=_display_name(key, old),
                old_resolved_ref=old.resolved_ref,
                old_resolved_commit=old.resolved_commit,
                old_content_hash=old.content_hash,
                deployed_files=tuple(old.deployed_files),
            )
        )

    plan_entries.sort(key=lambda e: (_ACTION_ORDER.get(e.action, 99), e.display_name or e.dep_key))
    return UpdatePlan(entries=tuple(plan_entries))


_ACTION_ORDER = {
    _ACTION_UPDATE: 0,
    _ACTION_ADD: 1,
    _ACTION_REMOVE: 2,
    _ACTION_UNCHANGED: 3,
}


def _extract_new_ref_and_commit(dep: DependencyReference) -> tuple[str | None, str | None]:
    """Pull ``(resolved_ref, resolved_commit)`` from a resolved dep.

    ``DependencyReference`` carries an optional ``resolved_reference``
    (a :class:`ResolvedReference`) that the resolve phase populates.
    Both halves are optional; either may be ``None`` if resolution did
    not yield that field.
    """
    resolved = getattr(dep, "resolved_reference", None)
    if resolved is None:
        return (getattr(dep, "reference", None), None)
    new_ref = (
        getattr(resolved, "ref_name", None)
        or getattr(resolved, "original_ref", None)
        or getattr(dep, "reference", None)
    )
    new_commit = getattr(resolved, "resolved_commit", None)
    return (new_ref, new_commit)


def _format_ref_change(entry: PlanEntry) -> str:
    """Render the ref/commit transition for a single :class:`PlanEntry`.

    Examples (ASCII only):
        ``main (abc1234 -> def5678)``
        ``v1.0.0 (new)``
        ``main (abc1234 removed)``
    """
    if entry.action == _ACTION_ADD:
        ref = entry.new_resolved_ref or "-"
        commit = entry.short_new_commit
        return f"{ref} ({commit}, new)"
    if entry.action == _ACTION_REMOVE:
        ref = entry.old_resolved_ref or "-"
        commit = entry.short_old_commit
        return f"{ref} ({commit}, removed)"
    old_ref = entry.old_resolved_ref or "-"
    new_ref = entry.new_resolved_ref or old_ref
    ref_part = old_ref if old_ref == new_ref else f"{old_ref} -> {new_ref}"
    return f"{ref_part} ({entry.short_old_commit} -> {entry.short_new_commit})"


def render_plan_text(plan: UpdatePlan, *, verbose: bool = False) -> str:
    """Render an :class:`UpdatePlan` as ASCII terminal output.

    Empty string when ``plan.has_changes`` is False (callers display
    a higher-level "already up to date" message instead).

    Bracket-status symbols:
        ``[~]`` updated
        ``[+]`` added
        ``[-]`` removed
        ``[=]`` unchanged (verbose only)

    The output never includes a trailing newline; callers append one if
    needed.
    """
    if not plan.has_changes and not verbose:
        return ""

    lines: list[str] = ["[i] Update plan for apm.yml", ""]
    for entry in plan.entries:
        if entry.action == _ACTION_UNCHANGED and not verbose:
            continue
        symbol = _ACTION_SYMBOLS.get(entry.action, "[?]")
        lines.append(f"  {symbol} {entry.display_name}")
        lines.append(f"      ref: {_format_ref_change(entry)}")
        if entry.deployed_files:
            preview = ", ".join(entry.deployed_files[:3])
            if len(entry.deployed_files) > 3:
                preview += f", +{len(entry.deployed_files) - 3} more"
            lines.append(f"      files: {preview}")
        lines.append("")

    counts = plan.summary_counts
    summary_parts = []
    if counts.get(_ACTION_UPDATE):
        summary_parts.append(f"{counts[_ACTION_UPDATE]} updated")
    if counts.get(_ACTION_ADD):
        summary_parts.append(f"{counts[_ACTION_ADD]} added")
    if counts.get(_ACTION_REMOVE):
        summary_parts.append(f"{counts[_ACTION_REMOVE]} removed")
    if verbose and counts.get(_ACTION_UNCHANGED):
        summary_parts.append(f"{counts[_ACTION_UNCHANGED]} unchanged")
    if summary_parts:
        lines.append("  " + ", ".join(summary_parts))
        legend_bits = []
        if counts.get(_ACTION_UPDATE):
            legend_bits.append(f"{_ACTION_SYMBOLS[_ACTION_UPDATE]} updated")
        if counts.get(_ACTION_ADD):
            legend_bits.append(f"{_ACTION_SYMBOLS[_ACTION_ADD]} added")
        if counts.get(_ACTION_REMOVE):
            legend_bits.append(f"{_ACTION_SYMBOLS[_ACTION_REMOVE]} removed")
        if verbose and counts.get(_ACTION_UNCHANGED):
            legend_bits.append(f"{_ACTION_SYMBOLS[_ACTION_UNCHANGED]} unchanged")
        if legend_bits:
            lines.append("  " + "  ".join(legend_bits))

    return "\n".join(lines).rstrip()


_ACTION_SYMBOLS = {
    _ACTION_UPDATE: STATUS_SYMBOLS["update"],
    _ACTION_ADD: STATUS_SYMBOLS["check"],
    _ACTION_REMOVE: STATUS_SYMBOLS["remove"],
    _ACTION_UNCHANGED: STATUS_SYMBOLS["equal"],
}


def lockfile_satisfies_manifest(
    lockfile: LockFile,
    manifest_deps: Iterable[DependencyReference],
) -> tuple[bool, list[str]]:
    """Structural satisfaction check for ``apm install --frozen``.

    Verifies that every direct dependency declared in the manifest has
    a corresponding entry in the lockfile.  Does NOT perform any
    resolution or compare resolved refs against the remote -- those are
    ``apm update``'s job.

    Args:
        lockfile: The on-disk lockfile.
        manifest_deps: Direct deps from the apm.yml manifest (regular +
            dev).  Local deps are skipped as they have no remote ref to
            satisfy.

    Returns:
        ``(satisfied, reasons)`` -- ``reasons`` is a list of
        human-readable strings explaining each mismatch, empty when
        ``satisfied`` is True.
    """
    from apm_cli.deps.lockfile import _SELF_KEY

    locked_keys = {key for key in lockfile.dependencies if key != _SELF_KEY}

    reasons: list[str] = []
    for dep in manifest_deps:
        if getattr(dep, "is_local", False):
            continue
        key = _dep_ref_key(dep)
        if key not in locked_keys:
            reasons.append(f"  - {key} is declared in apm.yml but missing from apm.lock.yaml")

    return (not reasons, reasons)


__all__ = [
    "PlanEntry",
    "UpdatePlan",
    "build_update_plan",
    "lockfile_satisfies_manifest",
    "render_plan_text",
]
