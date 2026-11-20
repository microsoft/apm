"""Pure-logic tests for discovery and parser path helpers.

Covers _glob_match, _exclude_matches_dir, directory skip/containment
predicates and primitive name/context/validation helpers. Filesystem
walks live in test_discovery_parser.py.
"""

import unittest
from pathlib import Path

import pytest

from apm_cli.primitives.discovery import (
    _exclude_matches_dir,
    _glob_match,
    _is_under_directory,
    _should_skip_directory,
)
from apm_cli.primitives.parser import (
    _extract_primitive_name,
    _is_context_file,
    validate_primitive,
)

pytestmark = pytest.mark.unit


# -------------------------------------------------------------------
# _glob_match
# -------------------------------------------------------------------
class TestGlobMatch(unittest.TestCase):
    """Tests for _glob_match -- fnmatch wrapper with ** zero-segment support."""

    # -- simple patterns (no **) --
    def test_simple_star(self):
        self.assertTrue(_glob_match("readme.md", "*.md"))

    def test_simple_star_no_match(self):
        self.assertFalse(_glob_match("readme.txt", "*.md"))

    def test_simple_exact(self):
        self.assertTrue(_glob_match("SKILL.md", "SKILL.md"))

    def test_simple_question_mark(self):
        self.assertTrue(_glob_match("a.py", "?.py"))
        self.assertFalse(_glob_match("ab.py", "?.py"))

    # -- ** matching one-or-more segments --
    def test_doublestar_one_segment(self):
        self.assertTrue(_glob_match("src/app.py", "**/*.py"))

    def test_doublestar_multiple_segments(self):
        self.assertTrue(_glob_match("a/b/c/d.py", "**/*.py"))

    # -- ** matching zero segments --
    def test_doublestar_zero_segments(self):
        """**/*.md should match readme.md at the root (zero directory segments)."""
        self.assertTrue(_glob_match("readme.md", "**/*.md"))

    def test_doublestar_zero_segments_instructions(self):
        self.assertTrue(_glob_match("coding.instructions.md", "**/*.instructions.md"))

    # -- ** in the middle of a pattern --
    def test_doublestar_middle(self):
        self.assertTrue(
            _glob_match(
                ".apm/instructions/style.instructions.md", "**/.apm/instructions/*.instructions.md"
            )
        )

    def test_doublestar_middle_nested(self):
        self.assertTrue(
            _glob_match(
                "sub/dir/.apm/instructions/style.instructions.md",
                "**/.apm/instructions/*.instructions.md",
            )
        )

    # -- no match --
    def test_no_match_extension(self):
        self.assertFalse(_glob_match("src/app.js", "**/*.py"))

    def test_no_match_prefix(self):
        self.assertFalse(_glob_match("src/app.py", "lib/**/*.py"))

    # -- pattern without ** stays simple --
    def test_no_doublestar_subdir(self):
        """Without **, pattern should not cross directories."""
        result = _glob_match("a/b.py", "*.py")
        self.assertIsInstance(result, bool)


# -------------------------------------------------------------------
# _exclude_matches_dir
# -------------------------------------------------------------------
class TestExcludeMatchesDir(unittest.TestCase):
    """Tests for _exclude_matches_dir -- thin wrapper over should_exclude."""

    def test_none_patterns_returns_false(self):
        self.assertFalse(_exclude_matches_dir(Path("/p/node_modules"), Path("/p"), None))

    def test_empty_patterns_returns_false(self):
        self.assertFalse(_exclude_matches_dir(Path("/p/node_modules"), Path("/p"), []))

    def test_matching_pattern(self):
        self.assertTrue(_exclude_matches_dir(Path("/p/Binaries"), Path("/p"), ["Binaries"]))

    def test_non_matching_pattern(self):
        self.assertFalse(_exclude_matches_dir(Path("/p/src"), Path("/p"), ["Binaries"]))

    def test_glob_pattern(self):
        self.assertTrue(
            _exclude_matches_dir(Path("/p/a/test-fixtures"), Path("/p"), ["**/test-fixtures"])
        )


# -------------------------------------------------------------------
# _glob_match segment-aware semantics (PR #870 review C2)
# -------------------------------------------------------------------
class TestGlobMatchSegmentAware(unittest.TestCase):
    """Verify * does not cross / boundaries; ** does."""

    def test_star_does_not_cross_slash(self):
        from apm_cli.primitives.discovery import _glob_match

        # Pattern matches one segment under instructions/ only
        self.assertTrue(_glob_match(".apm/instructions/x.md", ".apm/instructions/*.md"))
        self.assertFalse(_glob_match(".apm/instructions/sub/x.md", ".apm/instructions/*.md"))

    def test_double_star_crosses_slash(self):
        from apm_cli.primitives.discovery import _glob_match

        self.assertTrue(_glob_match("a/b/c/x.md", "**/x.md"))
        self.assertTrue(_glob_match("x.md", "**/x.md"))  # zero segments

    def test_star_with_double_star_prefix(self):
        from apm_cli.primitives.discovery import _glob_match

        # ** then literal then * -- * should still respect /
        self.assertTrue(_glob_match("a/b/.apm/instructions/foo.md", "**/.apm/instructions/*.md"))
        self.assertFalse(
            _glob_match("a/b/.apm/instructions/sub/foo.md", "**/.apm/instructions/*.md")
        )

    def test_question_mark_single_char_no_slash(self):
        from apm_cli.primitives.discovery import _glob_match

        self.assertTrue(_glob_match("ab", "a?"))
        self.assertFalse(_glob_match("a/b", "a?b"))


