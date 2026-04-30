"""Basic tests for workflow functionality."""

import gc
import os
import shutil
import sys
import tempfile
import time
import unittest

# Add the src directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from apm_cli.workflow.discovery import create_workflow_template
from apm_cli.workflow.parser import WorkflowDefinition, parse_workflow_file  # noqa: F401
from apm_cli.workflow.runner import substitute_parameters


def safe_rmdir(path):
    """Safely remove a directory with retry logic for Windows.

    Args:
        path (str): Path to directory to remove
    """
    try:
        shutil.rmtree(path)
    except PermissionError:
        # On Windows, give time for any lingering processes to release the lock
        time.sleep(0.5)
        gc.collect()  # Force garbage collection to release file handles
        try:
            shutil.rmtree(path)
        except PermissionError as e:
            print(f"Failed to remove directory {path}: {e}")
            # Continue without failing the test
            pass


class TestWorkflow(unittest.TestCase):
    """Basic test cases for workflow functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = self.temp_dir.name

    def tearDown(self):
        """Tear down test fixtures."""
        # Force garbage collection to release file handles
        gc.collect()

        # Give time for Windows to release locks
        if sys.platform == "win32":
            time.sleep(0.1)

        # First, try the standard cleanup
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            # If standard cleanup fails on Windows, use our safe_rmdir function
            if hasattr(self, "temp_dir_path") and os.path.exists(self.temp_dir_path):
                safe_rmdir(self.temp_dir_path)

    def test_workflow_definition(self):
        """Test the WorkflowDefinition class."""
        workflow = WorkflowDefinition(
            "test",
            ".github/prompts/test.prompt.md",
            {
                "description": "Test workflow",
                "author": "Test Author",
                "mcp": ["test-package"],
                "input": ["param1", "param2"],
            },
            "Test content",
        )

        self.assertEqual(workflow.name, "test")
        self.assertEqual(workflow.description, "Test workflow")
        self.assertEqual(workflow.author, "Test Author")
        self.assertEqual(workflow.mcp_dependencies, ["test-package"])
        self.assertEqual(workflow.input_parameters, ["param1", "param2"])
        self.assertEqual(workflow.content, "Test content")

    def test_parameter_substitution(self):
        """Test parameter substitution."""
        content = "This is ${input:param1} and ${input:param2}"
        params = {"param1": "value1", "param2": "value2"}

        result = substitute_parameters(content, params)
        self.assertEqual(result, "This is value1 and value2")

    def test_create_workflow_template(self):
        """Test creating a workflow template."""
        template_path = create_workflow_template("test-workflow", self.temp_dir_path)

        self.assertTrue(os.path.exists(template_path))
        # VSCode convention: .github/prompts/name.prompt.md
        self.assertEqual(os.path.basename(template_path), "test-workflow.prompt.md")


if __name__ == "__main__":
    unittest.main()
