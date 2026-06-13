"""Lifecycle hook executors -- one per action type.

Each executor is fire-and-forget: it catches all exceptions internally
and logs failures in verbose mode only (using ``[i]`` ASCII symbol).
Webhook calls run in a daemon thread so the CLI never blocks.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.core.lifecycle_hooks import HookDefinition, LifecycleEvent

_logger = logging.getLogger(__name__)

# Maximum time (seconds) for webhook HTTP calls and command/script execution.
_WEBHOOK_TIMEOUT = 2
_COMMAND_TIMEOUT = 30


def execute_hook(
    hook: HookDefinition,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> None:
    """Dispatch to the correct executor based on hook type."""
    if hook.hook_type == "webhook":
        _execute_webhook(hook, event, logger=logger, verbose=verbose)
    elif hook.hook_type == "command":
        _execute_command(hook, event, logger=logger, verbose=verbose)
    elif hook.hook_type == "script":
        _execute_script(hook, event, logger=logger, verbose=verbose, project_root=project_root)


# -- Webhook executor -------------------------------------------------------


def _execute_webhook(
    hook: HookDefinition,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> None:
    """Send an HTTP POST to the webhook URL in a daemon thread.

    Security hardening:
    - HTTPS-only (rejects ``http://``)
    - No redirect following
    - Short timeout (2s)
    - Bearer token via ``Authorization`` header
    """
    url = hook.url
    if not url:
        _logger.debug("Webhook hook has no URL, skipping")
        return

    parsed = urlparse(url)
    if parsed.scheme != "https":
        if verbose and logger:
            logger.verbose_detail(
                f"[i] Webhook hook rejected: URL must use https (got {parsed.scheme}://)"
            )
        _logger.debug("Rejecting non-HTTPS webhook URL: %s", url)
        return

    if not parsed.hostname:
        _logger.debug("Webhook URL has no hostname: %s", url)
        return

    # Read bearer token from the env var named by token_env.
    token = None
    if hook.token_env:
        token = os.environ.get(hook.token_env)

    payload = event.to_json()
    hostname = parsed.hostname

    def _send() -> None:
        try:
            import requests

            headers: dict[str, str] = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            requests.post(
                url,
                data=payload,
                headers=headers,
                timeout=_WEBHOOK_TIMEOUT,
                allow_redirects=False,
            )
        except Exception:
            _logger.debug("Webhook POST failed for %s", url, exc_info=True)

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()

    if verbose and logger:
        logger.verbose_detail(f"[i] {event.event} event sent to {hostname}")


# -- Command executor -------------------------------------------------------


def _execute_command(
    hook: HookDefinition,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> None:
    """Execute a shell command with ``APM_HOOK_EVENT`` in the environment."""
    cmd = hook.run
    if not cmd:
        _logger.debug("Command hook has no run string, skipping")
        return

    env = _build_hook_env(event)

    try:
        subprocess.run(
            cmd,
            shell=True,
            env=env,
            timeout=_COMMAND_TIMEOUT,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        _logger.debug("Command hook timed out: %s", cmd)
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle command hook timed out: {cmd}")
    except Exception:
        _logger.debug("Command hook failed: %s", cmd, exc_info=True)
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle command hook failed: {cmd}")


# -- Script executor --------------------------------------------------------


def _execute_script(
    hook: HookDefinition,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> None:
    """Execute a script file with ``APM_HOOK_EVENT`` in the environment.

    The script path is validated to be within the project root to
    prevent path traversal attacks.
    """
    script_path_str = hook.path
    if not script_path_str:
        _logger.debug("Script hook has no path, skipping")
        return

    root = Path(project_root) if project_root else Path.cwd()
    script_path = (root / script_path_str).resolve()

    # Path traversal guard.
    try:
        script_path.relative_to(root.resolve())
    except ValueError:
        _logger.debug("Script path traversal rejected: %s", script_path_str)
        if verbose and logger:
            logger.verbose_detail("[i] Lifecycle script hook rejected: path outside project root")
        return

    if not script_path.exists():
        _logger.debug("Script hook file not found: %s", script_path)
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle script hook not found: {script_path_str}")
        return

    env = _build_hook_env(event)

    try:
        subprocess.run(
            [str(script_path)],
            env=env,
            timeout=_COMMAND_TIMEOUT,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        _logger.debug("Script hook timed out: %s", script_path_str)
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle script hook timed out: {script_path_str}")
    except Exception:
        _logger.debug("Script hook failed: %s", script_path_str, exc_info=True)
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle script hook failed: {script_path_str}")


# -- Helpers ----------------------------------------------------------------


def _build_hook_env(event: LifecycleEvent) -> dict[str, str]:
    """Build the environment dict for command/script hooks.

    Inherits the current process environment and adds
    ``APM_HOOK_EVENT`` with the JSON-serialised event data.
    """
    env = dict(os.environ)
    env["APM_HOOK_EVENT"] = event.to_json()
    return env
