"""Utility modules for APM CLI."""

# PEP 562 lazy re-exports: importing any ``apm_cli.utils.<module>`` (for
# example ``git_env`` on the ``--version`` path) must not load ``rich``
# through ``utils.console``. Names resolve on first attribute access.
_LAZY_EXPORTS = {
    "STATUS_SYMBOLS": ".console",
    "_create_files_table": ".console",
    "_get_console": ".console",
    "_rich_echo": ".console",
    "_rich_error": ".console",
    "_rich_info": ".console",
    "_rich_panel": ".console",
    "_rich_success": ".console",
    "_rich_warning": ".console",
    "CATEGORY_COLLISION": ".diagnostics",
    "CATEGORY_ERROR": ".diagnostics",
    "CATEGORY_OVERWRITE": ".diagnostics",
    "CATEGORY_WARNING": ".diagnostics",
    "Diagnostic": ".diagnostics",
    "DiagnosticCollector": ".diagnostics",
    "portable_relpath": ".paths",
}

__all__ = [
    "CATEGORY_COLLISION",
    "CATEGORY_ERROR",
    "CATEGORY_OVERWRITE",
    "CATEGORY_WARNING",
    "STATUS_SYMBOLS",
    "Diagnostic",
    "DiagnosticCollector",
    "_create_files_table",
    "_get_console",
    "_rich_echo",
    "_rich_error",
    "_rich_info",
    "_rich_panel",
    "_rich_success",
    "_rich_warning",
    "portable_relpath",
]


def __getattr__(name: str):
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
