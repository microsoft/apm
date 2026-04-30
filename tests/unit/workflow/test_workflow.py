"""Unit tests for workflow functionality."""

import os
import shutil
import tempfile
import unittest

from apm_cli.workflow.discovery import create_workflow_template, discover_workflows
from apm_cli.workflow.parser import WorkflowDefinition, parse_workflow_file
from apm_cli.workflow.runner import collect_parameters, substitute_parameters  # noqa: F401


class TestWorkflowParser(unittest.TestCase):
    """Test cases for the workflow parser."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir_path = tempfile.mkdtemp()
        # Create .github/prompts directory structure
        self.prompts_dir = os.path.join(self.temp_dir_path, ".github", "prompts")
        os.makedirs(self.prompts_dir, exist_ok=True)
        self.temp_path = os.path.join(self.prompts_dir, "test-workflow.prompt.md")

        # Create a test workflow file
        with open(self.temp_path, "w") as f:
            f.write("""---
description: Test workflow
author: Test Author
mcp:
  - test-package
input:
  - param1
  - param2
---

# Test Workflow

1. Step One: ${input:param1}
2. Step Two: ${input:param2}
""")

    def tearDown(self):
        """Tear down test fixtures."""
        shutil.rmtree(self.temp_dir_path, ignore_errors=True)

    def test_parse_workflow_file(self):
        """Test parsing a workflow file."""
        workflow = parse_workflow_file(self.temp_path)

        self.assertEqual(workflow.name, "test-workflow")
        self.assertEqual(workflow.description, "Test workflow")
        self.assertEqual(workflow.author, "Test Author")
        self.assertEqual(workflow.mcp_dependencies, ["test-package"])
        self.assertEqual(workflow.input_parameters, ["param1", "param2"])
        self.assertIn("# Test Workflow", workflow.content)

    def test_workflow_validation(self):
        """Test workflow validation."""
        # Valid workflow
        workflow = WorkflowDefinition(
            "test",
            ".github/prompts/test.prompt.md",
            {"description": "Test", "input": ["param1"]},
            "content",
        )
        self.assertEqual(workflow.validate(), [])

        # Invalid workflow - missing description
        workflow = WorkflowDefinition(
            "test", ".github/prompts/test.prompt.md", {"input": ["param1"]}, "content"
        )
        errors = workflow.validate()
        self.assertEqual(len(errors), 1)
        self.assertIn("description", errors[0])

        # Input parameters are now optional, so this should not report an error
        workflow = WorkflowDefinition(
            "test", ".github/prompts/test.prompt.md", {"description": "Test"}, "content"
        )
        errors = workflow.validate()
        self.assertEqual(len(errors), 0)  # Expecting 0 errors as input is optional


class TestWorkflowRunner(unittest.TestCase):
    """Test cases for the workflow runner."""

    def test_parameter_substitution(self):
        """Test parameter substitution."""
        content = "This is a test with ${input:param1} and ${input:param2}."
        params = {"param1": "value1", "param2": "value2"}

        result = substitute_parameters(content, params)
        self.assertEqual(result, "This is a test with value1 and value2.")

    def test_parameter_substitution_with_missing_params(self):
        """Test parameter substitution with missing parameters."""
        content = "This is a test with ${input:param1} and ${input:param2}."
        params = {"param1": "value1"}

        result = substitute_parameters(content, params)
        self.assertEqual(result, "This is a test with value1 and ${input:param2}.")


class TestWorkflowDiscovery(unittest.TestCase):
    """Test cases for workflow discovery."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir_path = tempfile.mkdtemp()

        # Create .github/prompts directory structure
        self.prompts_dir = os.path.join(self.temp_dir_path, ".github", "prompts")
        os.makedirs(self.prompts_dir, exist_ok=True)

        # Create a few test workflow files
        self.workflow1_path = os.path.join(self.prompts_dir, "workflow1.prompt.md")
        with open(self.workflow1_path, "w") as f:
            f.write("""---
description: Workflow 1
input:
  - param1
---
# Workflow 1
""")

        self.workflow2_path = os.path.join(self.prompts_dir, "workflow2.prompt.md")
        with open(self.workflow2_path, "w") as f:
            f.write("""---
description: Workflow 2
input:
  - param1
---
# Workflow 2
""")

    def tearDown(self):
        """Tear down test fixtures."""
        shutil.rmtree(self.temp_dir_path, ignore_errors=True)

    def test_discover_workflows(self):
        """Test discovering workflows."""
        workflows = discover_workflows(self.temp_dir_path)

        self.assertEqual(len(workflows), 2)
        self.assertIn("workflow1", [w.name for w in workflows])
        self.assertIn("workflow2", [w.name for w in workflows])

    def test_create_workflow_template(self):
        """Test creating a workflow template."""
        template_path = create_workflow_template("test-template", self.temp_dir_path)

        self.assertTrue(os.path.exists(template_path))
        with open(template_path) as f:
            content = f.read()
            self.assertIn("description:", content)
            self.assertIn("author:", content)
            self.assertIn("mcp:", content)
            self.assertIn("input:", content)
            self.assertIn("# Test Template", content)


if __name__ == "__main__":
    unittest.main()
