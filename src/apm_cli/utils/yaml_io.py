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

from pathlib import Path
from typing import Any, Dict, Optional, Union  # noqa: F401, UP035

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
