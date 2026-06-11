"""Unit tests for apm_cli.compilation.user_root_context.

Covers compile_user_root_contexts() for user-scope root context file generation:

* apm_modules directory discovery
* Target filtering (for_scope, compile_family)
* Global instructions discovery and filtering
* File generation and placement (deploy roots)
* Overwrite protection (generated marker detection)
* Dry-run behavior
* Content generation with Build ID
* Error handling (OSError on read/write)
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Sentinel value to distinguish "not provided" from "explicitly None"
_UNSET = object()


def _make_target(
    name: str,
    compile_family: str,
    for_scope_returns=_UNSET,
    deploy_root=None,
):
    """Create a mock TargetProfile."""
    target = MagicMock()
    target.name = name
    target.compile_family = compile_family
    target.resolved_deploy_root = deploy_root
    target.root_dir = f".{name}"

    scoped = MagicMock()
    scoped.name = name
    scoped.compile_family = compile_family
    scoped.resolved_deploy_root = deploy_root
    scoped.root_dir = f".{name}"

    if for_scope_returns is _UNSET:
        target.for_scope = MagicMock(return_value=scoped)
    else:
        target.for_scope = MagicMock(return_value=for_scope_returns)

    return target


def _make_instruction(name="global", apply_to=None, content="Use type hints"):
    """Create a mock Instruction."""
    instr = MagicMock()
    instr.name = name
    instr.apply_to = apply_to  # None = global instruction
    instr.content = content
    instr.file_path = Path(f"/tmp/{name}.instructions.md")
    return instr


# ---------------------------------------------------------------------------
# test_no_apm_modules_returns_empty
# ---------------------------------------------------------------------------


class TestNoApmModulesReturnsEmpty:
    """When apm_modules dir does not exist, return []."""

    def test_apm_modules_missing(self, tmp_path):
        """apm_modules directory does not exist -> return []."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        # apm_modules does NOT exist
        targets = [_make_target("claude", "claude")]

        result = compile_user_root_contexts(targets, source_root)

        assert result == []

    def test_apm_modules_is_file_not_dir(self, tmp_path):
        """apm_modules exists as a file (not dir) -> return []."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.touch()  # Create as file, not directory

        targets = [_make_target("claude", "claude")]
        result = compile_user_root_contexts(targets, source_root)

        assert result == []


# ---------------------------------------------------------------------------
# test_skip_when_for_scope_returns_none
# ---------------------------------------------------------------------------


class TestSkipWhenForScopeReturnsNone:
    """Target that does not support user scope (for_scope returns None) is skipped."""

    def test_for_scope_returns_none(self, tmp_path):
        """for_scope(user_scope=True) returns None -> skip target."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        target = _make_target("agents", "agents", for_scope_returns=None)
        result = compile_user_root_contexts([target], source_root)

        assert result == []
        target.for_scope.assert_called_once_with(user_scope=True)


# ---------------------------------------------------------------------------
# test_skip_when_no_root_filename_family
# ---------------------------------------------------------------------------


