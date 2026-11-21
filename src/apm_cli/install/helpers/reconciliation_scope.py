"""Catalog-backed target scope for manifest reconciliation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.deployment_state import DeploymentLedger
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector


def _profiles_by_name(
    targets: Iterable[TargetProfile] | None,
) -> dict[str, TargetProfile]:
    """Return profiles keyed by catalog target name."""
    by_name: dict[str, TargetProfile] = {}
    for target in targets or ():
        name = getattr(target, "name", None)
        if isinstance(name, str) and name not in by_name:
            by_name[name] = target
    return by_name


def _is_manifest_target(name: str) -> bool:
    """Return whether *name* can be declared in ``apm.yml`` ``targets:``.

    Every other profile in the declared universe is supplemental, included
    only so its rows are not dropped as ghosts.
    """
    from apm_cli.core.apm_yml import CANONICAL_TARGETS

    return name in CANONICAL_TARGETS


def _has_gated_resolver(profile: TargetProfile) -> bool:
    """Return whether an inactive supplemental target must not resolve."""
    return profile.requires_flag is not None and profile.user_root_resolver is not None


def _record_inactive_resolver_skip(
    profile: TargetProfile,
    diagnostics: DiagnosticCollector | None,
) -> None:
    """Record the intentional skip of an inactive experimental resolver.

    Verbose only: the skip is expected for every user who has not opted in.
    """
    if diagnostics is None or not diagnostics.verbose:
        return
    from apm_cli.utils.diagnostics import CATEGORY_INFO

    message = (
        f"Skipped inactive experimental resolver for target '{profile.name}' "
        "during lockfile reconciliation."
    )
    if any(
        diagnostic.message == message
        for diagnostic in diagnostics.by_category().get(CATEGORY_INFO, ())
    ):
        return
    flag = profile.requires_flag or profile.name
    diagnostics.info(
        message,
        detail=(
            f"To include it, enable '{flag.replace('_', '-')}' and select "
            f"target '{profile.name}' for this install."
        ),
    )


def _scoped_known_targets_for_reconciliation(
    *,
    user_scope: bool,
    active_targets: Iterable[TargetProfile],
    declared_targets: Iterable[TargetProfile] | None,
) -> dict[str, TargetProfile]:
    """Return known targets without running inactive experimental resolvers."""
    from apm_cli.integration.targets import KNOWN_TARGETS, scoped_profile_or_none

    active_by_name = _profiles_by_name(active_targets)
    declared_by_name = _profiles_by_name(declared_targets)
    scoped_known_targets: dict[str, TargetProfile] = {}
    for name, profile in KNOWN_TARGETS.items():
        resolved = active_by_name.get(name) or declared_by_name.get(name)
        if resolved is not None:
            scoped_known_targets[name] = resolved
            continue
        if _has_gated_resolver(profile):
            scoped_known_targets[name] = profile
            continue
        scoped = scoped_profile_or_none(profile, user_scope=user_scope)
        if scoped is not None:
            scoped_known_targets[name] = scoped
    return scoped_known_targets


def _retain_dropped(
    files: list[str],
    hashes: dict[str, str],
    ledger: DeploymentLedger,
    paths: set[str],
    prior_hashes: dict[str, str],
    prior_ledger: DeploymentLedger | None,
) -> DeploymentLedger:
    """Keep *paths* in the returned rows, hashes and ledger instead of pruning them."""
    for path in paths:
        if path not in files:
            files.append(path)
        if path in prior_hashes:
            hashes[path] = prior_hashes[path]
    if prior_ledger is None:
        return ledger
    from apm_cli.core.deployment_state import DeploymentLedger

    records = dict(ledger.records)
    records.update(
        {
            key: record
            for key, record in prior_ledger.records.items()
            if record.locator.value in paths
        }
    )
    return DeploymentLedger(records=records)


def _skills_root(profile: TargetProfile) -> str | None:
    """Return the profile's skills root, or None when it deploys no skills."""
    if "skills" not in (getattr(profile, "primitives", None) or {}):
        return None
    return getattr(profile, "skills_rel_root", None)


def report_stale_kept_skill_copies(
    *,
    kept: Iterable[str],
    current_hashes: Mapping[str, str],
    prior_hashes: Mapping[str, str],
    active_targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None,
    user_scope: bool,
    diagnostics: DiagnosticCollector,
) -> None:
    """Name each kept inactive-target skill copy whose bytes this install no longer renders.

    A ``--target``-narrowed install preserves another target's recorded copy
    without writing it. When the same skill was rendered into an active target
    with different member bytes (or without a kept member), that copy is stale;
    say so with the command that refreshes it instead of keeping it silently (#4692).
    """
    active_names = [target.name for target in active_targets]
    active_roots = {root for target in active_targets if (root := _skills_root(target))}
    rendered: dict[str, str] = {}
    for path, digest in current_hashes.items():
        for root in active_roots:
            if path.startswith(f"{root}/"):
                rendered[path[len(root) + 1 :]] = digest
    rendered_skills = {member.split("/", 1)[0] for member in rendered}
    inactive_roots = {
        root: name
        for name, profile in _scoped_known_targets_for_reconciliation(
            user_scope=user_scope, active_targets=active_targets, declared_targets=declared_targets
        ).items()
        if name not in active_names and (root := _skills_root(profile)) and root not in active_roots
    }
    stale: dict[tuple[str, str], str] = {}
    for path in kept:
        for root, name in inactive_roots.items():
            if not path.startswith(f"{root}/") or path not in prior_hashes:
                continue
            member = path[len(root) + 1 :]
            skill = member.split("/", 1)[0]
            if skill in rendered_skills and rendered.get(member) != prior_hashes[path]:
                stale[(root, skill)] = name
    targets = ",".join(dict.fromkeys([*active_names, *sorted(set(stale.values()))]))
    command = f"apm install{' -g' if user_scope else ''} --target {targets}"
    for (root, skill), name in sorted(stale.items()):
        location = f"{root}/{skill}"
        if user_scope and not PurePath(root).is_absolute():
            location = f"~/{location}"
        diagnostics.warn(
            f"Kept stale skill copy {location}: target '{name}' is not part of this "
            f"install, so it was not refreshed.\n    Run '{command}' to refresh it."
        )
