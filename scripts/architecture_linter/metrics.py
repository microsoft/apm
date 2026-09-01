"""Deterministic metrics serialization for the architecture linter.

Metrics are written as a single, deterministically-ordered ASCII JSON
document. Writing happens strictly outside the timed portion of the run:
:func:`scripts.architecture_linter.runner.run` measures and returns
``total_seconds`` before this module ever touches the filesystem, so the act
of writing the metrics file never inflates the metric it is reporting.

Writing fails closed: any I/O error raises :class:`MetricsWriteError` instead
of being swallowed, so a broken ``--metrics-json`` destination is a hard,
visible failure rather than a silently-missing artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.architecture_linter.models import RunMetrics


class MetricsWriteError(RuntimeError):
    """Raised when the metrics JSON cannot be written."""


def to_json_dict(metrics: RunMetrics) -> dict[str, object]:
    """Return a plain, JSON-serializable, deterministically-keyed dict."""
    return {
        "inventory_file_count": metrics.inventory_file_count,
        "excluded_root_count": metrics.excluded_root_count,
        "read_attempts": metrics.read_attempts,
        "read_successes": metrics.read_successes,
        "read_errors": metrics.read_errors,
        "max_reads_per_file": metrics.max_reads_per_file,
        "parse_attempts": metrics.parse_attempts,
        "parse_successes": metrics.parse_successes,
        "parse_errors": metrics.parse_errors,
        "max_parses_per_file": metrics.max_parses_per_file,
        "ast_visits": metrics.ast_visits,
        "tree_index_builds": metrics.tree_index_builds,
        "tree_index_cache_hits": metrics.tree_index_cache_hits,
        "max_tree_index_builds_per_file": metrics.max_tree_index_builds_per_file,
        "peak_tree_index_nodes": metrics.peak_tree_index_nodes,
        "per_group_seconds": {name: seconds for name, seconds in metrics.per_group_seconds},
        "total_seconds": metrics.total_seconds,
        "child_process_count": metrics.child_process_count,
    }


def write_metrics(metrics: RunMetrics, path: Path) -> None:
    """Serialize `metrics` deterministically to `path`; fail closed on error."""
    payload = json.dumps(
        to_json_dict(metrics),
        indent=2,
        sort_keys=True,
        ensure_ascii=True,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="ascii")
    except OSError as exc:
        raise MetricsWriteError(f"cannot write metrics JSON to {path}: {exc}") from exc
