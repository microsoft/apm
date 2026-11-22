"""Pinned recipes of the perf fixtures: every benchmark names one of these.

A recipe is the whole input of the generator (``generator.py``): the same
recipe and seed always produce byte-identical package trees and the same git
commit SHAs, so numbers from different runs and machines compare. Change a
pinned recipe only with a note in the benchmark history; add a new one instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PrimitiveMix:
    """Primitives authored in every package (hooks and MCP on a subset)."""

    instructions: int = 4
    agents: int = 2
    skills: int = 2
    prompts: int = 3
    # One hook document on every ``hook_every``-th package (0: none).
    hook_every: int = 3
    # One stdio MCP server on every ``mcp_every``-th package (0: none).
    mcp_every: int = 8

    def per_package(self) -> int:
        return self.instructions + self.agents + self.skills + self.prompts


@dataclass(frozen=True)
class TreeRecipe:
    """A layered dependency DAG of ``packages`` git packages, ``depth`` levels deep.

    Every package of level ``i`` depends on ``fanout`` packages of level
    ``i + 1`` and, with probability ``skip_ratio``, on one of level ``i + 2``
    (transitive overlap). Two parents sharing a child form a diamond; the
    generator guarantees at least ``min_diamonds`` of them.
    """

    name: str
    packages: int
    depth: int = 5
    seed: int = 4242
    fanout: tuple[int, int] = (1, 3)
    skip_ratio: float = 0.25
    min_diamonds: int = 2
    primitives: PrimitiveMix = field(default_factory=PrimitiveMix)
    targets: tuple[str, ...] = ("copilot",)
    owner: str = "perf-org"
    tag: str = "v1.0.0"


@dataclass(frozen=True)
class MonorepoRecipe:
    """A repository with several ``apm.yml`` roots sharing local and remote packages.

    ``apps`` project roots live under ``apps/<name>/`` beside the repository
    root manifest; ``local_packages`` live under ``packages/<name>/`` and are
    referenced by relative path. Every root also depends on a slice of the
    remote ``tree`` (served like any tree fixture).
    """

    name: str
    apps: int = 4
    local_packages: int = 12
    local_per_app: int = 4
    remote_per_app: int = 3
    seed: int = 7
    primitives: PrimitiveMix = field(
        default_factory=lambda: PrimitiveMix(instructions=6, agents=3, skills=3, prompts=4)
    )
    tree: TreeRecipe = field(default_factory=lambda: TreeRecipe("monorepo-remote", 20, seed=20))
    targets: tuple[str, ...] = ("copilot",)


TREE_RECIPES: dict[str, TreeRecipe] = {
    recipe.name: recipe
    for recipe in (
        TreeRecipe("tree-10", packages=10, depth=5, seed=10),
        TreeRecipe("tree-50", packages=50, depth=6, seed=50),
        TreeRecipe("tree-200", packages=200, depth=8, seed=200),
    )
}

MONOREPO_RECIPES: dict[str, MonorepoRecipe] = {
    recipe.name: recipe for recipe in (MonorepoRecipe("monorepo-4"),)
}


def tree_recipe(name: str) -> TreeRecipe:
    try:
        return TREE_RECIPES[name]
    except KeyError:
        raise KeyError(f"unknown tree recipe {name!r}; known: {sorted(TREE_RECIPES)}") from None


def monorepo_recipe(name: str) -> MonorepoRecipe:
    try:
        return MONOREPO_RECIPES[name]
    except KeyError:
        known = sorted(MONOREPO_RECIPES)
        raise KeyError(f"unknown monorepo recipe {name!r}; known: {known}") from None
