"""Pinned recipes of the perf fixtures: every benchmark names one of these.

A recipe is the whole input of the generator (``generator.py``): the same
recipe and seed always produce byte-identical package trees and the same git
commit SHAs, so numbers from different runs and machines compare. Change a
pinned recipe only with a note in the benchmark history; add a new one instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


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
    # Git host shapes the packages are referenced through (``generator.HOST_SPECS``);
    # each package keeps one host for every parent, so its identity never forks.
    hosts: tuple[str, ...] = ("github",)
    # How the consumer pins its direct dependencies: "tag" (``#<tag>``), "tip"
    # (no ref: the default branch tip) or "sha" (``#<full SHA>`` of a commit
    # that is no longer the branch tip). Transitive edges keep "tag" except
    # under "tip"; the tag names the same commit as the "sha" pin.
    pin: str = "tag"
    # Serve the world with a developer-style global gitconfig (credential
    # helpers, credential.interactive, scoped useHttpPath, an SSH rewrite for
    # another host): the config real users carry, which a fast path must handle.
    user_gitconfig: bool = False


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


# Enterprise shapes: the same trees referenced across GitHub, GHE Cloud, GitLab
# and Azure DevOps (``-multi``), and with a stdio MCP server in every package
# (``-mcp``). Pin shapes: branch-tip (``-tip``) and non-tip commit SHA (``-sha``)
# direct dependencies. ``-usercfg`` serves the world with a developer-style
# global gitconfig. Same seeds, so only the variant's axis differs.
_MULTI_HOSTS = ("github", "ghe", "gitlab", "ado")
_MCP_HEAVY = PrimitiveMix(mcp_every=1)
_BASE_TREES = (
    TreeRecipe("tree-10", packages=10, depth=5, seed=10),
    TreeRecipe("tree-50", packages=50, depth=6, seed=50),
    TreeRecipe("tree-200", packages=200, depth=8, seed=200),
)

TREE_RECIPES: dict[str, TreeRecipe] = {
    recipe.name: recipe
    for base in _BASE_TREES
    for recipe in (
        base,
        replace(base, name=f"{base.name}-multi", hosts=_MULTI_HOSTS),
        replace(base, name=f"{base.name}-mcp", primitives=_MCP_HEAVY),
        replace(base, name=f"{base.name}-tip", pin="tip"),
        replace(base, name=f"{base.name}-sha", pin="sha"),
        replace(base, name=f"{base.name}-usercfg", user_gitconfig=True),
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
