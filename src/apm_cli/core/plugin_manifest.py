"""Plugin manifest builder -- generates ``plugin.json`` for each target ecosystem.

Triggered when ``apm.yml`` declares a ``target:`` (or ``targets:``) field containing
``claude`` or ``copilot``.

Supported ecosystems and their output paths
-------------------------------------------
* ``claude``  -> ``.claude-plugin/plugin.json``
* ``copilot`` -> ``.github/plugin/plugin.json``

Only canonical targets that survive :func:`apm_cli.core.apm_yml.parse_targets_field`
validation are listed here -- there is exactly one source of truth for valid
ecosystem names (``CANONICAL_TARGETS``), so this module never declares aliases
that the parser would reject before the producer runs.

The builder delegates all heavy lifting to the existing
:func:`apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml` helper and
the :func:`collect_mcp_servers` local utility -- it adds only the per-ecosystem
routing and the final write step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..utils.console import _rich_info, _rich_success, _rich_warning
from ..utils.path_security import ensure_path_within

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_MANIFEST_ECOSYSTEMS: frozenset[str] = frozenset({"claude", "copilot"})
"""Target names that trigger plugin manifest generation.

Every entry MUST also be a member of
:data:`apm_cli.core.apm_yml.CANONICAL_TARGETS`; otherwise
:func:`apm_cli.core.apm_yml.parse_targets_field` rejects the token with
``UnknownTargetError`` before this module is ever reached, leaving the path
permanently dead.
"""

PLUGIN_ECOSYSTEM_PATHS: dict[str, str] = {
    "claude": ".claude-plugin/plugin.json",
    "copilot": ".github/plugin/plugin.json",
}
"""Output path (relative to project root) for each ecosystem's ``plugin.json``."""


# Server-object keys that may carry live credentials. Any key whose lowercased
# name matches an entry here (or contains one of the substrings below) is
# stripped before the manifest is serialised -- a committed plugin.json must
# never leak secrets that were resolved at MCP-host startup from .mcp.json.
_SENSITIVE_MCP_KEY_NAMES: frozenset[str] = frozenset({"env", "environment"})
_SENSITIVE_MCP_KEY_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "credential",
    "apikey",
)


def _is_sensitive_mcp_key(key: str) -> bool:
    """Return True when *key* names a credential-bearing field to strip."""
    normalized = key.lower().replace("_", "")
    if normalized in _SENSITIVE_MCP_KEY_NAMES:
        return True
    return any(token in normalized for token in _SENSITIVE_MCP_KEY_SUBSTRINGS)


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------


