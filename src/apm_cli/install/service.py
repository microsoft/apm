"""Application Service: orchestrates one install invocation.

The ``InstallService`` is the *behaviour-bearing* entry point for installs.
Adapters (the Click handler today; programmatic / API callers tomorrow)
build an :class:`InstallRequest` and call :meth:`InstallService.run`,
which returns a :class:`InstallResult`.  Adapters own presentation,
process-exit translation, and CLI option parsing -- the service does not.

Why a class rather than a free function?
----------------------------------------
The class encapsulates the *seam* for future dependency injection.  Today
the underlying ``run_install_pipeline`` builds collaborators internally;
when (and only when) a programmatic caller needs to swap the downloader
or integrator factories, the service can grow constructor parameters
without changing every call site.

For now the service is intentionally lean: it validates that the dep
system is available, then delegates to the existing pipeline.  This
gives every adapter a typed Request -> Result contract today without
the blast radius of a deeper DI rewrite.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core import remediation
from apm_cli.deps.lockfile_write_authority import LockfileWriter, lockfile_writer
from apm_cli.install.frozen_selection import (
    _changed_selections,
    _outermost,  # noqa: F401 -- re-exported for install.frozen_withdrawal and tests
    frozen_selection_changes,
)
from apm_cli.install.request import InstallRequest
from apm_cli.models.results import InstallDisposition

if TYPE_CHECKING:
    from collections.abc import Iterable

    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.lifecycle_scripts import LifecycleEvent, LifecycleScriptRunner
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.dependency.reference import DependencyReference
    from apm_cli.models.results import InstallResult
    from apm_cli.security.trust import McpAdmission


class InstallNotAvailableError(RuntimeError):
    """Raised when the APM dependency subsystem failed to import."""


@dataclass(frozen=True)
class FrozenDisposition:
    """What a ``--frozen`` replay withholds from install phases (req-lk-006).

    Built only by :meth:`InstallService.frozen_disposition`, the single reader
    of the ``frozen`` flag. Phases consume this answer instead of the flag.
    """

    replay: bool

    @property
    def retain_package_orphans(self) -> bool:
        """Undeclared packages keep files, checkout and lock claims (#3662)."""
        return self.replay

    @property
    def retain_stale_files(self) -> bool:
        """A locked file this version no longer renders stays on disk and in the lock."""
        return self.replay

    @property
    def withhold_lockfile_writes(self) -> bool:
        """apm.lock.yaml is never rewritten, so nothing new may be recorded."""
        return self.replay


def ci_selects_frozen(environ: Mapping[str, str] | None = None) -> bool:
    """req-lk-018: ``CI`` is truthy when present and not ``""``, ``"0"`` or ``"false"``."""
    value = (os.environ if environ is None else environ).get("CI")
    return value is not None and value.lower() not in {"", "0", "false"}


def resolve_frozen(
    frozen: bool | None,
    resolve_intent: Any,
    global_: bool,
    root: str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """The single decision whether ``apm install`` runs frozen (req-lk-006, req-lk-018).

    An explicit ``--frozen``/``--no-frozen`` wins. An invocation that asks to
    resolve (``--update``, ``--refresh``, positional packages, ``--mcp``) stays
    non-frozen. Otherwise a truthy ``CI`` selects frozen when the scope's
    ``apm.lock.yaml`` exists; without one, install resolves and writes it.
    """
    if frozen is not None:
        return frozen
    if resolve_intent or not ci_selects_frozen(environ):
        return False
    from apm_cli.core.scope import InstallScope, get_lockfile_dir

    if root is not None and not global_:
        lockfile_dir = Path(root)
    else:
        lockfile_dir = get_lockfile_dir(InstallScope.USER if global_ else InstallScope.PROJECT)
    if not (lockfile_dir / "apm.lock.yaml").is_file():
        return False
    from apm_cli.utils.console import _rich_info

    _rich_info(
        "CI is set and apm.lock.yaml exists: installing as --frozen. "
        "Pass --no-frozen to resolve and update it.",
        symbol="info",
    )
    return True


def frozen_services_error(reasons: list[str]) -> Exception:
    """The ``--frozen`` refusal for service claims the lockfile cannot replay."""
    from apm_cli.install.errors import FrozenInstallError

    return FrozenInstallError(
        "--frozen cannot replay the selected native services.",
        reasons=reasons,
        tip=(
            "Tip: native MCP/LSP configs were not changed. Rerun the same "
            "install command without --frozen, retaining the intended --target, "
            "--only, --root DIR or --global options, then commit apm.lock.yaml."
        ),
    )


class InstallService:
    """Application service for the APM install pipeline.

    Stateless: a single instance can serve multiple ``run(request)``
    invocations.  Constructor takes no arguments today but exists as the
    extension point for collaborator injection (downloader, scanner,
    integrator factory) when programmatic callers need to swap them.
    """

    @staticmethod
    def frozen_disposition(source: Any) -> FrozenDisposition:
        """The single reader of an install context's ``frozen`` flag."""
        return FrozenDisposition(replay=source.frozen is True)

    @staticmethod
    def frozen_replay_lockfile(source: Any) -> LockFile | None:
        """The recorded lockfile a frozen replay deploys against; None otherwise."""
        frozen = InstallService.frozen_disposition(source)
        return source.existing_lockfile if frozen.replay else None

    @staticmethod
    def frozen_writes(frozen: bool) -> AbstractContextManager[list[str] | None]:
        """Protect the complete caller lifetime with the canonical write chokepoint."""
        from apm_cli.deps.lockfile import suppress_lockfile_writes

        return suppress_lockfile_writes() if frozen else nullcontext()

    @staticmethod
    def catalog_freshness(
        *, update_refs: bool, refresh: bool, frozen: bool = False
    ) -> AbstractContextManager[None]:
        """Bind marketplace catalog freshness to the whole command invocation.

        Explicit update or refresh intent -- the same intent that makes Git
        refs resolve from upstream -- revalidates each needed catalog once and
        fails rather than using stale data (#3279). Ordinary installs keep
        cached catalogs, and frozen replay never acquires a live-catalog
        requirement.
        """
        from apm_cli.deps.tiered_ref_resolver import RefFreshnessPolicy
        from apm_cli.marketplace.client import catalog_freshness_scope

        policy = RefFreshnessPolicy.for_install_intent(update_refs=update_refs, refresh=refresh)
        return catalog_freshness_scope(require_fresh=policy.requires_remote and not frozen)

    @staticmethod
    def enforce_frozen_services(
        lockfile: LockFile,
        *,
        mcp_servers: set[str],
        mcp_targets: list[str],
        lsp_target_servers: dict[str, set[str]],
        current_mcp: CurrentMcpConfigView | None,
        user_scope: bool,
        project_root: Path | None = None,
        mcp_declared_transports: dict[str, str | None] | None = None,
        mcp_target_servers: dict[str, set[str]] | None = None,
        mcp_native_runtimes: dict[str, frozenset[str]] | None = None,
    ) -> frozenset[tuple[str, str]]:
        """Refuse unrecorded service claims before either service mutates native state.

        MCP claims every server on every target, narrowed by
        *mcp_target_servers* when a dependency's ``targets:`` selects fewer,
        except the runtimes that load it natively (*mcp_native_runtimes*).
        LSP claims are per target
        (*lsp_target_servers* maps each selected runtime to the servers it can
        express), so a server a target skips is never required to be owned there.

        Returns the MCP ``(server, runtime)`` pairs that may be unclaimed only
        because the runtime refuses the server's remote transport (never
        claimed) and whose native config has no entry for it. The MCP stage
        skips them once the registry payload confirms the refusal and raises
        this same error otherwise.
        """
        from apm_cli.adapters.client._remote_shapes import may_refuse_remote
        from apm_cli.install.errors import frozen_unrecorded_reason
        from apm_cli.install.mcp.native_entry import (
            native_mcp_entry_matches_declaration,
            native_mcp_entry_present,
        )
        from apm_cli.install.mcp.ownership import migrate_legacy_project_target_servers

        mcp_ownership = {
            target: set(names) for target, names in lockfile.mcp_target_servers.items()
        }
        migrate_legacy_project_target_servers(
            mcp_ownership, active_runtimes=set(mcp_targets), user_scope=user_scope
        )
        reasons = []
        if current_mcp is not None:
            reasons.extend(
                f"  - {problem.package_key}: {problem.message}" for problem in current_mcp.problems
            )
            difference = current_mcp.diff(lockfile.mcp_configs)
            for name in sorted(difference.changed | difference.source_only | difference.lock_only):
                reasons.append(
                    f"  - MCP server {name!a} cannot reproduce its locked config "
                    "from the available source declarations"
                )
        deferred: set[tuple[str, str]] = set()
        native = mcp_native_runtimes or {}
        claims = [
            (
                "MCP",
                target,
                {
                    name
                    for name in (mcp_target_servers or {}).get(target, mcp_servers)
                    if target not in native.get(name, ())
                },
                mcp_ownership,
            )
            for target in mcp_targets
        ] + [
            ("LSP", target, names, lockfile.lsp_target_servers)
            for target, names in lsp_target_servers.items()
        ]
        for service, target, names, recorded in claims:
            for name in sorted(names - set(recorded.get(target, ()))):
                if (
                    service == "MCP"
                    and name in lockfile.mcp_servers
                    and may_refuse_remote(target, (mcp_declared_transports or {}).get(name))
                    and not native_mcp_entry_present(
                        target,
                        name,
                        stored_config=lockfile.mcp_configs.get(name),
                        project_root=project_root,
                        user_scope=user_scope,
                    )
                ):
                    deferred.add((name, target))
                    continue
                if service == "MCP" and native_mcp_entry_matches_declaration(
                    target,
                    name,
                    stored_config=lockfile.mcp_configs.get(name),
                    project_root=project_root,
                    user_scope=user_scope,
                ):
                    continue
                reasons.append(frozen_unrecorded_reason(service, name, target))
        if reasons:
            raise frozen_services_error(reasons)
        return frozenset(deferred)

    @lockfile_writer(LockfileWriter.INSTALL)
    def run(self, request: InstallRequest) -> InstallResult:
        """Execute the install pipeline and return the structured result.

        Fires ``pre-install`` / ``post-install`` lifecycle scripts around
        the pipeline when scripts are configured.

        Raises:
            InstallNotAvailableError: if the dependency subsystem failed
                to import (e.g. missing optional extras).  Adapters are
                responsible for presenting this to the user.
            FrozenInstallError: when ``request.frozen`` is True and the
                lockfile is missing, structurally out of sync with
                ``request.apm_package``, or not what the install
                produces.  The first two are raised before the pipeline
                runs so no resolve / download work is wasted.
        """
        # Enforce --frozen BEFORE invoking the pipeline.  The check is
        # structural (no network). The resolver also verifies immutable
        # transitive requirements as locked package manifests become available.
        if request.frozen:
            self.enforce_frozen(request)
            # req-lk-017: judge the committed bytes before any integrator can
            # rewrite or keep them, so every primitive type answers alike.
            self._enforce_frozen_deployed_hashes(request, deployed=False)

        from apm_cli.core.scope import InstallScope
        from apm_cli.marketplace.project_marketplaces import bind_project_marketplaces

        # The root project's ``marketplaces:`` map resolves this install,
        # previews included (#2903). Each path enters the scope once.
        marketplace_scope = bind_project_marketplaces(
            getattr(request.apm_package, "marketplaces", None),
            user_scope=request.scope is InstallScope.USER,
        )
        if request.dry_run:
            # A preview runs no lifecycle scripts and commits nothing.
            with marketplace_scope:
                return self._run_pipeline(request)

        runner = self._build_script_runner(request)
        event = self._build_event("pre-install", request)

        with marketplace_scope:
            if request.frozen:
                # A changed local-catalog selection refuses before any script (#4639).
                self._verify_frozen_local_catalogs(request)
            runner.fire("pre-install", event)
            if request.frozen:
                # req-lk-006: the lockfile is never written in frozen mode.
                # Suppress at the chokepoint, then decide from what was
                # withheld whether this install can report success.  The
                # pipeline still deploys, as `npm ci` does.
                from apm_cli.install.frozen_withdrawal import frozen_capture

                with frozen_capture(_frozen_lockfile_path(request)) as withheld:
                    result = self._run_pipeline(request)
            else:
                withheld = None
                result = self._run_pipeline(request)

        install_worked = result.disposition in {
            InstallDisposition.SUCCESS,
            InstallDisposition.PARTIAL_SUCCESS,
        }

        # Only judge the withheld lockfile against an install that got far
        # enough to be meaningful.  A failed pipeline has its own
        # diagnostics and an unreliable ledger, so raising here would
        # replace the real cause with a confusing lockfile complaint.
        if withheld is not None and install_worked:
            self._enforce_frozen_no_rewrite(request, withheld, result.active_package_keys)

        if install_worked:
            request.hold_deploy_evidence()  # before a post-install script resets the memos
            post_event = self._build_event("post-install", request)
            if request.post_install_after_services:
                result.post_install = lambda: runner.fire("post-install", post_event)
            else:
                runner.fire("post-install", post_event)

        return result

    @staticmethod
    def _run_pipeline(request: InstallRequest) -> InstallResult:
        """Invoke the install pipeline for *request*."""
        # Local import keeps service module import-cheap and matches the
        # existing pipeline's lazy-import discipline.
        try:
            from apm_cli.install.pipeline import run_install_pipeline
        except ImportError as e:  # pragma: no cover -- defensive
            raise InstallNotAvailableError(f"APM dependency system not available: {e}") from e

        return run_install_pipeline(
            request.apm_package,
            update_refs=request.update_refs,
            frozen=request.frozen,
            trust_transitive_mcp=request.trust_transitive_mcp,
            install_mcp=request.install_mcp,
            mcp_runtime=request.mcp_runtime,
            dry_run=request.dry_run,
            verbose=request.verbose,
            only_packages=request.only_packages,
            force=request.force,
            parallel_downloads=request.parallel_downloads,
            logger=request.logger,
            scope=request.scope,
            auth_resolver=request.auth_resolver,
            target=request.target,
            target_decision=request.target_decision,
            allow_insecure=request.allow_insecure,
            allow_insecure_hosts=request.allow_insecure_hosts,
            marketplace_provenance=request.marketplace_provenance,
            protocol_pref=request.protocol_pref,
            allow_protocol_fallback=request.allow_protocol_fallback,
            no_policy=request.no_policy,
            audit_override=request.audit_override,
            skill_subset=request.skill_subset,
            skill_subset_from_cli=request.skill_subset_from_cli,
            legacy_skill_paths=request.legacy_skill_paths,
            plan_callback=request.plan_callback,
            refresh=request.refresh,
            lockfile_only=request.lockfile_only,
            trust_bin=request.trust_bin,
            transaction=request.transaction,
            lsp_preflight=request.lsp_preflight,
            reconcile_services=request.reconcile_services,
        )

    # -- Lifecycle script helpers ------------------------------------------

    @staticmethod
    def _build_script_runner(request: InstallRequest) -> LifecycleScriptRunner:
        """Build a :class:`LifecycleScriptRunner` from the request context."""
        from apm_cli.core.lifecycle_scripts import build_runner_from_context

        project_root = None
        pkg_path = getattr(request.apm_package, "package_path", None)
        if pkg_path is not None:
            project_root = str(pkg_path)

        return build_runner_from_context(
            logger=request.logger,
            verbose=request.verbose,
            project_root=project_root,
        )

    @staticmethod
    def _build_event(event_name: str, request: InstallRequest) -> LifecycleEvent:
        """Build a :class:`LifecycleEvent` from the request."""
        from apm_cli.core.lifecycle_scripts import event_for_dependencies

        scope_name = "project"
        if request.scope is not None:
            scope_name = (
                request.scope.value if hasattr(request.scope, "value") else str(request.scope)
            )

        pkg_path = getattr(request.apm_package, "package_path", None)
        return event_for_dependencies(
            event_name,
            request.apm_package.get_apm_dependencies(),
            scope=scope_name,
            working_directory=str(pkg_path) if pkg_path is not None else None,
        )

    @staticmethod
    def enforce_frozen_restored(failures: Iterable[tuple[str, str]]) -> None:
        """Refuse a frozen install whose locked dependencies could not be restored.

        Runs before the hydrated MCP check, so a package that failed to
        download (for example a lockfile hash mismatch, #4013) is reported
        with its own cause instead of the MCP drift its missing manifest
        would imply. Nothing has been deployed at this point.
        """
        failures = list(failures)
        reasons = [f"  - {message}" for _, message in failures]
        if not reasons:
            return
        from apm_cli.install.errors import FrozenInstallError

        names = ", ".join(dict.fromkeys(name for name, _ in failures))
        raise FrozenInstallError(
            f"--frozen: locked dependencies could not be restored: {names}.",
            reasons=reasons,
            tip="Tip: nothing was deployed. Fix the cause above and re-run.",
        )

    @staticmethod
    def enforce_frozen_selections(deps_to_install: Iterable[Any], lockfile: LockFile) -> None:
        """Refuse a frozen install whose resolved selections differ (req-rs-018).

        Runs before any deployment, so neither the recorded nor the resolved
        selection is deployed.
        """
        from apm_cli.install.errors import FrozenInstallError

        changes = frozen_selection_changes(deps_to_install, lockfile)
        if changes:
            raise FrozenInstallError(
                "--frozen: dependency selections differ from apm.lock.yaml.",
                reasons=[f"  - {reason}" for reason in changes],
                tip=f"Tip: {remediation.FROZEN_SELECTIONS_CHANGED.sentence()}",
            )

    @staticmethod
    def enforce_frozen_hook_pins(lockfile: LockFile, modules_dir: Path, trust_ctx: Any) -> None:
        """Before deploying, refuse locked hooks their pin no longer admits, as audit --ci does."""
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.exec_gate import locked_hook_pin_mismatches

        mismatches = locked_hook_pin_mismatches(lockfile, modules_dir, trust_ctx)
        if mismatches:
            raise FrozenInstallError(
                "--frozen: apm.lock.yaml records hooks their approval no longer admits.",
                reasons=[f"  - {mismatch.describe()}" for mismatch in mismatches],
                tip=(
                    "Tip: re-approve the package as shown above, or "
                    f"{remediation.FROZEN_HOOK_PINS_STALE.clause()}."
                ),
            )

    @staticmethod
    def reject_frozen_mutation(frozen: bool, operation: str) -> None:
        """Reject an add-style install operation before it can mutate state."""
        if not frozen:
            return
        from apm_cli.install.errors import FrozenInstallError

        raise FrozenInstallError(
            f"--frozen cannot be combined with {operation}. "
            f"{remediation.FROZEN_OPERATION_REJECTED.sentence()}"
        )

    @staticmethod
    def reject_missing_frozen_root(frozen: bool, root: str | None) -> None:
        """Reject a missing redirected root before the redirect creates it."""
        if not frozen or root is None:
            return
        from pathlib import Path

        from apm_cli.install.errors import FrozenInstallError

        if not Path(root).exists():
            raise FrozenInstallError(
                f"--frozen requires --root directory {root!r} to exist. "
                "Create and populate it with a normal install before retrying."
            )

    @staticmethod
    def enforce_frozen(request: InstallRequest, *, allow_pending_mcp: bool = True) -> None:
        """Raise :class:`FrozenInstallError` if lockfile is absent or stale.

        Reads the scoped installation's ``apm.lock.yaml`` (manifest-relative
        for unscoped API callers) and checks package dependencies and the canonical
        current MCP config view. Any miss raises before install mutation.
        """
        from apm_cli.deps import lockfile as lockfile_module
        from apm_cli.deps.lockfile import LockFile, LockfileConflictError
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.plan import (
            lockfile_satisfies_manifest,
            lockfile_satisfies_replacements,
            manifest_requires_lockfile,
        )

        lockfile_path = _frozen_lockfile_path(request)
        missing_message = (
            f"--frozen requires apm.lock.yaml to exist at {lockfile_path}. "
            "Rerun the same install command without --frozen, retaining "
            "--root DIR or --global if specified."
        )

        if not lockfile_path.exists():
            manifest = request.apm_package
            if manifest is not None and not manifest_requires_lockfile(manifest):
                # Nothing declared: install writes no lockfile, so this state
                # is already converged and --frozen replays it as a no-op.
                return
            raise FrozenInstallError(missing_message, tip="")

        try:
            lockfile = LockFile.read(lockfile_path)
        except LockfileConflictError as e:
            raise FrozenInstallError(
                f"--frozen cannot read the lockfile. {e}\nFor CI, commit the resolved lockfile.",
                tip="",
            ) from e
        except lockfile_module.UnsupportedLockfileVersionError as e:
            raise FrozenInstallError(f"--frozen could not read apm.lock.yaml: {e}", tip="") from e
        except Exception as e:
            raise FrozenInstallError(
                f"--frozen could not read apm.lock.yaml: {e}. Fix the file, or restore "
                "a known-good lockfile from version control, before retrying.",
                tip="",
            ) from e

        if lockfile is None:
            raise FrozenInstallError(missing_message, tip="")

        replacements_satisfied, replacement_reasons = lockfile_satisfies_replacements(
            lockfile, getattr(request.apm_package, "replace_rules", None)
        )
        if not replacements_satisfied:
            raise FrozenInstallError(
                "--frozen: replacement rules differ from apm.lock.yaml.",
                reasons=replacement_reasons,
            )

        from apm_cli.core.scope import get_modules_dir
        from apm_cli.models.dependency.replacement import project_manifest_dependencies

        modules_dir = (
            get_modules_dir(request.scope)
            if request.scope is not None
            else lockfile_path.parent / "apm_modules"
        )
        manifest_deps = project_manifest_dependencies(request.apm_package, modules_dir=modules_dir)
        satisfied, reasons = lockfile_satisfies_manifest(lockfile, manifest_deps)

        # Same admission inputs as install, so a gate-parked or approved server
        # reaches the same verdict frozen or not (#3893, #3902).
        from apm_cli.core.scope import is_user_scope
        from apm_cli.integration.mcp_config_view import without_parked_notices
        from apm_cli.security.trust import McpAdmission

        admission = McpAdmission.for_project(
            request.apm_package.package_path or lockfile_path.parent,
            user_scope=is_user_scope(request.scope),
            one_off=request.trust_transitive_mcp,
            no_policy=request.no_policy,
        )
        reasons.extend(
            InstallService._frozen_mcp_reasons(
                request.apm_package,
                lockfile,
                modules_dir,
                admission=admission,
                # The hydrated check and the MCP phase report parked servers.
                logger=without_parked_notices(request.logger),
                allow_pending=allow_pending_mcp,
                include_dependency_mcp=request.install_mcp,
            )
        )

        satisfied = satisfied and not reasons
        if not satisfied:
            raise FrozenInstallError(
                "--frozen: apm.lock.yaml is out of sync with apm.yml.",
                reasons=reasons,
            )
        # A deny of a local bundle refuses before its LSP server is retired (RA-041).
        from apm_cli.install.frozen_retirement import refuse_withdrawn_bundle_lsp

        refuse_withdrawn_bundle_lsp(request, lockfile, lockfile_path.parent)
        InstallService._refuse_frozen_dropped_targets(request, lockfile, lockfile_path)
        InstallService._verify_frozen_local_content(request, lockfile, manifest_deps)

    @staticmethod
    def _refuse_frozen_dropped_targets(
        request: InstallRequest, lockfile: LockFile, lockfile_path: Path
    ) -> None:
        """Refuse, before any write, a lock that records files of a target apm.yml dropped (#4517).

        Retiring them deletes deployed files and rewrites apm.lock.yaml, which
        ``--frozen`` may not do; a refused frozen install changes nothing.
        """
        from apm_cli.core.scope import is_user_scope
        from apm_cli.core.target_detection import FLAG_TARGET_SOURCE, EffectiveTargetDecision
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.manifest_reconcile import dropped_target_files
        from apm_cli.integration.targets import KNOWN_TARGETS

        root = getattr(request.apm_package, "package_path", None) or lockfile_path.parent
        # A harness this run's --target still names keeps its native declaration (#4874).
        decision = request.target_decision or EffectiveTargetDecision(
            request.target, FLAG_TARGET_SOURCE
        )
        flagged = decision.canonical_targets if decision.from_flag else ()
        dropped = dropped_target_files(
            Path(root),
            lockfile,
            user_scope=is_user_scope(request.scope),
            run_targets=[KNOWN_TARGETS[name] for name in flagged or () if name in KNOWN_TARGETS],
        )
        if not dropped:
            return
        targets = ", ".join(sorted(set(dropped.values())))
        raise FrozenInstallError(
            f"--frozen: apm.yml no longer declares target(s) {targets}, "
            "but apm.lock.yaml still records their files.",
            reasons=[f"  - {path} (target {dropped[path]})" for path in sorted(dropped)],
            tip=f"Tip: {remediation.FROZEN_TARGET_DROPPED.sentence()}",
        )

    @staticmethod
    def _selected_locked_replacements(
        lockfile: LockFile, manifest_deps: list[DependencyReference]
    ) -> list[DependencyReference]:
        """Select reachable local replacements from locked edges without source I/O."""
        from collections import deque

        if lockfile.replacements is None:
            return []
        children: dict[str, list[str]] = {}
        for key, dependency in lockfile.dependencies.items():
            for parent in dependency.parent_keys:
                if parent is not None:
                    children.setdefault(parent, []).append(key)
        queue = deque(dep.get_unique_key() for dep in manifest_deps)
        seen: set[str] = set()
        selected: list[DependencyReference] = []
        while queue:
            key = queue.popleft()
            if key in seen:
                continue
            seen.add(key)
            dependency = lockfile.get_dependency(key)
            if (
                dependency is not None
                and dependency.replacement is not None
                and dependency.is_local_source
            ):
                selected.append(dependency.to_dependency_ref())
            queue.extend(children.get(key, ()))
        return selected

    @staticmethod
    def _verify_frozen_local_catalogs(request: InstallRequest) -> None:
        """Refuse a frozen install whose local catalog no longer selects its lock rows."""
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.local_content_verdict import local_catalog_selection_reasons
        from apm_cli.models.dependency.replacement import project_manifest_dependencies

        lockfile = LockFile.read(_frozen_lockfile_path(request))
        if lockfile is None:
            return
        reasons = local_catalog_selection_reasons(
            lockfile, project_manifest_dependencies(request.apm_package)
        )
        if reasons:
            raise FrozenInstallError(
                "--frozen: a local marketplace catalog no longer selects apm.lock.yaml.",
                reasons=reasons,
            )

    @staticmethod
    def _verify_frozen_local_content(
        request: InstallRequest,
        lockfile: LockFile,
        manifest_deps: list[DependencyReference],
    ) -> None:
        """Verify selected local declarations before any lifecycle or deployment."""
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.local_content_verdict import local_content_reasons

        reasons = local_content_reasons(request.apm_package, lockfile, manifest_deps)
        if reasons:
            raise FrozenInstallError(
                "--frozen: local dependency content does not match apm.lock.yaml.",
                reasons=reasons,
                tip=(
                    "Restore the locked local content, or "
                    f"{remediation.FROZEN_LOCAL_CONTENT_CHANGED.clause()}."
                ),
            )

    @staticmethod
    def _frozen_mcp_reasons(
        package: APMPackage,
        lockfile: LockFile,
        modules_dir: Path,
        *,
        admission: McpAdmission,
        logger: InstallLogger | None,
        allow_pending: bool,
        active_package_keys: frozenset[str] | None = None,
        include_dependency_mcp: bool = True,
    ) -> list[str]:
        """Compare actual declarations, deferring only unavailable source evidence.

        The view is gated by *admission* (the trust owner) and checked against
        the recorded omission evidence exactly as install derives it (#3925,
        #3902). The pre-download check discovers policy itself; the hydrated
        check uses the pipeline's routed trust context and is authoritative
        before anything deploys.
        """
        from apm_cli.install.frozen_mcp_drift import frozen_mcp_drift_reasons
        from apm_cli.integration.mcp_config_view import CurrentMcpConfigView

        if allow_pending and not (
            package.get_all_mcp_dependencies()
            or lockfile.mcp_configs
            or lockfile.mcp_servers
            or lockfile.mcp_omissions
        ):
            return []
        current = CurrentMcpConfigView.select(
            package,
            lockfile,
            modules_dir,
            admission=admission,
            recorded=lockfile,
            logger=logger,
            allow_pending=allow_pending,
            active_package_keys=active_package_keys,
            include_dependency_mcp=include_dependency_mcp,
        )
        pending = current.pending_servers(lockfile) if allow_pending else frozenset()
        diff = current.diff(lockfile.mcp_configs)
        return frozen_mcp_drift_reasons(current, lockfile, diff, pending)

    @staticmethod
    def enforce_hydrated_mcp(
        package: APMPackage,
        lockfile: LockFile,
        modules_dir: Path,
        *,
        admission: McpAdmission,
        logger: InstallLogger | None,
        active_package_keys: frozenset[str] | None = None,
        include_dependency_mcp: bool = True,
    ) -> None:
        """Reject restored MCP drift before a package can activate native configuration."""
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.integration.mcp_config_view import without_parked_notices

        reasons = InstallService._frozen_mcp_reasons(
            package,
            lockfile,
            modules_dir,
            admission=admission,
            logger=without_parked_notices(logger),
            allow_pending=False,
            active_package_keys=active_package_keys,
            include_dependency_mcp=include_dependency_mcp,
        )
        if reasons:
            raise FrozenInstallError(
                "--frozen: restored MCP declarations differ from apm.lock.yaml.",
                reasons=reasons,
                tip=(
                    "Tip: review the restored sources and lockfile before running "
                    "'apm install' without --frozen to record the intended declarations. "
                    "MCP configuration has not been activated."
                ),
            )

    @staticmethod
    def _enforce_frozen_no_rewrite(
        request: InstallRequest, withheld: list[str], active_keys: frozenset[str] | None = None
    ) -> None:
        """Fail a frozen install whose result does not match the committed lockfile.

        Three post-deploy checks share this owner:

        * **Unrecorded claims.** Each lockfile the frozen install *would*
          have written is compared against the committed ``apm.lock.yaml``.
          A file no lockfile row claims carries no recorded hash, which puts
          it outside ``content-integrity``'s hash comparison *and* its
          hidden-Unicode scan (#2379).  Deliberately one-directional:
          claims a narrower install would *drop* (``--target`` filter,
          ``--only``, a removed dependency) do not fail.  Equivalence ignores
          ``generated_at`` and ``apm_version`` (Section 5.5).  The refusal
          first removes the unrecorded files this install wrote (#4309).
        * **Deployed presence and exec decisions** of the graph (#4043, #4053).
        * **Deployed hashes (req-lk-017).** Every ``deployed_file_hashes``
          entry is re-verified through audit's verifier, so ``install --frozen``
          and ``audit --ci`` agree on kept user edits and re-renders (#3654).

        Native target files are outside the install transaction, so recorded
        files already deployed stay on disk; only the lockfile write is withheld.
        """
        from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.frozen_retirement import enforce_frozen_presence

        # Re-read rather than reuse the copy `enforce_frozen` loaded: this
        # judges what is on disk now.  `--frozen` is a CI primitive with a
        # single writer, so nothing else is expected to touch the file
        # between the two reads; drift.py's own membership checks make the
        # same assumption.
        lockfile_path = _frozen_lockfile_path(request)
        committed = LockFile.read(lockfile_path)
        if committed is None:  # nothing declared, nothing locked (#4050)
            return
        # DeploymentLedgerCodec owns the deployed-files vocabulary (see
        # `scripts/lint-architecture-boundaries.sh`); ask it directly rather
        # than borrowing drift.py's private wrapper around the same call.
        committed_paths = set(DeploymentLedgerCodec.legacy_deployed_file_claims(committed))

        changed_selections: dict[str, str] = {}
        unclaimed: set[str] = set()
        for payload in withheld:
            candidate = LockFile.from_yaml(payload)
            if committed.is_semantically_equivalent(candidate):
                continue
            changed_selections.update(_changed_selections(committed, candidate))
            claimed = set(DeploymentLedgerCodec.legacy_deployed_file_claims(candidate))
            unclaimed |= claimed - committed_paths
        if changed_selections:
            # An older lockfile recorded one parent's selection where the
            # graph now merges every declaring edge (#3735).
            raise FrozenInstallError(
                "--frozen: dependency selections differ from apm.lock.yaml.",
                reasons=[f"  - {reason}" for _key, reason in sorted(changed_selections.items())],
                tip=f"Tip: {remediation.FROZEN_SELECTIONS_CHANGED.sentence()}",
            )
        if unclaimed:
            # I7 (#4309): withdraw what this install deployed, then refuse.
            from apm_cli.install.frozen_withdrawal import refuse_unrecorded_deployments

            raise refuse_unrecorded_deployments(
                withheld, unclaimed, committed, lockfile_path.parent
            )

        enforce_frozen_presence(request, committed, withheld, lockfile_path.parent, active_keys)
        InstallService._enforce_frozen_deployed_hashes(request, committed, deployed=True)

    @staticmethod
    def _enforce_frozen_deployed_hashes(
        request: InstallRequest, committed: LockFile | None = None, *, deployed: bool
    ) -> None:
        """Fail a frozen install whose deployed bytes differ from the lock (req-lk-017).

        Runs twice through the verifier ``apm audit`` uses.  Before the
        pipeline (*deployed* False) it judges the committed bytes, so a local
        edit fails the same way whether the primitive's integrator would keep
        it (skills) or rewrite it (instructions, agents, commands).  After
        deployment it catches an APM upgrade that renders a locked file
        differently (#3654).
        """
        from apm_cli.claude_plugins.registrar import native_deployed_drift
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.local_edits import edited_file_remediations
        from apm_cli.policy.ci_checks import resolve_audit_scope, verify_deployed_file_hashes
        from apm_cli.policy.ci_checks import resolve_claim_targets as claims  # #5312

        lockfile_path = _frozen_lockfile_path(request)
        committed = committed or LockFile.read(lockfile_path)
        if committed is None:  # nothing declared, nothing locked (#4050)
            return
        deploy_root, user_scope, targets = resolve_audit_scope(lockfile_path.parent, committed)
        targets = claims(lockfile_path.parent, committed, user_scope=user_scope, targets=targets)
        drifted, unresolved = verify_deployed_file_hashes(deploy_root, committed, targets)
        if not deployed and drifted:
            # Stale APM output (receipt-proven) is replaced, then re-verified.
            from apm_cli.install.local_edits import unedited_apm_output

            stale = unedited_apm_output(deploy_root, user_scope, committed, targets, drifted)
            drifted = [drift for drift in drifted if drift.path not in stale]
        drifted += [] if deployed else native_deployed_drift(deploy_root, committed)
        if not drifted and not unresolved:
            return
        if deployed:
            tip = (
                f"{_FROZEN_REPAIR_TIP} To keep local edits, first "
                f"{remediation.LOCAL_EDITS_EXPORT.clause()}, or move them aside."
            )
        else:
            # Never destructive (RA-044): see install/local_edits.py.
            from apm_cli.core.remediation import either_sentence

            advice = either_sentence(*edited_file_remediations(user_scope=user_scope))
            tip = f"Tip: nothing was deployed. {advice}"
        raise FrozenInstallError(
            "--frozen: deployed files do not match the hashes recorded in apm.lock.yaml.",
            reasons=[
                *(
                    f"  - {drift.path}: expected {drift.expected}, observed {drift.actual}"
                    for drift in sorted(drifted, key=lambda drift: drift.path)
                ),
                *(f"  - {path}: unsafe or unresolved deployed path" for path in sorted(unresolved)),
            ],
            tip=tip,
        )


_FROZEN_REPAIR_TIP = (
    "Tip: the files were still deployed; only the lockfile write was withheld. "
    f"{remediation.FROZEN_LOCKFILE_WITHHELD.sentence()}"
)


def _frozen_lockfile_path(request: InstallRequest) -> Path:
    """Locate the scoped ``apm.lock.yaml`` (manifest-relative when unscoped)."""
    from apm_cli.core.scope import get_lockfile_dir

    manifest_path = getattr(request.apm_package, "package_path", None)
    if request.scope is not None:
        project_dir = get_lockfile_dir(request.scope)
    elif manifest_path is None:
        project_dir = Path(".")
    elif Path(manifest_path).is_file():
        project_dir = Path(manifest_path).parent
    else:
        project_dir = Path(manifest_path)
    return project_dir / "apm.lock.yaml"
