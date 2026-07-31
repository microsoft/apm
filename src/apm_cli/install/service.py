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

from typing import TYPE_CHECKING

from apm_cli.install.request import InstallRequest
from apm_cli.models.results import InstallDisposition

if TYPE_CHECKING:
    from apm_cli.core.lifecycle_scripts import LifecycleEvent, LifecycleScriptRunner
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

    def run(self, request: InstallRequest) -> InstallResult:
        """Execute the install pipeline and return the structured result.

        Fires ``pre-install`` / ``post-install`` lifecycle scripts around
        the pipeline when scripts are configured.

        Raises:
            InstallNotAvailableError: if the dependency subsystem failed
                to import (e.g. missing optional extras).  Adapters are
                responsible for presenting this to the user.
            FrozenInstallError: when ``request.frozen`` is True and the
                lockfile is missing or structurally out of sync with
                ``request.apm_package``.  Raised before the pipeline
                runs so no resolve / download work is wasted.
        """
        # Enforce --frozen BEFORE invoking the pipeline.  The check is
        # purely structural (no network) so it must succeed or fail in
        # well under a second; running it here keeps the contract simple
        # for the pipeline (which never sees a `frozen` flag).
        if request.frozen:
            self.enforce_frozen(request)

        # Local import keeps service module import-cheap and matches the
        # existing pipeline's lazy-import discipline.
        try:
            from apm_cli.install.pipeline import run_install_pipeline
        except ImportError as e:  # pragma: no cover -- defensive
            raise InstallNotAvailableError(f"APM dependency system not available: {e}") from e

        runner = self._build_script_runner(request)
        event = self._build_event("pre-install", request)
        runner.fire("pre-install", event)

        result = run_install_pipeline(
            request.apm_package,
            update_refs=request.update_refs,
            verbose=request.verbose,
            only_packages=request.only_packages,
            force=request.force,
            parallel_downloads=request.parallel_downloads,
            logger=request.logger,
            scope=request.scope,
            auth_resolver=request.auth_resolver,
            target=request.target,
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
            transaction=request.transaction,
        )

        if result.disposition in {
            InstallDisposition.SUCCESS,
            InstallDisposition.PARTIAL_SUCCESS,
        }:
            post_event = self._build_event("post-install", request)
            runner.fire("post-install", post_event)

        return result

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
    def enforce_frozen(request: InstallRequest) -> None:
        """Raise :class:`FrozenInstallError` if lockfile is absent or stale.

        Looks up ``apm.lock.yaml`` next to the manifest's ``apm.yml``,
        loads it, and checks both package dependencies and the canonical
        current MCP config view. Any miss raises before install mutation.
        """
        from pathlib import Path

        from apm_cli.deps.lockfile import LockFile
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.plan import lockfile_satisfies_manifest

        manifest_path = getattr(request.apm_package, "package_path", None)
        if manifest_path is None:
            project_dir = Path(".")
        elif Path(manifest_path).is_file():
            project_dir = Path(manifest_path).parent
        else:
            project_dir = Path(manifest_path)
        lockfile_path = project_dir / "apm.lock.yaml"

        if not lockfile_path.exists():
            raise FrozenInstallError(
                "--frozen requires apm.lock.yaml to exist. "
                "Run 'apm install' (without --frozen) or 'apm update' first.",
            )

        try:
            lockfile = LockFile.read(lockfile_path)
        except Exception as e:
            raise FrozenInstallError(
                f"--frozen could not read apm.lock.yaml: {e}",
            ) from e

        if lockfile is None:
            raise FrozenInstallError(
                "--frozen requires apm.lock.yaml to exist. "
                "Run 'apm install' (without --frozen) or 'apm update' first.",
            )

        manifest_deps = list(request.apm_package.get_apm_dependencies())
        manifest_deps.extend(request.apm_package.get_dev_apm_dependencies())

        satisfied, reasons = lockfile_satisfies_manifest(lockfile, manifest_deps)
        from apm_cli.core.scope import get_modules_dir
        from apm_cli.integration.mcp_config_view import CurrentMcpConfigView

        root_mcp = list(request.apm_package.get_all_mcp_dependencies())
        check_mcp = bool(root_mcp) or bool(lockfile.mcp_configs) or bool(lockfile.mcp_servers)
        if check_mcp:
            current_mcp = CurrentMcpConfigView.derive(
                request.apm_package,
                lockfile,
                get_modules_dir(request.scope),
                trust_transitive_self_defined=request.trust_transitive_mcp,
            )
            config_diff = current_mcp.diff(lockfile.mcp_configs)
            if current_mcp.problems:
                reasons.extend(
                    f"  - {problem.package_key}: {problem.message}"
                    for problem in current_mcp.problems
                )
            if not config_diff.is_empty:
                for name in sorted(config_diff.changed):
                    reasons.append(f"  - MCP server {name!r} config differs from apm.lock.yaml")
                for name in sorted(config_diff.source_only):
                    reasons.append(f"  - MCP server {name!r} is missing from apm.lock.yaml")
                for name in sorted(config_diff.lock_only):
                    reasons.append(f"  - MCP server {name!r} is no longer declared in apm.yml")
            expected_names = set(current_mcp.configs)
            locked_names = set(lockfile.mcp_servers)
            if expected_names != locked_names and config_diff.is_empty:
                reasons.append("  - MCP server names in apm.lock.yaml are out of sync with apm.yml")

        satisfied = satisfied and not reasons
        if not satisfied:
            raise FrozenInstallError(
                "--frozen: apm.lock.yaml is out of sync with apm.yml.",
                reasons=reasons,
            )
