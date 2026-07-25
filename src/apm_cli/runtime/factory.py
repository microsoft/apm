"""Runtime factory for automatic runtime detection and instantiation."""

from typing import Any

from .base import RuntimeAdapter
from .registry import adapter_descriptors


class RuntimeFactory:
    """Factory for creating runtime adapters with auto-detection."""

    # Compatibility projection for callers that patch the old test seam.
    # The canonical authority is runtime.registry.RUNTIME_DESCRIPTORS.
    _RUNTIME_ADAPTERS: list[type[RuntimeAdapter]] = [  # noqa: RUF012
        descriptor.adapter for descriptor in adapter_descriptors() if descriptor.adapter is not None
    ]

    @classmethod
    def adapter_classes(cls) -> tuple[type[RuntimeAdapter], ...]:
        """Return runtime adapters in canonical preference order."""
        return tuple(cls._RUNTIME_ADAPTERS)

    @classmethod
    def get_available_runtimes(cls) -> list[dict[str, Any]]:
        """Get list of available runtimes on the system.

        Returns:
            List[Dict[str, Any]]: List of available runtime information
        """
        available = []

        for adapter_class in cls.adapter_classes():
            if adapter_class.is_available():
                try:
                    # Create a temporary instance to get runtime info
                    temp_instance = adapter_class()
                    runtime_info = temp_instance.get_runtime_info()
                    runtime_info["available"] = True
                    available.append(runtime_info)
                except Exception as e:
                    # If instantiation fails, still mark as available but with error
                    available.append(
                        {
                            "name": adapter_class.get_runtime_name(),
                            "available": True,
                            "error": f"Available but failed to initialize: {e}",
                        }
                    )

        return available

    @classmethod
    def get_runtime_by_name(
        cls, runtime_name: str, model_name: str | None = None
    ) -> RuntimeAdapter:
        """Get a runtime adapter by name.

        Args:
            runtime_name: Name of the runtime to get ('llm', 'codex')
            model_name: Optional model name for the runtime

        Returns:
            RuntimeAdapter: Runtime adapter instance

        Raises:
            ValueError: If runtime not found or not available
        """
        for adapter_class in cls.adapter_classes():
            if adapter_class.get_runtime_name() == runtime_name:
                if not adapter_class.is_available():
                    raise ValueError(f"Runtime '{runtime_name}' is not available on this system")

                if model_name:
                    return adapter_class(model_name)
                else:
                    return adapter_class()

        raise ValueError(f"Unknown runtime: {runtime_name}")

    @classmethod
    def get_best_available_runtime(cls, model_name: str | None = None) -> RuntimeAdapter:
        """Get the best available runtime based on preference order.

        Args:
            model_name: Optional model name for the runtime

        Returns:
            RuntimeAdapter: Best available runtime adapter instance

        Raises:
            RuntimeError: If no runtimes are available
        """
        for adapter_class in cls.adapter_classes():
            if adapter_class.is_available():
                try:
                    if model_name:
                        return adapter_class(model_name)
                    else:
                        return adapter_class()
                except Exception as e:  # noqa: F841, S112
                    # Continue to next runtime if this one fails to initialize
                    continue

        raise RuntimeError(
            "No runtimes available. Install at least one of: "
            "Copilot CLI (npm i -g @github/copilot), Codex CLI (npm i -g @openai/codex@native), or LLM library (pip install llm)"
        )

    @classmethod
    def create_runtime(
        cls, runtime_name: str | None = None, model_name: str | None = None
    ) -> RuntimeAdapter:
        """Create a runtime adapter with optional runtime and model specification.

        Args:
            runtime_name: Optional runtime name. If None, uses best available.
            model_name: Optional model name for the runtime

        Returns:
            RuntimeAdapter: Runtime adapter instance
        """
        if runtime_name:
            return cls.get_runtime_by_name(runtime_name, model_name)
        else:
            return cls.get_best_available_runtime(model_name)

    @classmethod
    def runtime_exists(cls, runtime_name: str) -> bool:
        """Check if a runtime exists and is available.

        Args:
            runtime_name: Name of the runtime to check

        Returns:
            bool: True if runtime exists and is available
        """
        try:
            cls.get_runtime_by_name(runtime_name)
            return True
        except ValueError:
            return False
