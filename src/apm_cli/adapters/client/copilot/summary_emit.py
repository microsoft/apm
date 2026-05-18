"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

from __future__ import annotations

import os
import re
import sys

import click

from ..base import _ENV_VAR_RE
from .class_ import _has_env_placeholder

_COPILOT_ENV_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>|" + _ENV_VAR_RE.pattern)
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")


def _scan_env_block(block: dict) -> set:
    """Return the set of env-block keys whose values are literal (non-placeholder) strings."""
    from .class_ import _has_env_placeholder

    keys: set = set()
    if isinstance(block, dict):
        for k, v in block.items():
            if isinstance(v, str) and v.strip() and not _has_env_placeholder(v):
                keys.add(k)
    return keys


def _scan_headers_block(block: dict) -> bool:
    """Return True if any headers-block value is a literal (non-placeholder) string."""
    from .class_ import _has_env_placeholder

    if isinstance(block, dict):
        for v in block.values():
            if isinstance(v, str) and v.strip() and not _has_env_placeholder(v):
                return True
    return False


def _collect_previously_baked_keys(self, server_url, server_name):
    """Return ``(env_keys, headers_were_baked)`` for the existing on-disk
    entry: the set of env-block keys whose values are literal
    (non-placeholder) strings, and a flag indicating whether the headers
    block contained any literal values. Together these drive the
    security-improvement notice. Headers don't expose env-var names
    directly, so the caller unions current-write placeholder keys when
    ``headers_were_baked`` is True.
    """
    try:
        current = self.get_current_config()
    except Exception:
        return set(), False
    servers = current.get("mcpServers") or {}
    # Match the same key resolution rule used below.
    if server_name:
        key = server_name
    elif "/" in server_url:
        key = server_url.split("/")[-1]
    else:
        key = server_url
    existing = servers.get(key)
    if not isinstance(existing, dict):
        return set(), False
    baked_env_keys = _scan_env_block(existing.get("env") or {})
    headers_were_baked = _scan_headers_block(existing.get("headers") or {})
    return baked_env_keys, headers_were_baked


def _emit_install_summary(self, config_key, server_config):
    """Record env-var references for the post-install aggregated
    summary. No per-server line is emitted here; the integrator's
    tree (``|  +  {name} -> Copilot (configured)``) is the success
    signal. The summary references env-var names only -- never their
    values.
    """
    if not self._supports_runtime_env_substitution:
        return
    keys = set(self._last_env_placeholder_keys)
    if isinstance(server_config, dict):
        for block_key in ("env", "headers"):
            block = server_config.get(block_key)
            if not isinstance(block, dict):
                continue
            for value in block.values():
                if isinstance(value, str):
                    for match in _ENV_VAR_RE.finditer(value):
                        keys.add(match.group(1))
    unset = sorted(name for name in keys if not os.environ.get(name))
    if unset:
        self.__class__._unset_env_keys_by_server.setdefault(config_key, []).extend(
            u
            for u in unset
            if u not in self.__class__._unset_env_keys_by_server.get(config_key, [])
        )


def emit_install_run_summary(cls):
    """Emit aggregated cross-server diagnostics at the end of an install
    run. Idempotent: subsequent calls within the same process are no-ops.

    Three diagnostics are emitted (when applicable):

    1. Security improvement notice -- when the install rewrote
       previously baked literal env values to runtime placeholders.
       Emitted as a warning because it is an action item (the user
       must export the affected vars).
    2. Aggregated unset-env warning -- when one or more configured
       servers reference env vars that are not currently exported.
       Includes a copy-pasteable ``export`` hint.
    3. Aggregated legacy ``<VAR>`` deprecation warning -- one line
       naming all affected servers, mirroring the established VS Code
       adapter pattern.

    State is drained after emission so a subsequent install run in
    the same process (e.g. tests) starts clean.
    """
    if cls._install_run_summary_emitted:
        return

    # Visual separator from the install tree's closing line so the
    # post-tree summary block reads as a distinct section.
    emitted_any = False

    def _emit_separator_once():
        nonlocal emitted_any
        if not emitted_any:
            click.echo("")
            emitted_any = True

    if cls._security_upgraded_keys:
        visible = sorted(cls._security_upgraded_keys)
        count = len(visible)
        noun = "variable" if count == 1 else "variables"
        affected = ", ".join(visible)
        _emit_separator_once()
        sys.modules[__package__]._rich_warning(
            f"Security improvement: {count} environment {noun} previously stored as "
            f"plaintext in the Copilot config are now resolved at runtime.\n"
            f"    Affected: {affected}\n"
            f"    Ensure these are exported in your shell before running 'gh copilot'",
            symbol="warning",
        )
    if cls._unset_env_keys_by_server:
        all_unset: set[str] = set()
        for names in cls._unset_env_keys_by_server.values():
            all_unset.update(names)
        sorted_unset = sorted(all_unset)
        export_hint = " ".join(f"{name}=..." for name in sorted_unset)
        count = len(sorted_unset)
        noun = "variable" if count == 1 else "variables"
        _emit_separator_once()
        sys.modules[__package__]._rich_warning(
            f"Copilot CLI will resolve {count} environment {noun} at runtime "
            f"that {'is' if count == 1 else 'are'} not currently set: "
            f"{', '.join(sorted_unset)}.\n"
            f"    Export {'it' if count == 1 else 'them'} in your shell before "
            f"running 'gh copilot', e.g.:\n"
            f"      export {export_hint}",
            symbol="warning",
        )
    # Deprecation notice is informational housekeeping (not a runtime
    # blocker), but it ships unguarded for now so legacy <VAR> usage
    # remains visible until the v1.0 removal. If --quiet gating is
    # added in future, the unset-env and security warnings above must
    # remain unsuppressible because they describe action-required state.
    if cls._legacy_angle_offenders_by_server:
        servers = sorted(cls._legacy_angle_offenders_by_server.keys())
        count = len(servers)
        noun = "server" if count == 1 else "servers"
        _emit_separator_once()
        sys.modules[__package__]._rich_warning(
            f"Deprecated: <VAR> placeholder syntax used in {count} {noun} "
            f"({', '.join(servers)}). Migrate to ${{VAR}} in apm.yml. "
            f"<VAR> support will be removed in v1.0.",
            symbol="warning",
        )
    cls._install_run_summary_emitted = True


def reset_install_run_state(cls):
    """Reset the process-wide aggregation buckets. Intended for tests
    and for explicitly starting a new install run within the same
    process."""
    cls._legacy_angle_offenders_by_server = {}
    cls._security_upgraded_keys = set()
    cls._unset_env_keys_by_server = {}
    cls._install_run_summary_emitted = False
