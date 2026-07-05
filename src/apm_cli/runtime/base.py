"""Base runtime adapter interface for APM."""

import os
import subprocess
from abc import ABC, abstractmethod
from typing import Any

from ..core.tls_trust import build_child_tls_env


def _stream_subprocess_output(
    cmd: list, timeout: int | None = None, env: dict | None = None
) -> tuple[list, int]:
    """Run *cmd* as a subprocess, stream stdout in real-time, and return output.

    Args:
        cmd: Command and arguments list passed to :class:`subprocess.Popen`.
        timeout: Optional wait timeout in seconds passed to
            :meth:`subprocess.Popen.wait`.  ``None`` waits indefinitely.
        env: Optional child environment. When ``None``, the current process
            environment is used with the OS-trust child shim wired in so the
            child runtime verifies HTTPS against the OS trust store too.

    Returns:
        ``(output_lines, return_code)`` where *output_lines* is the list of
        streamed stdout lines (including newlines) and *return_code* is the
        process exit code.
    """
    if env is None:
        env = build_child_tls_env(os.environ)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Merge stderr into stdout for streaming
        text=True,
        encoding="utf-8",
        bufsize=1,  # Line buffered
        env=env,
    )

    output_lines = []

    for line in iter(process.stdout.readline, ""):
        print(line, end="", flush=True)
        output_lines.append(line)

    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        for line in iter(process.stdout.readline, ""):
            output_lines.append(line)
        process.stdout.close()
        raise
    return output_lines, return_code


class RuntimeAdapter(ABC):
    """Base adapter interface for LLM runtimes."""

    @abstractmethod
    def execute_prompt(self, prompt_content: str, **kwargs) -> str:
        """Execute a single prompt and return the response.

        Args:
            prompt_content: The prompt text to execute
            **kwargs: Additional arguments passed to the runtime

        Returns:
            str: The response text from the runtime
        """
        pass

    @abstractmethod
    def list_available_models(self) -> dict[str, Any]:
        """List all available models in the runtime.

        Returns:
            Dict[str, Any]: Dictionary of available models and their info
        """
        pass

    @abstractmethod
    def get_runtime_info(self) -> dict[str, Any]:
        """Get information about this runtime.

        Returns:
            Dict[str, Any]: Runtime information including name, version, capabilities
        """
        pass

    @staticmethod
    @abstractmethod
    def is_available() -> bool:
        """Check if this runtime is available on the system.

        Returns:
            bool: True if runtime is available, False otherwise
        """
        pass

    @staticmethod
    @abstractmethod
    def get_runtime_name() -> str:
        """Get the name of this runtime.

        Returns:
            str: Runtime name (e.g., 'llm', 'codex')
        """
        pass

    def __str__(self) -> str:
        """String representation of the runtime."""
        return f"{self.get_runtime_name()}RuntimeAdapter"
