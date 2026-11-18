"""Read owner for ``apm.yml`` manifests: one mapping per top-level key, no anchors.

YAML loaders disagree on duplicate mapping keys: the bounded PyYAML loader
keeps the last value silently, while the ruamel round-trip loader raises a
parser-internal error. A manifest with two ``dependencies:`` blocks (a
common merge-conflict leftover) would therefore drop packages on one path
and fail with an unreadable traceback-style message on another.

OpenAPM req-mf-020(b) also requires a consumer to reject YAML anchors and
aliases (``&name`` / ``*name``) in a manifest.

Every ``apm.yml`` read routes through this module so the file is rejected with
one actionable error before any command touches the project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_MERGE_TAG = "tag:yaml.org,2002:merge"


class InvalidManifestError(ValueError):
    """An ``apm.yml`` that every command refuses before touching the project."""


class DuplicateManifestKeyError(InvalidManifestError):
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


class ManifestAnchorError(InvalidManifestError):
    """Raised when an ``apm.yml`` uses a YAML anchor or alias (req-mf-020(b))."""

    def __init__(self, path: Path, kind: str, name: str, line: int) -> None:
        self.path = path
        self.kind = kind
        self.line = line
        sigil = "&" if kind == "anchor" else "*"
        super().__init__(
            f"{path} uses the YAML {kind} '{sigil}{name}' on line {line}. "
            "apm.yml must not use YAML anchors (&name) or aliases (*name). "
            "Write the value out in full wherever it is used, then retry."
        )


def reject_manifest_anchors(path: Path, text: str) -> None:
    """Raise :class:`ManifestAnchorError` for the first anchor or alias in *text*.

    Scanning tokens never builds or expands the document. Malformed YAML with
    no anchor before the error is left to the bounded manifest loader.
    """
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(token, yaml.AnchorToken | yaml.AliasToken):
                kind = "anchor" if isinstance(token, yaml.AnchorToken) else "alias"
                raise ManifestAnchorError(path, kind, token.value, token.start_mark.line + 1)
    except (yaml.YAMLError, RecursionError):
        return


def check_manifest_text(path: str | Path, text: str) -> str:
    """Return *text* after the checks every ``apm.yml`` read applies."""
    manifest_path = Path(path)
    reject_duplicate_manifest_keys(manifest_path, text)
    reject_manifest_anchors(manifest_path, text)
    return text


def read_manifest_text(path: str | Path) -> str:
    """Return ``apm.yml`` text after rejecting duplicate keys, anchors and aliases."""
    return check_manifest_text(path, Path(path).read_text(encoding="utf-8"))


def load_manifest_text(path: str | Path, text: str) -> Any:
    """Load in-memory ``apm.yml`` text, such as a rewrite, with the read checks."""
    from ..utils.yaml_io import load_yaml_str

    return load_yaml_str(check_manifest_text(path, text))


def load_manifest(path: str | Path) -> Any:
    """Load ``apm.yml`` with the bounded loader."""
    from ..utils.yaml_io import load_yaml_str

    return load_yaml_str(read_manifest_text(path))


def load_manifest_roundtrip(path: str | Path) -> Any:
    """Load ``apm.yml`` for a comment-preserving rewrite."""
    from ..utils.yaml_io import load_yaml_roundtrip_str

    return load_yaml_roundtrip_str(read_manifest_text(path))
