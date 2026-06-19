"""Lifecycle hook executors -- one per action type.

Each executor is fire-and-forget: it catches all exceptions internally
and logs failures in verbose mode only (using ``[i]`` ASCII symbol).

Two hook types (Copilot CLI aligned):

- ``command`` -- shell command via subprocess, event JSON on **stdin**
- ``http``    -- HTTPS POST with JSON body, env-var expansion in headers

Hook output is appended to ``~/.apm/logs/hooks.log`` so administrators
can audit what hooks produce without enabling verbose CLI output.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
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

# Credential variable denylist -- these must never be expanded into HTTP
# headers or leaked to hook subprocesses. Matches names that END with these
# suffixes (e.g. GITHUB_APM_PAT, API_KEY) but not unrelated names like PATH.
_CREDENTIAL_DENYLIST = re.compile(
    r"(?:_|^)(?:TOKEN|SECRET|PAT|KEY|PASSWORD|CREDENTIAL)(?:_|$)", re.IGNORECASE
)


# -- Hook output log -------------------------------------------------------


def _get_hooks_log_path() -> Path:
    """Return the path to the hooks output log file."""
    apm_home = os.environ.get("APM_HOME")
    base = Path(apm_home) if apm_home else Path.home() / ".apm"
    return base / "logs" / "hooks.log"


def _append_to_hook_log(
    event_name: str,
    hook_type: str,
    target: str,
    *,
    stdout: str = "",
    stderr: str = "",
    status: str = "ok",
    exit_code: int | None = None,
) -> None:
    """Append a timestamped entry to the hooks log file.

    Creates ``~/.apm/logs/`` on first write.  Errors are silently
    swallowed -- logging must never break the CLI.
    """
    try:
        log_path = _get_hooks_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [f"[{ts}] event={event_name} type={hook_type} target={target} status={status}"]
        if exit_code is not None:
            lines[0] += f" exit_code={exit_code}"
        if stdout and stdout.strip():
            lines.append(f"  stdout: {stdout.strip()}")
        if stderr and stderr.strip():
            lines.append(f"  stderr: {stderr.strip()}")
        lines.append("")  # blank line separator

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        _logger.debug("Failed to write to hooks log", exc_info=True)


def execute_hook(
    hook: HookEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> threading.Thread | None:
    """Dispatch to the correct executor based on hook type.

    Returns the daemon thread for HTTP hooks (so callers can optionally
    join it), or None for command hooks and no-ops.
    """
    if hook.hook_type == "http":
        return _execute_http(hook, event, logger=logger, verbose=verbose)
    elif hook.hook_type == "command":
        _execute_command(hook, event, logger=logger, verbose=verbose, project_root=project_root)
    return None


# -- HTTP executor ----------------------------------------------------------


def _expand_env_vars(value: str) -> str:
    """Expand ``$VAR`` and ``${VAR}`` references in *value*.

    Variables whose names match the credential denylist pattern
    (TOKEN, SECRET, PAT, KEY, PASSWORD, CREDENTIAL) are never expanded
    -- they resolve to an empty string to prevent accidental exfiltration.
    """

    def _replace(match: re.Match) -> str:
        var_name = match.group(1) or match.group(2)
        if _CREDENTIAL_DENYLIST.search(var_name):
            _logger.debug("Blocked credential variable expansion: %s", var_name)
            return ""
        return os.environ.get(var_name, "")

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _execute_http(
    hook: HookEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> threading.Thread | None:
    """Send an HTTP POST to the hook URL in a daemon thread.

    Returns the started thread so callers can optionally join it.

    Security hardening:
    - HTTPS-only (rejects ``http://``)
    - No redirect following
    - Configurable timeout (default 10s)
    - Header values support ``$ENV_VAR`` expansion (credential vars blocked)
    """
    url = hook.url
    if not url:
        _logger.debug("HTTP hook has no URL, skipping")
        return None

    parsed = urlparse(url)
    if parsed.scheme != "https":
        if verbose and logger:
            logger.verbose_detail(
                f"[i] HTTP hook rejected: URL must use https (got {parsed.scheme}://)"
            )
        _logger.debug("Rejecting non-HTTPS hook URL: %s", url)
        return None

    if not parsed.hostname:
        _logger.debug("HTTP hook URL has no hostname: %s", url)
        return None

    # Build headers with env-var expansion.
    request_headers: dict[str, str] = {"Content-Type": "application/json"}
    if hook.headers:
        for key, val in hook.headers.items():
            request_headers[key] = _expand_env_vars(val)

    payload = event.to_json()
    timeout = hook.effective_timeout
    hostname = parsed.hostname

    event_name = event.event

    def _send() -> None:
        try:
            import requests

            resp = requests.post(
                url,
                data=payload,
                headers=request_headers,
                timeout=timeout,
                allow_redirects=False,
            )
            _append_to_hook_log(
                event_name,
                "http",
                url,
                stdout=f"HTTP {resp.status_code}",
                status="ok" if resp.ok else "error",
            )
        except Exception as exc:
            _logger.debug("HTTP POST failed for %s", url, exc_info=True)
            _append_to_hook_log(event_name, "http", url, stderr=str(exc), status="error")

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()

    if verbose and logger:
        logger.verbose_detail(f"[i] {event.event} event dispatched to {hostname}")

    return thread


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
        result = subprocess.run(
            cmd,
            shell=True,
            env=env,
            input=event.to_json(),
            timeout=timeout,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        _append_to_hook_log(
            event.event,
            "command",
            cmd,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            status="ok" if result.returncode == 0 else "error",
        )
    except subprocess.TimeoutExpired:
        _logger.debug("Command hook timed out: %s", cmd)
        _append_to_hook_log(event.event, "command", cmd, status="timeout")
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle command hook timed out: {cmd}")
    except Exception as exc:
        _logger.debug("Command hook failed: %s", cmd, exc_info=True)
        _append_to_hook_log(event.event, "command", cmd, stderr=str(exc), status="error")
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle command hook failed: {cmd}")


# -- Helpers ----------------------------------------------------------------


def _build_hook_env(hook: HookEntry) -> dict[str, str]:
    """Build the environment dict for command hooks.

    Inherits the current process environment but strips any variables
    whose names match the credential denylist (TOKEN, SECRET, PAT, KEY,
    PASSWORD, CREDENTIAL) to prevent accidental exfiltration via hooks.
    """
    env = {k: v for k, v in os.environ.items() if not _CREDENTIAL_DENYLIST.search(k)}
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
