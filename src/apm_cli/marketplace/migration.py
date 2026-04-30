"""Detection + migration helpers for marketplace authoring config.

Two config sources are supported:

* ``apm.yml`` with a top-level ``marketplace:`` block (current).
* Standalone ``marketplace.yml`` (legacy, deprecated).

This module provides:

* :class:`ConfigSource` -- enum identifying which file (if any) holds
  the marketplace config.
* :func:`detect_config_source` -- which loader the commands should use,
  with a hard error if both files exist (no silent precedence).
* :func:`load_marketplace_config` -- smart loader that emits a one-line
  deprecation warning when the legacy file is in play.
* :func:`migrate_marketplace_yml` -- one-shot conversion from legacy
  ``marketplace.yml`` into ``apm.yml``'s ``marketplace:`` block.
"""

from __future__ import annotations

import enum
import logging
from io import StringIO
from pathlib import Path
from typing import Optional  # noqa: F401

import yaml

from .errors import MarketplaceYmlError
from .yml_schema import (
    MarketplaceConfig,
    load_marketplace_from_apm_yml,
    load_marketplace_from_legacy_yml,
)

__all__ = [
    "DEPRECATION_MESSAGE",
    "ConfigSource",
    "detect_config_source",
    "load_marketplace_config",
    "migrate_marketplace_yml",
]


DEPRECATION_MESSAGE = (
    "marketplace.yml is deprecated. Run 'apm marketplace migrate' to "
    "fold it into apm.yml's 'marketplace:' block."
)


logger = logging.getLogger(__name__)


class ConfigSource(enum.Enum):
    """Which file (if any) holds the marketplace authoring config."""

    APM_YML = "apm.yml"
    LEGACY_YML = "marketplace.yml"
    NONE = "none"