class TestSkipWhenNoRootFilenameFamily:
    """Target with unknown compile_family (not in _ROOT_FILENAME) is skipped."""

    def test_unknown_compile_family(self, tmp_path):
        """compile_family not in _ROOT_FILENAME map -> skip."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        # Use mock that returns family="unknown"
        target = _make_target("mystery", "unknown")
        result = compile_user_root_contexts([target], source_root)

        assert result == []


# ---------------------------------------------------------------------------
# test_skipped_no_instructions
# ---------------------------------------------------------------------------


class TestSkippedNoInstructions:
    """When no global instructions found, return skipped-no-instructions."""

    def test_no_global_instructions(self, tmp_path):
        """No global instructions in apm_modules -> skipped-no-instructions."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        target = _make_target("claude", "claude")

        primitives = MagicMock()
        primitives.instructions = []  # No instructions at all

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result = compile_user_root_contexts([target], source_root)

        assert len(result) == 1
        assert result[0]["target"] == "claude"
        assert result[0]["path"] is None
        assert result[0]["status"] == "skipped-no-instructions"

    def test_only_scoped_instructions_no_global(self, tmp_path):
        """All instructions are scoped (have apply_to) -> no global -> skipped."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        target = _make_target("claude", "claude")

        # All have apply_to (not global)
        instr = _make_instruction("scoped", apply_to="**/*.py", content="Use hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result = compile_user_root_contexts([target], source_root)

        assert len(result) == 1
        assert result[0]["status"] == "skipped-no-instructions"


# ---------------------------------------------------------------------------
# test_skipped_hand_authored
# ---------------------------------------------------------------------------


class TestSkippedHandAuthored:
    """Existing file without generated marker is skipped."""

    def test_hand_authored_no_marker(self, tmp_path):
        """Existing file without APM marker -> skipped-hand-authored."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        # Claude deploy root
        deploy_root = tmp_path / ".claude"
        deploy_root.mkdir(parents=True, exist_ok=True)
        output_path = deploy_root / "CLAUDE.md"

        # Write hand-authored file (no marker)
        output_path.write_text("# My custom Claude context\nSome notes here.\n")

        target = _make_target("claude", "claude", deploy_root=deploy_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result = compile_user_root_contexts([target], source_root)

        assert len(result) == 1
        assert result[0]["target"] == "claude"
        assert result[0]["path"] == output_path
        assert result[0]["status"] == "skipped-hand-authored"

        # File should NOT have been modified
        existing = output_path.read_text()
        assert existing == "# My custom Claude context\nSome notes here.\n"


# ---------------------------------------------------------------------------
# test_written_new_file
# ---------------------------------------------------------------------------


class TestWrittenNewFile:
    """New file (does not exist) is created with status written."""

    def test_write_new_file(self, tmp_path):
        """No existing file -> create and write with status written."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        deploy_root = tmp_path / ".claude"
        # Do NOT create deploy_root; it should be created by the function
        output_path = deploy_root / "CLAUDE.md"

        target = _make_target("claude", "claude", deploy_root=deploy_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result = compile_user_root_contexts([target], source_root)

        assert len(result) == 1
        assert result[0]["target"] == "claude"
        assert result[0]["path"] == output_path
        assert result[0]["status"] == "written"

        # File should exist
        assert output_path.exists()
        content = output_path.read_text()
        assert "Generated by APM CLI" in content
        assert "Use type hints" in content


# ---------------------------------------------------------------------------
# test_unchanged
# ---------------------------------------------------------------------------


class TestUnchanged:
    """Existing file that matches generated content has status unchanged."""

    def test_file_unchanged(self, tmp_path):
        """Existing file matches generated content -> unchanged."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        deploy_root = tmp_path / ".claude"
        deploy_root.mkdir(parents=True, exist_ok=True)

        target = _make_target("claude", "claude", deploy_root=deploy_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        # Generate content and write it once
        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result1 = compile_user_root_contexts([target], source_root)
            assert result1[0]["status"] == "written"

        # Now generate again with the same content
        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result2 = compile_user_root_contexts([target], source_root)

        assert len(result2) == 1
        assert result2[0]["target"] == "claude"
        assert result2[0]["status"] == "unchanged"


# ---------------------------------------------------------------------------
# test_dry_run_would_write
# ---------------------------------------------------------------------------


class TestDryRunWouldWrite:
    """dry_run=True produces status would-write without writing file."""

    def test_dry_run_no_file_written(self, tmp_path):
        """dry_run=True -> would-write, no file created."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        deploy_root = tmp_path / ".claude"
        # Do NOT create deploy_root
        output_path = deploy_root / "CLAUDE.md"

        target = _make_target("claude", "claude", deploy_root=deploy_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result = compile_user_root_contexts([target], source_root, dry_run=True)

        assert len(result) == 1
        assert result[0]["target"] == "claude"
        assert result[0]["path"] == output_path
        assert result[0]["status"] == "would-write"

        # File should NOT exist (no write happened)
        assert not output_path.exists()


# ---------------------------------------------------------------------------
# test_marker_in_written_file
# ---------------------------------------------------------------------------


class TestMarkerInWrittenFile:
    """Written file contains the _COPILOT_ROOT_GENERATED_MARKER."""

    def test_marker_present_in_generated_file(self, tmp_path):
        """Generated file contains APM marker for overwrite detection."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        deploy_root = tmp_path / ".claude"
        output_path = deploy_root / "CLAUDE.md"

        target = _make_target("claude", "claude", deploy_root=deploy_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result = compile_user_root_contexts([target], source_root)

        assert result[0]["status"] == "written"
        content = output_path.read_text()
        # Check for the marker (from agents_compiler)
        assert "Generated by APM CLI from .apm/ primitives" in content


# ---------------------------------------------------------------------------
# test_claude_config_dir
# ---------------------------------------------------------------------------


class TestClaudeConfigDir:
    """Resolved deploy root is honored when set."""

    def test_resolved_deploy_root_honored(self, tmp_path):
        """resolved_deploy_root is used instead of home()/.claude."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        # Use a non-standard deploy root
        custom_root = tmp_path / "custom_deploy"
        output_path = custom_root / "CLAUDE.md"

        target = _make_target("claude", "claude", deploy_root=custom_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result = compile_user_root_contexts([target], source_root)

        assert len(result) == 1
        assert result[0]["target"] == "claude"
        assert result[0]["path"] == output_path
        assert result[0]["status"] == "written"

        # File should be at custom_root, not home()/.claude
        assert output_path.exists()


# ---------------------------------------------------------------------------
# test_multiple_targets
# ---------------------------------------------------------------------------


class TestMultipleTargets:
    """Multiple targets are processed in sequence."""

    def test_multiple_targets_all_written(self, tmp_path):
        """Multiple targets, all write their files."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        claude_root = tmp_path / ".claude"
        vscode_root = tmp_path / ".vscode"

        targets = [
            _make_target("claude", "claude", deploy_root=claude_root),
            _make_target("vscode", "vscode", deploy_root=vscode_root),
        ]

        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result = compile_user_root_contexts(targets, source_root)

        assert len(result) == 2
        assert result[0]["target"] == "claude"
        assert result[0]["status"] == "written"
        assert result[1]["target"] == "vscode"
        assert result[1]["status"] == "written"

        # Both files exist
        assert (claude_root / "CLAUDE.md").exists()
        assert (vscode_root / "AGENTS.md").exists()


# ---------------------------------------------------------------------------
# test_error_on_read
# ---------------------------------------------------------------------------


class TestErrorOnRead:
    """OSError during read returns error status."""

    def test_oserror_on_read_existing_file(self, tmp_path):
        """Cannot read existing file -> error:<msg>."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        deploy_root = tmp_path / ".claude"
        deploy_root.mkdir(parents=True, exist_ok=True)
        output_path = deploy_root / "CLAUDE.md"
        output_path.write_text("existing content")

        target = _make_target("claude", "claude", deploy_root=deploy_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        # Patch read_text to raise OSError
        with (
            patch(
                "apm_cli.primitives.discovery.discover_primitives",
                return_value=primitives,
            ),
            patch.object(Path, "read_text", side_effect=OSError("permission denied")),
        ):
            result = compile_user_root_contexts([target], source_root)

        assert len(result) == 1
        assert result[0]["target"] == "claude"
        assert result[0]["status"].startswith("error:")
        assert "permission denied" in result[0]["status"]


# ---------------------------------------------------------------------------
# test_error_on_write
# ---------------------------------------------------------------------------


class TestErrorOnWrite:
    """OSError during write returns error status."""

    def test_oserror_on_write_new_file(self, tmp_path):
        """Cannot write new file -> error:<msg>."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        deploy_root = tmp_path / ".claude"

        target = _make_target("claude", "claude", deploy_root=deploy_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        # Patch write_text to raise OSError
        with (
            patch(
                "apm_cli.primitives.discovery.discover_primitives",
                return_value=primitives,
            ),
            patch.object(Path, "write_text", side_effect=OSError("disk full")),
        ):
            result = compile_user_root_contexts([target], source_root)

        assert len(result) == 1
        assert result[0]["target"] == "claude"
        assert result[0]["status"].startswith("error:")
        assert "disk full" in result[0]["status"]


# ---------------------------------------------------------------------------
# test_symlink_escape
# ---------------------------------------------------------------------------


class TestSymlinkEscape:
    """Symlinked output files that escape deploy root are rejected."""

    def test_output_symlink_escape_returns_error(self, tmp_path):
        """Root context symlink pointing outside deploy root is not followed."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        deploy_root = tmp_path / ".claude"
        deploy_root.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("outside content", encoding="utf-8")
        (deploy_root / "CLAUDE.md").symlink_to(outside)

        target = _make_target("claude", "claude", deploy_root=deploy_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            result = compile_user_root_contexts([target], source_root)

        assert len(result) == 1
        assert result[0]["target"] == "claude"
        assert result[0]["status"].startswith("error:")
        assert "outside" in result[0]["status"]
        assert outside.read_text(encoding="utf-8") == "outside content"


# ---------------------------------------------------------------------------
# test_logger_usage
# ---------------------------------------------------------------------------


class TestLoggerUsage:
    """Logger is used for debug/info/warning output."""

    def test_logger_called_on_success(self, tmp_path):
        """Logger receives debug calls for various steps."""
        from apm_cli.compilation.user_root_context import compile_user_root_contexts

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        deploy_root = tmp_path / ".claude"

        target = _make_target("claude", "claude", deploy_root=deploy_root)
        instr = _make_instruction("global", apply_to=None, content="Use type hints")
        primitives = MagicMock()
        primitives.instructions = [instr]

        mock_logger = MagicMock(spec=logging.Logger)

        with patch(
            "apm_cli.primitives.discovery.discover_primitives",
            return_value=primitives,
        ):
            compile_user_root_contexts([target], source_root, logger=mock_logger)

        # Logger should have received debug/info calls
        assert mock_logger.debug.called or mock_logger.info.called
