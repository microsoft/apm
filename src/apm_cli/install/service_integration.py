"""Shared MCP and LSP reconciliation after package installation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from apm_cli.constants import InstallMode

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.service import FrozenDisposition
    from apm_cli.models.results import InstallResult


class ServiceCommandContext(Protocol):
    """Command fields consumed by native service reconciliation."""

    project_root: Path
    scope: InstallScope
    install_mode: InstallMode
    logger: InstallLogger
    runtime: str | None
    exclude: str | None
    trust_transitive_mcp: bool
    no_policy: bool
    verbose: bool
    force: bool
    exec_allow_map: dict[str, dict[str, bool]] | None
    exec_allow_resolved: bool


@dataclass(frozen=True)
class ServiceIntegrationResult:
    """Counts and the shared target decision produced by reconciliation."""

    mcp_count: int
    lsp_count: int
    target_decision: EffectiveTargetDecision | None


def run_service_integrations(  # noqa: PLR0913
    ctx: ServiceCommandContext,
    *,
    apm_package: Any,
    mcp_deps: list[Any],
    lock_path: Path,
    existing_lock: LockFile | None,
    old_mcp_servers: set[str],
    old_mcp_configs: dict[str, Any],
    old_mcp_provenance: dict[str, str | list[str]],
    old_mcp_target_servers: dict[str, list[str]],
    old_mcp_target_servers_present: bool,
    diagnostics: Any,
    explicit_target: str | list[str] | None,
    target_decision: EffectiveTargetDecision | None,
    active_package_keys: frozenset[str] | None = None,
    claude_native_keys: frozenset[str] = frozenset(),
    frozen_disposition: FrozenDisposition,
) -> ServiceIntegrationResult:
    """Resolve once, then reconcile package-declared MCP and LSP services."""
    from apm_cli.core.scope import InstallScope, get_modules_dir
    from apm_cli.core.target_detection import resolve_package_target_decision
    from apm_cli.install.lsp import run_lsp_integration
    from apm_cli.install.mcp import run_mcp_integration

    should_install_mcp = ctx.install_mode != InstallMode.APM
    should_install_lsp = ctx.install_mode is InstallMode.ALL
    lsp_deps = apm_package.get_lsp_dependencies()
    if not isinstance(lsp_deps, list):
        ctx.logger.verbose_detail("LSP dependencies were not a list; defaulting to empty")
        lsp_deps = []
    old_lsp_servers = set(existing_lock.lsp_servers) if existing_lock else set()
    if (
        target_decision is None
        and (should_install_mcp or should_install_lsp)
        and (mcp_deps or lsp_deps or old_mcp_servers or old_lsp_servers)
    ):
        target_decision = resolve_package_target_decision(
            ctx.project_root,
            package=apm_package,
            explicit_target=explicit_target,
            user_scope=ctx.scope is InstallScope.USER,
        )

    from apm_cli.install.lsp.integration import lsp_target_config, prepare_lsp_integration

    apm_modules_path = get_modules_dir(ctx.scope)
    apm_config = lsp_target_config(apm_package)
    selected_target = target_decision.value if target_decision else explicit_target
    # Declared LSP without a capable target fails here, before any MCP or LSP
    # write (#3727).
    prepared_lsp = prepare_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=apm_modules_path,
        lock_path=lock_path,
        project_root=ctx.project_root,
        should_install=should_install_lsp,
        existing_lock=existing_lock,
        diagnostics=diagnostics,
        executable_context=(ctx.exec_allow_map, ctx.exec_allow_resolved),
        no_policy=ctx.no_policy,
        user_scope=ctx.scope is InstallScope.USER,
        runtime=ctx.runtime,
        exclude=ctx.exclude,
        target_context=(apm_config, selected_target, ctx.scope),
        target_decision=target_decision,
        logger=ctx.logger,
        claude_native_keys=claude_native_keys,
    )
    prepared_lsp.require_capable_target()
    prepared_mcp = None
    resolved_mcp_targets = None
    frozen_unrecorded: frozenset[tuple[str, str]] = frozenset()
    if frozen_disposition.withhold_lockfile_writes:
        from apm_cli.install.mcp.integration import prepare_mcp_integration
        from apm_cli.install.service import InstallService
        from apm_cli.integration.lsp_integrator import LSPIntegrator
        from apm_cli.integration.mcp_integrator import MCPIntegrator
        from apm_cli.integration.mcp_integrator_install import (
            mcp_servers_for_runtime,
            resolve_mcp_targets,
        )

        prepared_mcp = prepare_mcp_integration(
            apm_package=apm_package,
            mcp_deps=mcp_deps,
            apm_modules_path=apm_modules_path,
            lock_path=lock_path,
            should_install=should_install_mcp,
            trust_transitive_mcp=ctx.trust_transitive_mcp,
            logger=ctx.logger,
            diagnostics=diagnostics,
            allow_pending=False,
            active_package_keys=active_package_keys,
            user_scope=ctx.scope is InstallScope.USER,
            project_root=ctx.project_root,
            executable_context=(ctx.exec_allow_map, ctx.exec_allow_resolved),
            no_policy=ctx.no_policy,
            claude_native_keys=claude_native_keys,
        )
        # Resolved (and announced) once: the MCP install below reuses it.
        resolved_mcp_targets = (
            resolve_mcp_targets(
                runtime=ctx.runtime,
                exclude=ctx.exclude,
                apm_config=apm_config,
                project_root=ctx.project_root,
                user_scope=ctx.scope is InstallScope.USER,
                explicit_target=selected_target,
                target_decision=target_decision,
                scope=ctx.scope,
                logger=ctx.logger,
                verbose=ctx.verbose,
                console=None,
            )
            if should_install_mcp and prepared_mcp.dependencies
            else None
        )
        mcp_targets = list(resolved_mcp_targets.runtimes or ()) if resolved_mcp_targets else []
        # The command's structural frozen check already required this baseline,
        # unless nothing is declared and so nothing is locked (#4050).
        if existing_lock is None:
            from apm_cli.deps.lockfile import LockFile
            from apm_cli.install.plan import manifest_requires_lockfile

            if manifest_requires_lockfile(apm_package):
                raise RuntimeError("Frozen service integration requires the checked lockfile")
            existing_lock = LockFile()
        frozen_unrecorded = InstallService.enforce_frozen_services(
            existing_lock,
            mcp_servers=MCPIntegrator.get_server_names(prepared_mcp.dependencies),
            mcp_targets=mcp_targets or [],
            lsp_target_servers={
                target: LSPIntegrator.expressible_server_names(prepared_lsp.dependencies, target)
                - LSPIntegrator.matching_entry_names(
                    prepared_lsp.dependencies,
                    target,
                    project_root=ctx.project_root,
                    user_scope=ctx.scope is InstallScope.USER,
                )
                for target in prepared_lsp.target_runtimes or []
            },
            current_mcp=prepared_mcp.current_view,
            user_scope=ctx.scope is InstallScope.USER,
            project_root=ctx.project_root,
            mcp_declared_transports={
                dep.name: getattr(dep, "transport", None)
                for dep in prepared_mcp.dependencies
                if hasattr(dep, "name")
            },
            mcp_target_servers={
                target: mcp_servers_for_runtime(prepared_mcp.dependencies, target)
                for target in mcp_targets or []
            },
            mcp_native_runtimes={
                dep.name: dep.native_runtimes
                for dep in prepared_mcp.dependencies
                if getattr(dep, "native_runtimes", None)
            },
        )
    from apm_cli.install.mcp.ownership import frozen_unrecorded_mcp_pairs
    from apm_cli.install.mcp.write_receipts import journal_mcp_writes

    lock_before_mcp = _staged_lockfile_state(lock_path)
    with (
        frozen_unrecorded_mcp_pairs(frozen_unrecorded),
        journal_mcp_writes(
            ctx.project_root, user_scope=ctx.scope is InstallScope.USER, logger=ctx.logger
        ),
    ):
        mcp_count, mcp_apm_config = run_mcp_integration(
            apm_package=apm_package,
            mcp_deps=mcp_deps,
            apm_modules_path=apm_modules_path,
            lock_path=lock_path,
            old_mcp_servers=old_mcp_servers,
            old_mcp_configs=old_mcp_configs,
            old_mcp_provenance=old_mcp_provenance,
            old_mcp_target_servers=old_mcp_target_servers,
            old_mcp_target_servers_present=old_mcp_target_servers_present,
            project_root=ctx.project_root,
            user_scope=ctx.scope is InstallScope.USER,
            should_install=should_install_mcp,
            logger=ctx.logger,
            diagnostics=diagnostics,
            runtime=ctx.runtime,
            exclude=ctx.exclude,
            trust_transitive_mcp=ctx.trust_transitive_mcp,
            no_policy=ctx.no_policy,
            verbose=ctx.verbose,
            explicit_target=target_decision.value if target_decision else None,
            target_decision=target_decision,
            scope=ctx.scope,
            prepared=prepared_mcp,
            active_package_keys=active_package_keys,
            claude_native_keys=claude_native_keys,
            executable_context=(ctx.exec_allow_map, ctx.exec_allow_resolved),
            pre_install_lock=existing_lock,
            frozen=frozen_disposition.withhold_lockfile_writes,
            policy_enforced=getattr(ctx, "mcp_policy_enforced", False) is True,
            # The one server apm install --mcp adds (install/mcp/entry_integration.py).
            mcp_entry=getattr(ctx, "mcp_entry", None),
            resolved_targets=resolved_mcp_targets,
        )
    lsp_count = run_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=apm_modules_path,
        lock_path=lock_path,
        existing_lock=existing_lock,
        project_root=ctx.project_root,
        user_scope=ctx.scope is InstallScope.USER,
        should_install=should_install_lsp,
        logger=ctx.logger,
        diagnostics=diagnostics,
        runtime=ctx.runtime,
        exclude=ctx.exclude,
        target_context=(
            mcp_apm_config,
            target_decision.value if target_decision else None,
            ctx.scope,
        ),
        target_decision=target_decision,
        fail_on_write_error=True,
        effective_allow_executables=ctx.exec_allow_map,
        effective_allow_resolved=ctx.exec_allow_resolved,
        force=ctx.force,
        no_policy=ctx.no_policy,
        prepared=prepared_lsp,
        # MCP writes are recorded only by this command's lockfile (#4697).
        refuse_unwritten_conflict=_staged_lockfile_unchanged(lock_path, lock_before_mcp),
    )
    return ServiceIntegrationResult(mcp_count, lsp_count, target_decision)


def _staged_lockfile_state(lock_path: Path) -> object:
    """What this command has staged for *lock_path*, without emitting a staged document."""
    from apm_cli.deps.lockfile_write_authority import (
        staged_lockfile_document,
        staged_lockfile_text,
    )

    return staged_lockfile_document(lock_path) or staged_lockfile_text(lock_path)


def _staged_lockfile_unchanged(lock_path: Path, before: object) -> bool:
    """Whether the lockfile staged for *lock_path* still holds the text it held *before*."""
    from apm_cli.deps.lockfile_write_authority import StagedDocument

    after = _staged_lockfile_state(lock_path)
    if after is before:
        return True
    # Compare the state already read: never read the lockfile a second time.
    texts = [
        state.text() if isinstance(state, StagedDocument) else state for state in (before, after)
    ]
    return texts[0] == texts[1]


class ServiceReconciliation:
    """One install's MCP/LSP reconciliation, rendered as the install reports it.

    ``apm install`` hands it to the package pipeline, which calls it with the
    pre-commit result before native Claude registration commits the lockfile,
    so the install writes the lockfile once (#4329). When no pipeline ran it,
    the command calls it with the pipeline's result, or ``None``.
    """

    def __init__(
        self,
        ctx: Any,
        call_options: Callable[[InstallResult | None], dict[str, Any]],
        **options: Any,
    ) -> None:
        self._ctx = ctx
        # Options read from the pipeline's result at call time.
        self._call_options = call_options
        self._options = options
        self.result: ServiceIntegrationResult | None = None
        self.failure: Exception | None = None

    def settle(self, install_result: InstallResult | None) -> ServiceIntegrationResult:
        """Return the reconciliation result, raising the failure the pipeline deferred."""
        if self.failure is not None:
            raise self.failure
        return self.result or self(install_result)

    def __call__(self, install_result: InstallResult | None) -> ServiceIntegrationResult:
        try:
            self.result = self._reconcile(install_result)
        except Exception as error:
            self.failure = error
            raise
        return self.result

    def _reconcile(self, install_result: InstallResult | None) -> ServiceIntegrationResult:
        from apm_cli.install.commit_gate import refuse_blocked_mcp
        from apm_cli.install.errors import InstallFailureAlreadyRendered, RequiredIntegrationError
        from apm_cli.policy.install_preflight import PolicyBlockError

        ctx = self._ctx
        call_options = self._call_options(install_result)
        if ctx.update:
            from apm_cli.models.apm_package import clear_apm_yml_cache

            clear_apm_yml_cache()
        try:
            return run_service_integrations(ctx, **call_options, **self._options)
        except PolicyBlockError as exc:
            refuse_blocked_mcp(ctx.logger, exc)
        except RequiredIntegrationError as exc:
            ctx.logger.error(str(exc))
            ctx.logger.render_summary()
            raise InstallFailureAlreadyRendered(str(exc)) from exc
