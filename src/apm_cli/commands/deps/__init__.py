"""APM dependency management commands."""

__all__ = [  # noqa: RUF022
    # CLI commands
    "deps",
    "list_packages",
    "tree",
    "clean",
    "update",
    "info",
    "why",
    # Utility functions (used by tests)
    "_is_nested_under_package",
    "_count_primitives",
    "_count_package_files",
    "_count_workflows",
    "_get_detailed_context_counts",
    "_get_package_display_info",
    "_get_detailed_package_info",
]

_LAZY = {
    "_count_package_files": ("_utils", "_count_package_files"),
    "_count_primitives": ("_utils", "_count_primitives"),
    "_count_workflows": ("_utils", "_count_workflows"),
    "_get_detailed_context_counts": ("_utils", "_get_detailed_context_counts"),
    "_get_detailed_package_info": ("_utils", "_get_detailed_package_info"),
    "_get_package_display_info": ("_utils", "_get_package_display_info"),
    "_is_nested_under_package": ("_utils", "_is_nested_under_package"),
    "clean": ("cli", "clean"),
    "deps": ("cli", "deps"),
    "info": ("cli", "info"),
    "list_packages": ("cli", "list_packages"),
    "tree": ("cli", "tree"),
    "update": ("cli", "update"),
    "why": ("why", "why"),
}


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f".{target[0]}", __name__), target[1])
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
