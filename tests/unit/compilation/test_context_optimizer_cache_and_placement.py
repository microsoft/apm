"""Tests for ContextOptimizer cache + placement changes from PR #871.

Covers:
- ``_directory_files_cache`` population during ``_analyze_project_structure``
  (skips DEFAULT_SKIP_DIRS and user-supplied ``exclude_patterns``).
- ``_cached_glob`` filtering pre-built file list via ``_glob_match`` and
  reusing cached results across calls.
- ``_optimize_single_point_placement`` selecting the lowest common ancestor
  inside a deep subtree (regression: narrow ``applyTo`` patterns must not
  bias toward the project root).
"""

import tempfile
import unittest
from pathlib import Path

from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction


class TestCachedGlobUsesFileList(unittest.TestCase):
    """Verify _cached_glob filters the pre-built file list via _glob_match."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cached_glob_respects_exclude_patterns(self):
        """_cached_glob should not return files under excluded directories."""
        (self.base / "src").mkdir()
        (self.base / "src" / "app.py").touch()
        (self.base / "vendor" / "lib").mkdir(parents=True)
        (self.base / "vendor" / "lib" / "dep.py").touch()

        optimizer = ContextOptimizer(
            base_dir=str(self.base),
            exclude_patterns=["vendor"],
        )

        matches = optimizer._cached_glob("**/*.py")
        match_strs = [m.replace("\\", "/") for m in matches]

        self.assertTrue(any("src/app.py" in m for m in match_strs))
        self.assertFalse(any("vendor" in m for m in match_strs))

    def test_cached_glob_caches_results(self):
        """Second call with same pattern reuses cached glob data."""
        (self.base / "a.py").touch()
        optimizer = ContextOptimizer(base_dir=str(self.base))
        first = optimizer._cached_glob("**/*.py")
        second = optimizer._cached_glob("**/*.py")
        self.assertEqual(first, second)
        self.assertIn("**/*.py", optimizer._glob_cache)
        self.assertEqual(first, optimizer._glob_cache["**/*.py"])

    def test_cached_glob_respects_file_level_excludes(self):
        """File-level exclude patterns must keep excluded files out of _cached_glob."""
        (self.base / "src").mkdir()
        (self.base / "src" / "app.py").touch()
        (self.base / "src" / "generated.dll").touch()
        (self.base / "src" / "auto.generated.h").touch()

        optimizer = ContextOptimizer(
            base_dir=str(self.base),
            exclude_patterns=["**/*.dll", "**/*.generated.h"],
        )

        # Glob for any file should not surface excluded file extensions.
        all_matches = optimizer._cached_glob("**/*")
        match_strs = [m.replace("\\", "/") for m in all_matches]
        self.assertTrue(any("src/app.py" in m for m in match_strs))
        self.assertFalse(any(m.endswith(".dll") for m in match_strs))
        self.assertFalse(any(m.endswith(".generated.h") for m in match_strs))

        # And the underlying file cache must not contain them either.
        all_cached = [
            str(f) for files in optimizer._directory_files_cache.values() for f in files
        ]
        self.assertFalse(any(s.endswith(".dll") for s in all_cached))
        self.assertFalse(any(s.endswith(".generated.h") for s in all_cached))

    def test_directory_files_cache_skips_default_dirs(self):
        """_directory_files_cache must not include files from DEFAULT_SKIP_DIRS."""
        (self.base / "src").mkdir()
        (self.base / "src" / "ok.py").touch()
        (self.base / "node_modules" / "pkg").mkdir(parents=True)
        (self.base / "node_modules" / "pkg" / "bad.js").touch()
        (self.base / "__pycache__").mkdir()
        (self.base / "__pycache__" / "mod.pyc").touch()

        optimizer = ContextOptimizer(base_dir=str(self.base))
        optimizer._analyze_project_structure()
        all_files = [str(f) for files in optimizer._directory_files_cache.values() for f in files]

        self.assertTrue(any("ok.py" in s for s in all_files))
        self.assertFalse(any("node_modules" in s for s in all_files))
        self.assertFalse(any("__pycache__" in s for s in all_files))

    def test_directory_files_cache_skips_custom_excludes(self):
        """_directory_files_cache must also respect user-supplied exclude_patterns."""
        (self.base / "src").mkdir()
        (self.base / "src" / "ok.py").touch()
        (self.base / "Binaries" / "Win64").mkdir(parents=True)
        (self.base / "Binaries" / "Win64" / "huge.dll").touch()

        optimizer = ContextOptimizer(
            base_dir=str(self.base),
            exclude_patterns=["Binaries"],
        )
        optimizer._analyze_project_structure()
        all_files = [str(f) for files in optimizer._directory_files_cache.values() for f in files]

        self.assertTrue(any("ok.py" in s for s in all_files))
        self.assertFalse(any("Binaries" in s for s in all_files))


class TestSinglePointPlacementNonRootLCA(unittest.TestCase):
    """Regression test for low-distribution placement at a non-root LCA.

    Before the fix, a narrow ``applyTo`` pattern whose matches all sit deep
    inside the same subtree (e.g. ``Engine/Plugins/PCG*/**/*``) was scored
    and could be placed at the project root. The corrected implementation
    routes single-point placement straight through
    ``_find_minimal_coverage_placement`` (LCA), which must return the
    deepest covering directory -- ``Engine/Plugins`` in this case, not ``./``.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = Path(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, rel: str) -> None:
        p = self.base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()

    def test_lca_placement_is_non_root_when_matches_share_deep_subtree(self):
        # Create unrelated content at root + several siblings so the project
        # is not trivially small (would otherwise force root placement).
        for d in ("Source", "Content", "Config", "Docs"):
            (self.base / d).mkdir()
            self._touch(f"{d}/keep.txt")

        # Two PCG plugins under the same Engine/Plugins parent. The LCA of
        # their matched files must be Engine/Plugins -- never the project root.
        self._touch("Engine/Plugins/PCG/Source/Foo.cpp")
        self._touch("Engine/Plugins/PCG/Source/Foo.h")
        self._touch("Engine/Plugins/PCGExtra/Source/Bar.cpp")
        self._touch("Engine/Plugins/PCGExtra/Source/Bar.h")

        optimizer = ContextOptimizer(base_dir=str(self.base))
        instruction = Instruction(
            name="pcg-standards",
            file_path=Path("pcg.instructions.md"),
            description="PCG plugin coding standards",
            apply_to="Engine/Plugins/PCG*/**/*",
            content="PCG standards",
        )

        result = optimizer.optimize_instruction_placement([instruction])

        self.assertEqual(len(result), 1, f"expected single placement, got {result}")
        placement_dir = next(iter(result.keys()))

        # Must be the Engine/Plugins LCA, not the project root.
        self.assertNotEqual(
            placement_dir.resolve(),
            self.base.resolve(),
            f"placement landed at project root instead of LCA: {placement_dir}",
        )
        rel = placement_dir.resolve().relative_to(self.base.resolve())
        self.assertEqual(
            rel.as_posix(),
            "Engine/Plugins",
            f"expected LCA Engine/Plugins, got {rel.as_posix()}",
        )


if __name__ == "__main__":
    unittest.main()
