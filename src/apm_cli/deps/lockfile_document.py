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

    if parsed_current is None and current is None:
        return False
    try:
        written = load_yaml_str(serialized)
    except (UnicodeError, yaml.YAMLError, ValueError):
        return False
    return holds_document(current, written, parsed_current)


def holds_document(current: str | None, plain: object, parsed_current: object = None) -> bool:
    """Whether *current* holds *plain*, a document the safe dumper round-trips.

    ``same_document`` for a document not yet emitted: the comparison skips the
    dump and re-parse of the new side (#4939).
    """
    from ..utils.yaml_io import load_yaml_str

    try:
        if parsed_current is None:
            if current is None:
                return False
            parsed_current = load_yaml_str(current)
        return parsed_current == plain
    except (UnicodeError, yaml.YAMLError, ValueError):
        return False


def read_destination(
    text: str | None, staged: dict[str, Any] | None, *, parse: bool
) -> tuple[Any, dict[str, Any] | None]:
    """The lockfile a write replaces, as ``(parsed data, no-op snapshot)``.

    *staged* is a private copy of a document this command staged as data, so
    nothing emits or parses it (#4939). Without *parse* only *staged* yields
    a snapshot; the caller already holds the existing lockfile.
    """
    from ..utils.yaml_io import copy_plain_tree, load_yaml_str

    if staged is not None:
        return (copy_plain_tree(staged) if parse else None), staged
    if not parse or text is None:
        return None, None
    try:
        data = load_yaml_str(text)
    except (yaml.YAMLError, ValueError):
        return None, None
    # Construction shares nested objects; snapshot for the no-op check.
    return data, (document_snapshot(data) if isinstance(data, dict) else None)
