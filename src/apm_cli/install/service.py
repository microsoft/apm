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

from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.install.request import InstallRequest
from apm_cli.models.results import InstallDisposition

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.lifecycle_scripts import LifecycleEvent, LifecycleScriptRunner
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.dependency.reference import DependencyReference
    from apm_cli.models.results import InstallResult


class InstallNotAvailableError(RuntimeError):
    """Raised when the APM dependency subsystem failed to import."""


class InstallService:
    """Application service for the APM install pipeline.

    Stateless: a single instance can serve multiple ``run(request)``
    invocations.  Constructor takes no arguments today but exists as the
    extension point for collaborator injection (downloader, scanner,
    integrator factory) when programmatic callers need to swap them.
    """

    @staticmethod
    def frozen_writes(frozen: bool) -> AbstractContextManager[list[str] | None]:
        """Protect the complete caller lifetime with the canonical write chokepoint."""
        from apm_cli.deps.lockfile import suppress_lockfile_writes

        return suppress_lockfile_writes() if frozen else nullcontext()

    @staticmethod
    def enforce_frozen_services(
        lockfile: LockFile,
        *,
        mcp_servers: set[str],
        mcp_targets: list[str],
        lsp_servers: set[str],
        lsp_targets: list[str],
        current_mcp: CurrentMcpConfigView | None,
        user_scope: bool,
    ) -> None:
        """Refuse unrecorded service claims before either service mutates native state."""
        from apm_cli.install.errors import FrozenInstallError
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
        for service, names, targets, recorded in (
            ("MCP", mcp_servers, mcp_targets, mcp_ownership),
            ("LSP", lsp_servers, lsp_targets, lockfile.lsp_target_servers),
        ):
            for target in targets:
                for name in sorted(names - set(recorded.get(target, ()))):
                    reasons.append(
                        f"  - {service} server {name!a} target {target!a} "
                        "ownership is not recorded in apm.lock.yaml"
                    )
        if reasons:
            raise FrozenInstallError(
                "--frozen cannot replay the selected native services.",
                reasons=reasons,
                tip=(
                    "Tip: native MCP/LSP configs were not changed. Rerun the same "
                    "install command without --frozen, retaining the intended --target, "
                    "--only, --root DIR or --global options, then commit apm.lock.yaml."
                ),
            )

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

        runner = self._build_script_runner(request)
        event = self._build_event("pre-install", request)
        runner.fire("pre-install", event)

        if request.frozen:
            # req-lk-006: the lockfile is never written in frozen mode.
            # Suppress at the chokepoint, then decide from what was
            # withheld whether this install can report success.  The
            # pipeline still deploys, as `npm ci` does.
            from apm_cli.deps.lockfile import suppress_lockfile_writes

            with suppress_lockfile_writes() as withheld:
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
            self._enforce_frozen_no_rewrite(request, withheld)

        if install_worked:
            post_event = self._build_event("post-install", request)
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
        from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo

        packages = []
        for dep in request.apm_package.get_apm_dependencies():
            packages.append(
                PackageInfo(
                    name=dep.repo_url or str(dep),
                    reference=dep.reference,
                )
            )

        scope_name = "project"
        if request.scope is not None:
            scope_name = (
                request.scope.value if hasattr(request.scope, "value") else str(request.scope)
            )

        project_root = None
        pkg_path = getattr(request.apm_package, "package_path", None)
        if pkg_path is not None:
            project_root = str(pkg_path)

        return LifecycleEvent.create(
            event=event_name,
            packages=packages,
            scope=scope_name,
            working_directory=project_root,
        )

    @staticmethod
    def reject_frozen_mutation(frozen: bool, operation: str) -> None:
        """Reject an add-style install operation before it can mutate state."""
        if not frozen:
            return
        from apm_cli.install.errors import FrozenInstallError

        raise FrozenInstallError(
            f"--frozen cannot be combined with {operation}. "
            "Update apm.yml first, run 'apm install' without --frozen to refresh "
            "apm.lock.yaml, then retry the frozen install."
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
        from apm_cli.deps.lockfile import LockFile, LockfileConflictError
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.plan import (
            lockfile_satisfies_manifest,
            lockfile_satisfies_replacements,
        )

        lockfile_path = _frozen_lockfile_path(request)
        missing_message = (
            f"--frozen requires apm.lock.yaml to exist at {lockfile_path}. "
            "Rerun the same install command without --frozen, retaining "
            "--root DIR or --global if specified."
        )

        if not lockfile_path.exists():
            raise FrozenInstallError(missing_message, tip="")

        try:
            lockfile = LockFile.read(lockfile_path)
        except LockfileConflictError as e:
            raise FrozenInstallError(
                f"--frozen cannot read the lockfile. {e}\nFor CI, commit the resolved lockfile.",
                tip="",
            ) from e
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

        reasons.extend(
            InstallService._frozen_mcp_reasons(
                request.apm_package,
                lockfile,
                modules_dir,
                trust_transitive_mcp=request.trust_transitive_mcp,
                logger=request.logger,
                allow_pending=allow_pending_mcp,
            )
        )

        satisfied = satisfied and not reasons
        if not satisfied:
            raise FrozenInstallError(
                "--frozen: apm.lock.yaml is out of sync with apm.yml.",
                reasons=reasons,
            )

        InstallService._verify_frozen_local_content(request, lockfile, manifest_deps)

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
    def _verify_frozen_local_content(
        request: InstallRequest,
        lockfile: LockFile,
        manifest_deps: list[DependencyReference],
    ) -> None:
        """Verify selected local declarations before any lifecycle or deployment."""
        from collections import deque
        from dataclasses import replace

        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.deps.path_anchoring import (
            LocalResolutionError,
            build_local_parent_index,
            resolve_local_dep_dir,
        )
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.phases.local_content import compute_local_package_hash
        from apm_cli.install.plan import lockfile_satisfies_manifest
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.models.dependency.replacement import build_replacement_index

        queue = deque(dep for dep in manifest_deps if dep.is_local)
        queue.extend(InstallService._selected_locked_replacements(lockfile, manifest_deps))
        if not queue:
            return
        package_root = Path(request.apm_package.package_path).resolve()
        consumer_root = request.apm_package.source_path or package_root
        rules = build_replacement_index(request.apm_package.replace_rules or [])
        parent_index = build_local_parent_index(lockfile)
        resolved_cache: dict[str, Path] = {}
        seen: set[str] = set()
        reasons: list[str] = []
        while queue:
            declared = queue.popleft()
            key = declared.get_unique_key()
            if key in seen:
                continue
            seen.add(key)
            satisfied, missing = lockfile_satisfies_manifest(lockfile, [declared])
            if not satisfied:
                reasons.extend(missing)
                continue
            dependency = lockfile.get_dependency(key)
            try:
                source = resolve_local_dep_dir(
                    dependency,
                    lockfile,
                    consumer_root,
                    parent_index=parent_index,
                    resolved_cache=resolved_cache,
                )
                selected_source = source
                if source.is_file():
                    # Admission must see the selected leaf, not a resolved symlink.
                    source_base = resolve_local_dep_dir(
                        replace(dependency, local_path="."),
                        lockfile,
                        package_root,
                        parent_index=parent_index,
                    )
                    selected_source = source_base / Path(dependency.local_path).expanduser()
                observed_hash = compute_local_package_hash(selected_source)
                expected_hash = dependency.content_hash
                # v0.1 readers also accept the identical SHA-256 digest without its envelope.
                if (
                    observed_hash != expected_hash
                    and observed_hash.removeprefix("sha256:") != expected_hash
                ):
                    reasons.append(
                        f"  - local dependency {declared.local_path!r} content differs "
                        "from its locked snapshot"
                    )
                    continue
                # Only selected, verified local manifests can introduce further
                # local declarations. Do not inspect orphan lock entries or
                # expand local paths declared by untrusted remote packages.
                if (source / "apm.yml").is_file():
                    package = APMPackage.from_apm_yml(source / "apm.yml", source_path=source)
                    for child in package.get_apm_dependencies():
                        child = rules.bind(
                            child,
                            consumer_root=consumer_root,
                            modules_dir=package_root / "apm_modules",
                            parent=key,
                            is_dev=dependency.is_dev,
                        )
                        if not child.is_local:
                            continue
                        if child.replacement is not None:
                            queue.append(child)
                            continue
                        anchored = APMDependencyResolver._compute_dep_source_path(
                            child, package, source
                        )
                        child = replace(
                            child,
                            declaring_parent=key,
                            anchored_local_path=APMDependencyResolver._portable_anchor_identity(
                                anchored, request.apm_package.source_path or package_root
                            ),
                        )
                        queue.append(child)
            except (LocalResolutionError, OSError, ValueError) as exc:
                reasons.append(
                    f"  - local dependency {declared.local_path!r} cannot be verified: {exc}"
                )
        if reasons:
            raise FrozenInstallError(
                "--frozen: local dependency content does not match apm.lock.yaml.",
                reasons=reasons,
                tip=(
                    "Restore the locked local content, or run 'apm install' without "
                    "--frozen to explicitly accept and record the change."
                ),
            )

    @staticmethod
    def _frozen_mcp_reasons(
        package: APMPackage,
        lockfile: LockFile,
        modules_dir: Path,
        *,
        trust_transitive_mcp: bool,
        logger: InstallLogger | None,
        allow_pending: bool,
        active_package_keys: frozenset[str] | None = None,
    ) -> list[str]:
        """Compare actual declarations, deferring only unavailable source evidence."""
        from apm_cli.integration.mcp_config_view import CurrentMcpConfigView

        if allow_pending and not (
            package.get_all_mcp_dependencies() or lockfile.mcp_configs or lockfile.mcp_servers
        ):
            return []
        current = CurrentMcpConfigView.derive(
            package,
            lockfile,
            modules_dir,
            trust_transitive_self_defined=trust_transitive_mcp,
            logger=logger,
            allow_pending=allow_pending,
            active_package_keys=active_package_keys,
        )
        pending = current.pending_servers(lockfile) if allow_pending else frozenset()
        diff = current.diff(lockfile.mcp_configs)
        reasons = [f"  - {problem.package_key}: {problem.message}" for problem in current.problems]
        for name in sorted(diff.changed):
            reasons.append(f"  - MCP server {name!r} config differs from apm.lock.yaml")
        for name in sorted(diff.source_only):
            reasons.append(f"  - MCP server {name!r} is missing from apm.lock.yaml")
        for name in sorted(diff.lock_only - pending):
            reasons.append(f"  - MCP server {name!r} is no longer declared in apm.yml")
        if set(current.configs) | pending != set(lockfile.mcp_servers) and not reasons:
            reasons.append("  - MCP server names in apm.lock.yaml are out of sync with apm.yml")
        return reasons

    @staticmethod
    def enforce_hydrated_mcp(
        package: APMPackage,
        lockfile: LockFile,
        modules_dir: Path,
        *,
        trust_transitive_mcp: bool,
        logger: InstallLogger | None,
        active_package_keys: frozenset[str] | None = None,
    ) -> None:
        """Reject restored MCP drift before a package can activate native configuration."""
        from apm_cli.install.errors import FrozenInstallError

        reasons = InstallService._frozen_mcp_reasons(
            package,
            lockfile,
            modules_dir,
            trust_transitive_mcp=trust_transitive_mcp,
            logger=logger,
            allow_pending=False,
            active_package_keys=active_package_keys,
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
    def _enforce_frozen_no_rewrite(request: InstallRequest, withheld: list[str]) -> None:
        """Raise when a withheld write claims paths the committed lockfile omits.

        Compares each lockfile the frozen install *would* have written
        against the committed ``apm.lock.yaml`` on disk.  It reads that one
        file and does not otherwise inspect the filesystem: the deployed set
        is taken from the withheld lockfiles, which is what install recorded
        for the files it just deployed.

        Withholding the write satisfies req-lk-006 on its own, but silently:
        a committed lockfile that under-records the deployed set would stay
        green while install deployed files it does not claim, and a file no
        lockfile row claims carries no recorded hash, which puts it outside
        ``content-integrity``'s hash comparison *and* its hidden-Unicode
        scan for as long as it stays omitted (#2379).  CI is the one place
        positioned to catch that, so frozen mode reports it.

        Deliberately one-directional, matching the tolerance
        :class:`FrozenInstallError` already documents.  Only paths the
        install would *add* to the ledger fail; claims it would drop do
        not, because a legitimately narrower install (``--target`` filter,
        ``--only``, a removed dependency) produces exactly that and
        ``no-orphaned-packages`` / ``deployed-files-present`` already own
        the opposite direction.

        Equivalence is judged by ``is_semantically_equivalent``, which
        ignores ``generated_at`` and ``apm_version``: per Section 5.5 those
        two fields "MUST NOT affect content-equivalence comparison", so a
        newer CLI reading an older lockfile is not by itself a rewrite.
        """
        if not withheld:
            return

        from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.install.errors import FrozenInstallError

        # Re-read rather than reuse the copy `enforce_frozen` loaded: this
        # judges what is on disk now.  `--frozen` is a CI primitive with a
        # single writer, so nothing else is expected to touch the file
        # between the two reads; drift.py's own membership checks make the
        # same assumption.
        committed = LockFile.read(_frozen_lockfile_path(request))
        if committed is None:  # pragma: no cover -- enforce_frozen ran first
            return
        # DeploymentLedgerCodec owns the deployed-files vocabulary (see
        # `scripts/lint-architecture-boundaries.sh`); ask it directly rather
        # than borrowing drift.py's private wrapper around the same call.
        committed_paths = set(DeploymentLedgerCodec.legacy_deployed_file_claims(committed))

        unclaimed: set[str] = set()
        for payload in withheld:
            candidate = LockFile.from_yaml(payload)
            if committed.is_semantically_equivalent(candidate):
                continue
            claimed = set(DeploymentLedgerCodec.legacy_deployed_file_claims(candidate))
            unclaimed |= claimed - committed_paths
        if not unclaimed:
            return

        raise FrozenInstallError(
            "--frozen: apm.lock.yaml does not record everything this install deploys.",
            reasons=[
                f"  - {path} is deployed by this install but not recorded in apm.lock.yaml"
                for path in _outermost(unclaimed)
            ],
            tip=(
                "Tip: the files were still deployed; only the lockfile write was "
                "withheld. Rerun 'apm install' without --frozen, retaining --root DIR "
                "or --global if specified, then commit apm.lock.yaml."
            ),
        )


def _outermost(paths: set[str]) -> list[str]:
    """Sort *paths*, dropping any already covered by a listed ancestor.

    ``deployed_files`` records a primitive's directory as well as each file
    beneath it, so an unrecorded skill otherwise reports twice.  Sorted
    order puts an ancestor before its descendants, so a single pass over
    the accumulated output is enough.

    The prefix scan is O(n^2) in the number of unclaimed paths.  It runs
    only on the error path of a failing frozen install, where *n* is what
    one install left unrecorded and the process is about to exit.
    """
    outermost: list[str] = []
    for path in sorted(paths):
        if not any(path.startswith(f"{parent}/") for parent in outermost):
            outermost.append(path)
    return outermost


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
