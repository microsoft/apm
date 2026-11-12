"""Read-only absence observations for canonical deployment reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.deployment_state import CompletedReintegration
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector


def verified_absent_dependency_values(
    *,
    project_root: Path,
    completed: CompletedReintegration,
    prior_files: list[str],
    current_files: list[str],
    targets: list[TargetProfile],
    cleanup_retained_hashes: dict[str, str | None] | None,
    diagnostics: DiagnosticCollector,
    user_scope: bool,
) -> frozenset[str]:
    """Supply safe absence observations, not deletion or ownership authority."""
    from apm_cli.integration.base_integrator import BaseIntegrator

    processed = [target for target in targets if target.name in completed.targets]
    candidates = (
        set(prior_files)
        - set(current_files)
        - completed.current_values
        - (cleanup_retained_hashes or {}).keys()
    )
    absent = set()
    for path in candidates:
        if (
            "://" in path
            or Path(path).is_absolute()
            or not BaseIntegrator.validate_deploy_path(
                path, project_root, targets=processed, user_scope=user_scope
            )
        ):
            continue
        try:
            (project_root / path).lstat()
        except FileNotFoundError:
            absent.add(path)
        except OSError as exc:
            diagnostics.warn(
                f"Could not confirm whether {path} is absent; ownership retained. "
                "Check path permissions, then rerun apm install.",
                package=completed.owner,
                detail=str(exc),
            )
    return frozenset(absent)
