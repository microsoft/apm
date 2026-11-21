"""Network offline: what a project installed online can still do with no network (#4104).

PROMISE: P-INV-I7 -- nothing is skipped silently; a failed command preserves
state (source: .apm/skills/apm-lifecycle-tests/SKILL.md, cross-cutting invariants).
PROMISE: P-OWN-ci-audit-scratch-materialization -- ``audit --ci`` decides
from the lock and the deployed files without fetching (source:
install/audit_replay.py "Replaying install (cache-only)").

NET=offline (T-6e): every row first installs online, then closes the network
the way the source machine does (``HTTPS_PROXY`` at a closed port, so no world
host is reachable) and runs one command per credited row. A converged project
needs no network: install and its modifiers, the I3 pair, lock, compile,
prune, approve and deny, uninstall and a local-only update all succeed from
the lock and ``apm_modules``. Each row applies I1-I9 through the scenario kit
and credits the scenario-lane pairs its factor assignment implies
(``CREDITS``).
"""

from __future__ import annotations

import json

import pytest

from tests.lifecycle import driver
from tests.lifecycle.scenarios._kit import (
    PROJECT_HOME_ALLOWED,
    Scenario,
    assert_changes,
    credits,
)
from tests.lifecycle.world import use_policy, use_registry
from tests.lifecycle.world.services import REGISTRY_SERVER

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I3",
        "P-INV-I5",
        "P-INV-I7",
        "P-OWN-ci-audit-scratch-materialization",
    ),
]

# The source machine's closed proxy (tests/lifecycle/machines/test_m_source.py).
_CLOSED_PROXY = "http://127.0.0.1:9"
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
TARGETS = {"single-unchanged": ("copilot",), "multi-unchanged": ("copilot", "claude")}
# The world context every offline row holds: project scope, the files the online
# install deployed left untouched, and no network.
_CONTEXT = {"SCOPE": "project", "NET": "offline", "DEPLOYED": "clean"}
# Package -> the units it proves: (SRC, PRIM, GATE) once its executables are approved.
_UNITS = {
    "p-skill:git": (("git", "skill", "none"),),
    # bin/ never deploys at project scope (U-BIN), so no project row proves a bin unit.
    "p-exec": (
        ("local-path", "hook", "allowed"),
        ("local-path", "mcp", "allowed"),
    ),
    "p-lsp": (("local-path", "lsp", "allowed"),),
    "p-canvas": (("local-path", "canvas", "allowed"),),
    "p-native-claude-bin": (("native-plugin", "skill", "allowed"),),
    "p-skill:registry": (("pkg-registry", "skill", "none"),),
}
# p-skill:registry resolves through the registry only in the scenario that selects
# it (use_registry), so the converged project leaves it out.
_REGISTRY = ("p-skill:registry",)
_CONVERGED = tuple(name for name in _UNITS if name not in _REGISTRY)


def go_offline(world) -> None:
    """Close the network: every world host is unreachable from now on."""
    for name in _PROXY_VARS:
        world.env.pop(name, None)
    world.env["HTTPS_PROXY"] = world.env["https_proxy"] = _CLOSED_PROXY
    world.env["NO_PROXY"] = world.env["no_proxy"] = "localhost"


def _assignments(op: str, tgt: str, packages, **overrides: str) -> list[dict[str, str]]:
    return [
        {"OP": op, "TGT": tgt, "SRC": src, "PRIM": prim, "GATE": gate, **_CONTEXT, **overrides}
        for name in packages
        for src, prim, gate in _UNITS[name]
    ]