def collect_mcp_servers(project_root: Path, *, logger: Any = None) -> dict:
    """Return ``mcpServers`` dict from ``.mcp.json`` with credentials stripped.

    Returns an empty dict when the file is absent, is a symlink, or cannot be
    parsed.

    Each server object is sanitised before it is returned: any key that may
    carry a live credential (``env``/``environment`` blocks and any key whose
    name contains ``token``, ``secret``, ``password``, ``credential``, or
    ``apikey`` -- case-insensitive) is dropped. ``.mcp.json`` routinely embeds
    secrets so an MCP host can inject them at startup; copying them verbatim
    into a committed ``plugin.json`` would exfiltrate them into the distributed
    artefact. A loud warning is emitted for every key dropped.
    """
    mcp_file = project_root / ".mcp.json"
    if not mcp_file.is_file() or mcp_file.is_symlink():
        return {}
    try:
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            servers = data.get("mcpServers", {})
            if not isinstance(servers, dict):
                return {}
            return _sanitize_mcp_servers(dict(servers), logger=logger)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _sanitize_mcp_servers(servers: dict, *, logger: Any = None) -> dict:
    """Strip credential-bearing keys from every server object in *servers*."""
    cleaned: dict = {}
    dropped: list[str] = []
    for server_name, server in servers.items():
        if not isinstance(server, dict):
            cleaned[server_name] = server
            continue
        safe_server = {}
        for key, value in server.items():
            if _is_sensitive_mcp_key(str(key)):
                dropped.append(f"{server_name}.{key}")
                continue
            safe_server[key] = value
        cleaned[server_name] = safe_server

    if dropped:
        _warn = (
            "Stripped credential-bearing keys from .mcp.json before writing "
            "plugin.json (these would be committed as plaintext secrets): " + ", ".join(dropped)
        )
        if logger:
            logger.warning(_warn)
        else:
            _rich_warning(_warn, symbol="warning")
    return cleaned


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
                _rich_warning(_warn_msg, symbol="warning")

    elif not suppress_missing_warning:
        # Synthesis from apm.yml is the APM-native happy path for plugin
        # authoring, not a defect -- so this is info, not a warning.
        _info_msg = "No plugin.json found; synthesising from apm.yml."
        if logger:
            logger.info(_info_msg)
        else:
            _rich_info(_info_msg, symbol="info")

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
      a ``.mcp.json`` is present (with credential-bearing keys stripped).
    * **copilot**: author kept as ``{"name": ...}``; ``mcpServers`` omitted
      (not part of the Copilot plugin manifest schema).

    Convention directories (``agents/``, ``skills/``, ``commands/``) are
    auto-discovered by the host, so they are never listed explicitly in the
    manifest.
    """
    from ..deps.plugin_parser import synthesize_plugin_json_from_apm_yml

    # Always synthesise fresh from apm.yml -- apm.yml is the source of truth for
    # the generated manifest, so we intentionally do NOT consult an on-disk
    # plugin.json here (unlike find_or_synthesize_plugin_json, which is the
    # disk-first reader used by the bundle exporter).
    manifest: dict = synthesize_plugin_json_from_apm_yml(apm_yml_path)

    # Strip any convention-directory keys -- hosts auto-discover these.
    for key in ("agents", "skills", "commands", "instructions"):
        manifest.pop(key, None)

    if ecosystem == "claude":
        mcp_servers = collect_mcp_servers(project_root, logger=logger)
        if mcp_servers:
            manifest["mcpServers"] = mcp_servers
    else:
        # copilot -- omit mcpServers
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
    force: bool = False,
    logger: Any = None,
) -> Path | None:
    """Write *manifest* as ``plugin.json`` for *ecosystem* inside *project_root*.

    The output path is resolved from :data:`PLUGIN_ECOSYSTEM_PATHS`.  Parent
    directories are created automatically.

    **Overwrite policy.** If a ``plugin.json`` already exists at the target
    path it is preserved unless *force* is set (threaded from ``apm pack
    --force``). Without ``--force`` the function emits a warning and skips the
    write, returning ``None`` -- this mirrors the collision contract the rest
    of ``apm pack`` already honours and prevents a compromised ``.mcp.json``
    from silently replacing a hand-audited file. With ``--force`` the existing
    file is overwritten and a warning records the replacement.

    In dry-run mode the function logs what *would* be written and returns
    ``None`` without touching the filesystem.

    Returns the output :class:`~pathlib.Path` on a successful write, or ``None``
    for an unknown ecosystem, a dry-run, or a skipped overwrite.
    """
    rel_path = PLUGIN_ECOSYSTEM_PATHS.get(ecosystem)
    if rel_path is None:
        _warn = f"Unknown plugin ecosystem {ecosystem!r}; skipping plugin.json generation."
        if logger:
            logger.warning(_warn)
        else:
            _rich_warning(_warn, symbol="warning")
        return None

    output_path = project_root / rel_path

    # Containment guard: reject symlink-based escapes (e.g. a symlinked
    # .github/ directory pointing outside the project root).
    ensure_path_within(output_path, project_root)

    if dry_run:
        _msg = f"Would write plugin manifest to {output_path}"
        if logger:
            logger.info(_msg)
        else:
            _rich_info(_msg, symbol="preview")
        return None

    if output_path.exists():
        if not force:
            _skip_warn = (
                f"{output_path} already exists; skipping plugin.json generation. "
                "Re-run with --force to allow overwriting, or commit the generated "
                "file to silence this warning in CI."
            )
            if logger:
                logger.warning(_skip_warn)
            else:
                _rich_warning(_skip_warn, symbol="warning")
            return None

        _overwrite_warn = (
            f"Overwriting {output_path} with generated manifest from apm.yml (--force)."
        )
        if logger:
            logger.warning(_overwrite_warn)
        else:
            _rich_warning(_overwrite_warn, symbol="warning")

    # Generated content under .github/ is granted elevated trust by GitHub
    # Actions -- surface the write so operators with branch-protection on
    # .github/ paths are not surprised.
    if rel_path.startswith(".github/"):
        _gh_note = f"Writing generated plugin manifest under .github/: {output_path}"
        if logger:
            logger.info(_gh_note)
        else:
            _rich_info(_gh_note, symbol="info")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Re-check containment after mkdir to shrink the TOCTOU window -- a parent
    # component could have been swapped for a symlink between the first check
    # and directory creation.
    ensure_path_within(output_path, project_root)
    output_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    _success = f"Generated plugin manifest: {output_path}"
    if logger:
        logger.info(_success)
    else:
        _rich_success(_success, symbol="check")

    return output_path
