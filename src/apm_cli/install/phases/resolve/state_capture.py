"""State capture helpers for resolve phase."""

from __future__ import annotations

import builtins
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)


def _build_dep_base_dirs(dependency_graph, project_root: Path) -> dict[str, Path]:
    """Build dep_key -> parent source_path map for transitive local deps."""
    dep_base_dirs: builtins.dict[str, Path] = {}
    try:
        tree = dependency_graph.dependency_tree
        for node in tree.nodes.values():
            parent_node = node.parent
            if parent_node is None or parent_node.package is None:
                continue
            anchor = (
                parent_node.package.source_path
                if parent_node.package.source_path is not None
                else project_root
            )
            key = node.dependency_ref.get_unique_key()
            existing = dep_base_dirs.get(key)
            if existing is not None and existing != anchor:
                _logger.warning(
                    "Local dep %r is referenced from two parents with different anchors (%s vs %s). "
                    "Using the first; rename one of the local_path values or use absolute paths to "
                    "disambiguate.",
                    key,
                    existing,
                    anchor,
                )
                continue
            dep_base_dirs[key] = anchor
    except (AttributeError, KeyError):
        return {}
    return dep_base_dirs
