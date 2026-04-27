"""GitHub Copilot CLI runtime adapter for APM."""

import json
import os  # noqa: F401
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional  # noqa: F401, UP035

from .base import RuntimeAdapter


class CopilotRuntime(RuntimeAdapter):
    """APM adapter for the GitHub Copilot CLI."""

    def __init__(self, model_name: str | None = None):
        """Initialize Copilot runtime.

        Args:
            model_name: Model name (not used for Copilot CLI, included for compatibility)
        """
        if not self.is_available():
            raise RuntimeError(
                "GitHub Copilot CLI not available. Install with: npm install -g @github/copilot"
            )

        self.model_name = model_name or "default"

    def execute_prompt(self, prompt_content: str, **kwargs) -> str:
        """Execute a single prompt and return the response.

        Args:
            prompt_content: The prompt text to execute
            **kwargs: Additional arguments that may include:
                - full_auto: Enable automatic tool execution (default: False)
                - log_level: Copilot CLI log level (default: "default")
                - add_dirs: Additional directories to allow file access

        Returns:
            str: The response text from Copilot CLI
        """
        try:
            # Build Copilot CLI command
            cmd = ["copilot", "-p", prompt_content]

            # Add optional arguments from kwargs
            if kwargs.get("full_auto", False):
                cmd.append("--allow-all-tools")

            log_level = kwargs.get("log_level", "default")
            if log_level != "default":
                cmd.extend(["--log-level", log_level])

            # Add additional directories if specified
            add_dirs = kwargs.get("add_dirs", [])
            for directory in add_dirs:
                cmd.extend(["--add-dir", str(directory)])

            # Execute Copilot CLI with real-time streaming
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout for streaming
                text=True,
                encoding="utf-8",
                bufsize=1,  # Line buffered
            )

            output_lines = []

            # Stream output in real-time
            for line in iter(process.stdout.readline, ""):
                # Print to terminal in real-time
                print(line, end="", flush=True)
                output_lines.append(line)

            # Wait for process to complete
            return_code = process.wait(timeout=600)  # 10 minute timeout for complex tasks

            if return_code != 0:
                full_output = "".join(output_lines)
                # Check for common issues
                if "not logged in" in full_output.lower():
                    raise RuntimeError(
                        "Copilot CLI execution failed: Not logged in. Run 'copilot' and use '/login' command."
                    )
                else:
                    raise RuntimeError(f"Copilot CLI execution failed with exit code {return_code}")

            return "".join(output_lines).strip()

        except subprocess.TimeoutExpired:
            if "process" in locals():
                process.kill()
            raise RuntimeError("Copilot CLI execution timed out after 10 minutes")  # noqa: B904
        except FileNotFoundError:
            raise RuntimeError(  # noqa: B904
                "Copilot CLI not found. Install with: npm install -g @github/copilot"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to execute prompt with Copilot CLI: {e}")  # noqa: B904

    def list_available_models(self) -> dict[str, Any]:
        """List all available models in the Copilot CLI runtime.

        Note: Copilot CLI manages its own models, so we return generic info.

        Returns:
            Dict[str, Any]: Dictionary of available models and their info
        """
        try:
            # Copilot CLI doesn't expose model listing via CLI, return generic info
            return {
                "copilot-default": {
                    "id": "copilot-default",
                    "provider": "github-copilot",
                    "description": "Default GitHub Copilot model (managed by Copilot CLI)",
                }
            }
        except Exception as e:
            return {"error": f"Failed to list Copilot CLI models: {e}"}

    def get_runtime_info(self) -> dict[str, Any]:
        """Get information about this runtime.

        Returns:
            Dict[str, Any]: Runtime information including name, version, capabilities
        """
        try:
            # Try to get Copilot CLI version
            version_result = subprocess.run(
                ["copilot", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )

            version = version_result.stdout.strip() if version_result.returncode == 0 else "unknown"

            # Check for MCP configuration
            mcp_config_path = Path.home() / ".copilot" / "mcp-config.json"
            mcp_configured = mcp_config_path.exists()

            return {
                "name": "copilot",
                "type": "copilot_cli",
                "version": version,
                "capabilities": {
                    "model_execution": True,
                    "mcp_servers": "native_support" if mcp_configured else "manual_setup_required",
                    "configuration": "~/.copilot/mcp-config.json",
                    "interactive_mode": True,
                    "background_processes": True,
                    "file_operations": True,
                    "directory_access": "configurable",
                },
                "description": "GitHub Copilot CLI runtime adapter",
                "mcp_config_path": str(mcp_config_path),
                "mcp_configured": mcp_configured,
            }
        except Exception as e:
            return {"error": f"Failed to get Copilot CLI runtime info: {e}"}

    @staticmethod
    def is_available() -> bool:
        """Check if this runtime is available on the system.

        Returns:
            bool: True if runtime is available, False otherwise
        """
        return shutil.which("copilot") is not None

    @staticmethod
    def get_runtime_name() -> str:
        """Get the name of this runtime.

        Returns:
            str: Runtime name
        """
        return "copilot"

    def get_mcp_config_path(self) -> Path:
        """Get the path to the MCP configuration file.

        Returns:
            Path: Path to the MCP configuration file
        """
        return Path.home() / ".copilot" / "mcp-config.json"

    def is_mcp_configured(self) -> bool:
        """Check if MCP servers are configured.

        Returns:
            bool: True if MCP configuration exists, False otherwise
        """
        return self.get_mcp_config_path().exists()

    def get_mcp_servers(self) -> dict[str, Any]:
        """Get configured MCP servers.

        Returns:
            Dict[str, Any]: Dictionary of configured MCP servers
        """
        mcp_config_path = self.get_mcp_config_path()
        if not mcp_config_path.exists():
            return {}

        try:
            with open(mcp_config_path) as f:
                config = json.load(f)
                return config.get("servers", {})
        except Exception as e:
            return {"error": f"Failed to read MCP configuration: {e}"}

    def __str__(self) -> str:
        return f"CopilotRuntime(model={self.model_name})"
