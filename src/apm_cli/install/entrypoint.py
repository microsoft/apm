"""Programmatic entry point for dependency installation."""

# This compatibility adapter intentionally mirrors the pipeline's request fields.
# pylint: disable=duplicate-code

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable  # noqa: UP035

if TYPE_CHECKING:
    from apm_cli.core.auth import AuthResolver
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.install.plan import UpdatePlan
    from apm_cli.install.transaction import InstallTransaction
    from apm_cli.models.apm_package import APMPackage


def install_apm_dependencies(  # noqa: PLR0913
    apm_package: APMPackage,
    update_refs: bool = False,
    verbose: bool = False,
    only_packages: list[str] | None = None,
    force: bool = False,
    parallel_downloads: int = 4,
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    auth_resolver: AuthResolver | None = None,
    target: str | list[str] | None = None,
    target_decision: EffectiveTargetDecision | None = None,
    allow_insecure: bool = False,
    allow_insecure_hosts: tuple[str, ...] = (),
    marketplace_provenance: dict[str, Any] | None = None,
    protocol_pref: Any = None,
    allow_protocol_fallback: bool | None = None,
    no_policy: bool = False,
    audit_override: str | None = None,
    agent_subset: tuple[str, ...] | None = None,
    agent_subset_from_cli: bool = False,
    skill_subset: tuple[str, ...] | None = None,
    skill_subset_from_cli: bool = False,
    legacy_skill_paths: bool = False,
    trust_transitive_mcp: bool = False,
    frozen: bool = False,
    plan_callback: Callable[[UpdatePlan], bool] | None = None,
    refresh: bool = False,
    lockfile_only: bool = False,
    transaction: InstallTransaction | None = None,
):
    """Build an install request and delegate it to the application service."""
    from apm_cli.install.request import InstallRequest
    from apm_cli.install.service import InstallService

    request = InstallRequest(
        apm_package=apm_package,
        update_refs=update_refs,
        verbose=verbose,
        only_packages=only_packages,
        force=force,
        parallel_downloads=parallel_downloads,
        logger=logger,
        scope=scope,
        auth_resolver=auth_resolver,
        target=target,
        target_decision=target_decision,
        allow_insecure=allow_insecure,
        allow_insecure_hosts=allow_insecure_hosts,
        marketplace_provenance=marketplace_provenance,
        protocol_pref=protocol_pref,
        allow_protocol_fallback=allow_protocol_fallback,
        no_policy=no_policy,
        audit_override=audit_override,
        agent_subset=agent_subset,
        agent_subset_from_cli=agent_subset_from_cli,
        skill_subset=skill_subset,
        skill_subset_from_cli=skill_subset_from_cli,
        legacy_skill_paths=legacy_skill_paths,
        trust_transitive_mcp=trust_transitive_mcp,
        frozen=frozen,
        plan_callback=plan_callback,
        refresh=refresh,
        lockfile_only=lockfile_only,
        transaction=transaction,
    )
    return InstallService().run(request)
