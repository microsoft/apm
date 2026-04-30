"""APM dependency management commands."""

from ._utils import (
    _count_package_files,
    _count_primitives,
    _count_workflows,
    _get_detailed_context_counts,
    _get_detailed_package_info,
    _get_package_display_info,
    _is_nested_under_package,
)
from .cli import clean, deps, info, list_packages, tree, update

__all__ = [  # noqa: RUF022
    # CLI commands
    "deps",
    "list_packages",
    "tree",
    "clean",
    "update",
    "info",
    # Utility functions (used by tests)
    "_is_nested_under_package",
    "_count_primitives",
    "_count_package_files",
    "_count_workflows",
    "_get_detailed_context_counts",
    "_get_package_display_info",
    "_get_detailed_package_info",
]
