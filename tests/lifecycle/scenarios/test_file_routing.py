"""File routing: deployed vs compile-staged file primitives across ops and targets (#4102).

PROMISE: P-OWN-native-instruction-projection -- an instruction deploys to a
target that has native instructions; skills deploy either way (rule E3 in
tests/lifecycle/generator/rules.py).
PROMISE: P-OWN-local-bundle-compile-staging -- a target without native
instructions gets them staged for ``apm compile``, which renders them into
its context file (source: install/services.py ``_staged = ...
"instructions" not in target.primitives``).
PROMISE: P-OWN-compiled-output-writes -- ``apm compile`` writes the context
file a staged route needs.

FILEROUTE (T-6c): ``deployed`` targets (copilot, claude, cursor) declare the
``instructions`` primitive; ``compile-staged`` targets (codex, gemini,
opencode) do not (``KNOWN_TARGETS[...].primitives``). Each row authors one
package with a skill and an instruction, runs real CLI commands through the
scenario kit with org policy on (I1-I9 after every step) and checks the
route: a deployed instruction is a target file carrying the marker; a staged
one exists only under ``apm_modules`` until ``apm compile`` renders it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.integration.targets import KNOWN_TARGETS
from tests.lifecycle.scenarios._kit import Scenario, credits, skill_package
from tests.lifecycle.world import use_policy
from tests.lifecycle.world.builder import _pack_bundle
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I5",
        "P-INV-I7",
        "P-OWN-native-instruction-projection",
        "P-OWN-local-bundle-compile-staging",
        "P-OWN-compiled-output-writes",
    ),
]

MARKER = "Route marker for the lifecycle file-routing lane."
_INSTRUCTION = f"---\napplyTo: '**'\ndescription: File routing marker.\n---\n\n{MARKER}\n"
# Route -> TGT -> (declared targets before the step, after it). TGT follows ruling #3
# (tests/lifecycle/generator/factors.py TARGET_CHANGES).
HISTORIES = {
    "deployed": {
        "single-unchanged": (("copilot",), ("copilot",)),
        "multi-unchanged": (("copilot", "claude"), ("copilot", "claude")),
        "single-widened": (("copilot",), ("copilot", "claude")),
        "single-narrowed": (("copilot",), ("claude",)),
        "multi-narrowed": (("copilot", "claude"), ("copilot",)),
        "multi-widened": (("copilot", "claude"), ("copilot", "claude", "cursor")),
    },
    "compile-staged": {
        "single-unchanged": (("codex",), ("codex",)),
        "multi-unchanged": (("codex", "gemini"), ("codex", "gemini")),
        "single-widened": (("codex",), ("codex", "gemini")),
        "single-narrowed": (("codex",), ("gemini",)),
        "multi-narrowed": (("codex", "gemini"), ("codex",)),
        "multi-widened": (("codex", "gemini"), ("codex", "gemini", "opencode")),
    },
}
_FILE_PRIMS = ("skill", "other-file")


def _route_of(targets) -> str:
    routes = {
        "deployed" if "instructions" in KNOWN_TARGETS[name].primitives else "compile-staged"
        for name in targets
    }
    assert len(routes) == 1, f"{targets} mix routes"
    return routes.pop()


def _assignments(op: str, tgt: str, route: str) -> list[dict[str, str]]:
    return [{"OP": op, "TGT": tgt, "PRIM": prim, "FILEROUTE": route} for prim in _FILE_PRIMS]


def _author(world, name: str) -> Path:
    """A local package with skill *name* and one instruction carrying MARKER."""
    root = skill_package(LocalPackageFactory(world.root / f"route-{name}"), name)
    instruction = root / ".apm" / "instructions" / f"{name}.instructions.md"
    instruction.parent.mkdir(parents=True, exist_ok=True)
    instruction.write_text(_INSTRUCTION, encoding="ascii")
    return root


def _marked(root: Path) -> set[str]:
    """Project files outside apm_modules that carry the instruction MARKER."""
    found = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or relative.startswith(("apm_modules/", ".git/")):
            continue
        if MARKER in path.read_text(encoding="utf-8", errors="replace"):
            found.add(relative)
    return found


def _assert_route(scenario: Scenario, route: str, *, compiled: bool = False) -> None:
    marked = _marked(scenario.world.project)
    staged = [
        path.relative_to(scenario.world.project).as_posix()
        for path in (scenario.world.project / "apm_modules").rglob("*.instructions.md")
    ]
    if route == "deployed":
        assert marked, "a deployed instruction is a target file carrying the marker"
        assert not any(name.endswith(("AGENTS.md", "GEMINI.md")) for name in marked), marked
        return
    assert staged, "a compile-staged instruction stays under apm_modules"
    if compiled:
        assert marked and all(name.endswith(("AGENTS.md", "GEMINI.md")) for name in marked), marked
    else:
        assert not marked, f"a staged instruction deployed before compile: {sorted(marked)}"


@pytest.mark.parametrize("route", sorted(HISTORIES))
@pytest.mark.parametrize("tgt", sorted(HISTORIES["deployed"]))
def test_install_routes_file_primitives(lifecycle_world, route, tgt) -> None:
    before, after = HISTORIES[route][tgt]
    assert _route_of(before) == _route_of(after) == route
    use_policy(lifecycle_world, "allow")
    spec = str(_author(lifecycle_world, "f-doc"))
    scenario = Scenario(
        lifecycle_world, {"f-doc": spec}, kinds={"f-doc": ["skill"]}, targets=before, policy="allow"
    )
    scenario.run("install f-doc")
    if after != before:
        scenario.set_targets(*after)
    scenario.run("install", credit=_assignments("install", tgt, route))
    scenario.assert_deployed()
    _assert_route(scenario, route)
    scenario.finish()


# Ops on one project per route (single target): each row exits 0 and keeps the route.
_SINGLE = "single-unchanged"


def _op_credits(route: str) -> list[dict[str, str]]:
    return [
        item
        for op in ("install", "compile", "update", "prune", "uninstall", "bundle-install")
        for item in _assignments(op, _SINGLE, route)
    ]


@pytest.mark.parametrize("route", sorted(HISTORIES))
def test_operations_keep_the_file_route(lifecycle_world, route) -> None:
    targets = HISTORIES[route][_SINGLE][0]
    use_policy(lifecycle_world, "allow")
    specs = {name: str(_author(lifecycle_world, name)) for name in ("f-doc", "f-two")}
    kinds = {name: ["skill"] for name in specs}
    scenario = Scenario(lifecycle_world, specs, kinds=kinds, targets=targets, policy="allow")
    scenario.run("install f-doc f-two", credit=_assignments("install", _SINGLE, route))
    _assert_route(scenario, route)
    scenario.run("compile", credit=_assignments("compile", _SINGLE, route))
    _assert_route(scenario, route, compiled=True)
    scenario.run("update --yes", credit=_assignments("update", _SINGLE, route))
    scenario.drop_from_manifest("f-two")
    scenario.run("prune", credit=_assignments("prune", _SINGLE, route))
    scenario.run("uninstall f-doc", credit=_assignments("uninstall", _SINGLE, route))
    scenario.assert_deployed()

    bundle_root = _author(lifecycle_world, "f-bun")
    manifest = bundle_root / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"] = {"apm": []}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    bundle = _pack_bundle(bundle_root, lifecycle_world.root / "route-bundle", lifecycle_world.env)
    scenario.source_moved()
    scenario.run(("install", str(bundle)), credit=_assignments("bundle-install", _SINGLE, route))
    # A staged route already compiled once in this project: AGENTS.md may carry the marker.
    _assert_route(scenario, route, compiled=True)
    scenario.finish()


def test_uninstall_keeps_a_bundle_owned_instruction(lifecycle_world) -> None:
    """Required cell uninstall-keeps-a-bundle-owned-instruction (#4703).

    Uninstalling a path dependency keeps the instruction a local bundle still
    owns, so the lock stays equal to disk (I1), and never reports a package
    failure while exiting 0 (I8).
    """
    use_policy(lifecycle_world, "allow")
    bundle_root = lifecycle_world.root / "route-only" / "f-doc"
    instruction = bundle_root / ".apm" / "instructions" / "f-doc.instructions.md"
    instruction.parent.mkdir(parents=True)
    instruction.write_text(_INSTRUCTION, encoding="ascii")
    (bundle_root / "apm.yml").write_text(
        "name: f-doc\nversion: 1.0.0\ndependencies:\n  apm: []\n", encoding="ascii"
    )
    bundle = _pack_bundle(bundle_root, lifecycle_world.root / "only-bundle", lifecycle_world.env)
    spec = str(_author(lifecycle_world, "f-doc"))
    scenario = Scenario(
        lifecycle_world,
        {"f-doc": spec},
        kinds={"f-doc": ["skill"]},
        targets=("copilot",),
        policy="allow",
    )
    scenario.run(("install", str(bundle)))
    scenario.source_moved()
    scenario.run("install f-doc")
    step = scenario.run("uninstall f-doc")
    shared = scenario.world.project / ".github" / "instructions" / "f-doc.instructions.md"
    assert shared.is_file(), step.output
    assert step.result.returncode != 0 or "[x]" not in step.output, step.output
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        **{
            f"test_install_routes_file_primitives[{tgt}-{route}]": _assignments(
                "install", tgt, route
            )
            for route, histories in HISTORIES.items()
            for tgt in histories
        },
        **{
            f"test_operations_keep_the_file_route[{route}]": _op_credits(route)
            for route in HISTORIES
        },
    },
)
