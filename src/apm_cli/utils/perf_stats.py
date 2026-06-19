"""Process-scoped performance counters for the install pipeline.

Captures granular timing of expensive operations (project-tree walks,
discovery passes, cache lookups) and emits a verbose-only summary at
the end of the install. Lives outside the hot path itself so the
inner loops only pay the cost of one ``perf_counter`` call and one
counter increment per event.

Lifecycle
---------
- ``reset()`` is called at the start of every ``run_install_pipeline``
  invocation so counts from previous runs (test harness, REPL) do not
  leak into the next one.
- ``record_walk()`` is called by ``find_primitive_files`` once per
  walk completion with the elapsed wall-time and the number of
  files visited.
- ``record_discovery()`` is called by ``discover_primitives`` once
  per call with ``cache_hit`` set to True or False.
- ``render_summary(logger)`` is called from the integrate phase
  finalize hook; emits one block under ``verbose_detail``.

All wall-time numbers use ``time.perf_counter``. Counts are stored
in a module-global ``_Stats`` dataclass instance; not thread-safe by
design because the integrate phase is sequential.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.command_logger import InstallLogger


@dataclass
class _WalkRecord:
    """One ``find_primitive_files`` invocation."""

    base_dir: str
    pattern_count: int
    duration_s: float
    files_visited: int
    files_matched: int


@dataclass
class _DiscoveryRecord:
    """One ``discover_primitives`` invocation."""

    base_dir: str
    duration_s: float
    cache_hit: bool


@dataclass
class _Stats:
    walks: list[_WalkRecord] = field(default_factory=list)
    discoveries: list[_DiscoveryRecord] = field(default_factory=list)
    started_at: float = field(default_factory=time.perf_counter)


_stats = _Stats()


def reset() -> None:
    """Clear all counters. Call at the start of every install run."""
    global _stats
    _stats = _Stats()


def record_walk(
    base_dir: str,
    pattern_count: int,
    duration_s: float,
    files_visited: int,
    files_matched: int,
) -> None:
    """Record one ``find_primitive_files`` invocation."""
    _stats.walks.append(
        _WalkRecord(
            base_dir=base_dir,
            pattern_count=pattern_count,
            duration_s=duration_s,
            files_visited=files_visited,
            files_matched=files_matched,
        )
    )


def record_discovery(base_dir: str, duration_s: float, cache_hit: bool) -> None:
    """Record one ``discover_primitives`` invocation."""
    _stats.discoveries.append(
        _DiscoveryRecord(base_dir=base_dir, duration_s=duration_s, cache_hit=cache_hit)
    )


def snapshot() -> dict:
    """Return an immutable snapshot of current counters (for tests)."""
    return {
        "walks": list(_stats.walks),
        "discoveries": list(_stats.discoveries),
    }


def render_summary(
    logger: InstallLogger | None,
    project_root: str | None = None,
) -> None:
    """Emit a verbose-only perf summary. No-op when logger is missing.

    Args:
        logger: Install logger; if ``None``, this is a no-op.
        project_root: Optional anchor used to relativize base-dir paths so
            the log lines stay short and reproducible across machines.

    Output shape (ASCII only, one block under ``verbose_detail``):

        [#] Perf: 4 walks, 18 file matches, 0.124s total walk time
        [#] Perf:   .: 1 walk (12ms, 1923 files visited)
        [#] Perf:   apm_modules/_local/foo: 1 walk (3ms, 47 files visited)
        [#] Perf: discovery: 9 calls (1 unique base, 8 cache hits, 89%)
    """
    if logger is None:
        return
    if not _stats.walks and not _stats.discoveries:
        return

    def _short(base: str) -> str:
        # base_dir defaults to '.' in discover_primitives; preserve that
        # rather than printing a misleading "<unknown>" placeholder.
        if not base:
            return "."
        if project_root:
            try:
                rel = os.path.relpath(base, project_root)
                # Keep absolute paths that escape the project_root rather
                # than emitting long ``../../../...`` chains.
                if not rel.startswith(".."):
                    return rel or "."
            except ValueError:
                pass  # Different drives on Windows.
        return base

    try:
        total_walks = len(_stats.walks)
        total_walk_time = sum(w.duration_s for w in _stats.walks)
        total_matched = sum(w.files_matched for w in _stats.walks)
        total_visited = sum(w.files_visited for w in _stats.walks)
        logger.verbose_detail(
            f"[#] Perf: {total_walks} walks, {total_matched} file matches, "
            f"{total_visited} files visited, {total_walk_time:.3f}s total walk time"
        )

        per_base: dict[str, list[_WalkRecord]] = {}
        for w in _stats.walks:
            per_base.setdefault(_short(w.base_dir), []).append(w)
        for base, walks in per_base.items():
            elapsed_ms = sum(w.duration_s for w in walks) * 1000.0
            visited = sum(w.files_visited for w in walks)
            matched = sum(w.files_matched for w in walks)
            logger.verbose_detail(
                f"[#] Perf:   {base}: {len(walks)} walk(s) "
                f"({elapsed_ms:.0f}ms, {visited} files visited, {matched} matched)"
            )

        if _stats.discoveries:
            disc_total = len(_stats.discoveries)
            disc_hits = sum(1 for d in _stats.discoveries if d.cache_hit)
            unique_bases = len({d.base_dir for d in _stats.discoveries})
            hit_pct = int(disc_hits / disc_total * 100) if disc_total else 0
            logger.verbose_detail(
                f"[#] Perf: discovery: {disc_total} call(s) "
                f"({unique_bases} unique base(s), {disc_hits} cache hit(s), {hit_pct}%)"
            )
    except Exception as exc:  # pragma: no cover -- perf logging must not break install
        logger.verbose_detail(f"[!] Perf summary render failed: {exc}")