# Row -> (offline command line, OP, packages whose units it proves, factor overrides).
# The rows run in this order on one converged project; every one must exit 0.
CONVERGED_ROWS = {
    "install": ("install", "install", _CONVERGED, {}),
    "install-dry-run": ("install --dry-run", "install", _CONVERGED, {"DRYRUN": "on"}),
    "install-force": ("install --force", "install", _CONVERGED, {"FORCE": "on"}),
    "install-select": ("install p-exec", "install", ("p-exec",), {"SELECT": "on"}),
    "lock": ("lock", "lock", _CONVERGED, {}),
    "compile": ("compile", "compile", _CONVERGED, {}),
    "prune": ("prune", "prune", _CONVERGED, {}),
    "audit": ("audit", "audit", _CONVERGED, {}),
}
# The I3 pair on the converged project: audit --ci, then install --frozen.
PAIR_CREDITS = (("audit", {}), ("install", {"FROZEN": "on"}))
# Revoked after deploy: deny an approved, deployed canvas, then install removes it.
REVOKED_ROWS = (
    ("deny p-canvas", "approve-deny"),
    ("install", "install"),
)


def _converged_assignments(tgt: str) -> list[dict[str, str]]:
    found = [
        item
        for _line, op, packages, overrides in CONVERGED_ROWS.values()
        for item in _assignments(op, tgt, packages, **overrides)
    ]
    found += [
        item
        for op, overrides in PAIR_CREDITS
        for item in _assignments(op, tgt, _CONVERGED, **overrides)
    ]
    found += [
        item
        for _line, op in REVOKED_ROWS
        for item in _assignments(op, tgt, ("p-canvas",), GATE="revoked-after-deploy")
    ]
    found += _assignments("uninstall", tgt, ("p-lsp",))
    return found


@pytest.mark.parametrize("tgt", sorted(TARGETS))
def test_converged_project_works_offline(lifecycle_world, tgt) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, _CONVERGED, targets=TARGETS[tgt], policy="allow")
    scenario.run(f"install {' '.join(_CONVERGED)}")
    scenario.run("approve p-exec p-lsp p-canvas")
    scenario.run("install")
    scenario.assert_deployed()
    go_offline(lifecycle_world)

    for line, op, packages, overrides in CONVERGED_ROWS.values():
        step = scenario.run(line, credit=_assignments(op, tgt, packages, **overrides))
        assert_changes(step.changed, (), label=f"offline {line}")
    audit = _assignments("audit", tgt, _CONVERGED)
    frozen = _assignments("install", tgt, _CONVERGED, FROZEN="on")
    scenario.run("audit --ci", read_only=True, exit=0, credit=audit)
    scenario.run("install --frozen", paired={"audit": 0}, credit=frozen)
    for line, op in REVOKED_ROWS:
        revoked = _assignments(op, tgt, ("p-canvas",), GATE="revoked-after-deploy")
        scenario.run(line, credit=revoked)
    scenario.assert_deployed()
    scenario.run("uninstall p-lsp", credit=_assignments("uninstall", tgt, ("p-lsp",)))
    scenario.assert_deployed()
    scenario.finish()


# -- a local-only project updates offline ----------------------------------------

# update and install --update re-resolve every dependency: with a git or registry
# dependency they need the network (exit 1, state kept); with local paths only
# they succeed offline.
_LOCAL = ("p-exec", "p-canvas")
UPDATE_ROWS = {
    "update": ("update --yes", "update", {}),
    "install-update": ("install --update", "install", {"UPDATE": "on"}),
}


def _update_assignments() -> list[dict[str, str]]:
    return [
        item
        for _line, op, overrides in UPDATE_ROWS.values()
        for item in _assignments(op, "single-unchanged", _LOCAL, **overrides)
    ]


