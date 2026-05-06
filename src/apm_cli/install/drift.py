"""Drift-detection replay engine for ``apm audit --check drift``.

Reproduces the integration step from the lockfile in an isolated scratch
directory, then diffs the resulting tree against the working project to
surface three kinds of divergence:

* ``modified``     -- a tracked deployed file's content differs.
* ``unintegrated`` -- a tracked deployed file is missing from the project.
* ``orphaned``     -- a managed-directory file exists in the project but
  is not present in the scratch replay AND not tracked in the lockfile.

The replay is **cache-only** in v1 (no network): cached package contents
under ``apm_modules/`` are the source of truth.  A miss is reported as a
check error rather than auto-fetched.

Design constraints (see ``WIP/drift/06-final-plan.md``):
* Pure read-only against the project tree -- writes go to the scratch
  directory only.  ``ensure_path_within`` guards every write redirection.
* ASCII-only console output (Windows cp1252 safety).
* Normalization strips line-ending differences, BOMs, and the APM
  ``Build ID`` header that legitimately changes on every recompile.
"""

from __future__ import annotations

import atexit
import json
import shutil
import tempfile
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

from apm_cli.core.command_logger import CommandLogger
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


@dataclass(frozen=True)
class DriftFinding:
    """A single divergence between the replay scratch tree and the project."""

    path: str
    kind: str  # one of "modified" | "unintegrated" | "orphaned"
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

