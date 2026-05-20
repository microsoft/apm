"""Unit tests for workflow functionality."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# Additional coverage for missed lines
# ---------------------------------------------------------------------------


class TestWorkflowParserMissedLines(unittest.TestCase):
    """Cover lines 61-62 (error path) and 88-92 (non-github/prompts name extraction)."""

    def test_parse_workflow_file_raises_on_nonexistent_file(self) -> None:
        """Lines 61-62: IOError from open() is re-raised as ValueError."""

        with self.assertRaises(ValueError, msg="Failed to parse workflow file"):
            parse_workflow_file("/nonexistent/path/does_not_exist.prompt.md")

    def test_extract_name_from_prompt_md_not_in_github_prompts(self) -> None:
        """Lines 88-89: .prompt.md file outside .github/prompts uses basename."""
        with tempfile.TemporaryDirectory() as tmp:
            fpath = os.path.join(tmp, "my-workflow.prompt.md")
            with open(fpath, "w") as f:
                f.write("---\ndescription: My workflow\n---\nContent\n")
            workflow = parse_workflow_file(fpath)
        assert workflow.name == "my-workflow"

    def test_extract_name_from_non_prompt_md_extension(self) -> None:
        """Line 92: non-.prompt.md file uses splitext fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            fpath = os.path.join(tmp, "my-workflow.md")
            with open(fpath, "w") as f:
                f.write("---\ndescription: My workflow\n---\nContent\n")
            workflow = parse_workflow_file(fpath)
        assert workflow.name == "my-workflow"

    def test_extract_name_from_nested_prompt_md_not_in_github_prompts(self) -> None:
        """Lines 88-89: .prompt.md in a directory that is NOT .github/prompts."""
        with tempfile.TemporaryDirectory() as tmp:
            subdir = os.path.join(tmp, "custom", "workflows")
            os.makedirs(subdir)
            fpath = os.path.join(subdir, "deploy.prompt.md")
            with open(fpath, "w") as f:
                f.write("---\ndescription: Deploy workflow\n---\nDeploy\n")
            workflow = parse_workflow_file(fpath)
        assert workflow.name == "deploy"


class TestDiscoveryCoverage:
    """Cover discovery.py lines 19, 44-45, 63, 96."""

    def test_discover_defaults_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Line 19: base_dir=None falls back to os.getcwd()."""
        prompts = tmp_path / ".github" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "a.prompt.md").write_text("---\ndescription: A\n---\nA\n")
        monkeypatch.chdir(tmp_path)
        result = discover_workflows()
        assert len(result) == 1

    def test_discover_skips_unparseable_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lines 44-45: malformed file triggers parse error and is skipped."""
        prompts = tmp_path / ".github" / "prompts"
        prompts.mkdir(parents=True)
        bad = prompts / "bad.prompt.md"
        bad.write_text("---\ndescription: ok\n---\nContent\n")
        # Make the file unreadable after discovery finds it
        import apm_cli.workflow.discovery as disc_mod

        def _fail(path: str) -> None:
            raise Exception("forced parse error")

        monkeypatch.setattr(disc_mod, "parse_workflow_file", _fail)
        result = discover_workflows(str(tmp_path))
        assert result == []

    def test_create_template_non_vscode(self, tmp_path: Path) -> None:
        """Line 96: use_vscode_convention=False writes to output_dir directly."""
        path = create_workflow_template("my-wf", str(tmp_path), use_vscode_convention=False)
        assert os.path.basename(path) == "my-wf.prompt.md"
        assert os.path.dirname(path) == str(tmp_path)

    def test_create_template_defaults_output_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Line 63: output_dir=None falls back to os.getcwd()."""
        monkeypatch.chdir(tmp_path)
        path = create_workflow_template("x")
        assert os.path.exists(path)


class TestContentHashSymlink:
    """Cover content_hash.py line 91."""

    def test_symlink_returns_empty_hash(self, tmp_path: Path) -> None:
        from apm_cli.utils.content_hash import _EMPTY_HASH, compute_file_hash

        target = tmp_path / "real.txt"
        target.write_text("hello")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = compute_file_hash(link)
        assert result == _EMPTY_HASH


class TestPackageManagerFactory:
    """Cover factory.py lines 94-102."""

    def test_create_default(self) -> None:
        from apm_cli.factory import PackageManagerFactory

        manager = PackageManagerFactory.create_package_manager()
        assert manager is not None

    def test_create_unsupported_raises(self) -> None:
        from apm_cli.factory import PackageManagerFactory

        with pytest.raises(ValueError, match=r"Unsupported package manager type"):
            PackageManagerFactory.create_package_manager(manager_type="nonexistent")


if __name__ == "__main__":
    unittest.main()
