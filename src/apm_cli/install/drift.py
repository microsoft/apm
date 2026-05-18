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

Module layout
-------------
``drift.py`` (this file)
    Scratch-dir lifecycle, :class:`CheckLogger`, package-materialization
    helpers, and the :func:`run_replay` orchestrator.

``_drift_types.py``
    :class:`ReplayConfig`, :class:`DriftFinding`, :class:`CacheMissError` --
    kept separate to avoid circular imports between the diff engine and the
    replay orchestrator.

``_drift_diff.py``
    Pure diff engine: walks managed directories, compares normalized content,
    emits :class:`DriftFinding` instances.

``_drift_render.py``
    Human-readable text, JSON, and SARIF renderers.

All public names remain importable from ``apm_cli.install.drift``.
"""

from __future__ import annotations

import tracemalloc
from pathlib import Path
from typing import TYPE_CHECKING

import click

from apm_cli.core.command_logger import CommandLogger
from apm_cli.utils.console import STATUS_SYMBOLS
from apm_cli.utils.guards import _ReadOnlyProjectGuard

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile

# ---------------------------------------------------------------------------
# Back-compat re-exports
#
# Public types live in ``_drift_types`` (kept separate to avoid circular
# imports between the diff engine and the replay orchestrator).
#
# Normalization helpers live in ``apm_cli.utils.normalization``; re-exported
# here so callers and tests that import ``_strip_build_id`` / ``_normalize``
# from this module keep working without changes.
# ---------------------------------------------------------------------------
from apm_cli.utils.normalization import (  # noqa: E402  -- re-exported for back-compat
    _normalize,
    _normalize_line_endings,
    _strip_bom,
    _strip_build_id,
)

from ._drift_helpers import (
    _assert_scratch_bound,
    _build_package_info,
    _make_scratch_root,
    _materialize_install_path,
)
from ._drift_types import CacheMissError, DriftFinding, ReplayConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Stderr-only logger for audit phases (CommandLogger writes to stdout)
# ---------------------------------------------------------------------------


class CheckLogger(CommandLogger):
    """CheckLogger emits drift phase markers to stderr."""

    def __init__(self, verbose: bool = False) -> None:
        super().__init__("audit-drift", verbose=verbose)

    def _emit(self, symbol_key: str, msg: str) -> None:
        click.echo(f"{STATUS_SYMBOLS[symbol_key]} {msg}", err=True)

    def replay_start(self) -> None:
        self._emit("running", "Replaying install (cache-only)...")

    def scratch_root(self, path: Path) -> None:
        if not self.verbose:
            return
        click.echo(f"{STATUS_SYMBOLS['info']} drift scratch root: {path}", err=True)

    def diff_start(self) -> None:
        self._emit("running", "Diffing scratch vs working tree...")

    def replay_complete(self, count: int) -> None:
        self._emit("check", f"Replayed {count} package(s)")

    def clean(self) -> None:
        self._emit("check", "No drift detected")

    def findings(self, count: int) -> None:
        self._emit("warning", f"Drift detected: {count} file(s)")


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
    from apm_cli.install.services import _IntegratorSet, integrate_package_primitives
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
                    integrators=_IntegratorSet(
                        prompt_integrator=integrators["prompt"],
                        agent_integrator=integrators["agent"],
                        skill_integrator=integrators["skill"],
                        instruction_integrator=integrators["instruction"],
                    ),
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
# Re-export diff engine and renderers (implementations live in sibling
# private modules to keep this file under 500 lines).
# All names remain importable from ``apm_cli.install.drift``.
# ---------------------------------------------------------------------------

from ._drift_diff import (  # noqa: E402
    _governed_root_dirs,
    diff_scratch_against_project,
)
from ._drift_render import (  # noqa: E402
    render_drift,
    render_drift_json,
    render_drift_sarif,
    render_drift_text,
)
