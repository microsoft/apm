"""Compatibility wrapper for the marketplace package command group."""

from .marketplace.plugin import (
    _SHA_RE,
    _ensure_yml_exists,
    _parse_tags,
    _resolve_ref,
    _verify_source,
    _yml_path,
    add,
    package,
    remove,
    set_cmd,
)

__all__ = [
    "package",
    "add",
    "set_cmd",
    "remove",
    "_SHA_RE",
    "_yml_path",
    "_ensure_yml_exists",
    "_parse_tags",
    "_verify_source",
    "_resolve_ref",
]
