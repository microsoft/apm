"""Plugin manifest builder -- generates ``plugin.json`` for each target ecosystem.

Triggered when ``apm.yml`` declares a ``target:`` (or ``targets:``) field containing
``claude``, ``copilot``, ``vscode``, or ``agents``.

Supported ecosystems and their output paths
-------------------------------------------
* ``claude``  → ``.claude-plugin/plugin.json``
* ``copilot`` → ``.github/plugin/plugin.json``
* ``vscode``  → ``.github/plugin/plugin.json``  (alias for copilot)
* ``agents``  → ``.github/plugin/plugin.json``  (alias for copilot)

The builder delegates all heavy lifting to the existing
:func:`apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml` helper and
the :func:`collect_mcp_servers` local utility -- it adds only the per-ecosystem
routing and the final write step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..utils.console import _rich_info, _rich_warning

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_MANIFEST_ECOSYSTEMS: frozenset[str] = frozenset({"claude", "copilot", "vscode", "agents"})
"""Target names that trigger plugin manifest generation."""

PLUGIN_ECOSYSTEM_PATHS: dict[str, str] = {
    "claude": ".claude-plugin/plugin.json",
    "copilot": ".github/plugin/plugin.json",
    "vscode": ".github/plugin/plugin.json",
    "agents": ".github/plugin/plugin.json",
}
"""Output path (relative to project root) for each ecosystem's ``plugin.json``."""


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------


def collect_mcp_servers(project_root: Path) -> dict:
    """Return ``mcpServers`` dict from ``.mcp.json``.

    Returns an empty dict when the file is absent, is a symlink, or cannot be
    parsed.
    """
    mcp_file = project_root / ".mcp.json"
    if not mcp_file.is_file() or mcp_file.is_symlink():
        return {}
    try:
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            servers = data.get("mcpServers", {})
            return dict(servers) if isinstance(servers, dict) else {}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Plugin JSON source
# ---------------------------------------------------------------------------


def find_or_synthesize_plugin_json(
    project_root: Path,
    apm_yml_path: Path,
    *,
    suppress_missing_warning: bool = False,
    logger: Any = None,
) -> dict:
    """Locate an existing ``plugin.json`` or synthesise one from ``apm.yml``.

    Resolution order:

    1. Call :func:`apm_cli.utils.helpers.find_plugin_json` to locate an
       on-disk ``plugin.json``.
    2. If found, parse and return it.  On a parse error, warn and fall back to
       synthesis.
    3. If not found, synthesise from ``apm.yml`` via
       :func:`apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml`.

    ``suppress_missing_warning`` silences the "no plugin.json on disk" info
    message when the caller knows synthesis is the expected path -- for example
    a marketplace-publishing run that also has a ``dependencies:`` block for
    local development.  Genuine parse errors on an existing file are always
    surfaced.
    """
    from ..deps.plugin_parser import synthesize_plugin_json_from_apm_yml
    from ..utils.helpers import find_plugin_json

    plugin_json_path = find_plugin_json(project_root)
    if plugin_json_path is not None:
        try:
            return json.loads(plugin_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _warn_msg = (
                f"Found plugin.json at {plugin_json_path} but could not parse it: {exc}. "
                "Falling back to synthesis from apm.yml."
            )
            if logger:
                logger.warning(_warn_msg)
            else:
                _rich_warning(_warn_msg)

    elif not suppress_missing_warning:
        # Demoted from warning to info: synthesis from apm.yml is the
        # APM-native happy path for plugin authoring, not a defect.
        _info_msg = (
            "No plugin.json on disk; deriving it from apm.yml (the APM-native source of truth)."
        )
        if logger:
            logger.info(_info_msg)
        else:
            _rich_info(_info_msg)

    return synthesize_plugin_json_from_apm_yml(apm_yml_path)


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


def build_plugin_manifest(
    project_root: Path,
    apm_yml_path: Path,
    ecosystem: str,
    *,
    logger: Any = None,
) -> dict:
    """Build a ``plugin.json`` payload for the given *ecosystem*.

    The base fields are synthesised from ``apm.yml`` via
    :func:`apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml`.
    Per-ecosystem rules are then applied:

    * **claude**: author kept as ``{"name": ...}``; ``mcpServers`` included when
      a ``.mcp.json`` is present.
    * **copilot / vscode / agents**: author kept as ``{"name": ...}``; ``mcpServers``
      omitted (not part of the Copilot plugin manifest schema).

    Convention directories (``agents/``, ``skills/``, ``commands/``) are
    auto-discovered by the host, so they are never listed explicitly in the
    manifest.
    """
    from ..deps.plugin_parser import synthesize_plugin_json_from_apm_yml

    manifest: dict = synthesize_plugin_json_from_apm_yml(apm_yml_path)

    # Strip any convention-directory keys -- hosts auto-discover these.
    for key in ("agents", "skills", "commands", "instructions"):
        manifest.pop(key, None)

    if ecosystem == "claude":
        mcp_servers = collect_mcp_servers(project_root)
        if mcp_servers:
            manifest["mcpServers"] = mcp_servers
    else:
        # copilot / vscode / agents -- omit mcpServers
        manifest.pop("mcpServers", None)

    return manifest


# ---------------------------------------------------------------------------
# Manifest writer
# ---------------------------------------------------------------------------


def write_plugin_manifest(
    project_root: Path,
    manifest: dict,
    ecosystem: str,
    *,
    dry_run: bool = False,
    logger: Any = None,
) -> Path | None:
    """Write *manifest* as ``plugin.json`` for *ecosystem* inside *project_root*.

    The output path is resolved from :data:`PLUGIN_ECOSYSTEM_PATHS`.  Parent
    directories are created automatically.

    If the target file already exists a loud warning is emitted together with a
    suppression hint.

    In dry-run mode the function logs what *would* be written and returns
    ``None`` without touching the filesystem.

    Returns the output :class:`~pathlib.Path` on success, or ``None`` for an
    unknown ecosystem or dry-run execution.
    """
    rel_path = PLUGIN_ECOSYSTEM_PATHS.get(ecosystem)
    if rel_path is None:
        _warn = f"Unknown plugin ecosystem {ecosystem!r}; skipping plugin.json generation."
        if logger:
            logger.warning(_warn)
        else:
            _rich_warning(_warn)
        return None

    output_path = project_root / rel_path

    if dry_run:
        _msg = f"[dry-run] Would write plugin manifest to {output_path}"
        if logger:
            logger.info(_msg)
        else:
            _rich_info(_msg)
        return None

    if output_path.exists():
        _overwrite_warn = (
            f"[!] Overwriting {output_path} with generated manifest from apm.yml. "
            f"To suppress: remove '{ecosystem}' from target in apm.yml."
        )
        if logger:
            logger.warning(_overwrite_warn)
        else:
            _rich_warning(_overwrite_warn)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    return output_path
