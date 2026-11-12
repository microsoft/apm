"""Integration test for multi-runtime architecture."""

from apm_cli.runtime.factory import RuntimeFactory


def test_runtime_factory_integration():
    """Test runtime factory integration on real system."""
    # Test getting available runtimes
    available = RuntimeFactory.get_available_runtimes()

    # Should have at least LLM available
    assert len(available) >= 1
    assert any(rt.get("name") == "llm" for rt in available)

    # Test runtime existence checks
    assert RuntimeFactory.runtime_exists("llm") is True
    assert RuntimeFactory.runtime_exists("unknown") is False

    # Test getting best available runtime
    best_runtime = RuntimeFactory.get_best_available_runtime()
    assert best_runtime is not None
    assert best_runtime.get_runtime_name() in ["llm", "codex", "copilot"]

    # Test creating specific runtime
    llm_runtime = RuntimeFactory.create_runtime("llm")
    assert llm_runtime.get_runtime_name() == "llm"
