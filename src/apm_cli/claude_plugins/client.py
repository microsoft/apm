"""Bounded metadata-only calls to the qualified native Claude CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from apm_cli.utils.git_env import clear_git_platform_token_env
from apm_cli.utils.subprocess_env import external_process_env

from .admission import ClaudeNativePluginError

# Exact Claude Code releases whose plugin metadata/consent contract was qualified.
QUALIFIED_VERSIONS = ("2.1.291", "2.1.322")
_QUALIFIED_TEXT = " or ".join(QUALIFIED_VERSIONS)


class ClaudePluginCLI:
    """Never invokes an agent, plugin code, onboarding, or an enable operation."""

    def __init__(self) -> None:
        executable = shutil.which("claude")
        if executable is None:
            raise ClaudeNativePluginError(
                f"Native Claude registration needs Claude Code {_QUALIFIED_TEXT} on PATH. "
                "Install that client, then retry with the original target selection. "
                "No loose fallback was used."
            )
        self.executable = executable

    def run(self, *args: str) -> str:
        """Execute a metadata command with no interactive input or shell."""
        allowed = (
            args == ("--version",)
            or args[:3]
            in {
                ("plugin", "marketplace", "list"),
                ("plugin", "marketplace", "add"),
            }
            or args[:2]
            in {
                ("plugin", "list"),
                ("plugin", "install"),
                ("plugin", "update"),
                ("plugin", "uninstall"),
            }
        )
        if not allowed:
            raise ClaudeNativePluginError("Refusing a non-metadata Claude operation.")
        env = external_process_env()
        clear_git_platform_token_env(env, remove=True)
        try:
            result = subprocess.run(
                [self.executable, *args],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaudeNativePluginError(
                "Claude plugin metadata operation failed or timed out. Repair the native client, "
                "then retry with the original target selection. "
                "The APM intent ledger is retained for reconciliation."
            ) from exc
        if result.returncode:
            # Native output can contain user configuration or authentication data.
            raise ClaudeNativePluginError(
                f"Claude plugin metadata operation exited {result.returncode}. "
                "Inspect the native client locally, then retry with the original target selection."
            )
        return result.stdout

    def check_version(self) -> None:
        """Refuse unqualified metadata/consent contracts rather than guessing."""
        reported = self.run("--version").split()
        if not reported or reported[0] not in QUALIFIED_VERSIONS:
            raise ClaudeNativePluginError(
                f"Native Claude registration is qualified only with Claude Code {_QUALIFIED_TEXT}. "
                "Use a qualified version or exclude Claude from this dependency's targets."
            )

    def records(self, *args: str) -> list[dict[str, Any]]:
        """Validate the native list envelope before any ownership decision."""
        try:
            value = json.loads(self.run(*args))
        except ValueError as exc:
            raise ClaudeNativePluginError("Claude returned invalid plugin metadata JSON.") from exc
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ClaudeNativePluginError("Claude returned an unsupported plugin metadata shape.")
        return value
