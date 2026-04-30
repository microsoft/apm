#!/usr/bin/env python
"""
Non-interactive test script for workflow commands.
"""

import os
import sys
import tempfile

# Add the src directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from apm_cli.workflow.discovery import create_workflow_template, discover_workflows
from apm_cli.workflow.parser import parse_workflow_file
from apm_cli.workflow.runner import substitute_parameters


def test_workflow_features():
    """Test the core workflow features without interactive prompts."""
    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Created temporary directory: {temp_dir}")

        # 1. Create a workflow template
        workflow_name = "test-workflow"
        print(f"\n=== Creating workflow template: {workflow_name} ===")
        file_path = create_workflow_template(workflow_name, temp_dir)
        print(f"Created workflow template at: {file_path}")

        # 2. Parse the workflow file
        print("\n=== Parsing workflow file ===")
        workflow = parse_workflow_file(file_path)
        print(f"Name: {workflow.name}")
        print(f"Description: {workflow.description}")
        print(f"Author: {workflow.author}")
        print(f"MCP Dependencies: {workflow.mcp_dependencies}")
        print(f"Input Parameters: {workflow.input_parameters}")

        # 3. Validate the workflow
        print("\n=== Validating workflow ===")
        errors = workflow.validate()
        if errors:
            print(f"Validation errors: {errors}")
        else:
            print("Workflow is valid")

        # 4. Test parameter substitution
        print("\n=== Testing parameter substitution ===")
        params = {"param1": "value1", "param2": "value2"}
        result = substitute_parameters(workflow.content, params)
        print("Result after parameter substitution:")
        print(result)

        # 5. List workflows
        print("\n=== Listing workflows ===")
        workflows = discover_workflows(temp_dir)
        for wf in workflows:
            print(f"  - {wf.name}: {wf.description}")


if __name__ == "__main__":
    test_workflow_features()
