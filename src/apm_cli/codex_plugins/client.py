"""Bounded public Codex app-server operations; no sessions or plugin execution."""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.core.scope import InstallScope

QUALIFIED_CODEX_VERSION = "0.163.0"
_TIMEOUT = 60
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class CodexNativeError(ValueError):
    """Native registration failed or its version/scope is not qualified."""


class CodexRpc:
    """One bounded stdio connection to the public app-server protocol."""

    def __init__(self, executable: Path, cwd: Path) -> None:
        self._process = subprocess.Popen(
            [str(executable), "app-server", "--stdio"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._responses: queue.Queue[dict[str, Any] | Exception] = queue.Queue(maxsize=32)
        self._reader_error: Exception | None = None
        self._request_id = 0
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self._process.stdout
        if stream is None:
            self._responses.put(CodexNativeError("Codex app-server has no response stream."))
            return
        try:
            while line := stream.readline(_MAX_RESPONSE_BYTES + 1):
                if len(line) > _MAX_RESPONSE_BYTES:
                    raise CodexNativeError("Codex app-server response exceeds the size limit.")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise CodexNativeError("Codex app-server returned an invalid response.")
                if "id" in message:
                    self._responses.put(message, timeout=_TIMEOUT)
        except (OSError, ValueError, queue.Full) as error:
            self._reader_error = error
        error = self._reader_error or CodexNativeError(
            "Codex app-server closed before completing the request."
        )
        try:
            self._responses.put_nowait(error)
        except queue.Full:
            self._reader_error = error

    def _send(self, message: dict[str, Any]) -> None:
        stream = self._process.stdin
        if stream is None:
            raise CodexNativeError("Codex app-server has no request stream.")
        try:
            stream.write(json.dumps(message).encode("utf-8") + b"\n")
            stream.flush()
        except OSError as error:
            raise CodexNativeError("Codex app-server stopped accepting requests.") from error

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one request, refusing errors without echoing native configuration."""
        self._request_id += 1
        self._send({"id": self._request_id, "method": method, "params": params})
        try:
            response = self._responses.get(timeout=_TIMEOUT)
        except queue.Empty as error:
            raise CodexNativeError(
                f"Codex {method} timed out; retry the native command."
            ) from error
        if self._reader_error is not None:
            response = self._reader_error
        if isinstance(response, Exception):
            raise CodexNativeError(
                f"Codex {method} failed to return a valid response."
            ) from response
        if response.get("id") != self._request_id:
            raise CodexNativeError(f"Codex {method} returned an unexpected response identifier.")
        if "error" in response:
            raise CodexNativeError(
                f"Codex {method} refused the operation. Inspect the native configuration "
                "and retry; APM does not override native policy or concurrent edits."
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise CodexNativeError(f"Codex {method} returned an invalid result.")
        return result

    def __enter__(self) -> CodexRpc:
        try:
            self.call(
                "initialize",
                {"clientInfo": {"name": "apm-native-plugins", "version": "1.0.0"}},
            )
            self._send({"method": "initialized", "params": {}})
        except (CodexNativeError, OSError):
            self.close()
            raise
        return self

    def close(self) -> None:
        """Stop only this connection's process; never leave a helper running."""
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        finally:
            self._stop_process()

    def _stop_process(self) -> None:
        """Reap the helper even if closing its broken request pipe fails."""
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._reader.join(timeout=1)
        if self._process.stdout is not None:
            self._process.stdout.close()

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class NativeUserConfig:
    """The public, writable user layer and its native compare-and-swap version."""

    path: Path
    version: str
    values: dict[str, Any]


@dataclass(frozen=True)
class CodexNativeClient:
    """Qualified user-scope native operations, without private registry writes."""

    executable: Path
    cwd: Path

    @staticmethod
    def require_scope(scope: InstallScope) -> None:
        """Refuse unsupported scopes before inspecting any user-native state."""
        if scope is not InstallScope.USER:
            raise CodexNativeError(
                "Codex compatibility plugins are qualified only at user scope. "
                "Use 'apm install -g --target codex'; no project plugin was registered."
            )

    @classmethod
    def for_scope(cls, scope: InstallScope, cwd: Path) -> CodexNativeClient:
        """Resolve the exactly qualified runtime without changing native settings."""
        cls.require_scope(scope)
        executable = shutil.which("codex")
        if executable is None:
            raise CodexNativeError(
                f"Codex native registration requires Codex CLI {QUALIFIED_CODEX_VERSION} on PATH. "
                "Install that qualified client and retry."
            )
        try:
            result = subprocess.run(
                [executable, "--version"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CodexNativeError(
                f"Cannot start Codex CLI {QUALIFIED_CODEX_VERSION}. Check 'codex --version' and retry."
            ) from error
        if result.returncode or result.stdout.strip() != f"codex-cli {QUALIFIED_CODEX_VERSION}":
            raise CodexNativeError(
                f"Codex native registration is qualified only for Codex CLI {QUALIFIED_CODEX_VERSION}. "
                "Check 'codex --version'; other versions are not silently admitted."
            )
        return cls(Path(executable), cwd)

    def connect(self) -> CodexRpc:
        """Create a short-lived connection without starting an agent thread."""
        return CodexRpc(self.executable, self.cwd)

    @staticmethod
    def read_config(rpc: CodexRpc) -> NativeUserConfig:
        """Read user ownership and CAS from the public API, not merged project state."""
        result = rpc.call("config/read", {"includeLayers": True})
        for layer in result.get("layers", []):
            name = layer.get("name", {})
            if name.get("type") == "user" and name.get("profile") is None:
                version, path, values = layer.get("version"), name.get("file"), layer.get("config")
                if isinstance(version, str) and isinstance(path, str) and isinstance(values, dict):
                    return NativeUserConfig(Path(path), version, values)
                break
        raise CodexNativeError(
            "Codex did not expose a writable user configuration version. "
            "Check the native configuration before retrying."
        )

    @staticmethod
    def write_config(rpc: CodexRpc, edits: list[dict[str, Any]], version: str) -> None:
        """Apply precisely owned fields using native validation and optimistic locking."""
        result = rpc.call(
            "config/batchWrite",
            {
                "edits": [{**edit, "mergeStrategy": "replace"} for edit in edits],
                "expectedVersion": version,
            },
        )
        if result.get("status") != "ok":
            raise CodexNativeError(
                "Codex configuration was overridden by another layer. "
                "Resolve that native policy override before retrying."
            )

    @staticmethod
    def upgrade(rpc: CodexRpc, marketplace: str) -> list[Path]:
        """Refresh exactly one owned marketplace; never request a global refetch."""
        result = rpc.call("marketplace/upgrade", {"marketplaceName": marketplace})
        if (
            result.get("errors")
            or result.get("selectedMarketplaces") != [marketplace]
            or not isinstance(result.get("upgradedRoots"), list)
            or not all(isinstance(path, str) for path in result["upgradedRoots"])
        ):
            raise CodexNativeError(
                "Codex refused the owned marketplace refresh. "
                "Inspect native marketplace policy and retry; no policy bypass was attempted."
            )
        return [Path(path) for path in result["upgradedRoots"]]

    @staticmethod
    def read_plugin(rpc: CodexRpc, marketplace_path: Path, name: str) -> dict[str, Any]:
        """Read the native installed-plugin view without enabling or running it."""
        result = rpc.call(
            "plugin/read",
            {"marketplacePath": str(marketplace_path), "pluginName": name},
        )
        plugin = result.get("plugin")
        if not isinstance(plugin, dict):
            raise CodexNativeError("Codex did not return installed plugin details; retry install.")
        return plugin

    @staticmethod
    def remove_plugin(rpc: CodexRpc, plugin_id: str) -> None:
        """Remove a preflighted, minimally owned entry through the native lifecycle."""
        rpc.call("plugin/uninstall", {"pluginId": plugin_id})

    @staticmethod
    def remove_marketplace(rpc: CodexRpc, marketplace: str) -> None:
        """Remove exactly the preflighted APM-owned marketplace."""
        rpc.call("marketplace/remove", {"marketplaceName": marketplace})
