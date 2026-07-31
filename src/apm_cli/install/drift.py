"""Drift-detection replay engine for ``apm audit --check drift``.

Reproduces the integration step from the lockfile in an isolated scratch
directory, then diffs the resulting tree against the working project to
surface four kinds of divergence:

* ``modified``     -- a tracked deployed file's content differs.
* ``unintegrated`` -- a tracked deployed file is missing from the project.
* ``orphaned``     -- a managed-directory file exists in the project but
  is not present in the scratch replay AND not tracked in the lockfile.
* ``unrecorded``   -- the replay deploys the file and the project has it,
  but no lockfile entry claims it, which exempts it from every
  membership-driven check (issue #2379).

Bare ``apm audit`` keeps the original **cache-only** contract: cached package
contents under ``apm_modules/`` are the source of truth and a miss is reported
instead of auto-fetched. ``apm audit --ci`` can opt into a lock-pinned,
scratch-only self-hydration path so cold-cache CI still evaluates drift
without mutating the checkout.

Design constraints (see ``WIP/drift/06-final-plan.md``):
* Pure read-only against the project tree -- writes go to the scratch
  directory only.  ``ensure_path_within`` guards every write redirection.
* ASCII-only console output (Windows cp1252 safety).
* Normalization strips line-ending differences, BOMs, and the APM
  ``Build ID`` header that legitimately changes on every recompile.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from apm_cli.core.command_logger import CommandLogger
from apm_cli.deps.path_anchoring import resolve_local_dep_dir
from apm_cli.install.drift_render import (
    render_drift as render_drift,
)
from apm_cli.install.drift_render import (
    render_drift_json as render_drift_json,
)
from apm_cli.install.drift_render import (
    render_drift_sarif as render_drift_sarif,
)
from apm_cli.install.drift_render import (
    render_drift_text as render_drift_text,
)
from apm_cli.utils.console import STATUS_SYMBOLS
from apm_cli.utils.guards import _ReadOnlyProjectGuard

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayConfig:
    """Locked configuration for a drift replay run.

    Frozen so callers cannot mutate it mid-replay -- any change requires
    a new instance, which keeps the contract auditable.
    """

    project_root: Path
    lockfile_path: Path
    targets: frozenset[str] | None = None
    cache_only: bool = True
    no_hooks: bool = True
    parallel_downloads: int = 1
    scratch_root: Path | None = None
    modules_root: Path | None = None


@dataclass(frozen=True)
class DriftFinding:
    """A single divergence between the replay scratch tree and the project."""

    path: str
    kind: str  # one of "modified" | "unintegrated" | "orphaned" | "unrecorded"
    package: str = ""
    inline_diff: str = ""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CacheMissError(RuntimeError):
    """Raised when ``cache_only=True`` but a package is not in the cache."""


# ---------------------------------------------------------------------------
# Normalization helpers (operate on bytes; bytes-in / bytes-out)
#
# Re-exported from ``apm_cli.utils.normalization`` so existing callers and
# tests that import ``_strip_build_id`` / ``_normalize`` from this module
# keep working. The implementation lives in ``utils/`` so future callers
# (policy linters, content-scan helpers) can reuse it without importing
# the drift module.
# ---------------------------------------------------------------------------

from apm_cli.utils.normalization import (  # noqa: E402, F401  -- re-exported; tests import helpers from apm_cli.install.drift
    _normalize,
    _normalize_line_endings,
    _strip_bom,
    _strip_build_id,
)

# ---------------------------------------------------------------------------
# Scratch directory lifecycle
# ---------------------------------------------------------------------------


def _assert_scratch_bound(project_root: Path, scratch_root: Path) -> None:
    """Defense-in-depth: a scratch dir must NOT live inside the project tree.

    Prevents the replay engine from accidentally writing into the live
    project (which would defeat the read-only contract).
    """
    project_root = project_root.resolve()
    scratch_root = scratch_root.resolve()
    try:
        scratch_root.relative_to(project_root)
    except ValueError:
        return
    raise RuntimeError(
        f"drift scratch dir {scratch_root!s} is inside project tree "
        f"{project_root!s}; refusing to proceed"
    )


def _make_scratch_root(project_root: Path) -> Path:
    """Allocate a scratch dir outside the project tree, with atexit cleanup."""
    scratch = Path(tempfile.mkdtemp(prefix="apm_drift_"))
    _assert_scratch_bound(project_root, scratch)

    def _cleanup() -> None:
        try:
            shutil.rmtree(scratch, ignore_errors=False)
        except OSError as exc:
            click.echo(
                f"{STATUS_SYMBOLS['warning']} failed to clean drift scratch dir {scratch}: {exc}",
                err=True,
            )

    atexit.register(_cleanup)
    return scratch


def _clear_path(path: Path) -> None:
    """Remove one file tree so a fresh materialization can replace it."""
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()


def _copy_install_tree(source: Path, target: Path) -> None:
    """Clone one materialized package tree into the scratch modules root."""
    _clear_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True)


# ---------------------------------------------------------------------------
# Stderr-only logger for audit phases (CommandLogger writes to stdout)
# ---------------------------------------------------------------------------


class CheckLogger(CommandLogger):
    """CheckLogger emits drift phase markers to stderr.

    ``CommandLogger._rich_*`` writes to stdout (intended for human
    install output).  Audit/drift output must stay on stderr so that
    machine-parseable JSON/SARIF on stdout is never polluted.
    """

    def __init__(self, verbose: bool = False) -> None:
        super().__init__("audit-drift", verbose=verbose)

    def _emit(self, symbol_key: str, msg: str) -> None:
        click.echo(f"{STATUS_SYMBOLS[symbol_key]} {msg}", err=True)

    def replay_start(self) -> None:
        self._emit("running", "Replaying install...")

    def scratch_root(self, path: Path) -> None:
        """Verbose-only: announce the scratch tmpdir to stderr.

        Stays on stderr so JSON/SARIF stdout payloads remain
        machine-parseable. Self-gates on ``self.verbose`` so the
        normal-mode user never sees it.
        """
        if not self.verbose:
            return
        click.echo(
            f"{STATUS_SYMBOLS['info']} drift scratch root: {path}",
            err=True,
        )

    def diff_start(self) -> None:
        self._emit("running", "Diffing scratch vs working tree...")

    def replay_complete(self, n: int) -> None:
        self._emit("check", f"Replayed {n} package(s)")

    def clean(self) -> None:
        self._emit("check", "No drift detected")

    def findings(self, n: int) -> None:
        self._emit("warning", f"Drift detected: {n} file(s)")


# ---------------------------------------------------------------------------
# Package materialization
# ---------------------------------------------------------------------------


def _verify_remote_cache_candidate(lock_dep: LockedDependency, candidate: Path) -> None:
    """Fail closed when a cached remote dependency is absent or unverifiable."""
    if (
        getattr(lock_dep, "source", None) not in {"local", "registry"}
        and not lock_dep.resolved_commit
    ):
        raise CacheMissError(
            f"cannot replay {lock_dep.repo_url}: lockfile entry has no resolved_commit "
            "(cache freshness unverifiable). Re-run 'apm install' with a pinned ref "
            "(commit, tag, or specific branch HEAD) before audit."
        )
    if not candidate.exists():
        _ref_label = lock_dep.resolved_commit or lock_dep.version or "unknown"
        raise CacheMissError(
            f"cache miss for {lock_dep.repo_url}@{_ref_label}: "
            f"expected {candidate}; run 'apm install' to populate the cache"
        )
    if lock_dep.resolved_commit:
        from apm_cli.install.cache_pin import CachePinError, verify_marker

        try:
            verify_marker(candidate, lock_dep.resolved_commit)
        except CachePinError as exc:
            raise CacheMissError(f"{exc}; run 'apm install' to refresh apm_modules cache") from exc


def _materialize_install_path(
    lock_dep: LockedDependency,
    project_root: Path,
    apm_modules_dir: Path,
    cache_only: bool,
    *,
    lockfile: LockFile | None = None,
    live_modules_dir: Path | None = None,
    downloader: Any | None = None,
    registry_resolver: Any | None = None,
    registries: dict[str, str] | None = None,
) -> Path:
    """Resolve the on-disk path for a locked dep's package contents.

    For local deps -- contents live at the source directory the install
    resolver anchored on: ``project_root`` for direct (root-declared) deps,
    or the declaring package's directory for transitive ``../sibling`` deps
    (resolved via ``resolved_by``; see
    :func:`apm_cli.deps.path_anchoring.resolve_local_dep_dir`). The
    ``lockfile`` is required to walk that chain; it is unused for remote
    deps and for direct local deps (``resolved_by is None``).
    For remote deps -- contents live at the canonical apm_modules subpath.
    In ``cache_only`` mode the replay reads a verified live cache entry. In
    self-hydrating mode it materializes a scratch-private copy using the lock's
    pinned commit or registry URL/hash, never the mutable manifest ref.

    Raises
    ------
    CacheMissError
        If ``cache_only`` is True and the resolved source path does not
        exist (cold-cache-like: the source is simply not present yet).
    LocalResolutionError
        If a local dep's ``resolved_by`` chain is internally inconsistent
        (missing / ambiguous / non-local / cyclic parent). This is a
        corrupt-lockfile condition and MUST fail loud -- it is not caught
        by the drift gate's cache-miss soft-skip.
    """
    if lock_dep.source == "local":
        if not lock_dep.local_path:
            raise CacheMissError(f"local dep {lock_dep.repo_url!r} has no local_path in lockfile")
        candidate = resolve_local_dep_dir(lock_dep, lockfile, project_root)
        if not candidate.exists():
            raise CacheMissError(
                f"local source missing for {lock_dep.local_path!r}: expected {candidate}"
            )
        return candidate

    dep_ref = lock_dep.to_dependency_ref()
    candidate = dep_ref.get_install_path(apm_modules_dir)
    live_root = live_modules_dir or (project_root / "apm_modules")
    live_candidate = dep_ref.get_install_path(live_root)
    if live_candidate.exists():
        try:
            _verify_remote_cache_candidate(lock_dep, live_candidate)
        except CacheMissError:
            if cache_only:
                raise
        else:
            if candidate != live_candidate:
                _copy_install_tree(live_candidate, candidate)
            return candidate
    if cache_only:
        _verify_remote_cache_candidate(lock_dep, live_candidate)

    if lock_dep.source == "registry":
        if registry_resolver is None:
            raise CacheMissError(
                f"registry replay unavailable for {lock_dep.repo_url}: no registry resolver configured"
            )
        if not lock_dep.resolved_url or not lock_dep.resolved_hash or not lock_dep.version:
            raise CacheMissError(
                f"cannot replay {lock_dep.repo_url}: lockfile entry is missing "
                "resolved_url/resolved_hash/version"
            )
        from apm_cli.deps.registry.auth import (
            dependency_ref_with_registry_name_from_lockfile,
        )

        download_ref = dependency_ref_with_registry_name_from_lockfile(
            dep_ref,
            registries or {},
            locked_dep=lock_dep,
        )
        registry_resolver.download_from_lockfile(
            download_ref,
            candidate,
            resolved_url=lock_dep.resolved_url,
            resolved_hash=lock_dep.resolved_hash,
            version=lock_dep.version,
        )
        return candidate

    if downloader is None:
        raise CacheMissError(f"git replay unavailable for {lock_dep.repo_url}: no downloader")
    from apm_cli.drift import build_download_ref

    download_ref = build_download_ref(
        dep_ref,
        lockfile,
        update_refs=False,
        ref_changed=False,
    )
    package_info = downloader.download_package(download_ref, candidate)
    resolved_reference = getattr(package_info, "resolved_reference", None)
    if (
        lock_dep.resolved_commit
        and resolved_reference is not None
        and getattr(resolved_reference, "resolved_commit", None)
        not in {None, lock_dep.resolved_commit}
    ):
        raise CacheMissError(
            f"scratch replay resolved {lock_dep.repo_url} to "
            f"{resolved_reference.resolved_commit}, expected {lock_dep.resolved_commit}"
        )
    return candidate


def _build_package_info(
    lock_dep: LockedDependency,
    install_path: Path,
):
    """Construct a real ``PackageInfo`` for the integrators.

    Loads ``apm.yml`` when present so integrators that read
    ``package_info.package.name`` see the right package identity.
    """
    from apm_cli.models.apm_package import (
        APMPackage,
        GitReferenceType,
        PackageInfo,
        ResolvedReference,
    )
    from apm_cli.models.validation import detect_package_type

    apm_yml = install_path / "apm.yml"
    if apm_yml.exists():
        try:
            pkg = APMPackage.from_apm_yml(apm_yml, source_path=install_path)
        except Exception:
            pkg = APMPackage(
                name=install_path.name,
                version=lock_dep.version or "unknown",
                package_path=install_path,
                source=lock_dep.repo_url,
            )
        if not pkg.source:
            pkg.source = lock_dep.repo_url
    else:
        pkg = APMPackage(
            name=install_path.name,
            version=lock_dep.version or "unknown",
            package_path=install_path,
            source=lock_dep.repo_url,
        )

    resolved_ref = ResolvedReference(
        original_ref=lock_dep.resolved_ref or "locked",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=lock_dep.resolved_commit or "locked",
        ref_name=lock_dep.resolved_ref or "locked",
    )

    info = PackageInfo(
        package=pkg,
        install_path=install_path,
        resolved_reference=resolved_ref,
        dependency_ref=lock_dep.to_dependency_ref(),
    )
    try:
        pkg_type, _ = detect_package_type(install_path)
        info.package_type = pkg_type
    except Exception:
        info.package_type = None
    return info


# ---------------------------------------------------------------------------
# Replay orchestrator
# ---------------------------------------------------------------------------


def _make_integrators():
    """Build a fresh integrator set for one replay run.

    Mirrors ``apm_cli.install.phases.targets:208-215`` so the replay
    behaves identically to a real ``apm install --integrate``.
    """
    from apm_cli.integration.agent_integrator import AgentIntegrator
    from apm_cli.integration.command_integrator import CommandIntegrator
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.instruction_integrator import InstructionIntegrator
    from apm_cli.integration.prompt_integrator import PromptIntegrator
    from apm_cli.integration.skill_integrator import SkillIntegrator

    return {
        "prompt": PromptIntegrator(),
        "agent": AgentIntegrator(),
        "skill": SkillIntegrator(),
        "command": CommandIntegrator(),
        "hook": HookIntegrator(),
        "instruction": InstructionIntegrator(),
    }


def _filter_targets(all_targets, names: frozenset[str] | None):
    """Restrict resolved targets to the explicit allowlist when provided."""
    if not names:
        return all_targets
    return [t for t in all_targets if t.name in names]


def _read_apm_yml_target(project_root: Path):
    """Return the explicit target list from ``apm.yml`` if declared, else ``None``.

    Handles both the singular ``target:`` and plural ``targets:`` forms so
    that the replay uses the same target set the install pipeline used.
    Without this, a project with ``targets: [claude, codex]`` (no copilot)
    that also has a ``.github/`` directory for unrelated CI workflows would
    have copilot auto-detected during replay, producing false
    ``unintegrated`` findings for ``.github/instructions/`` (#1924).
    """
    apm_yml = project_root / "apm.yml"
    if not apm_yml.exists():
        return None
    try:
        # Route through the merge/alias-bounded loader (not stock yaml.safe_load)
        # so a hostile apm.yml shipped in a cloned repo cannot wedge the default-on
        # ``apm audit`` drift replay with a billion-laughs merge/alias bomb.
        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(apm_yml) or {}
    except Exception:
        # Manifest unreadable / corrupt: fall back to auto-detect rather
        # than crashing the replay; the caller still surfaces a useful
        # error elsewhere if the project is truly broken.
        return None
    # parse_targets_field handles both 'target:' (singular) and 'targets:'
    # (plural list) and validates the tokens against the canonical set.
    try:
        from apm_cli.core.apm_yml import parse_targets_field

        tokens = parse_targets_field(data)
        return tokens if tokens else None
    except Exception:
        return None


def run_replay(config: ReplayConfig, logger: CheckLogger) -> Path:
    """Execute the scratch replay and return the populated scratch dir.

    The scratch directory is registered for atexit cleanup so callers do
    not need to manage its lifetime.

    Raises
    ------
    CacheMissError
        Surfaced verbatim when a locked dep cannot be materialized.
    """
    from apm_cli.deps.lockfile import _SELF_KEY, LockFile
    from apm_cli.install.services import (
        IntegrationOptions,
        IntegratorBundle,
        integrate_package_primitives,
    )
    from apm_cli.integration.targets import resolve_targets
    from apm_cli.utils.diagnostics import DiagnosticCollector

    if not config.lockfile_path.exists():
        raise CacheMissError(
            f"lockfile not found at {config.lockfile_path}; run 'apm install' to generate it"
        )

    lock = LockFile.read(config.lockfile_path)
    if lock is None:
        raise CacheMissError(f"lockfile at {config.lockfile_path} is empty or unreadable")

    project_root = config.project_root.resolve()
    scratch_root = (
        config.scratch_root.resolve()
        if config.scratch_root is not None
        else _make_scratch_root(project_root)
    )
    _assert_scratch_bound(project_root, scratch_root)
    logger.scratch_root(scratch_root)
    apm_modules_dir = (
        config.modules_root.resolve()
        if config.modules_root is not None
        else project_root / "apm_modules"
    )
    live_modules_dir = project_root / "apm_modules"

    # Honor apm.yml's ``target:`` field so multi-target projects replay
    # into all governed roots (not just whichever directory happens to
    # already exist via auto-detection). Without this, a project that
    # targets ``copilot,claude,cursor`` would replay only the primary
    # auto-detected target and report the others as ``orphaned``.
    explicit_target = _read_apm_yml_target(project_root)
    all_targets = resolve_targets(project_root, explicit_target=explicit_target)
    targets = _filter_targets(all_targets, config.targets)
    registries: dict[str, str] | None = None
    downloader = None
    registry_resolver = None
    if not config.cache_only:
        from apm_cli.core.auth import AuthResolver
        from apm_cli.deps.github_downloader import GitHubPackageDownloader
        from apm_cli.deps.registry.resolver import RegistryPackageResolver
        from apm_cli.models.apm_package import APMPackage

        downloader = GitHubPackageDownloader(auth_resolver=AuthResolver())
        apm_yml = project_root / "apm.yml"
        if apm_yml.exists():
            manifest = APMPackage.from_apm_yml(apm_yml)
            registries = getattr(manifest, "registries", None) or {}
            if registries:
                registry_resolver = RegistryPackageResolver(registries)

    diagnostics = DiagnosticCollector(verbose=logger.verbose)
    integrators = _make_integrators()

    # Pre-create target root dirs in scratch so integrators with
    # auto_create=False do not skip non-skill primitives during replay.
    # During a real install, these directories already exist in the project;
    # in the scratch replay they must be seeded explicitly.
    for _target in targets:
        _scratch_target_root = scratch_root / _target.root_dir
        _scratch_target_root.mkdir(parents=True, exist_ok=True)

    # Defense-in-depth: snapshot every file under a governed root and
    # under apm.lock.yaml, then assert no mutation on exit. The primary
    # write-redirect is ``scratch_root=scratch_root`` threaded into every
    # integrator; this guard catches accidental direct-path writes that
    # bypass the redirect (e.g. an integrator that hard-codes
    # ``project_root / target.root_dir``). See guards.py for semantics.
    governed = _governed_root_dirs(targets)
    protected_subpaths = [*sorted(governed), "apm.lock.yaml", "AGENTS.md"]

    snapshot_started = False
    if logger.verbose:
        try:
            tracemalloc.start()
            snapshot_started = True
        except RuntimeError:
            snapshot_started = False

    logger.replay_start()
    replayed_count = 0
    try:
        with _ReadOnlyProjectGuard(project_root, protected_subpaths):
            for lock_dep in lock.get_all_dependencies():
                if lock_dep.local_path == _SELF_KEY:
                    # Synthesized self-entry: project's own local content.
                    # Re-integrate from project_root itself.
                    install_path = project_root
                else:
                    install_path = _materialize_install_path(
                        lock_dep,
                        project_root,
                        apm_modules_dir,
                        cache_only=config.cache_only,
                        lockfile=lock,
                        live_modules_dir=live_modules_dir,
                        downloader=downloader,
                        registry_resolver=registry_resolver,
                        registries=registries,
                    )

                package_info = _build_package_info(lock_dep, install_path)
                if lock_dep.local_path == _SELF_KEY:
                    package_info.root_local_project_root = project_root
                    package_info.deployment_package_root = scratch_root
                else:
                    package_info.deployment_package_root = (
                        lock_dep.to_dependency_ref().get_install_path(scratch_root / "apm_modules")
                    )
                dep_key = lock_dep.get_unique_key()

                integrate_package_primitives(
                    package_info,
                    scratch_root,
                    targets=targets,
                    integrators=IntegratorBundle.from_mapping(integrators),
                    force=True,
                    managed_files=set(),
                    diagnostics=diagnostics,
                    package_name=dep_key,
                    logger=None,
                    scope=None,
                    ctx=None,
                    options=IntegrationOptions(
                        scratch_root=scratch_root,
                        # Forward verbatim: integrate_package_primitives is the
                        # canonical owner of skill-subset filtering. run_replay
                        # must not recompute or normalise the locked subset --
                        # only adapt list->tuple to satisfy IntegrationOptions
                        # typing; None is preserved as None (not collapsed via
                        # "or ()"/..."or None" which could drop empty-list intent).
                        skill_subset=(
                            tuple(package_info.dependency_ref.skill_subset)
                            if package_info.dependency_ref.skill_subset is not None
                            else None
                        ),
                    ),
                    dep_target_subset=lock_dep.target_subset or None,
                )
                replayed_count += 1
    finally:
        if snapshot_started:
            try:
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                click.echo(
                    f"{STATUS_SYMBOLS['info']} drift replay peak memory: "
                    f"{peak / (1024 * 1024):.2f} MB",
                    err=True,
                )
            except RuntimeError:
                pass

    logger.replay_complete(replayed_count)
    return scratch_root


# ---------------------------------------------------------------------------
# Diff engine
#
# The implementation lives in ``_drift_diff.py`` to keep this module under
# the file-length gate. Re-exported here (double-name so the linter keeps
# them) because callers and tests import these from ``apm_cli.install.drift``.
# ---------------------------------------------------------------------------

from ._drift_diff import _INLINE_DIFF_BYTE_CAP as _INLINE_DIFF_BYTE_CAP  # noqa: E402
from ._drift_diff import _canvas_deploy_prefixes as _canvas_deploy_prefixes  # noqa: E402
from ._drift_diff import _governed_root_dirs as _governed_root_dirs  # noqa: E402
from ._drift_diff import _inline_diff_for as _inline_diff_for  # noqa: E402
from ._drift_diff import _walk_managed as _walk_managed  # noqa: E402
from ._drift_diff import diff_scratch_against_project as diff_scratch_against_project  # noqa: E402

# ---------------------------------------------------------------------------
# Deployment-membership projections.
#
# AC "Drift deployment membership must route through DeploymentLedgerCodec"
# greps these two ``def`` bodies in THIS file by name, so they stay here
# rather than moving to ``_drift_diff`` with the rest of the differ. The
# differ reaches them late through the ``drift`` module so the seam and the
# guard agree.
# ---------------------------------------------------------------------------


def _collect_tracked_files(lockfile: LockFile) -> dict[str, str]:
    """Return scanner membership claims as ``{path: package_owner}``."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    return DeploymentLedgerCodec.legacy_deployed_file_claims(lockfile)


def _collect_hashed_files(lockfile: LockFile) -> set[str]:
    """Return every deployed path whose lock claim is explicitly file-shaped."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    return set(DeploymentLedgerCodec.legacy_deployed_file_hash_paths(lockfile))


from ._drift_diff import _claimed_prefixes as _claimed_prefixes  # noqa: E402
