"""Unit tests for ScriptExecutionFormatter.

Covers the Rich console.capture() fallback branches (G1 from complexity
review) as well as basic happy-path formatting.
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock


class TestFormatContentPreviewRichFallback(unittest.TestCase):
    """Verify the except-Exception fallback in format_content_preview (~line 176)."""

    def test_format_content_preview_rich_fallback(self):
        """When Rich Panel rendering raises, plain-text fallback is returned."""
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        formatter = ScriptExecutionFormatter(use_color=True)

        # Only proceed with mock path if Rich was actually loaded
        if formatter.console is None:
            self.skipTest("Rich is not available; fallback branch is not reachable")

        # Patch Panel so it raises on construction. This leaves _styled()
        # (which uses Text, not Panel) unaffected and triggers the
        # except-Exception fallback inside format_content_preview.
        with patch("apm_cli.output.script_formatters.Panel", side_effect=Exception("render boom")):
            lines = formatter.format_content_preview("Hello world content", max_preview=200)

        # The fallback path produces separator lines around the content
        self.assertTrue(len(lines) > 0, "Expected non-empty output from fallback path")
        text = "\n".join(lines)
        self.assertIn("Hello world content", text)
        self.assertIn("-" * 50, text)


class TestFormatAutoDiscoveryMessageRichFallback(unittest.TestCase):
    """Verify the except-Exception fallback in format_auto_discovery_message (~line 332)."""

    def test_format_auto_discovery_message_rich_fallback(self):
        """When Rich Text rendering raises, plain-text fallback is returned."""
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        formatter = ScriptExecutionFormatter(use_color=True)

        if formatter.console is None:
            self.skipTest("Rich is not available; fallback branch is not reachable")

        # Patch Text so it raises on construction inside format_auto_discovery_message.
        # This triggers the except-Exception fallback at ~line 332.
        with patch("apm_cli.output.script_formatters.Text", side_effect=Exception("render boom")):
            result = formatter.format_auto_discovery_message(
                script_name="my-script",
                prompt_file=Path("prompts/hello.md"),
                runtime="copilot",
            )

        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0, "Expected non-empty fallback string")
        self.assertIn("Auto-discovered", result)
        self.assertIn("prompts/hello.md", result)
        self.assertIn("copilot", result)


class TestFormatContentPreviewSuccess(unittest.TestCase):
    """Happy-path test for format_content_preview."""

    def test_format_content_preview_success(self):
        """Formatting a content preview returns lines containing the content."""
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        # Use colour=False so we exercise the non-Rich branch deterministically
        formatter = ScriptExecutionFormatter(use_color=False)

        lines = formatter.format_content_preview("Sample prompt content", max_preview=200)

        self.assertTrue(len(lines) > 0)
        text = "\n".join(lines)
        self.assertIn("Prompt preview:", text)
        self.assertIn("Sample prompt content", text)

    def test_format_content_preview_truncates_long_content(self):
        """Content longer than max_preview is truncated with an ellipsis."""
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        formatter = ScriptExecutionFormatter(use_color=False)
        long_content = "x" * 300

        lines = formatter.format_content_preview(long_content, max_preview=50)

        text = "\n".join(lines)
        self.assertIn("...", text)


class TestFormatAutoDiscoveryMessageSuccess(unittest.TestCase):
    """Happy-path test for format_auto_discovery_message."""

    def test_format_auto_discovery_message_success(self):
        """Formatting an auto-discovery message returns expected elements."""
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        formatter = ScriptExecutionFormatter(use_color=False)

        result = formatter.format_auto_discovery_message(
            script_name="deploy",
            prompt_file=Path("scripts/deploy.prompt.md"),
            runtime="codex",
        )

        self.assertIsInstance(result, str)
        self.assertIn("Auto-discovered", result)
        self.assertIn("scripts/deploy.prompt.md", result)
        self.assertIn("codex", result)


class TestFormatExecutionResultSuccess(unittest.TestCase):
    """Happy-path tests for execution result formatting methods."""

    def test_format_execution_success(self):
        """format_execution_success returns a line with the runtime name."""
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        formatter = ScriptExecutionFormatter(use_color=False)
        lines = formatter.format_execution_success("copilot", execution_time=1.23)

        self.assertEqual(len(lines), 1)
        self.assertIn("Copilot", lines[0])
        self.assertIn("1.23s", lines[0])

    def test_format_execution_error(self):
        """format_execution_error returns header and error detail."""
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        formatter = ScriptExecutionFormatter(use_color=False)
        lines = formatter.format_execution_error("codex", error_code=1, error_msg="bad input")

        text = "\n".join(lines)
        self.assertIn("Codex", text)
        self.assertIn("exit code: 1", text)
        self.assertIn("bad input", text)

    def test_format_script_header(self):
        """format_script_header includes script name and parameters."""
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        formatter = ScriptExecutionFormatter(use_color=False)
        lines = formatter.format_script_header("build", {"env": "prod", "verbose": "true"})

        text = "\n".join(lines)
        self.assertIn("build", text)
        self.assertIn("env", text)
        self.assertIn("prod", text)


if __name__ == "__main__":
    unittest.main()
