"""Package sources the scenario suites install from, at either scope.

One helper per SRC factor value that names a package: git, the three
marketplace forms, the package registry and a native plugin. Each returns a
scenario where the source is reachable and nothing is installed yet.
"""

from __future__ import annotations

import json

from tests.lifecycle import driver
from tests.lifecycle.scenarios._kit import PROJECT_HOME_ALLOWED, Scenario
from tests.lifecycle.world import use_policy, use_registry

SOURCES = {
    "git": ("p-skill", "p-skill:git"),
    "marketplace-git": ("p-skill", "p-skill:marketplace"),
    "marketplace-local": ("p-skill", "p-skill@lifecycle-local"),
    "marketplace-file": ("p-skill", "p-skill@lifecycle-file"),
    "pkg-registry": ("p-skill", "p-skill:registry"),
    "native-plugin": ("p-native-claude", "p-native-claude"),
}
MARKETPLACES = ("marketplace-git", "marketplace-local", "marketplace-file")
# A project-scope marketplace install may refresh the catalog cache under HOME.
_MARKET_HOME = {"home": (*PROJECT_HOME_ALLOWED, ".apm/cache/marketplace")}


def local_catalog(world, src: str) -> str:
    """A marketplace on disk: a catalog directory, or one ``marketplace.json`` file."""
    name = "lifecycle-local" if src == "marketplace-local" else "lifecycle-file"
    plugin = {
        "name": "p-skill",
        "description": "Lifecycle world package p-skill",
        "source": {"source": "github", "repo": "lifecycle/p-skill", "ref": "v1.0.0"},
    }
    document = {"name": name, "owner": {"name": "APM Lifecycle"}, "plugins": [plugin]}
    root = world.root / name
    catalog = root / ".claude-plugin" / "marketplace.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps(document, indent=2) + "\n", encoding="ascii")
    return str(root if src == "marketplace-local" else catalog)


def from_source(world, src: str, *, scope: str = "project") -> tuple[Scenario, str]:
    """(scenario, package name) with *src* reachable and nothing installed yet."""
    use_policy(world, "allow")
    if src == "pkg-registry":
        # The registry is the default of the manifest the install reads (APM_HOME at -g).
        use_registry(world, scope)
    name, spec = SOURCES[src]
    spec = world.packages[spec].spec if spec in world.packages else spec
    targets = ("claude",) if src == "native-plugin" else ("copilot",)
    scenario = Scenario(world, {name: spec}, policy="allow", targets=targets, scope=scope)
    if src in MARKETPLACES:
        catalog = world.services.marketplace_url if src == "marketplace-git" else None
        catalog = catalog or local_catalog(world, src)
        if scope == "user":
            # ``apm marketplace`` has no user-scope form; the catalog list is
            # global, so a user-scope install reads what it registers (an input).
            result = driver.run(world, ("marketplace", "add", catalog), scope="project")
            assert result.returncode == 0, result.stdout + result.stderr
            scenario.source_moved()
        else:
            scenario.run(f"marketplace add {catalog}", forbidden={"project": ("*",)}, allowed={})
    return scenario, name


def source_fields(src: str, scope: str = "project") -> dict[str, object]:
    """Step fields a project-scope marketplace install needs (its HOME catalog cache)."""
    return {"allowed": _MARKET_HOME} if src in MARKETPLACES and scope == "project" else {}
