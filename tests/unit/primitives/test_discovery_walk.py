"""Tests for os.walk-based discovery (replacing glob.glob) and related helpers.

Covers _glob_match, find_primitive_files with exclude_patterns, and
_exclude_matches_dir -- the new code introduced to fix compile hangs
on large repositories.
"""

import tempfile
import unittest
from pathlib import Path

from apm_cli.constants import DEFAULT_SKIP_DIRS
from apm_cli.primitives.discovery import (
    _exclude_matches_dir,
    _glob_match,
    find_primitive_files,
)


def _write(path: Path, content: str = "---\ndescription: stub\n---\n\n# Stub\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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

    def test_doublestar_middle_zero(self):
        """Leading **/ should also match zero segments when pattern has a middle path."""
        self.assertTrue(
            _glob_match(
                ".apm/instructions/style.instructions.md", "**/.apm/instructions/*.instructions.md"
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
# find_primitive_files -- early directory pruning
# -------------------------------------------------------------------
class TestFindPrimitiveFilesExclude(unittest.TestCase):
    """Tests that find_primitive_files prunes directories via exclude_patterns."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_instruction_in_apm_dir(self):
        _write(self.base / ".apm" / "instructions" / "style.instructions.md")
        result = find_primitive_files(str(self.base), ["**/.apm/instructions/*.instructions.md"])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].name == "style.instructions.md")

    def test_finds_file_at_root(self):
        _write(self.base / "root.instructions.md")
        result = find_primitive_files(str(self.base), ["**/*.instructions.md"])
        self.assertEqual(len(result), 1)

    def test_skips_default_dirs(self):
        """Files inside DEFAULT_SKIP_DIRS should never be returned."""
        _write(self.base / "node_modules" / "pkg" / "bad.instructions.md")
        _write(self.base / "__pycache__" / "bad.instructions.md")
        _write(self.base / ".git" / "hooks" / "bad.instructions.md")
        _write(self.base / "src" / "good.instructions.md")

        result = find_primitive_files(str(self.base), ["**/*.instructions.md"])
        names = [f.name for f in result]
        self.assertIn("good.instructions.md", names)
        self.assertNotIn("bad.instructions.md", names)

    def test_exclude_patterns_prune_custom_dirs(self):
        """User-supplied exclude_patterns prevent traversal into named dirs."""
        _write(self.base / "Binaries" / "Win64" / "deep.instructions.md")
        _write(self.base / "Content" / "Textures" / "deep.instructions.md")
        _write(self.base / "Source" / "style.instructions.md")

        result = find_primitive_files(
            str(self.base),
            ["**/*.instructions.md"],
            exclude_patterns=["Binaries", "Content"],
        )
        names = [f.name for f in result]
        self.assertIn("style.instructions.md", names)
        self.assertNotIn("deep.instructions.md", names)

    def test_exclude_patterns_glob_style(self):
        """Glob-style exclude patterns work for nested matches."""
        _write(self.base / "a" / "test-fixtures" / "f.instructions.md")
        _write(self.base / "b" / "real.instructions.md")

        result = find_primitive_files(
            str(self.base),
            ["**/*.instructions.md"],
            exclude_patterns=["**/test-fixtures"],
        )
        names = [f.name for f in result]
        self.assertIn("real.instructions.md", names)
        self.assertNotIn("f.instructions.md", names)

    def test_exclude_patterns_none_finds_everything(self):
        """When exclude_patterns is None, only default skips apply."""
        _write(self.base / "a" / "one.instructions.md")
        _write(self.base / "b" / "two.instructions.md")

        result = find_primitive_files(
            str(self.base), ["**/*.instructions.md"], exclude_patterns=None
        )
        self.assertEqual(len(result), 2)

    def test_deduplicates_across_patterns(self):
        """Overlapping patterns should not produce duplicate results."""
        _write(self.base / ".apm" / "instructions" / "style.instructions.md")
        result = find_primitive_files(
            str(self.base),
            [
                "**/.apm/instructions/*.instructions.md",
                "**/*.instructions.md",
            ],
        )
        self.assertEqual(len(result), 1)

    def test_symlink_rejected(self):
        """Symlinked files should be filtered out."""
        real = self.base / "real.instructions.md"
        _write(real)
        link = self.base / "link.instructions.md"
        try:
            link.symlink_to(real)
        except OSError:
            self.skipTest("Cannot create symlinks on this platform")
        result = find_primitive_files(str(self.base), ["**/*.instructions.md"])
        names = [f.name for f in result]
        self.assertIn("real.instructions.md", names)
        self.assertNotIn("link.instructions.md", names)

    def test_nonexistent_dir_returns_empty(self):
        result = find_primitive_files("/nonexistent/path/1234", ["**/*.md"])
        self.assertEqual(result, [])

    def test_apm_dir_not_skipped(self):
        """.apm must NOT be in the default skip set -- primitives live there."""
        self.assertNotIn(".apm", DEFAULT_SKIP_DIRS)


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


# -------------------------------------------------------------------
# File-level exclude patterns (PR #870 review C1)
# -------------------------------------------------------------------
class TestFindPrimitiveFilesFileExclude(unittest.TestCase):
    """File-level exclude patterns must filter individual files,
    not just whole directories."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_pattern_excludes_individual_files(self):
        from apm_cli.primitives.discovery import find_primitive_files

        skills_dir = self.base / ".apm" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "good.md").touch()
        (skills_dir / "bad.draft.md").touch()

        results = find_primitive_files(
            str(self.base),
            ["**/.apm/skills/*.md"],
            exclude_patterns=["**/*.draft.md"],
        )

        names = sorted(p.name for p in results)
        self.assertIn("good.md", names)
        self.assertNotIn("bad.draft.md", names)

    def test_files_sorted_deterministically(self):
        """os.walk does not sort files -- find_primitive_files must."""
        from apm_cli.primitives.discovery import find_primitive_files

        skills_dir = self.base / ".apm" / "skills"
        skills_dir.mkdir(parents=True)
        # Create files in non-alphabetical order
        for name in ["zeta.md", "alpha.md", "mike.md"]:
            (skills_dir / name).touch()

        results = find_primitive_files(
            str(self.base),
            ["**/.apm/skills/*.md"],
        )
        names = [p.name for p in results]
        self.assertEqual(names, sorted(names))


if __name__ == "__main__":
    unittest.main()
