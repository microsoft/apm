"""Read-only target resolution and scratch deployment roots for audit."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import click
import yaml

from apm_cli.integration.targets import KNOWN_TARGETS, TargetProfile, resolve_targets
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

_EXTERNAL_REPLAY_ROOT = ".apm-audit-targets"

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile


class AuditTargetError(ValueError):
    """Current audit target intent cannot be resolved safely."""


def resolve_audit_targets(
    project_root: Path, *, user_scope: bool = False
) -> tuple[TargetProfile, ...]:
    """Adapt the canonical current-intent decision to read-only audit profiles."""
    from apm_cli.core.apm_yml import read_declared_target_names
    from apm_cli.core.target_detection import resolve_effective_target_decision

    try:
        decision = resolve_effective_target_decision(
            project_root,
            explicit_target=None,
            manifest_target=read_declared_target_names(project_root),
            user_scope=user_scope,
            auto_detect=False,
            create_config=False,
            strict_config=True,
        )
        selected_targets = decision.canonical_targets
        targets = resolve_targets(
            project_root,
            user_scope=user_scope,
            explicit_target=list(selected_targets) if selected_targets is not None else None,
            create_config=False,
        )
    except (OSError, ValueError, yaml.YAMLError, click.ClickException) as exc:
        raise AuditTargetError(str(exc)) from exc

    unavailable = set(selected_targets or ()) - {target.name for target in targets}
    if unavailable:
        names = ", ".join(sorted(unavailable))
        raise AuditTargetError(
            f"Cannot audit selected target(s): {names}. Restore their experimental/runtime "
            "prerequisites for this scope, or correct apm.yml / 'apm config set target'."
        )
    return tuple(targets)


def _require_filesystem_replay(target: TargetProfile) -> None:
    """Native runtime records without a filesystem decoder cannot use scratch."""
    if target.external_locator_encoder is not None and target.external_locator_decoder is None:
        raise AuditTargetError(
            f"Target '{target.name}' has no isolated filesystem scratch replay backend. "
            "Audit cannot safely replay this native runtime; live state was not modified."
        )


def replay_target(target: TargetProfile, scratch_root: Path) -> TargetProfile:
    """Return a scratch-contained profile for replay-only integration."""
    _require_filesystem_replay(target)
    if target.managed_deploy_root is None:
        return target
    return replace(
        target,
        root_dir=f"{_EXTERNAL_REPLAY_ROOT}/{target.name}",
        resolved_deploy_root=(
            external_replay_root(scratch_root, target)
            if target.resolved_deploy_root is not None
            else None
        ),
    )


def audit_comparison_targets(
    lockfile: LockFile, selected: tuple[TargetProfile, ...], *, user_scope: bool
) -> tuple[TargetProfile, ...]:
    """Retain recorded native roots for comparison without authorizing replay."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    names = {record.locator.target for record in ledger.records.values()}
    claims = DeploymentLedgerCodec.legacy_deployed_file_claims(lockfile)
    result = {target.name: target for target in selected}
    for name, profile in KNOWN_TARGETS.items():
        if name in result or not (
            name in names or any(path.startswith(profile.lockfile_uri_schemes) for path in claims)
        ):
            continue
        _require_filesystem_replay(profile)
        try:
            scoped = profile.for_scope(user_scope=user_scope)
        except (OSError, ValueError) as exc:
            raise AuditTargetError(f"Cannot compare recorded target '{name}': {exc}") from exc
        if scoped is None or (profile.lockfile_uri_schemes and scoped.managed_deploy_root is None):
            raise AuditTargetError(
                f"Cannot compare recorded target '{name}': its deployment root is unavailable. "
                "Restore the runtime root or reconcile the recorded ownership."
            )
        if scoped.managed_deploy_root is not None:
            result[name] = scoped
    return tuple(result.values())


def external_replay_root(scratch_root: Path, target: TargetProfile) -> Path:
    """Return the scratch projection root for an external target."""
    return scratch_root / _EXTERNAL_REPLAY_ROOT / target.name


def external_target_relative_roots(target: TargetProfile) -> set[str]:
    """Return bounded paths governed below an external target root."""
    roots: set[str] = set()
    if "skills" in target.primitives:
        from apm_cli.integration.skill_integrator import SkillIntegrator

        deploy_root = target.managed_deploy_root
        if deploy_root is not None:
            skills_root = SkillIntegrator._target_skills_root(target, deploy_root)
            roots.add(
                ensure_path_within(skills_root, deploy_root).relative_to(deploy_root).as_posix()
            )
    for mapping in target.primitives.values():
        if mapping.deploy_root:
            deploy_root = Path(mapping.deploy_root)
            if not deploy_root.is_absolute():
                roots.add(deploy_root.parts[0])
                continue
        if mapping.subdir:
            roots.add(Path(mapping.subdir).parts[0])
        elif mapping.extension:
            roots.add(mapping.extension.lstrip("/"))
    roots.update(Path(path).parts[0] for path in target.generated_files if Path(path).parts)
    return roots


def claims_for_root(
    claims: dict[str, str],
    root: Path,
    *,
    absolute_only: bool,
    targets: tuple[TargetProfile, ...] = (),
) -> dict[str, str]:
    """Rebase lock claims governed by *root* into comparison-relative paths."""
    root = root.resolve()
    rebased: dict[str, str] = {}
    for path, owner in claims.items():
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                validated = ensure_path_within(candidate, root)
                relative = validated.relative_to(root)
            except (PathTraversalError, ValueError):
                continue
            rebased[relative.as_posix()] = owner
        elif absolute_only:
            for target in targets:
                try:
                    decoded = target.decode_external_locator(path, root)
                except (PathTraversalError, ValueError):
                    continue
                if decoded is None:
                    continue
                validated = ensure_path_within(decoded, root)
                rebased[validated.relative_to(root).as_posix()] = owner
                break
        elif not absolute_only:
            rebased[path] = owner
    return rebased
