"""Lifecycle hook executors -- one per action type.

Each executor is fire-and-forget: it catches all exceptions internally
and logs failures in verbose mode only (using ``[i]`` ASCII symbol).

Two hook types (Copilot CLI aligned):

- ``command`` -- shell command via subprocess, event JSON on **stdin**
- ``http``    -- HTTPS POST with JSON body, env-var expansion in headers
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.core.lifecycle_hooks import HookEntry, LifecycleEvent

_logger = logging.getLogger(__name__)

# Fallback timeouts when hook entry does not specify one.
_DEFAULT_HTTP_TIMEOUT = 10
_DEFAULT_COMMAND_TIMEOUT = 30

# Pattern for $VAR or ${VAR} expansion in header values.
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def execute_hook(
    hook: HookEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> None:
    """Dispatch to the correct executor based on hook type."""
    if hook.hook_type == "http":
        _execute_http(hook, event, logger=logger, verbose=verbose)
    elif hook.hook_type == "command":
        _execute_command(hook, event, logger=logger, verbose=verbose, project_root=project_root)


# -- HTTP executor ----------------------------------------------------------


def _expand_env_vars(value: str) -> str:
    """Expand ``$VAR`` and ``${VAR}`` references in *value*."""

    def _replace(match: re.Match) -> str:
        var_name = match.group(1) or match.group(2)
        return os.environ.get(var_name, "")

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _execute_http(
    hook: HookEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> None:
    """Send an HTTP POST to the hook URL in a daemon thread.

    Security hardening:
    - HTTPS-only (rejects ``http://``)
    - No redirect following
    - Configurable timeout (default 10s)
    - Header values support ``$ENV_VAR`` expansion
    """
    url = hook.url
    if not url:
        _logger.debug("HTTP hook has no URL, skipping")
        return

    parsed = urlparse(url)
    if parsed.scheme != "https":
        if verbose and logger:
            logger.verbose_detail(
                f"[i] HTTP hook rejected: URL must use https (got {parsed.scheme}://)"
            )
        _logger.debug("Rejecting non-HTTPS hook URL: %s", url)
        return

    if not parsed.hostname:
        _logger.debug("HTTP hook URL has no hostname: %s", url)
        return

    # Build headers with env-var expansion.
    request_headers: dict[str, str] = {"Content-Type": "application/json"}
    if hook.headers:
        for key, val in hook.headers.items():
            request_headers[key] = _expand_env_vars(val)

    payload = event.to_json()
    timeout = hook.effective_timeout
    hostname = parsed.hostname

    def _send() -> None:
        try:
            import requests

            requests.post(
                url,
                data=payload,
                headers=request_headers,
                timeout=timeout,
                allow_redirects=False,
            )
        except Exception:
            _logger.debug("HTTP POST failed for %s", url, exc_info=True)

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()

    if verbose and logger:
        logger.verbose_detail(f"[i] {event.event} event sent to {hostname}")


# -- Command executor -------------------------------------------------------


def _execute_command(
    hook: HookEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> None:
    """Execute a shell command with the event payload on stdin."""
    cmd = hook.effective_command
    if not cmd:
        _logger.debug("Command hook has no command string, skipping")
        return

    env = _build_hook_env(hook)
    timeout = hook.effective_timeout
    cwd = _resolve_cwd(hook, project_root)

    try:
        subprocess.run(
            cmd,
            shell=True,
            env=env,
            input=event.to_json(),
            timeout=timeout,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        _logger.debug("Command hook timed out: %s", cmd)
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle command hook timed out: {cmd}")
    except Exception:
        _logger.debug("Command hook failed: %s", cmd, exc_info=True)
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle command hook failed: {cmd}")


# -- Helpers ----------------------------------------------------------------


def _build_hook_env(hook: HookEntry) -> dict[str, str]:
    """Build the environment dict for command hooks.

    Inherits the current process environment and merges any extra
    ``env`` entries from the hook definition.
    """
    env = dict(os.environ)
    if hook.env:
        env.update(hook.env)
    return env


def _resolve_cwd(hook: HookEntry, project_root: str | None) -> str | None:
    """Resolve the working directory for a command hook."""
    if not hook.cwd:
        return project_root
    from pathlib import Path

    if Path(hook.cwd).is_absolute():
        return hook.cwd
    root = Path(project_root) if project_root else Path.cwd()
    return str(root / hook.cwd)
