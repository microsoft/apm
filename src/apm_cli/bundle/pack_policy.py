"""Org dependency policy for ``apm pack``.

A pack ships the content of every package its lockfile records. Before it
does, the project's declared dependencies meet the install policy preflight
and every locked package meets ``dependencies.deny``, so a pack never moves
content that install, update or lock would refuse.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.dependency.reference import DependencyReference


def pack_policy_preflight(project_root: Path, logger: Any) -> None:
    """Refuse to pack when org policy refuses a package whose content would ship.

    A project with no locked packages ships only its own content and is not
    checked. Raises ``ValueError`` naming each refused package.
    """
    from ..core.command_logger import InstallLogger
    from ..deps.lockfile import resolve_lockfile_path_for_read
    from ..install.bundle_admission import locked_package_refs, project_registries
    from ..install.errors import PolicyViolationError
    from ..policy.install_preflight import run_policy_preflight

    bundled = locked_package_refs(resolve_lockfile_path_for_read(project_root, read_only=True))
    if not bundled:
        return
    try:
        run_policy_preflight(
            project_root=project_root,
            apm_deps=_declared_dependencies(project_root),
            logger=InstallLogger(verbose=bool(getattr(logger, "verbose", False)), command="pack"),
            registries=project_registries(project_root),
            bundled_deps=bundled,
        )
    except PolicyViolationError as exc:
        if exc.audit_result is None:
            raise ValueError(f"Cannot pack: {exc}") from exc
        refused = sorted(
            {
                subject.ref
                for check in exc.audit_result.failed_checks
                for subject in check.violation_subjects()
            }
        )
        raise ValueError(
            f"Pack blocked by org policy: {', '.join(refused)}. Nothing was packed."
        ) from exc


def _declared_dependencies(project_root: Path) -> list[DependencyReference]:
    """The project's direct declarations, as the install policy preflight judges them."""
    from ..core.scope import manifest_file
    from ..models.apm_package import APMPackage
    from ..models.dependency.replacement import project_manifest_dependencies

    manifest = manifest_file(project_root)
    if not manifest.is_file():
        return []
    return project_manifest_dependencies(APMPackage.from_apm_yml(manifest))
