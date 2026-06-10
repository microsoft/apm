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
    tmp_path = target.with_name(f".{target.name}{tmp_suffix}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    except Exception:
        with suppress(OSError):
            tmp_path.unlink()
        raise
