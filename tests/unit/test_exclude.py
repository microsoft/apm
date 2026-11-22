"""Tests for the shared exclude-pattern matching utility."""

import shutil
import tempfile
import unittest
from pathlib import Path

from apm_cli.utils.exclude import (
    _match_double_star,  # noqa: F401
    _match_glob_recursive,
    _matches_pattern,
    should_exclude,
    validate_exclude_patterns,
)


class TestValidateExcludePatterns(unittest.TestCase):
    """Tests for pattern validation and DoS guard."""

    def test_none_returns_empty(self):
        self.assertEqual(validate_exclude_patterns(None), [])

    def test_empty_list_returns_empty(self):
        self.assertEqual(validate_exclude_patterns([]), [])

    def test_valid_patterns_returned(self):
        result = validate_exclude_patterns(["docs/**", "tmp", "*.log"])
        self.assertEqual(result, ["docs/**", "tmp", "*.log"])

    def test_backslashes_normalized(self):
        result = validate_exclude_patterns(["docs\\labs\\**"])
        self.assertEqual(result, ["docs/labs/**"])

    def test_rejects_excessive_double_star(self):
        # 6 non-consecutive ** segments (separated by literals, can't collapse)
        pattern = "a/**/b/**/c/**/d/**/e/**/f/**"
        with self.assertRaises(ValueError) as ctx:
            validate_exclude_patterns([pattern])
        self.assertIn("6", str(ctx.exception))
        self.assertIn("max", str(ctx.exception).lower())

    def test_allows_max_double_star(self):
        pattern = "/".join(["**"] * 5)  # exactly 5 -- at the limit
        result = validate_exclude_patterns([pattern])
        self.assertEqual(len(result), 1)

    def test_double_star_count_ignores_non_star(self):
        # Only ** segments count, not * or other parts
        pattern = "a/*/b/**/c/**/d"
        result = validate_exclude_patterns([pattern])
        self.assertEqual(result, [pattern])

    def test_consecutive_double_stars_collapsed(self):
        # **/**/** is semantically identical to **
        result = validate_exclude_patterns(["**/**/**/*.md"])
        self.assertEqual(result, ["**/*.md"])

    def test_consecutive_collapse_then_count(self):
        # After collapsing, 6 consecutive ** become 1 -- well under limit
        pattern = "/".join(["**"] * 6) + "/*.txt"
        result = validate_exclude_patterns([pattern])
        self.assertEqual(result, ["**/*.txt"])


class TestMatchesPattern(unittest.TestCase):
    """Tests for individual pattern matching logic."""

    def test_simple_fnmatch(self):
        self.assertTrue(_matches_pattern("foo.log", "*.log"))

    def test_simple_fnmatch_no_match(self):
        self.assertFalse(_matches_pattern("foo.txt", "*.log"))

    def test_directory_prefix_with_slash(self):
        self.assertTrue(_matches_pattern("docs/foo.md", "docs/"))

    def test_directory_prefix_without_slash(self):
        self.assertTrue(_matches_pattern("docs/foo.md", "docs"))

    def test_exact_match(self):
        self.assertTrue(_matches_pattern("docs", "docs"))

    def test_double_star_recursive(self):
        self.assertTrue(_matches_pattern("a/b/c/d.txt", "a/**/d.txt"))

    def test_double_star_zero_dirs(self):
        self.assertTrue(_matches_pattern("a/d.txt", "a/**/d.txt"))

    def test_leading_double_star(self):
        self.assertTrue(_matches_pattern("a/b/c.md", "**/*.md"))

    def test_trailing_double_star(self):
        self.assertTrue(_matches_pattern("docs/a/b/c", "docs/**"))


class TestMatchGlobRecursive(unittest.TestCase):
    """Tests for the glob recursive matcher."""

    def test_exact_match(self):
        self.assertTrue(_match_glob_recursive(["a", "b"], ["a", "b"]))

    def test_wildcard(self):
        self.assertTrue(_match_glob_recursive(["a", "foo.md"], ["a", "*.md"]))

    def test_double_star_matches_multiple(self):
        self.assertTrue(_match_glob_recursive(["a", "b", "c", "d"], ["a", "**", "d"]))

    def test_double_star_matches_zero(self):
        self.assertTrue(_match_glob_recursive(["a", "d"], ["a", "**", "d"]))

    def test_no_match(self):
        self.assertFalse(_match_glob_recursive(["a", "b"], ["c", "d"]))

    def test_trailing_empty_part_from_slash(self):
        # Pattern "foo/" splits to ["foo", ""]
        self.assertTrue(_match_glob_recursive(["foo", "bar"], ["foo", "**"]))


class TestShouldExclude(unittest.TestCase):
    """Integration tests for should_exclude with real filesystem."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, rel_path: str) -> Path:
        p = self.base / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("test", encoding="utf-8")
        return p

    def test_no_patterns_returns_false(self):
        f = self._touch("a.txt")
        self.assertFalse(should_exclude(f, self.base, None))
        self.assertFalse(should_exclude(f, self.base, []))

    def test_excludes_matching_file(self):
        f = self._touch("docs/api.md")
        self.assertTrue(should_exclude(f, self.base, ["docs/**"]))

    def test_keeps_non_matching_file(self):
        f = self._touch("src/main.py")
        self.assertFalse(should_exclude(f, self.base, ["docs/**"]))

    def test_path_outside_base_not_excluded(self):
        import os  # noqa: F401

        outside = Path(tempfile.mkdtemp())
        try:
            f = outside / "secret.md"
            f.write_text("test", encoding="utf-8")
            self.assertFalse(should_exclude(f, self.base, ["**"]))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_directory_name_match(self):
        f = self._touch("tmp/build/out.bin")
        self.assertTrue(should_exclude(f, self.base, ["tmp"]))

    def test_multiple_patterns(self):
        f1 = self._touch("docs/a.md")
        f2 = self._touch("tmp/b.txt")
        f3 = self._touch("src/c.py")
        patterns = ["docs/**", "tmp/**"]
        self.assertTrue(should_exclude(f1, self.base, patterns))
        self.assertTrue(should_exclude(f2, self.base, patterns))
        self.assertFalse(should_exclude(f3, self.base, patterns))


class TestDoubleStarDoSGuard(unittest.TestCase):
    """Ensure pathological patterns are rejected before reaching recursion."""

    def test_twelve_stars_rejected(self):
        # 12 non-consecutive ** segments (consecutive ones collapse)
        parts = []
        for i in range(12):
            parts.extend([f"d{i}", "**"])
        pattern = "/".join(parts)
        with self.assertRaises(ValueError):
            validate_exclude_patterns([pattern])

    def test_normal_patterns_fast(self):
        import time

        patterns = validate_exclude_patterns(["docs/**/*.md"])
        start = time.monotonic()
        for _ in range(1000):
            _matches_pattern("docs/a/b/c/d/e/f/g.md", patterns[0])
        elapsed = time.monotonic() - start
        # 1000 iterations should complete in well under 1 second
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
