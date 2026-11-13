"""Read owner for ``apm.yml`` manifests: one mapping per top-level key.

YAML loaders disagree on duplicate mapping keys: the bounded PyYAML loader
keeps the last value silently, while the ruamel round-trip loader raises a
parser-internal error. A manifest with two ``dependencies:`` blocks (a
common merge-conflict leftover) would therefore drop packages on one path
and fail with an unreadable traceback-style message on another. Every
``apm.yml`` read routes through this module so the file is rejected with
one actionable error before any command touches the project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_MERGE_TAG = "tag:yaml.org,2002:merge"


class DuplicateManifestKeyError(ValueError):
    """Raised when an ``apm.yml`` declares the same top-level key twice."""

    def __init__(self, path: Path, key: str, first_line: int, second_line: int) -> None:
        self.path = path
        self.key = key
        self.lines = (first_line, second_line)
        if first_line == second_line:
            where, remedy = f"on line {first_line}", f"Merge both '{key}' entries into one"
        else:
            where = f"(lines {first_line} and {second_line})"
            remedy = f"Merge both '{key}:' blocks into one"
        super().__init__(
            f"{path} declares the top-level key '{key}' twice {where}, so one of "
            f"them would be ignored. {remedy}, then retry."
        )


def reject_duplicate_manifest_keys(path: Path, text: str) -> None:
    """Raise :class:`DuplicateManifestKeyError` for a repeated top-level key.

    Composing builds the node graph without expanding aliases or merge keys.
    Malformed or over-deep YAML is left to the bounded manifest loader, which
    reports it.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError):
        return
    if not isinstance(root, yaml.MappingNode):
        return
    first_lines: dict[str, int] = {}
    for key_node, _value_node in root.value:
        if not isinstance(key_node, yaml.ScalarNode) or key_node.tag == _MERGE_TAG:
            continue
        line = key_node.start_mark.line + 1
        first = first_lines.get(key_node.value)
        if first is not None:
            raise DuplicateManifestKeyError(path, key_node.value, first, line)
        first_lines[key_node.value] = line


def read_manifest_text(path: str | Path) -> str:
    """Return ``apm.yml`` text after rejecting duplicate top-level keys."""
    manifest_path = Path(path)
    text = manifest_path.read_text(encoding="utf-8")
    reject_duplicate_manifest_keys(manifest_path, text)
    return text


def load_manifest(path: str | Path) -> Any:
    """Load ``apm.yml`` with the bounded loader."""
    from ..utils.yaml_io import load_yaml_str

    return load_yaml_str(read_manifest_text(path))


def load_manifest_roundtrip(path: str | Path) -> Any:
    """Load ``apm.yml`` for a comment-preserving rewrite."""
    from ..utils.yaml_io import load_yaml_roundtrip_str

    return load_yaml_roundtrip_str(read_manifest_text(path))
