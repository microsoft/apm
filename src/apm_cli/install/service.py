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

from pathlib import Path
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
                lockfile is missing, structurally out of sync with
                ``request.apm_package``, or not what the install
                produces.  The first two are raised before the pipeline
                runs so no resolve / download work is wasted.
        """
        # Enforce --frozen BEFORE invoking the pipeline.  The check is
        # purely structural (no network) so it must succeed or fail in
        # well under a second; running it here keeps the contract simple
        # for the pipeline (which never sees a `frozen` flag).
        if request.frozen:
            self._enforce_frozen(request)

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
            self._enforce_frozen_no_rewrite(request, withheld)
        else:
            result = self._run_pipeline(request)

        if result.disposition in {
            InstallDisposition.SUCCESS,
            InstallDisposition.PARTIAL_SUCCESS,
        }:
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
    def _enforce_frozen(request: InstallRequest) -> None:
        """Raise :class:`FrozenInstallError` if lockfile is absent or stale.

        Looks up ``apm.lock.yaml`` next to the manifest's ``apm.yml``,
        loads it, and runs ``lockfile_satisfies_manifest`` against the
        manifest's direct deps (regular + dev).  Any miss raises with a
        list of human-readable reasons the renderer can show.
        """
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.install.errors import FrozenInstallError
        from apm_cli.install.plan import lockfile_satisfies_manifest

        lockfile_path = _frozen_lockfile_path(request)

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

    @staticmethod
    def _enforce_frozen_no_rewrite(request: InstallRequest, withheld: list[str]) -> None:
        """Raise when a withheld write claims deployed files disk does not.

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

        from apm_cli.deps.lockfile import LockFile
        from apm_cli.install.drift import _collect_tracked_files
        from apm_cli.install.errors import FrozenInstallError

        committed = LockFile.read(_frozen_lockfile_path(request))
        if committed is None:  # pragma: no cover -- _enforce_frozen ran first
            return
        committed_paths = set(_collect_tracked_files(committed))

        unclaimed: set[str] = set()
        for payload in withheld:
            candidate = LockFile.from_yaml(payload)
            if committed.is_semantically_equivalent(candidate):
                continue
            unclaimed |= set(_collect_tracked_files(candidate)) - committed_paths
        if not unclaimed:
            return

        raise FrozenInstallError(
            "--frozen: apm.lock.yaml does not record everything this install deploys.",
            reasons=[
                f"  - {path} is deployed by this install but not recorded in apm.lock.yaml"
                for path in _outermost(unclaimed)
            ],
            tip="Tip: run 'apm install' without --frozen, then commit apm.lock.yaml.",
        )


def _outermost(paths: set[str]) -> list[str]:
    """Sort *paths*, dropping any already covered by a listed ancestor.

    ``deployed_files`` records a primitive's directory as well as each file
    beneath it, so an unrecorded skill otherwise reports twice.  Sorted
    order puts an ancestor before its descendants, so a single pass over
    the accumulated output is enough.
    """
    outermost: list[str] = []
    for path in sorted(paths):
        if not any(path.startswith(f"{parent}/") for parent in outermost):
            outermost.append(path)
    return outermost


def _frozen_lockfile_path(request: InstallRequest) -> Path:
    """Locate ``apm.lock.yaml`` beside the request's manifest."""
    manifest_path = getattr(request.apm_package, "package_path", None)
    if manifest_path is None:
        project_dir = Path(".")
    elif Path(manifest_path).is_file():
        project_dir = Path(manifest_path).parent
    else:
        project_dir = Path(manifest_path)
    return project_dir / "apm.lock.yaml"
