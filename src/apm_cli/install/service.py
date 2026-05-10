"""Application Service: orchestrates one install invocation.

The ``InstallService`` is the *behaviour-bearing* entry point for installs.
Adapters (the Click handler today; programmatic / API callers tomorrow)
build an :class:`InstallRequest` and call :meth:`InstallService.run`,
which returns a :class:`InstallResult`.  Adapters own presentation,
``sys.exit``, and CLI option parsing -- the service does not.

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

if TYPE_CHECKING:
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
            self._enforce_frozen(request)

        # Local import keeps service module import-cheap and matches the
        # existing pipeline's lazy-import discipline.
        try:
            from apm_cli.install.pipeline import run_install_pipeline
        except ImportError as e:  # pragma: no cover -- defensive
            raise InstallNotAvailableError(f"APM dependency system not available: {e}") from e

        return run_install_pipeline(
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
            skill_subset=request.skill_subset,
            skill_subset_from_cli=request.skill_subset_from_cli,
            legacy_skill_paths=request.legacy_skill_paths,
            plan_callback=request.plan_callback,
        )

    @staticmethod
    def _enforce_frozen(request: InstallRequest) -> None:
        """Raise :class:`FrozenInstallError` if lockfile is absent or stale.

        Looks up ``apm.lock.yaml`` next to the manifest's ``apm.yml``,
        loads it, and runs ``lockfile_satisfies_manifest`` against the
        manifest's direct deps (regular + dev).  Any miss raises with a
        list of human-readable reasons the renderer can show.
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
        if not satisfied:
            raise FrozenInstallError(
                "--frozen: apm.lock.yaml is out of sync with apm.yml.",
                reasons=reasons,
            )
