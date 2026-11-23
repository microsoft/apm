"""No-op detection for lockfile writes (split from ``deps/lockfile.py``).

A write whose serialization holds the same data as the destination keeps the
destination's own bytes; these helpers compare parsed documents for that check.
"""

from __future__ import annotations

import copy
from typing import Any

import yaml


def document_snapshot(data: dict[str, Any]) -> dict[str, Any] | None:
    """A private copy of a parsed lockfile for the no-op check, or None to re-parse.

    A merge-reordered alias chain the bounded loader accepts can nest deeper
    than deepcopy recurses; the no-op check then parses the text instead.
    """
    try:
        return copy.deepcopy(data)
    except RecursionError:
        return None


def same_document(current: str | None, serialized: str, parsed_current: object = None) -> bool:
    """Whether *current* holds the data *serialized* would write; only formatting may differ.

    *parsed_current* is *current* already loaded, so the caller's parse is reused (#4742).
    """
    from ..utils.yaml_io import load_yaml_str

    try:
        if parsed_current is None:
            if current is None:
                return False
            parsed_current = load_yaml_str(current)
        return parsed_current == load_yaml_str(serialized)
    except (UnicodeError, yaml.YAMLError, ValueError):
        return False