from apm_cli.utils.normalization import (  # noqa: E402, F401  -- re-exported for back-compat
    _BOM,
    _BUILD_ID_PATTERN,
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
        self._emit("running", "Replaying install (cache-only)...")

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
# Package materialization (cache-only)
# ---------------------------------------------------------------------------


def _materialize_install_path(
    lock_dep: LockedDependency,
    project_root: Path,
    apm_modules_dir: Path,
    cache_only: bool,
) -> Path:
    """Resolve the on-disk path for a locked dep's package contents.

    For local deps -- contents live at ``project_root / lock_dep.local_path``.
    For remote deps -- contents live at the canonical apm_modules subpath.

    Raises
    ------
    CacheMissError
        If ``cache_only`` is True and the path does not exist.
    NotImplementedError
        If ``cache_only`` is False (network-enabled replay is a follow-up).
    """
    if not cache_only:
        raise NotImplementedError("--no-cache replay requires auth wiring; tracked in follow-up")

    if lock_dep.source == "local":
        if not lock_dep.local_path:
            raise CacheMissError(f"local dep {lock_dep.repo_url!r} has no local_path in lockfile")
        candidate = (project_root / lock_dep.local_path).resolve()
        if not candidate.exists():
            raise CacheMissError(
                f"local source missing for {lock_dep.local_path!r}: expected {candidate}"
            )
        return candidate

    dep_ref = lock_dep.to_dependency_ref()
    candidate = dep_ref.get_install_path(apm_modules_dir)
    # Supply-chain fail-closed: a remote dep without a resolved_commit is
    # unverifiable -- there is no marker we can write at install time and
    # no commit we can compare at audit time. Refuse to replay it rather
    # than silently trust whatever happens to live in the cache.
    if getattr(lock_dep, "source", None) != "local" and not lock_dep.resolved_commit:
        raise CacheMissError(
            f"cannot replay {lock_dep.repo_url}: lockfile entry has no resolved_commit "
            "(cache freshness unverifiable). Re-run 'apm install' with a pinned ref "
            "(commit, tag, or specific branch HEAD) before audit."
        )
    if not candidate.exists():
        raise CacheMissError(
            f"cache miss for {lock_dep.repo_url}@{lock_dep.resolved_commit}: "
            f"expected {candidate}; run 'apm install' to populate the cache"
        )
    # Stale-cache detection: verify the cache pin marker matches the
    # lockfile's resolved_commit. Catches the "teammate bumped the
    # lockfile, didn't reinstall" + "shared CI runner reused stale
    # apm_modules" scenarios. Not defense against active tampering.
    if lock_dep.resolved_commit:
        from apm_cli.install.cache_pin import CachePinError, verify_marker

        try:
            verify_marker(candidate, lock_dep.resolved_commit)
        except CachePinError as exc:
            raise CacheMissError(f"{exc}; run 'apm install' to refresh apm_modules cache") from exc
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
    """Return the ``target:`` field from ``apm.yml`` if present, else ``None``.

    This lets ``run_replay`` reproduce the SAME target set the install
    pipeline used, instead of falling back to directory auto-detection
    that misses targets whose deployment directories are still empty.
    """
    apm_yml = project_root / "apm.yml"
    if not apm_yml.exists():
        return None
    try:
        import yaml as _yaml  # local import: drift module avoids top-level yaml dep

        data = _yaml.safe_load(apm_yml.read_text(encoding="utf-8")) or {}
    except Exception:
        # Manifest unreadable / corrupt: fall back to auto-detect rather
        # than crashing the replay; the caller still surfaces a useful
        # error elsewhere if the project is truly broken.
        return None
    raw = data.get("target")
    if raw is None:
        return None
    try:
        from apm_cli.core.target_detection import parse_target_field

        return parse_target_field(raw, source_path=apm_yml)
    except Exception:
        return None


def run_replay(config: ReplayConfig, logger: CheckLogger) -> Path:
    """Execute the cache-only replay and return the populated scratch dir.

    The scratch directory is registered for atexit cleanup so callers do
    not need to manage its lifetime.

    Raises
    ------
    CacheMissError
        Surfaced verbatim when a locked dep is not in the cache.
    """
    from apm_cli.deps.lockfile import _SELF_KEY, LockFile
    from apm_cli.install.services import integrate_package_primitives
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
    scratch_root = _make_scratch_root(project_root)
    logger.scratch_root(scratch_root)
    apm_modules_dir = project_root / "apm_modules"

    # Honor apm.yml's ``target:`` field so multi-target projects replay
    # into all governed roots (not just whichever directory happens to
    # already exist via auto-detection). Without this, a project that
    # targets ``copilot,claude,cursor`` would replay only the primary
    # auto-detected target and report the others as ``orphaned``.
    explicit_target = _read_apm_yml_target(project_root)
    all_targets = resolve_targets(project_root, explicit_target=explicit_target)
    targets = _filter_targets(all_targets, config.targets)

    diagnostics = DiagnosticCollector(verbose=logger.verbose)
    integrators = _make_integrators()

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
                    )

                package_info = _build_package_info(lock_dep, install_path)
                dep_key = lock_dep.get_unique_key()

                integrate_package_primitives(
                    package_info,
                    scratch_root,
                    targets=targets,
                    prompt_integrator=integrators["prompt"],
                    agent_integrator=integrators["agent"],
                    skill_integrator=integrators["skill"],
                    instruction_integrator=integrators["instruction"],
                    command_integrator=integrators["command"],
                    hook_integrator=integrators["hook"],
                    force=True,
                    managed_files=set(),
                    diagnostics=diagnostics,
                    package_name=dep_key,
                    logger=None,
                    scope=None,
                    skill_subset=None,
                    ctx=None,
                    scratch_root=scratch_root,
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
# ---------------------------------------------------------------------------

_INLINE_DIFF_BYTE_CAP = 100 * 1024  # 100 KB


def _governed_root_dirs(targets) -> set[str]:
    """Return the set of top-level managed directory names to walk."""
    roots: set[str] = {".apm"}
    for t in targets or []:
        root = getattr(t, "root_dir", None)
        if root:
            roots.add(str(root).split("/", 1)[0])
    return roots


def _walk_managed(root: Path, governed_roots: set[str]) -> dict[str, Path]:
    """Return a mapping of project-relative posix paths to absolute paths."""
    out: dict[str, Path] = {}
    if not root.exists():
        return out
    for top in governed_roots:
        base = root / top
        if not base.exists():
            continue
        if base.is_file():
            out[top] = base
            continue
        for p in base.rglob("*"):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                out[rel] = p
    # AGENTS.md is a flat top-level file in some target layouts.
    agents_md = root / "AGENTS.md"
    if agents_md.is_file():
        out["AGENTS.md"] = agents_md
    return out


def _collect_tracked_files(lockfile: LockFile) -> dict[str, str]:
    """Return ``{deployed_path: package_name}`` aggregating all sources."""
    tracked: dict[str, str] = {}
    for key, dep in lockfile.dependencies.items():
        for path in dep.deployed_files or []:
            tracked.setdefault(path, key)
    for path in lockfile.local_deployed_files or []:
        tracked.setdefault(path, ".")
    return tracked


def _inline_diff_for(scratch_path: Path, project_path: Path) -> str:
    """Build an inline diff hint, capped to keep findings compact."""
    try:
        s_size = scratch_path.stat().st_size
        p_size = project_path.stat().st_size
    except OSError:
        return ""
    if s_size > _INLINE_DIFF_BYTE_CAP or p_size > _INLINE_DIFF_BYTE_CAP:
        return "(file too large for inline diff; use 'git diff --no-index' to compare)"
    return ""


def diff_scratch_against_project(
    scratch_root: Path,
    project_root: Path,
    lockfile: LockFile,
    targets,
) -> list[DriftFinding]:
    """Compare the replay scratch tree against the project tree.

    Three kinds of findings are emitted:

    * ``modified``     -- file exists in both, normalized content differs.
    * ``unintegrated`` -- file exists in scratch but not in project.
    * ``orphaned``     -- file exists in project + tracked in lockfile
      ``deployed_files`` but no longer in scratch.

    Untracked extra files in governed directories are intentionally
    ignored to avoid false positives from user-authored content.
    """
    scratch_root = scratch_root.resolve()
    project_root = project_root.resolve()
    governed = _governed_root_dirs(targets)
    scratch_files = _walk_managed(scratch_root, governed)
    project_files = _walk_managed(project_root, governed)
    tracked = _collect_tracked_files(lockfile)

    findings: list[DriftFinding] = []

    for rel, scratch_path in sorted(scratch_files.items()):
        project_path = project_files.get(rel)
        if project_path is None:
            findings.append(
                DriftFinding(
                    path=rel,
                    kind="unintegrated",
                    package=tracked.get(rel, ""),
                )
            )
            continue
        try:
            s_bytes = _normalize(scratch_path.read_bytes())
            p_bytes = _normalize(project_path.read_bytes())
        except OSError as exc:
            findings.append(
                DriftFinding(
                    path=rel,
                    kind="modified",
                    package=tracked.get(rel, ""),
                    inline_diff=f"(read error: {exc})",
                )
            )
            continue
        if s_bytes != p_bytes:
            findings.append(
                DriftFinding(
                    path=rel,
                    kind="modified",
                    package=tracked.get(rel, ""),
                    inline_diff=_inline_diff_for(scratch_path, project_path),
                )
            )

    for rel in sorted(project_files.keys()):
        if rel in scratch_files:
            continue
        if rel in tracked:
            findings.append(
                DriftFinding(
                    path=rel,
                    kind="orphaned",
                    package=tracked.get(rel, ""),
                )
            )
        # else: untracked governed file -- ignore (user authored).

    return findings


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_drift_text(findings: list[DriftFinding], verbose: bool = False) -> str:
    """Human-readable text rendering grouped by kind."""
    if not findings:
        return f"{STATUS_SYMBOLS['check']} No drift detected"

    lines: list[str] = [
        f"{STATUS_SYMBOLS['warning']} Drift detected: {len(findings)} file(s)",
        "",
    ]
    by_kind: dict[str, list[DriftFinding]] = {}
    for f in findings:
        by_kind.setdefault(f.kind, []).append(f)

    for kind in ("modified", "unintegrated", "orphaned"):
        items = by_kind.get(kind, [])
        if not items:
            continue
        lines.append(f"  {kind} ({len(items)}):")
        for item in items:
            suffix = f"  [{item.package}]" if item.package else ""
            lines.append(f"    - {item.path}{suffix}")
            if verbose and item.inline_diff:
                lines.append(f"      {item.inline_diff}")
        lines.append("")

    lines.append(
        f"  {STATUS_SYMBOLS['info']} Run 'apm install' to re-sync deployed files with the lockfile."
    )

    return "\n".join(lines).rstrip() + "\n"


def render_drift_json(findings: list[DriftFinding]) -> dict:
    """Machine-readable JSON shape: ``{\"drift\": [...]}``."""
    return {
        "drift": [
            {
                "path": f.path,
                "kind": f.kind,
                "package": f.package,
                "inline_diff": f.inline_diff,
            }
            for f in findings
        ]
    }


def render_drift_sarif(findings: list[DriftFinding]) -> list[dict]:
    """SARIF ``results`` array; rule IDs use ``apm/drift/<kind>``."""
    results: list[dict] = []
    for f in findings:
        results.append(
            {
                "ruleId": f"apm/drift/{f.kind}",
                "level": "warning" if f.kind != "modified" else "error",
                "message": {"text": f"drift ({f.kind}): {f.path}"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.path},
                        }
                    }
                ],
                "properties": {"package": f.package},
            }
        )
    return results


# ---------------------------------------------------------------------------
# CLI helper -- intentionally minimal so commands/audit.py can re-use it.
# ---------------------------------------------------------------------------


def render_drift(
    findings: list[DriftFinding],
    fmt: str = "text",
    verbose: bool = False,
) -> str:
    """Single rendering entrypoint for callers that pick a format string."""
    if fmt == "json":
        return json.dumps(render_drift_json(findings), indent=2)
    if fmt == "sarif":
        return json.dumps({"results": render_drift_sarif(findings)}, indent=2)
    return render_drift_text(findings, verbose=verbose)
