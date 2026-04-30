"""Integration test for multi-runtime architecture."""

import os
import tempfile
from unittest.mock import Mock, patch

from apm_cli.runtime.factory import RuntimeFactory
from apm_cli.workflow.runner import run_workflow


def test_runtime_type_selection():
    """Test explicit runtime type selection."""
    # Create a temporary workflow file
    workflow_content = """---
name: test-runtime-type
description: Test runtime type selection
input: [message]
---

# Runtime Type Test

${input:message}
"""

    with tempfile.TemporaryDirectory() as temp_dir:
        workflow_file = os.path.join(temp_dir, "test-runtime-type.prompt.md")
        with open(workflow_file, "w") as f:
            f.write(workflow_content)

        # Mock the RuntimeFactory for testing runtime type selection
        with patch("apm_cli.workflow.runner.RuntimeFactory") as mock_factory_class:
            mock_runtime = Mock()
            mock_runtime.execute_prompt.return_value = "Response from runtime"
            mock_factory_class.create_runtime.return_value = mock_runtime
            mock_factory_class.runtime_exists.return_value = True  # 'llm' is a valid runtime

            # Test with runtime type
            params = {"message": "Test message", "_runtime": "llm"}

            success, result = run_workflow("test-runtime-type", params, temp_dir)

            # Verify the result
            assert success is True
            assert result == "Response from runtime"

            # Verify factory calls for runtime type (runtime name and model name)
            mock_factory_class.runtime_exists.assert_called_once_with("llm")
            mock_factory_class.create_runtime.assert_called_once_with("llm", None)


def test_invalid_runtime_type():
    """Test error handling for invalid runtime type."""
    # Create a temporary workflow file
    workflow_content = """---
name: test-invalid-runtime
description: Test invalid runtime type
input: [message]
---

# Invalid Runtime Test

${input:message}
"""

    with tempfile.TemporaryDirectory() as temp_dir:
        workflow_file = os.path.join(temp_dir, "test-invalid-runtime.prompt.md")
        with open(workflow_file, "w") as f:
            f.write(workflow_content)

        # Mock the RuntimeFactory to raise ValueError for unknown runtime
        with patch("apm_cli.workflow.runner.RuntimeFactory") as mock_factory_class:
            mock_factory_class.create_runtime.side_effect = ValueError("Unknown runtime: unknown")

            # Test with invalid runtime type
            params = {"message": "Test message", "_runtime": "unknown"}

            success, result = run_workflow("test-invalid-runtime", params, temp_dir)

            # Verify the error result
            assert success is False
            assert "Runtime execution failed" in result
            assert "Unknown runtime: unknown" in result


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
