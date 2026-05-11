"""Test Runtime Factory."""

from unittest.mock import Mock, patch  # noqa: F401

import pytest

from apm_cli.runtime.factory import RuntimeFactory


class TestRuntimeFactory:
    """Test Runtime Factory."""

    @staticmethod
    def _is_runtime_available(runtime_name: str) -> bool:
        """Return whether a runtime reports itself available in this environment."""
        for adapter_class in RuntimeFactory._RUNTIME_ADAPTERS:
            if adapter_class.get_runtime_name() == runtime_name:
                return adapter_class.is_available()
        return False

    def test_get_available_runtimes_real_system(self):
        """Test getting available runtimes on the current system."""
        available = RuntimeFactory.get_available_runtimes()

        assert isinstance(available, list)
        assert all(rt.get("name") in {"copilot", "codex", "llm"} for rt in available)
        assert all(rt.get("available") for rt in available)

    def test_get_runtime_by_name_llm_depends_on_system(self):
        """Test getting LLM runtime by name depending on availability."""
        if self._is_runtime_available("llm"):
            runtime = RuntimeFactory.get_runtime_by_name("llm")

            assert runtime is not None
            assert runtime.get_runtime_name() == "llm"
            return

        with pytest.raises(ValueError, match="not available"):
            RuntimeFactory.get_runtime_by_name("llm")

    def test_get_runtime_by_name_unknown(self):
        """Test getting unknown runtime by name."""
        with pytest.raises(ValueError, match="Unknown runtime: unknown"):
            RuntimeFactory.get_runtime_by_name("unknown")

    def test_get_best_available_runtime_depends_on_system(self):
        """Test getting best available runtime depending on environment."""
        available = RuntimeFactory.get_available_runtimes()
        if available:
            runtime = RuntimeFactory.get_best_available_runtime()

            assert runtime is not None
            assert runtime.get_runtime_name() in ["copilot", "codex", "llm"]
            return

        with pytest.raises(RuntimeError, match="No runtimes available"):
            RuntimeFactory.get_best_available_runtime()

    def test_create_runtime_with_name_depends_on_system(self):
        """Test creating runtime with specific name depending on environment."""
        if self._is_runtime_available("llm"):
            runtime = RuntimeFactory.create_runtime("llm")

            assert runtime is not None
            assert runtime.get_runtime_name() == "llm"
            return

        with pytest.raises(ValueError, match="not available"):
            RuntimeFactory.create_runtime("llm")

    def test_create_runtime_auto_detect_depends_on_system(self):
        """Test creating runtime with auto-detection depending on environment."""
        available = RuntimeFactory.get_available_runtimes()
        if available:
            runtime = RuntimeFactory.create_runtime()

            assert runtime is not None
            assert runtime.get_runtime_name() in ["copilot", "codex", "llm"]
            return

        with pytest.raises(RuntimeError, match="No runtimes available"):
            RuntimeFactory.create_runtime()

    def test_runtime_exists_llm_depends_on_system(self):
        """Test runtime exists check for LLM depending on environment."""
        expected = self._is_runtime_available("llm")
        assert RuntimeFactory.runtime_exists("llm") is expected

    def test_runtime_exists_false(self):
        """Test runtime exists check - false."""
        assert RuntimeFactory.runtime_exists("unknown") is False

    def test_runtime_exists_codex_depends_on_system(self):
        """Test runtime exists check for Codex - depends on system."""
        # Codex availability depends on whether it's installed
        # This test just verifies the method doesn't crash
        result = RuntimeFactory.runtime_exists("codex")
        assert isinstance(result, bool)
