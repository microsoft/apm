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


class _BlockStringDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings as literal block scalars.

    Opt-in via ``yaml_to_str(..., multiline_block=True)``.  Single-line
    strings are unaffected.  The emitter falls back to a quoted style on
    its own when ``|`` cannot faithfully represent the value (e.g. trailing
    whitespace), so output stays valid and round-trips.
    """


def _represent_str_block(dumper: yaml.Dumper, data: str) -> yaml.nodes.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockStringDumper.add_representer(str, _represent_str_block)


def load_yaml(path: str | Path) -> dict[str, Any] | None:
    """Load a YAML file with explicit UTF-8 encoding.

    Returns parsed data or ``None`` for empty files.
    Raises ``FileNotFoundError`` or ``yaml.YAMLError`` on failure.
    """
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def dump_yaml(
    data: Any,
    path: str | Path,
    *,
    sort_keys: bool = False,
) -> None:
    """Write data to a YAML file with UTF-8 encoding and unicode support."""
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, **{**_DUMP_DEFAULTS, "sort_keys": sort_keys})


def yaml_to_str(data: Any, *, sort_keys: bool = False, multiline_block: bool = False) -> str:
    """Serialize data to a YAML string with unicode support.

    Use instead of bare ``yaml.dump()`` when building YAML content
    for later file writes or string returns.

    When *multiline_block* is True, multi-line strings render as literal
    block scalars (``key: |``) instead of quoted flow scalars -- the
    human-readable form for embedded prose (e.g. Goose recipe
    ``instructions``).  Single-line strings are unaffected.  A wide line
    width is used so a long single-line value (e.g. a recipe ``prompt``) is
    not wrapped mid-sentence.
    """
    if multiline_block:
        return yaml.dump(
            data,
            Dumper=_BlockStringDumper,
            width=4096,
            **{**_DUMP_DEFAULTS, "sort_keys": sort_keys},
        )
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
