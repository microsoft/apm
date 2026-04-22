"""Compatibility wrapper for the marketplace plugin command package."""

from .marketplace.plugin import (
    _SHA_RE,
    _ensure_yml_exists,
    _parse_tags,
    _resolve_ref,
    _verify_source,
    _yml_path,
    add,
    plugin,
    remove,
    set_cmd,
)

__all__ = [
    "plugin",
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
