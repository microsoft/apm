"""Integration test for LLM runtime with APM workflows."""

import os
import tempfile
from unittest.mock import Mock, patch

from apm_cli.workflow.runner import run_workflow


def test_workflow_with_invalid_runtime():
    """Test running a workflow with invalid runtime name should fail."""
    # Create a temporary workflow file
    workflow_content = """---
name: test-prompt
description: Test prompt for invalid runtime
input: [name]
---

# Test Prompt

Hello ${input:name}, this is a test prompt.
"""

    with tempfile.TemporaryDirectory() as temp_dir:
        workflow_file = os.path.join(temp_dir, "test-prompt.prompt.md")
        with open(workflow_file, "w") as f:
            f.write(workflow_content)

        # Mock the RuntimeFactory to control runtime existence check
        with patch("apm_cli.workflow.runner.RuntimeFactory") as mock_factory_class:
            mock_factory_class.runtime_exists.return_value = (
                False  # gpt-4o-mini is not a valid runtime
            )
            mock_factory_class._RUNTIME_ADAPTERS = []  # Mock empty adapters for error message

            # Run the workflow with invalid runtime parameter
            params = {
                "name": "World",
                "_runtime": "gpt-4o-mini",  # This should be invalid
            }

            success, result = run_workflow("test-prompt", params, temp_dir)

            # Verify the workflow fails with proper error message
            assert success is False
            assert "Invalid runtime 'gpt-4o-mini'" in result

            # Verify RuntimeFactory was called to check runtime existence
            mock_factory_class.runtime_exists.assert_called_once_with("gpt-4o-mini")


def test_workflow_without_runtime():
    """Test that workflows still work without runtime (copy mode)."""
    workflow_content = """---
name: test-copy
description: Test workflow for copy mode
input: [service]
---

# Deploy Service

Deploy the ${input:service} service to production.

1. Check current status
2. Run deployment
3. Verify health
"""

    with tempfile.TemporaryDirectory() as temp_dir:
        workflow_file = os.path.join(temp_dir, "test-copy.prompt.md")
        with open(workflow_file, "w") as f:
            f.write(workflow_content)

        # Preview without runtime (traditional copy mode)
        from apm_cli.workflow.runner import preview_workflow

        params = {"service": "api-gateway"}

        success, result = preview_workflow("test-copy", params, temp_dir)

        # Verify the result
        assert success is True
        assert "Deploy the api-gateway service" in result
        assert "${input:service}" not in result  # Parameter substitution worked


def test_workflow_with_valid_llm_runtime():
    """Test running a workflow with valid LLM runtime."""
    # Create a temporary workflow file
    workflow_content = """---
name: test-prompt
description: Test prompt for LLM runtime
input: [name]
---

# Test Prompt

Hello ${input:name}, this is a test prompt for the LLM runtime integration.

Please respond with a greeting.
"""

    with tempfile.TemporaryDirectory() as temp_dir:
        workflow_file = os.path.join(temp_dir, "test-prompt.prompt.md")
        with open(workflow_file, "w") as f:
            f.write(workflow_content)

        # Mock the RuntimeFactory to return a mocked LLM runtime
        with patch("apm_cli.workflow.runner.RuntimeFactory") as mock_factory_class:
            mock_runtime = Mock()
            mock_runtime.execute_prompt.return_value = "Hello World! Nice to meet you."
            mock_factory_class.create_runtime.return_value = mock_runtime
            mock_factory_class.runtime_exists.return_value = True  # 'llm' is a valid runtime

            # Run the workflow with valid runtime and model parameters
            params = {
                "name": "World",
                "_runtime": "llm",  # Valid runtime
                "_llm": "github/gpt-4o-mini",  # Model specified via --llm flag
            }

            success, result = run_workflow("test-prompt", params, temp_dir)

            # Verify the result
            assert success is True
            assert result == "Hello World! Nice to meet you."

            # Verify RuntimeFactory was called correctly
            mock_factory_class.runtime_exists.assert_called_once_with("llm")
            mock_factory_class.create_runtime.assert_called_once_with("llm", "github/gpt-4o-mini")
            mock_runtime.execute_prompt.assert_called_once()

            # Check that the prompt was properly substituted
            call_args = mock_runtime.execute_prompt.call_args[0]
            assert "Hello World" in call_args[0]  # Parameter substitution worked
            assert "${input:name}" not in call_args[0]  # No unsubstituted params
