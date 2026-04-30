"""Regression coverage for ContextOptimizer behavior tracked under #871.

Covers:
- ``_cached_glob`` reusing cached results across repeated calls (cache layer
  populated via ``_glob_cache``).
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

    def test_cached_glob_caches_results(self):
        """Second call with same pattern reuses cached glob data.

        Regression coverage for the cache layer added in #871: once a pattern
        has been resolved, subsequent calls must reuse ``_glob_cache`` and
        return equivalent results without re-scanning the filesystem.
        """
        (self.base / "a.py").touch()
        optimizer = ContextOptimizer(base_dir=str(self.base))
        first = optimizer._cached_glob("**/*.py")
        second = optimizer._cached_glob("**/*.py")
        self.assertEqual(first, second)
        self.assertIn("**/*.py", optimizer._glob_cache)
        self.assertEqual(first, optimizer._glob_cache["**/*.py"])


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
