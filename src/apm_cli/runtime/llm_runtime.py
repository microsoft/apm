"""LLM runtime adapter for APM."""

import os  # noqa: F401
import subprocess
import tempfile  # noqa: F401
from typing import Any, Dict, Optional  # noqa: F401, UP035

from .base import RuntimeAdapter


class LLMRuntime(RuntimeAdapter):
    """APM adapter for the llm CLI."""

    def __init__(self, model_name: str | None = None):
        """Initialize LLM runtime with specified model.

        Args:
            model_name: Name of the LLM model to use (optional)
        """
        self.model_name = model_name

        # Verify llm CLI is available
        try:
            result = subprocess.run(  # noqa: F841
                ["llm", "--version"], capture_output=True, text=True, encoding="utf-8", check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError("llm CLI not found. Please install: pip install llm")  # noqa: B904

    def execute_prompt(self, prompt_content: str, **kwargs) -> str:
        """Execute a single prompt using llm CLI and return the response.

        Args:
            prompt_content: The prompt text to execute
            **kwargs: Additional arguments (not used with CLI)

        Returns:
            str: The response text from the model
        """
        try:
            # Build command
            cmd = ["llm"]

            # Add model flag if specified
            if self.model_name:
                cmd.extend(["-m", self.model_name])

            # Add the prompt content
            cmd.append(prompt_content)

            # Execute the command with real-time streaming
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
            return_code = process.wait()

            if return_code != 0:
                full_output = "".join(output_lines)
                raise RuntimeError(f"LLM execution failed: {full_output}")

            return "".join(output_lines).strip()

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.strip() if e.stderr else str(e)
            raise RuntimeError(f"LLM execution failed: {error_msg}")  # noqa: B904
        except Exception as e:
            raise RuntimeError(f"Failed to execute prompt: {e}")  # noqa: B904

    def list_available_models(self) -> dict[str, Any]:
        """List all available models in the LLM runtime.

        Returns:
            Dict[str, Any]: Dictionary of available models and their info
        """
        try:
            result = subprocess.run(
                ["llm", "models", "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            models = {}
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    # Parse model info from llm models list output
                    model_id = line.strip()
                    models[model_id] = {"id": model_id, "provider": "llm"}
            return models
        except Exception as e:
            return {"error": f"Failed to list models: {e}"}

    @staticmethod
    def get_default_model() -> str | None:
        """Get the default model name."""
        return None  # Let llm CLI use its default

    def get_runtime_info(self) -> dict[str, Any]:
        """Get information about this runtime.

        Returns:
            Dict[str, Any]: Runtime information including name, version, capabilities
        """
        try:
            return {
                "name": "llm",
                "type": "llm_library",
                "current_model": self.model_name or "default",
                "capabilities": {
                    "model_execution": True,
                    "mcp_servers": "runtime_dependent",
                    "configuration": "llm_commands",
                    "sandboxing": "runtime_dependent",
                },
                "description": "LLM CLI runtime adapter",
            }
        except Exception as e:
            return {"error": f"Failed to get runtime info: {e}"}

    @staticmethod
    def is_available() -> bool:
        """Check if this runtime is available on the system.

        Returns:
            bool: True if runtime is available, False otherwise
        """
        try:
            subprocess.run(
                ["llm", "--version"], capture_output=True, text=True, encoding="utf-8", check=True
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    @staticmethod
    def get_runtime_name() -> str:
        """Get the name of this runtime.

        Returns:
            str: Runtime name
        """
        return "llm"

    def __str__(self) -> str:
        return f"LLMRuntime(model={self.model_name})"