def test_local_project_updates_offline(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(
        lifecycle_world, _LOCAL, targets=TARGETS["single-unchanged"], policy="allow"
    )
    scenario.run(f"install {' '.join(_LOCAL)}")
    scenario.run(f"approve {' '.join(_LOCAL)}")
    scenario.run("install")
    go_offline(lifecycle_world)
    for line, op, overrides in UPDATE_ROWS.values():
        credit = _assignments(op, "single-unchanged", _LOCAL, **overrides)
        step = scenario.run(line, credit=credit)
        assert_changes(step.changed, (), label=f"offline {line}")
    scenario.assert_deployed()
    scenario.finish()


# -- executables parked, then denied, with no network -------------------------------


def _gated(op: str, gates: dict[str, str]) -> list[dict[str, str]]:
    return [
        {**item, "GATE": gates[name]}
        for name in gates
        for item in _assignments(op, "multi-unchanged", (name,))
    ]


_PARKED = {"p-exec": "unapproved-exec", "p-canvas": "unapproved-exec"}
_DENIED = {"p-exec": "unapproved-exec", "p-canvas": "denied-executable"}
GATE_ROWS = (
    ("install", _gated("install", _PARKED)),
    ("deny p-canvas", _gated("approve-deny", {"p-canvas": "denied-executable"})),
    ("install", _gated("install", _DENIED)),
)


def test_parked_then_denied_executables_offline(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    targets = TARGETS["multi-unchanged"]
    scenario = Scenario(lifecycle_world, _LOCAL, targets=targets, policy="allow")
    scenario.run(f"install {' '.join(_LOCAL)}")
    go_offline(lifecycle_world)
    for line, credit in GATE_ROWS:
        scenario.run(line, credit=credit)
        scenario.assert_deployed()
    scenario.finish()


# -- a native plugin ---------------------------------------------------------------

_NATIVE = ("p-native-claude-bin",)


def test_native_plugin_works_offline(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, _NATIVE, targets=("claude",), policy="allow")
    scenario.run(f"install {' '.join(_NATIVE)}")
    scenario.run(f"approve {' '.join(_NATIVE)}")
    scenario.run("install")
    go_offline(lifecycle_world)
    step = scenario.run("install", credit=_assignments("install", "single-unchanged", _NATIVE))
    assert_changes(step.changed, (), label="offline install of a native plugin")
    scenario.run(
        "audit --ci",
        read_only=True,
        exit=0,
        credit=_assignments("audit", "single-unchanged", _NATIVE),
    )
    scenario.assert_deployed()
    scenario.finish()


# -- registries, MCP servers, bundles and pack ---------------------------------------

_INLINE = ("install", "--mcp", "p-inline", "--", "apm-lifecycle-inert-server", "--stdio")
_MCP_ROWS = {
    "mcp-registry": ("install", "--mcp", REGISTRY_SERVER),
    "inline-mcp": _INLINE,
}


def _source_assignments() -> list[dict[str, str]]:
    tgt = "single-unchanged"
    found = _assignments("install", tgt, _REGISTRY)
    found += [
        {"OP": "mcp-install", "TGT": tgt, "SRC": src, "PRIM": "mcp", **_CONTEXT}
        for src in _MCP_ROWS
    ]
    found += [
        {
            "OP": "bundle-install",
            "TGT": tgt,
            "SRC": "local-bundle",
            "PRIM": prim,
            "GATE": "unapproved-exec",
            **_CONTEXT,
        }
        for prim in ("hook", "mcp", "bin")
    ]
    found += [{**item, "OP": "pack"} for item in _assignments("pack", tgt, _REGISTRY)]
    for item in found[-1:]:
        item.pop("SRC")
    return found


def test_registry_mcp_and_bundle_sources_work_offline(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    use_registry(lifecycle_world)
    scenario = Scenario(lifecycle_world, _REGISTRY, targets=("copilot",), policy="allow")
    scenario.run(f"install {' '.join(_REGISTRY)}")
    scenario.source_moved()  # the user adds an MCP server: a new request
    scenario.run(("install", "--mcp", REGISTRY_SERVER))
    go_offline(lifecycle_world)
    tgt = "single-unchanged"
    scenario.run("install", credit=_assignments("install", tgt, _REGISTRY))
    for src, line in _MCP_ROWS.items():
        credit = [{"OP": "mcp-install", "TGT": tgt, "SRC": src, "PRIM": "mcp", **_CONTEXT}]
        scenario.source_moved()
        scenario.run(line, credit=credit)
    bundle = [
        {
            "OP": "bundle-install",
            "TGT": tgt,
            "SRC": "local-bundle",
            "PRIM": prim,
            "GATE": "unapproved-exec",
            **_CONTEXT,
        }
        for prim in ("hook", "mcp", "bin")
    ]
    scenario.source_moved()
    scenario.run(("install", lifecycle_world.packages["p-exec:bundle"].spec), credit=bundle)
    pack = [
        {key: value for key, value in item.items() if key != "SRC"}
        for item in _assignments("pack", tgt, _REGISTRY)
    ]
    scenario.run("pack", credit=pack)
    scenario.assert_deployed()
    scenario.finish()


# -- marketplaces: a git repository, a local directory and a catalog file -------------

# SRC follows the registration source (marketplace/registration.py:125-131): a git
# repository URL, a local directory holding marketplace.json, or the path of the
# marketplace.json file itself.
_MARKETPLACES = {
    "marketplace-git": ("lifecycle-market", "p-skill"),
    "marketplace-local": ("lifecycle-local", "p-lsp"),
    "marketplace-file": ("lifecycle-file", "p-exec"),
}
# The catalog cache a project install may refresh under APM_HOME (as the source machine).
_CATALOG_CACHE = (
    ".apm/cache/marketplace/version-pins.json",
    *(
        f".apm/cache/marketplace/{name}{suffix}"
        for name, _package in _MARKETPLACES.values()
        for suffix in (".json", ".meta.json")
    ),
)


def _register_marketplaces(world) -> dict[str, str]:
    """Register the three catalogs online; return package name -> ``name@marketplace``."""
    root = world.root / "catalogs"
    for src, (name, package) in _MARKETPLACES.items():
        if src == "marketplace-git":
            source = world.services.marketplace_url
        else:
            plugin = {
                "name": package,
                "description": f"Lifecycle world package {package}",
                "source": {"source": "github", "repo": f"lifecycle/{package}", "ref": "v1.0.0"},
            }
            document = {"name": name, "owner": {"name": "APM Lifecycle"}, "plugins": [plugin]}
            catalog = root / name / ".claude-plugin" / "marketplace.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps(document, indent=2) + "\n", encoding="ascii")
            source = str(catalog if src == "marketplace-file" else root / name)
        result = driver.run(world, ("marketplace", "add", source), scope="project")
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    return {package: f"{package}@{name}" for name, package in _MARKETPLACES.values()}


def _marketplace_assignments(op: str) -> list[dict[str, str]]:
    found = []
    for src, (_name, package) in _MARKETPLACES.items():
        for _src, prim, gate in _UNITS.get(package, (("git", "skill", "none"),)):
            gate = "none" if prim == "skill" else "unapproved-exec"
            found.append(
                {
                    "OP": op,
                    "TGT": "single-unchanged",
                    "SRC": src,
                    "PRIM": prim,
                    "GATE": gate,
                    **_CONTEXT,
                }
            )
    return found


def test_marketplace_sources_work_offline(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    specs = _register_marketplaces(lifecycle_world)
    scenario = Scenario(lifecycle_world, specs, targets=("copilot",), policy="allow")
    home = {"home": (*PROJECT_HOME_ALLOWED, *_CATALOG_CACHE)}
    scenario.run(f"install {' '.join(specs)}", allowed=home)
    go_offline(lifecycle_world)
    step = scenario.run("install", credit=_marketplace_assignments("install"), allowed=home)
    assert_changes(step.changed, (), label="offline install from marketplaces")
    scenario.run("marketplace list", read_only=True, credit=_marketplace_assignments("marketplace"))
    scenario.assert_deployed()
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        **{
            f"test_converged_project_works_offline[{tgt}]": _converged_assignments(tgt)
            for tgt in TARGETS
        },
        "test_local_project_updates_offline": _update_assignments(),
        "test_parked_then_denied_executables_offline": [
            item for _line, credit in GATE_ROWS for item in credit
        ],
        "test_native_plugin_works_offline": [
            *_assignments("install", "single-unchanged", _NATIVE),
            *_assignments("audit", "single-unchanged", _NATIVE),
        ],
        "test_registry_mcp_and_bundle_sources_work_offline": _source_assignments(),
        "test_marketplace_sources_work_offline": [
            *_marketplace_assignments("install"),
            *_marketplace_assignments("marketplace"),
        ],
    },
)
