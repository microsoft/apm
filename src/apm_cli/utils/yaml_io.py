"""Cross-platform YAML I/O with guaranteed UTF-8 encoding.

All YAML file operations in apm_cli should use these helpers to ensure
consistent encoding (UTF-8) and formatting (unicode, block style, key
order preserved).  This prevents silent mojibake on Windows where the
default file encoding is cp1252, not UTF-8.

Public API::

    load_yaml(path)        -- read a .yml/.yaml file -> dict | None
    dump_yaml(data, path)  -- write dict -> .yml/.yaml file
    yaml_to_str(data)      -- serialize dict -> YAML string
"""

import os
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

# Shared defaults matching existing codebase convention.
_DUMP_DEFAULTS: dict[str, Any] = dict(
    default_flow_style=False,
    sort_keys=False,
    allow_unicode=True,
)


class _BoundedSafeLoader(yaml.SafeLoader):
    """SafeLoader that bounds YAML merge-key (``<<``) expansion.

    PyYAML resolves merge keys EAGERLY in ``flatten_mapping``. A linear-size
    document that chains aliased merges (``<<: [*a, *a]`` once per level)
    doubles the merged value-list at each level, driving that resolution to
    O(2^N) work, so a sub-kilobyte ``apm.yml`` can hang the parser for minutes
    -- a CPU DoS reachable at PARSE time, before any post-parse structural
    guard (``_is_fingerprint_safe``) can run, and before the trust gate, so an
    untrusted clone could wedge ``apm install``.

    The stock ``flatten_mapping`` calls itself only O(N) times (it mutates
    each node in place, so a re-referenced alias is cheap on the second
    visit); the cost lives in the ``merge.extend`` copies whose CUMULATIVE
    volume grows like 2^N. So we reimplement ``flatten_mapping`` -- mirroring
    PyYAML 6.x exactly -- and bound (a) the cumulative count of merged entries
    and (b) the merge-recursion depth. A hostile manifest then raises a
    ``yaml.YAMLError`` (which every ``load_yaml`` caller already treats as
    fail-closed) within a small fixed budget instead of hanging. Both budgets
    are orders of magnitude above any legitimate hand-written config, so real
    ``<<`` merges still resolve correctly.
    """

    _MAX_MERGE_ENTRIES = 100_000
    _MAX_FLATTEN_DEPTH = 200

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._merge_entries = 0
        self._flatten_depth = 0

    def _merge_budget_guard(self, node: Any) -> None:
        if (
            self._merge_entries > self._MAX_MERGE_ENTRIES
            or self._flatten_depth > self._MAX_FLATTEN_DEPTH
        ):
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                getattr(node, "start_mark", None),
                "YAML merge-key expansion exceeded the safe budget "
                "(possible merge-key expansion bomb)",
                getattr(node, "start_mark", None),
            )

    def flatten_mapping(self, node: Any) -> Any:
        # Faithful reimplementation of PyYAML 6.x
        # ``SafeConstructor.flatten_mapping`` with a cumulative merged-entry
        # budget + depth guard woven into the recursion. Keep the control flow
        # identical to upstream so legitimate merges resolve identically.
        self._flatten_depth += 1
        try:
            self._merge_budget_guard(node)
            merge = []
            index = 0
            while index < len(node.value):
                key_node, value_node = node.value[index]
                if key_node.tag == "tag:yaml.org,2002:merge":
                    del node.value[index]
                    if isinstance(value_node, yaml.nodes.MappingNode):
                        self.flatten_mapping(value_node)
                        merge.extend(value_node.value)
                    elif isinstance(value_node, yaml.nodes.SequenceNode):
                        submerge = []
                        for subnode in value_node.value:
                            if not isinstance(subnode, yaml.nodes.MappingNode):
                                raise yaml.constructor.ConstructorError(
                                    "while constructing a mapping",
                                    node.start_mark,
                                    f"expected a mapping for merging, but found {subnode.id}",
                                    subnode.start_mark,
                                )
                            self.flatten_mapping(subnode)
                            submerge.append(subnode.value)
                        submerge.reverse()
                        for value in submerge:
                            merge.extend(value)
                    else:
                        raise yaml.constructor.ConstructorError(
                            "while constructing a mapping",
                            node.start_mark,
                            "expected a mapping or list of mappings for "
                            f"merging, but found {value_node.id}",
                            value_node.start_mark,
                        )
                    self._merge_entries += len(merge)
                    self._merge_budget_guard(node)
                elif key_node.tag == "tag:yaml.org,2002:value":
                    key_node.tag = "tag:yaml.org,2002:str"
                    index += 1
                else:
                    index += 1
            if merge:
                node.value = merge + node.value
        finally:
            self._flatten_depth -= 1


def load_yaml(path: str | Path) -> dict[str, Any] | None:
    """Load a YAML file with explicit UTF-8 encoding.

    Returns parsed data or ``None`` for empty files.
    Raises ``FileNotFoundError`` or ``yaml.YAMLError`` on failure.

    Uses a merge-bounded SafeLoader so a hostile manifest cannot wedge the
    parser via an eager ``<<`` merge-key expansion (see ``_BoundedSafeLoader``);
    the bomb fails closed as a ``yaml.YAMLError`` instead of hanging.
    """
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_BoundedSafeLoader)  # noqa: S506 - SafeLoader subclass


def dump_yaml(
    data: Any,
    path: str | Path,
    *,
    sort_keys: bool = False,
) -> None:
    """Write data to a YAML file with UTF-8 encoding and unicode support.

    Serializes to a string FIRST, then opens the destination file. A
    representer or value error (for example an integer whose decimal form
    exceeds CPython's ``int_max_str_digits`` limit -- reachable via a hex or
    octal literal that ``safe_load`` materialised without a digit cap) is
    therefore raised BEFORE the file is opened, so an unserialisable payload
    can never truncate the existing file to zero bytes.
    """
    text = yaml.safe_dump(data, **{**_DUMP_DEFAULTS, "sort_keys": sort_keys})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def yaml_to_str(data: Any, *, sort_keys: bool = False) -> str:
    """Serialize data to a YAML string with unicode support.

    Use instead of bare ``yaml.dump()`` when building YAML content
    for later file writes or string returns.
    """
    return yaml.safe_dump(data, **{**_DUMP_DEFAULTS, "sort_keys": sort_keys})


def write_yaml_text_atomic(
    path: str | Path,
    content: str,
    *,
    tmp_suffix: str = ".tmp",
) -> None:
    """Atomically replace a YAML file with already-rendered text.

    The replacement is written to a sibling file first and then moved into
    place with ``os.replace``. If the write or replace fails, the original
    file remains untouched.
    """
    target = Path(path)
    tmp_path: Path | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for _attempt in range(10):
            candidate = target.with_name(f".{target.name}.{secrets.token_hex(8)}{tmp_suffix}")
            try:
                fd = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            tmp_path = candidate
            break
        else:
            raise FileExistsError(f"Could not create a unique temp file for {target}")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, target)
        tmp_path = None
    except Exception:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()
        raise
