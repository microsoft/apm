"""Base runtime adapter interface for APM."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional  # noqa: F401, UP035


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
