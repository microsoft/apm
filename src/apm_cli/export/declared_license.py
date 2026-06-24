"""Install-time backfill of a dependency's DECLARED license (issue #1777, U6).

APM mirrors npm: it trusts the manifest's declared ``license`` field and never
reads the LICENSE file text or concludes a license. This helper reads the
declared SPDX expression from a resolved dependency at its install path.

Resolution order (apm.yml wins, mirroring how APM treats its own manifest as
authoritative over an ingested foreign one):

1. ``apm.yml`` top-level ``license:``
2. ``plugin.json`` ``license``

A blank or absent value yields ``None`` -- the caller OMITS the field from the
lockfile, keeping "not declared" (unknown) distinguishable from an explicit
declaration. No sentinel is ever stored.
"""

from __future__ import annotations

import json
from pathlib import Path

from apm_cli.utils.yaml_io import load_yaml


def _clean(value: object) -> str | None:
    """Return a stripped non-empty string, else None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _from_apm_yml(apm_yml: Path) -> str | None:
    try:
        data = load_yaml(apm_yml)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return _clean(data.get("license"))


def _from_plugin_json(plugin_json: Path) -> str | None:
    try:
        with open(plugin_json, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _clean(data.get("license"))


def read_declared_license(install_path: Path | str) -> str | None:
    """Read the declared license for a dependency installed at *install_path*.

    *install_path* may point at a directory or at a single primitive file; in
    the latter case the file's parent directory is searched for a manifest.
    Returns the declared SPDX expression verbatim, or ``None`` when no manifest
    declares one.
    """
    path = Path(install_path)
    base = path if path.is_dir() else path.parent

    apm_yml = base / "apm.yml"
    if apm_yml.is_file():
        declared = _from_apm_yml(apm_yml)
        if declared is not None:
            return declared

    plugin_json = base / "plugin.json"
    if plugin_json.is_file():
        return _from_plugin_json(plugin_json)

    return None


__all__ = ["read_declared_license"]
