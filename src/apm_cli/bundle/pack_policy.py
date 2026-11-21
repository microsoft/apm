"""Org dependency policy for ``apm pack``.

A pack ships the content of every package its lockfile records. Before it
does, the project's declared dependencies and every locked package meet the
org ``dependencies.deny`` list, so a pack never moves content of a package
install, update or lock would refuse. Allow, require and pin rules judge what
a project installs, not what it packs, so they do not apply here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.dependency.reference import DependencyReference


def pack_policy_preflight(project_root: Path, logger: Any) -> list[str]:
    """Refuse to pack when org policy denies a package whose content would ship.

    A project with no declared or locked package ships only its own content
    and is not checked. Raises ``ValueError`` naming each denied package
    under ``enforcement: block``; under ``warn`` returns one pack warning per
    denied package, which the pack result carries and reports.
    """
    from ..core.command_logger import InstallLogger
    from ..deps.lockfile import resolve_lockfile_path_for_read
    from ..install.bundle_admission import locked_package_refs
    from ..install.errors import PolicyViolationError
    from ..policy.install_preflight import DenyOnlySubjects, run_policy_preflight

    declared = _declared_dependencies(project_root)
    bundled = locked_package_refs(resolve_lockfile_path_for_read(project_root, read_only=True))
    if not declared and not bundled:
        return []
    policy_logger = InstallLogger(verbose=bool(getattr(logger, "verbose", False)), command="pack")
    try:
        run_policy_preflight(
            project_root=project_root,
            apm_deps=declared,
            logger=policy_logger,
            bundled_deps=DenyOnlySubjects(tuple(bundled)),
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
    from ..utils.diagnostics import CATEGORY_POLICY

    return [
        f"Policy warning: {item.package} -- {item.message}. It is packed because org "
        "policy enforcement is warn."
        for item in policy_logger.diagnostics.by_category().get(CATEGORY_POLICY, [])
    ]


def _declared_dependencies(project_root: Path) -> list[DependencyReference]:
    """The project's direct declarations, as the install policy preflight judges them."""
    from ..core.scope import manifest_file
    from ..models.apm_package import APMPackage
    from ..models.dependency.replacement import project_manifest_dependencies

    manifest = manifest_file(project_root)
    if not manifest.is_file():
        return []
    return project_manifest_dependencies(APMPackage.from_apm_yml(manifest))
