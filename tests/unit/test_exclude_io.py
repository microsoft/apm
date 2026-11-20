"""Tests for the shared exclude-pattern matching utility (filesystem, process and CLI boundary cases)."""

import shutil
import tempfile
import unittest
from pathlib import Path

import pytest

from apm_cli.utils.exclude import (
    _match_double_star,  # noqa: F401
    should_exclude,
)

pytestmark = pytest.mark.component


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


if __name__ == "__main__":
    unittest.main()
