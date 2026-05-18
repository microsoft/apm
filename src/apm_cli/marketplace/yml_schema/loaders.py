# pylint: disable=duplicate-code
"""Dataclasses, loader, and validation for marketplace authoring config.

The marketplace publisher configuration may live in two places:

* (Preferred, current) inside ``apm.yml`` under a top-level
  ``marketplace:`` block.  Loaded via
  :func:`load_marketplace_from_apm_yml`.
* (Legacy, deprecated) inside a standalone ``marketplace.yml`` file.
  Loaded via :func:`load_marketplace_from_legacy_yml`.

Both paths produce the same immutable :class:`MarketplaceConfig`
dataclass that the builder consumes.

Key design rules
----------------
* **Anthropic pass-through preservation.**  The ``metadata`` block is
  stored as a plain ``dict`` with original key casing (e.g.
  ``pluginRoot`` stays ``pluginRoot``).  Unknown keys inside ``metadata``
  are preserved -- only the builder decides what is forwarded.
* **APM-only vs Anthropic separation.**  Build-time fields (``build``,
  ``version``, ``ref``, ``subdir``, ``tag_pattern``,
  ``include_prerelease``) live as explicit dataclass attributes so the
  builder can strip them cleanly.
* **Strict key sets.**  Unknown keys inside the marketplace block raise
  ``MarketplaceYmlError`` so typos are never silently ignored.  The
  apm.yml top-level is intentionally NOT strict here -- only the
  ``marketplace:`` subtree is validated by this module.
* **Local-path packages.**  ``source`` accepts ``./...`` paths in
  addition to ``owner/repo`` shape.  Local packages skip ref resolution.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..errors import MarketplaceYmlError
from ._loader_validators import _build_config, _BuildConfigInput
from .class_ import MarketplaceConfig
from .parse_helpers import (
    _check_unknown_keys,
    _parse_build,
    _parse_claude,
    _parse_codex,
    _parse_outputs,
    _parse_owner,
    _parse_package_entry,
    _require_str,
    _validate_semver,
)

_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SOURCE_RE = re.compile(r"^(?:[^/]+/[^/]+|\./.*)$")
LOCAL_SOURCE_RE = re.compile(r"^\./")
_TAG_PLACEHOLDERS = ("{version}", "{name}")
_BUILD_KEYS = frozenset(
    {
        "tagPattern",
    }
)
_PACKAGE_ENTRY_KEYS = frozenset(
    {
        "name",
        "source",
        "subdir",
        "version",
        "ref",
        "tag_pattern",
        "include_prerelease",
        "description",
        "homepage",
        "tags",
        "author",
        "license",
        "repository",
        "keywords",
        "category",
    }
)
_MAX_TAGS_COUNT = 50
_MAX_TAG_LENGTH = 100
_AUTHOR_OBJECT_KEYS = frozenset({"name", "email", "url"})
_APM_MARKETPLACE_KEYS = frozenset(
    {
        "name",  # optional override of top-level apm.yml name
        "description",  # optional override of top-level apm.yml description
        "version",  # optional override of top-level apm.yml version
        "owner",
        "output",
        "outputs",
        "claude",
        "metadata",
        "build",
        "codex",
        "packages",
        "versioning",
    }
)
_CLAUDE_KEYS = frozenset(
    {
        "output",
    }
)
_CODEX_KEYS = frozenset(
    {
        "output",
    }
)
MarketplaceYml = MarketplaceConfig


def load_marketplace_yml(path: Path) -> MarketplaceConfig:
    """Backwards-compatible loader for a standalone ``marketplace.yml``.

    Equivalent to :func:`load_marketplace_from_legacy_yml`.  Preserved
    for callers that imported the original symbol.
    """
    return load_marketplace_from_legacy_yml(path)


def load_marketplace_from_legacy_yml(path: Path) -> MarketplaceConfig:
    """Load and validate a standalone ``marketplace.yml`` (legacy).

    The legacy file holds the marketplace block at the YAML root.
    ``name``, ``description``, ``version`` are all required at this
    level (they are not inheritable in the legacy world).

    Parameters
    ----------
    path : Path
        Filesystem path to the YAML file.

    Returns
    -------
    MarketplaceConfig
        Fully validated, immutable representation, with
        ``is_legacy=True`` and all override flags set to ``True`` (the
        legacy file always carries the values explicitly).

    Raises
    ------
    MarketplaceYmlError
        On any validation failure or YAML parse error.
    """
    data = _read_yaml_mapping(path)

    # -- strict top-level key check --
    _check_unknown_keys(data, _APM_MARKETPLACE_KEYS, context="top level")

    # -- required scalars --
    name = _require_str(data, "name")
    description = _require_str(data, "description")
    version_str = _require_str(data, "version")
    _validate_semver(version_str, context="version")

    return _build_config(
        _BuildConfigInput(
            marketplace_dict=data,
            name=name,
            description=description,
            version=version_str,
            source_path=path,
            is_legacy=True,
            name_overridden=True,
            description_overridden=True,
            version_overridden=True,
            default_output="marketplace.json",
        )
    )


def load_marketplace_from_apm_yml(apm_yml_path: Path) -> MarketplaceConfig:
    """Load marketplace config from apm.yml's ``marketplace:`` block.

    Reads the full YAML, extracts top-level ``name``/``version``/
    ``description``, then parses the ``marketplace:`` block.  Inherits
    the three top-level scalars when the marketplace block does not
    explicitly override them.

    Parameters
    ----------
    apm_yml_path : Path
        Filesystem path to apm.yml.

    Returns
    -------
    MarketplaceConfig
        Fully validated, immutable representation.

    Raises
    ------
    MarketplaceYmlError
        If apm.yml is missing the ``marketplace:`` block or any
        validation fails.
    """
    data = _read_yaml_mapping(apm_yml_path)

    raw_block = data.get("marketplace")
    if raw_block is None:
        raise MarketplaceYmlError(
            f"'{apm_yml_path}' has no 'marketplace:' block. "
            "Add one or run 'apm marketplace init' to scaffold it."
        )
    if not isinstance(raw_block, dict):
        raise MarketplaceYmlError("'marketplace' in apm.yml must be a mapping")

    # -- strict marketplace-block key check --
    _check_unknown_keys(raw_block, _APM_MARKETPLACE_KEYS, context="marketplace")

    # -- inheritance with optional overrides --
    top_name = data.get("name")
    top_desc = data.get("description")
    top_ver = data.get("version")

    name_overridden = "name" in raw_block and raw_block["name"] is not None
    desc_overridden = "description" in raw_block and raw_block["description"] is not None
    ver_overridden = "version" in raw_block and raw_block["version"] is not None

    if name_overridden:
        name = _require_str(raw_block, "name", context="marketplace")
    else:
        if not isinstance(top_name, str) or not top_name.strip():
            raise MarketplaceYmlError(
                "'name' is required (set it at apm.yml top level or override via marketplace.name)"
            )
        name = top_name.strip()

    if desc_overridden:
        description = _require_str(raw_block, "description", context="marketplace")
    elif not isinstance(top_desc, str) or not top_desc.strip():
        description = ""
    else:
        description = top_desc.strip()

    if ver_overridden:
        version_str = _require_str(raw_block, "version", context="marketplace")
    elif top_ver is None:
        version_str = ""
    else:
        version_str = str(top_ver).strip()

    if version_str:
        _validate_semver(version_str, context="version")

    return _build_config(
        _BuildConfigInput(
            marketplace_dict=raw_block,
            name=name,
            description=description,
            version=version_str,
            source_path=apm_yml_path,
            is_legacy=False,
            name_overridden=name_overridden,
            description_overridden=desc_overridden,
            version_overridden=ver_overridden,
        )
    )


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read *path* and return its top-level mapping or raise."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MarketplaceYmlError(f"Cannot read '{path}': {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = ""
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            mark = exc.problem_mark
            detail = f" (line {mark.line + 1}, column {mark.column + 1})"
        raise MarketplaceYmlError(f"YAML parse error in '{path}'{detail}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise MarketplaceYmlError(f"'{path}' must contain a YAML mapping at the top level")
    return data
