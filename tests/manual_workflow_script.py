#!/usr/bin/env python
"""
Manual test script for workflow commands.
This script creates a sample workflow, lists workflows, and runs a workflow.

NOTE: This is a manual test script that requires API keys and should not be run
as part of the automated test suite. Run it manually when needed.
"""

import os
import subprocess  # noqa: F401
import sys
import tempfile

# Add the src directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from apm_cli.workflow.discovery import create_workflow_template, discover_workflows
from apm_cli.workflow.runner import run_workflow


def manual_test_workflow_commands():
    """Test the workflow commands."""
    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Created temporary directory: {temp_dir}")

        # 1. Create a workflow template
        workflow_name = "test-workflow"
        print(f"\n=== Creating workflow template: {workflow_name} ===")
        file_path = create_workflow_template(workflow_name, temp_dir)
        print(f"Created workflow template at: {file_path}")

        # 2. List workflows
        print("\n=== Listing workflows ===")
        workflows = discover_workflows(temp_dir)
        for wf in workflows:
            print(f"  - {wf.name}: {wf.description}")

        # 3. Run a workflow
        print(f"\n=== Running workflow: {workflow_name} ===")
        params = {"param1": "value1", "param2": "value2"}
        success, result = run_workflow(workflow_name, params, temp_dir)
        if success:
            print("Workflow executed successfully!")
            print("Result:")
            print(result)
        else:
            print(f"Error: {result}")

        # 4. Create and run a workflow with missing parameters
        workflow2_name = "test-workflow2"
        print(f"\n=== Creating workflow template: {workflow2_name} ===")
        file_path2 = create_workflow_template(workflow2_name, temp_dir)
        print(f"Created workflow template at: {file_path2}")

        print(f"\n=== Running workflow with interactive parameters: {workflow2_name} ===")
        print("This will prompt for parameters. Enter 'test1' and 'test2' when prompted.")
        try:
            success, result = run_workflow(workflow2_name, {}, temp_dir)
            if success:
                print("Workflow executed successfully!")
                print("Result:")
                print(result)
            else:
                print(f"Error: {result}")
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    manual_test_workflow_commands()
