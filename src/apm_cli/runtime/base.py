"""Base runtime adapter interface for APM."""

import os
import queue
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from contextlib import suppress
from typing import Any


def _terminate_and_reap(process: subprocess.Popen) -> None:
    """Terminate a runtime process group and always reap the parent."""
    pid = getattr(process, "pid", None)
    try:
        if os.name != "nt" and isinstance(pid, int) and pid > 0:
            os.killpg(pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=1)
    except (OSError, TypeError, subprocess.TimeoutExpired):
        try:
            if os.name != "nt" and isinstance(pid, int) and pid > 0:
                os.killpg(pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, TypeError):
            pass
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
    finally:
        if process.stdout is not None:
            process.stdout.close()


def _stream_subprocess_output(
    cmd: list,
    timeout: float | None = None,
) -> tuple[list, int]:
    """Run *cmd* as a subprocess, stream stdout in real-time, and return output.

    Args:
        cmd: Command and arguments list passed to :class:`subprocess.Popen`.
        timeout: Optional wait timeout in seconds passed to
            :meth:`subprocess.Popen.wait`.  ``None`` waits indefinitely.

    Returns:
        ``(output_lines, return_code)`` where *output_lines* is the list of
        streamed stdout lines (including newlines) and *return_code* is the
        process exit code.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Merge stderr into stdout for streaming
        text=True,
        encoding="utf-8",
        bufsize=1,  # Line buffered
        start_new_session=os.name != "nt",
    )

    output_lines: list[str] = []
    stream_queue: queue.Queue[str | None] = queue.Queue(maxsize=1024)
    cancelled = threading.Event()

    def _queue_item(item: str | None) -> None:
        while not cancelled.is_set():
            try:
                stream_queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def _read_output() -> None:
        try:
            if process.stdout is not None:
                for line in iter(process.stdout.readline, ""):
                    _queue_item(line)
        finally:
            _queue_item(None)

    reader = threading.Thread(target=_read_output, daemon=True)
    reader.start()

    def _expire() -> None:
        cancelled.set()
        _terminate_and_reap(process)
        reader.join(timeout=1)

    deadline = time.monotonic() + timeout if timeout is not None else None
    stream_closed = False
    while not stream_closed:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            _expire()
            raise subprocess.TimeoutExpired(cmd, timeout, output="".join(output_lines))
        try:
            item = stream_queue.get(timeout=remaining)
        except queue.Empty:
            _expire()
            raise subprocess.TimeoutExpired(cmd, timeout, output="".join(output_lines)) from None
        if item is None:
            stream_closed = True
            continue
        print(item, end="", flush=True)
        output_lines.append(item)

    reader.join(timeout=1)
    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    try:
        return_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        _expire()
        raise subprocess.TimeoutExpired(cmd, timeout, output="".join(output_lines)) from None
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
