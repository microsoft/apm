"""No-op detection for lockfile writes (split from ``deps/lockfile.py``).

A write whose serialization holds the same data as the destination keeps the
destination's own bytes; these helpers compare parsed documents for that check.
"""

from __future__ import annotations

import copy
from pathlib import Path
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
    path: Path, *, parse: bool
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """The lockfile a write replaces: ``(text, parsed data, no-op snapshot)``.

    A document this command staged as data comes back as private copies with no
    text, so nothing emits it (#4939). Without *parse* only a staged document
    yields data; the caller already holds the existing lockfile.
    """
    from ..utils.yaml_io import load_yaml_str
    from .lockfile_write_authority import staged_document_copy, staged_lockfile_text

    staged = staged_document_copy(path)
    if staged is not None:
        return None, staged_document_copy(path) if parse else None, staged
    text = staged_lockfile_text(path)
    if not parse or text is None:
        return text, None, None
    try:
        data = load_yaml_str(text)
    except (yaml.YAMLError, ValueError):
        return text, None, None
    if not isinstance(data, dict):
        return text, data, None
    # Construction shares nested objects; snapshot for the no-op check.
    return text, data, document_snapshot(data)
