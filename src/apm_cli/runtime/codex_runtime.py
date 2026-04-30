"""Codex runtime adapter for APM."""

import shutil
import subprocess
from typing import Any, Dict, Optional  # noqa: F401, UP035

from .base import RuntimeAdapter


class CodexRuntime(RuntimeAdapter):
    """APM adapter for the Codex CLI."""

    def __init__(self, model_name: str | None = None):
        """Initialize Codex runtime.

        Args:
            model_name: Model name (not used for Codex, included for compatibility)
        """
        if not self.is_available():
            raise RuntimeError(
                "Codex CLI not available. Install with: npm i -g @openai/codex@native"
            )

        self.model_name = model_name or "default"

    def execute_prompt(self, prompt_content: str, **kwargs) -> str:
        """Execute a single prompt and return the response.

        Args:
            prompt_content: The prompt text to execute
            **kwargs: Additional arguments (not used for Codex)

        Returns:
            str: The response text from Codex
        """
        import os  # noqa: F401
        import sys  # noqa: F401

        try:
            # Use codex exec to execute the prompt with real-time streaming
            # Always skip git repo check when running from APM
            process = subprocess.Popen(
                ["codex", "exec", "--skip-git-repo-check", prompt_content],
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
            return_code = process.wait(timeout=300)  # 5 minute timeout

            if return_code != 0:
                full_output = "".join(output_lines)
                # Check for common API key issues
                if "OPENAI_API_KEY" in full_output:
                    raise RuntimeError(
                        "Codex execution failed: Missing or invalid OPENAI_API_KEY. Please set your OpenAI API key."
                    )
                else:
                    raise RuntimeError(f"Codex execution failed with exit code {return_code}")

            return "".join(output_lines).strip()

        except subprocess.TimeoutExpired:
            if "process" in locals():
                process.kill()
            raise RuntimeError("Codex execution timed out after 5 minutes")  # noqa: B904
        except FileNotFoundError:
            raise RuntimeError("Codex CLI not found. Install with: npm i -g @openai/codex@native")  # noqa: B904
        except Exception as e:
            raise RuntimeError(f"Failed to execute prompt with Codex: {e}")  # noqa: B904

    def list_available_models(self) -> dict[str, Any]:
        """List all available models in the Codex runtime.

        Note: Codex manages its own models, so we return generic info.

        Returns:
            Dict[str, Any]: Dictionary of available models and their info
        """
        try:
            # Codex doesn't expose model listing via CLI, return generic info
            return {
                "codex-default": {
                    "id": "codex-default",
                    "provider": "codex",
                    "description": "Default Codex model (managed by Codex CLI)",
                }
            }
        except Exception as e:
            return {"error": f"Failed to list Codex models: {e}"}

    def get_runtime_info(self) -> dict[str, Any]:
        """Get information about this runtime.

        Returns:
            Dict[str, Any]: Runtime information including name, version, capabilities
        """
        try:
            # Try to get Codex version
            version_result = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, encoding="utf-8", timeout=10
            )

            version = version_result.stdout.strip() if version_result.returncode == 0 else "unknown"

            return {
                "name": "codex",
                "type": "codex_cli",
                "version": version,
                "capabilities": {
                    "model_execution": True,
                    "mcp_servers": "native_support",
                    "configuration": "config.toml",
                    "sandboxing": "built_in",
                },
                "description": "OpenAI Codex CLI runtime adapter",
            }
        except Exception as e:
            return {"error": f"Failed to get Codex runtime info: {e}"}

    @staticmethod
    def is_available() -> bool:
        """Check if this runtime is available on the system.

        Returns:
            bool: True if runtime is available, False otherwise
        """
        return shutil.which("codex") is not None

    @staticmethod
    def get_runtime_name() -> str:
        """Get the name of this runtime.

        Returns:
            str: Runtime name
        """
        return "codex"

    def __str__(self) -> str:
        return f"CodexRuntime(model={self.model_name})"