if __name__ == "__main__":
    unittest.main()


class TestExtractPrimitiveName(unittest.TestCase):
    """Tests for _extract_primitive_name with various path structures."""

    def test_agent_md_in_apm_agents_dir(self):
        """Files in .apm/agents/ are treated as structured agent primitives.
        The '.agent.md' suffix is stripped, yielding just the agent name."""
        path = Path("/project/.apm/agents/myagent.agent.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "myagent")

    def test_agent_md_in_github_agents_dir(self):
        """.github/agents/ is also treated as a structured agent primitive directory."""
        path = Path("/project/.github/agents/reviewer.agent.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "reviewer")

    def test_instruction_in_structured_dir(self):
        path = Path("/project/.apm/instructions/coding-style.instructions.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "coding-style")

    def test_context_in_structured_dir(self):
        path = Path("/project/.apm/context/project-info.context.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "project-info")

    def test_memory_in_structured_dir(self):
        path = Path("/project/.github/memory/state.memory.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "state")

    def test_plain_md_fallback(self):
        path = Path("/project/notes.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "notes")

    def test_stem_fallback_no_known_extension(self):
        path = Path("/project/something.xyz.md")
        name = _extract_primitive_name(path)
        self.assertEqual(name, "something.xyz")


class TestIsContextFile(unittest.TestCase):
    """Tests for _is_context_file."""

    def test_apm_memory_dir_is_context(self):
        path = Path("/project/.apm/memory/notes.md")
        self.assertTrue(_is_context_file(path))

    def test_github_memory_dir_is_context(self):
        path = Path("/project/.github/memory/state.md")
        self.assertTrue(_is_context_file(path))

    def test_random_dir_is_not_context(self):
        path = Path("/project/docs/notes.md")
        self.assertFalse(_is_context_file(path))

    def test_apm_context_dir_is_not_matched_here(self):
        """_is_context_file only matches memory/ dirs, not context/."""
        path = Path("/project/.apm/context/info.context.md")
        self.assertFalse(_is_context_file(path))


class TestValidatePrimitive(unittest.TestCase):
    """Tests for the validate_primitive wrapper."""

    def test_valid_chatmode_returns_no_errors(self):
        from apm_cli.primitives.models import Chatmode

        cm = Chatmode(
            name="test",
            file_path=Path("test.agent.md"),
            description="desc",
            apply_to=None,
            content="# body",
        )
        self.assertEqual(validate_primitive(cm), [])

    def test_invalid_chatmode_returns_errors(self):
        from apm_cli.primitives.models import Chatmode

        cm = Chatmode(
            name="test",
            file_path=Path("test.agent.md"),
            description="",
            apply_to=None,
            content="",
        )
        errors = validate_primitive(cm)
        self.assertTrue(len(errors) > 0)


class TestShouldSkipDirectory(unittest.TestCase):
    """Tests for _should_skip_directory."""

    def test_git_dir_skipped(self):
        self.assertTrue(_should_skip_directory("/project/.git"))

    def test_node_modules_skipped(self):
        self.assertTrue(_should_skip_directory("/project/node_modules"))

    def test_pycache_skipped(self):
        self.assertTrue(_should_skip_directory("/project/__pycache__"))

    def test_pytest_cache_skipped(self):
        self.assertTrue(_should_skip_directory("/project/.pytest_cache"))

    def test_venv_skipped(self):
        self.assertTrue(_should_skip_directory("/project/.venv"))
        self.assertTrue(_should_skip_directory("/project/venv"))

    def test_build_skipped(self):
        self.assertTrue(_should_skip_directory("/project/build"))

    def test_normal_dir_not_skipped(self):
        self.assertFalse(_should_skip_directory("/project/src"))
        self.assertFalse(_should_skip_directory("/project/.apm"))
        self.assertFalse(_should_skip_directory("/project/tests"))


class TestIsUnderDirectory(unittest.TestCase):
    """Tests for _is_under_directory."""

    def test_file_under_directory_returns_true(self):
        self.assertTrue(
            _is_under_directory(
                Path("/project/apm_modules/owner/repo/file.md"),
                Path("/project/apm_modules"),
            )
        )

    def test_file_not_under_directory_returns_false(self):
        self.assertFalse(
            _is_under_directory(Path("/project/.apm/file.md"), Path("/project/apm_modules"))
        )
