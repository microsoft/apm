"""Durable, seeded performance fixtures for APM (see tests/README.md, "Perf fixtures").

- ``build_tree(recipe, root)`` / ``build_monorepo(recipe, root)``: generated
  package trees from the pinned recipes in ``recipes.py``.
- ``serve_offline(fixtures, directory, rtt_ms=...)``: a local mirror with an
  injectable RTT and per-command request / round-trip counters.
- ``REAL_PACKAGES`` + ``counting_tunnel()``: the real-network mode.

The end-to-end harness is ``tests/benchmarks/journeys.py``.
"""

from __future__ import annotations

from tests.fixtures.perf.generator import (
    MonorepoFixture,
    TreeFixture,
    build_monorepo,
    build_tree,
    plan_tree,
    publish_repositories,
)
from tests.fixtures.perf.offline import OfflineWorld, isolated_environment, serve_offline
from tests.fixtures.perf.real_network import REAL_PACKAGES, counting_tunnel
from tests.fixtures.perf.recipes import (
    MONOREPO_RECIPES,
    TREE_RECIPES,
    MonorepoRecipe,
    PrimitiveMix,
    TreeRecipe,
    monorepo_recipe,
    tree_recipe,
)

__all__ = [
    "MONOREPO_RECIPES",
    "REAL_PACKAGES",
    "TREE_RECIPES",
    "MonorepoFixture",
    "MonorepoRecipe",
    "OfflineWorld",
    "PrimitiveMix",
    "TreeFixture",
    "TreeRecipe",
    "build_monorepo",
    "build_tree",
    "counting_tunnel",
    "isolated_environment",
    "monorepo_recipe",
    "plan_tree",
    "publish_repositories",
    "serve_offline",
    "tree_recipe",
]