def _has_marketplace_block(apm_yml_path: Path) -> bool:
    """Return ``True`` when *apm_yml_path* has a non-null ``marketplace:``.

    Missing files and valid YAML without a top-level ``marketplace`` block
    return ``False``. Read failures and YAML parse errors raise
    :class:`MarketplaceYmlError` so callers do not mistake a malformed
    ``apm.yml`` for an absent marketplace configuration (which would
    surface a misleading "no marketplace config" message instead of the
    real parse error).
    """
    if not apm_yml_path.exists():
        return False
    try:
        text = apm_yml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MarketplaceYmlError(f"Could not read {apm_yml_path.name}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MarketplaceYmlError(f"Invalid YAML in {apm_yml_path.name}: {exc}") from exc

    return isinstance(data, dict) and "marketplace" in data and data["marketplace"] is not None


def detect_config_source(project_root: Path) -> ConfigSource:
    """Return the active config source for *project_root*.

    Raises :class:`MarketplaceYmlError` when both apm.yml (with a
    ``marketplace:`` block) and a standalone ``marketplace.yml`` are
    present -- the user must explicitly choose which one wins.
    """
    apm_yml = project_root / "apm.yml"
    legacy_yml = project_root / "marketplace.yml"

    has_apm_block = _has_marketplace_block(apm_yml)
    has_legacy = legacy_yml.exists()

    if has_apm_block and has_legacy:
        raise MarketplaceYmlError(
            "Both apm.yml (with a 'marketplace:' block) and "
            "marketplace.yml exist. Remove marketplace.yml or run "
            "'apm marketplace migrate --force' to consolidate."
        )
    if has_apm_block:
        return ConfigSource.APM_YML
    if has_legacy:
        return ConfigSource.LEGACY_YML
    return ConfigSource.NONE


def load_marketplace_config(
    project_root: Path,
    *,
    warn_callback=None,
) -> MarketplaceConfig:
    """Smart loader: detect source, load, warn if legacy.

    Parameters
    ----------
    project_root : Path
        Directory holding apm.yml / marketplace.yml.
    warn_callback : callable, optional
        Invoked with the deprecation message string when a legacy file
        is loaded.  Defaults to a stdlib ``logging.warning`` call.

    Raises
    ------
    MarketplaceYmlError
        When no config is found, or when both files are present, or
        on any validation error from the underlying loaders.
    """
    source = detect_config_source(project_root)
    if source == ConfigSource.APM_YML:
        return load_marketplace_from_apm_yml(project_root / "apm.yml")
    if source == ConfigSource.LEGACY_YML:
        msg = DEPRECATION_MESSAGE
        if warn_callback is not None:
            warn_callback(msg)
        else:
            logger.warning(msg)
        return load_marketplace_from_legacy_yml(project_root / "marketplace.yml")
    raise MarketplaceYmlError(
        "No marketplace config found. Add a 'marketplace:' block to "
        "apm.yml or run 'apm marketplace init' to scaffold one."
    )


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _rt_yaml():
    """Return a configured ruamel.yaml round-trip instance."""
    from ruamel.yaml import YAML

    rt = YAML(typ="rt")
    rt.preserve_quotes = True
    rt.indent(mapping=2, sequence=4, offset=2)
    return rt


def _build_marketplace_block(legacy_data, apm_top: dict):
    """Construct a ruamel ``CommentedMap`` for the marketplace block.

    Inheritable fields (``name``, ``description``, ``version``) are
    omitted from the block when they match the apm.yml top-level
    values, and emitted as overrides when they differ.
    """
    from ruamel.yaml.comments import CommentedMap

    block = CommentedMap()

    for key in ("name", "description", "version"):
        if key in legacy_data and legacy_data[key] is not None:
            top_val = apm_top.get(key)
            legacy_val = legacy_data[key]
            if top_val != legacy_val:
                block[key] = legacy_val

    for key in ("owner", "output", "build", "metadata", "packages"):
        if key in legacy_data and legacy_data[key] is not None:
            block[key] = legacy_data[key]

    return block


def migrate_marketplace_yml(
    project_root: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Fold ``marketplace.yml`` into ``apm.yml``'s ``marketplace:`` block.

    Parameters
    ----------
    project_root : Path
        Directory containing apm.yml and marketplace.yml.
    force : bool
        Overwrite an existing ``marketplace:`` block in apm.yml.
    dry_run : bool
        When ``True``, do not write to disk or delete the legacy file.

    Returns
    -------
    str
        Unified diff describing the proposed apm.yml changes.

    Raises
    ------
    MarketplaceYmlError
        When marketplace.yml is missing, apm.yml is missing, or apm.yml
        already has a ``marketplace:`` block and ``force`` is ``False``.
    """
    legacy_path = project_root / "marketplace.yml"
    apm_path = project_root / "apm.yml"

    if not legacy_path.exists():
        raise MarketplaceYmlError("marketplace.yml not found -- nothing to migrate.")
    if not apm_path.exists():
        raise MarketplaceYmlError("apm.yml not found. Run 'apm init' first.")

    # Validate legacy file before doing anything destructive.
    load_marketplace_from_legacy_yml(legacy_path)

    rt = _rt_yaml()

    # Load legacy file (round-trip so comments/order are preserved when
    # we copy the body into apm.yml).
    legacy_data = rt.load(legacy_path.read_text(encoding="utf-8"))

    # Load apm.yml round-trip so we can safely insert the new key.
    apm_text = apm_path.read_text(encoding="utf-8")
    try:
        apm_data = rt.load(apm_text)
    except Exception as exc:  # ruamel.yaml.YAMLError and parser subclasses
        from ruamel.yaml import YAMLError

        if not isinstance(exc, YAMLError):
            raise
        raise MarketplaceYmlError(f"apm.yml is malformed: {exc}") from exc

    if apm_data is None:
        # Empty apm.yml: round-trip with an empty mapping so we can
        # still insert the marketplace block.
        from ruamel.yaml.comments import CommentedMap

        apm_data = CommentedMap()
    elif not isinstance(apm_data, dict):
        raise MarketplaceYmlError(
            "apm.yml must be a YAML mapping at the top level "
            f"(got {type(apm_data).__name__}). Cannot migrate."
        )

    if "marketplace" in apm_data and apm_data["marketplace"] is not None:
        if not force:
            raise MarketplaceYmlError(
                "apm.yml already has a 'marketplace:' block. Re-run with --force to overwrite."
            )

    block = _build_marketplace_block(legacy_data, apm_data)
    apm_data["marketplace"] = block

    # Render the proposed apm.yml.
    out_buf = StringIO()
    rt.dump(apm_data, out_buf)
    new_apm_text = out_buf.getvalue()

    # Build a unified diff for the caller to display.
    import difflib

    diff_lines = list(
        difflib.unified_diff(
            apm_text.splitlines(keepends=True),
            new_apm_text.splitlines(keepends=True),
            fromfile="apm.yml (current)",
            tofile="apm.yml (after migrate)",
            n=3,
        )
    )
    diff = "".join(diff_lines)

    if not dry_run:
        apm_path.write_text(new_apm_text, encoding="utf-8")
        legacy_path.unlink()

    return diff


def detect_inheritance_conflicts(legacy_data: dict, apm_data: dict) -> list:
    """Return human-readable conflict descriptions.

    Compares the inheritable scalars between legacy and apm.yml.
    Each conflict suggests the override to add to preserve the legacy
    behaviour.
    """
    conflicts: list = []
    for key in ("name", "description", "version"):
        legacy_val = legacy_data.get(key)
        apm_val = apm_data.get(key)
        if legacy_val is None or apm_val is None:
            continue
        if legacy_val != apm_val:
            conflicts.append(
                f"{key} in marketplace.yml ({legacy_val!r}) differs from "
                f"apm.yml ({apm_val!r}). The marketplace will use "
                f"{apm_val!r} (from apm.yml). Add marketplace.{key}: "
                f"{legacy_val!r} to override."
            )
    return conflicts
