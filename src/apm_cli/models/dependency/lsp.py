"""LSP dependency model."""

import re
from dataclasses import dataclass
from typing import Any

from apm_cli.utils.path_security import PathTraversalError, validate_path_segments

_NAME_REGEX = re.compile(r"^[a-zA-Z0-9@_][a-zA-Z0-9._@/:=-]{0,127}$")


def _first_defined(data: dict, primary: str, fallback: str):
    """Return the first key whose value is not None."""
    value = data.get(primary)
    return value if value is not None else data.get(fallback)


@dataclass
class LSPDependency:
    """Represents an LSP server dependency.

    Supported runtimes read LSP config from target-specific JSON files. This
    model represents manifest entries and plugin-provided `lspServers` entries.

    Required fields:
    - name: server name (key in lspServers dict)
    - command: binary to run
    - extension_to_language: mapping from file extension to LSP language ID

    Optional fields mirror the Claude Code plugin schema (lspServers).
    """

    name: str
    command: str | None = None
    args: list[str] | None = None
    extension_to_language: dict[str, str] | None = None
    transport: str | None = None  # "stdio" (default) | "socket"
    env: dict[str, str] | None = None
    initialization_options: Any | None = None
    settings: Any | None = None
    workspace_folder: str | None = None
    startup_timeout: int | None = None
    shutdown_timeout: int | None = None
    restart_on_crash: bool | None = None
    max_restarts: int | None = None

    _VALID_TRANSPORTS = frozenset({"stdio", "socket"})

    @classmethod
    def from_string(cls, s: str) -> "LSPDependency":
        """Create an LSPDependency from a plain string (server name reference)."""
        instance = cls(name=s)
        instance.validate(strict=False)
        return instance

    @classmethod
    def from_dict(cls, d: dict) -> "LSPDependency":
        """Parse an LSPDependency from a dict.

        Handles camelCase to snake_case mapping for fields from plugin.json.
        Unknown keys are silently ignored for forward compatibility.
        """
        if "name" not in d:
            raise ValueError("LSP dependency dict must contain 'name'")

        instance = cls(
            name=d["name"],
            command=d.get("command"),
            args=d.get("args"),
            extension_to_language=_first_defined(d, "extensionToLanguage", "extension_to_language"),
            transport=d.get("transport"),
            env=d.get("env"),
            initialization_options=_first_defined(
                d, "initializationOptions", "initialization_options"
            ),
            settings=d.get("settings"),
            workspace_folder=_first_defined(d, "workspaceFolder", "workspace_folder"),
            startup_timeout=_first_defined(d, "startupTimeout", "startup_timeout"),
            shutdown_timeout=_first_defined(d, "shutdownTimeout", "shutdown_timeout"),
            restart_on_crash=_first_defined(d, "restartOnCrash", "restart_on_crash"),
            max_restarts=_first_defined(d, "maxRestarts", "max_restarts"),
        )

        instance.validate(strict=True)
        return instance

    def to_dict(self) -> dict:
        """Serialize to dict with camelCase keys for .lsp.json output.

        Includes only non-None fields.
        """
        result: dict[str, Any] = {"name": self.name}
        if self.command is not None:
            result["command"] = self.command
        if self.args is not None:
            result["args"] = self.args
        if self.extension_to_language is not None:
            result["extensionToLanguage"] = self.extension_to_language
        if self.transport is not None:
            result["transport"] = self.transport
        if self.env is not None:
            result["env"] = self.env
        if self.initialization_options is not None:
            result["initializationOptions"] = self.initialization_options
        if self.settings is not None:
            result["settings"] = self.settings
        if self.workspace_folder is not None:
            result["workspaceFolder"] = self.workspace_folder
        if self.startup_timeout is not None:
            result["startupTimeout"] = self.startup_timeout
        if self.shutdown_timeout is not None:
            result["shutdownTimeout"] = self.shutdown_timeout
        if self.restart_on_crash is not None:
            result["restartOnCrash"] = self.restart_on_crash
        if self.max_restarts is not None:
            result["maxRestarts"] = self.max_restarts
        return result

    def to_lsp_json_entry(self) -> dict:
        """Serialize to the format expected in .lsp.json (no name key).

        Returns just the server config without the name, since .lsp.json
        uses the server name as the dict key.
        """
        d = self.to_dict()
        d.pop("name", None)
        return d

    def __str__(self) -> str:
        """Return a human-friendly identifier for logging and CLI output."""
        if self.transport:
            return f"{self.name} ({self.transport})"
        return self.name

    def __repr__(self) -> str:
        """Return a redacted representation to keep secrets out of debug logs."""
        parts = [f"name={self.name!r}"]
        if self.command:
            parts.append(f"command={self.command!r}")
        if self.transport:
            parts.append(f"transport={self.transport!r}")
        if self.env:
            safe_env = {k: "***" for k in self.env}
            parts.append(f"env={safe_env}")
        if self.extension_to_language:
            parts.append(f"extensionToLanguage={self.extension_to_language!r}")
        return f"LSPDependency({', '.join(parts)})"

    def validate(self, strict: bool = True) -> None:
        """Validate the dependency. Raises ValueError on invalid state.

        Args:
            strict: If True, validates required fields (command, extensionToLanguage).
                    If False, only validates name format.
        """
        if not self.name:
            raise ValueError("LSP dependency 'name' must not be empty")
        if not _NAME_REGEX.match(self.name):
            raise ValueError(
                f"Invalid LSP dependency name '{self.name}': "
                f"must start with a letter, digit, '@', or '_' and contain "
                f"only [a-zA-Z0-9._@/:=-] (max 128 chars)."
            )
        if ".." in self.name.split("/"):
            raise ValueError(
                f"Invalid LSP dependency name '{self.name}': must not contain '..' path segments."
            )

        if self.command is not None:
            if not isinstance(self.command, str):
                raise ValueError(
                    f"LSP dependency '{self.name}': 'command' must be a string, "
                    f"got {type(self.command).__name__}."
                )
            try:
                validate_path_segments(
                    self.command,
                    context="LSP command",
                    allow_current_dir=True,
                )
            except PathTraversalError:
                raise ValueError(
                    f"Invalid LSP command '{self.command}': must not contain '..' path segments."
                ) from None

        if self.workspace_folder is not None:
            if not isinstance(self.workspace_folder, str):
                raise ValueError(
                    f"LSP dependency '{self.name}': 'workspaceFolder' must be a string, "
                    f"got {type(self.workspace_folder).__name__}."
                )
            try:
                validate_path_segments(
                    self.workspace_folder,
                    context="LSP workspaceFolder",
                    allow_current_dir=True,
                )
            except PathTraversalError:
                raise ValueError(
                    f"Invalid LSP workspaceFolder '{self.workspace_folder}': "
                    "must not contain '..' path segments."
                ) from None

        if self.transport is not None and self.transport not in self._VALID_TRANSPORTS:
            raise ValueError(
                f"LSP dependency '{self.name}' has unsupported transport "
                f"'{self.transport}'. Valid values: {', '.join(sorted(self._VALID_TRANSPORTS))}"
            )

        if not strict:
            return

        if not self.command:
            raise ValueError(f"LSP dependency '{self.name}' requires 'command'")
        if not self.extension_to_language:
            raise ValueError(f"LSP dependency '{self.name}' requires 'extensionToLanguage'")
        if not isinstance(self.extension_to_language, dict):
            raise ValueError(
                f"LSP dependency '{self.name}': 'extensionToLanguage' must be a dict, "
                f"got {type(self.extension_to_language).__name__}"
            )
        if not all(
            isinstance(ext, str) and isinstance(language, str)
            for ext, language in self.extension_to_language.items()
        ):
            raise ValueError(
                f"LSP dependency '{self.name}': 'extensionToLanguage' must map "
                "string extensions to string language IDs."
            )
