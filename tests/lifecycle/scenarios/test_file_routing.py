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

import zipfile
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
        "P-OWN-claude-agents-md-instruction-routing",
    ),
]

MARKER = "Route marker for the lifecycle file-routing lane."
# A file-type glob keeps a per-file rule on every deployed target; Claude routes
# '**' instructions to AGENTS.md instead (#3939).
_INSTRUCTION = f"---\napplyTo: '**/*.py'\ndescription: File routing marker.\n---\n\n{MARKER}\n"
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


def _assignments(op: str, tgt: str, route: str, prims=_FILE_PRIMS) -> list[dict[str, str]]:
    return [{"OP": op, "TGT": tgt, "PRIM": prim, "FILEROUTE": route} for prim in prims]


# pack leaves a root skills/ out of a bundle whose package has .apm/ (#4709): a
# bundle install proves only the instruction's route until that is fixed.
_BUNDLE_PRIMS = ("other-file",)


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


def _instruction_root(name: str) -> str:
    target = KNOWN_TARGETS[name]
    return f"{target.root_dir}/{target.primitives['instructions'].subdir}/"


def _assert_route(
    scenario: Scenario,
    route: str,
    *,
    compiled: bool = False,
    targets: tuple[str, ...] = (),
    dropped: tuple[str, ...] = (),
) -> None:
    """The instruction follows its route; a deployed one sits under every current
    target's instruction root and under no dropped target's."""
    marked = _marked(scenario.world.project)
    staged = [
        path.relative_to(scenario.world.project).as_posix()
        for path in (scenario.world.project / "apm_modules").rglob("*.instructions.md")
    ]
    if route == "deployed":
        assert marked, "a deployed instruction is a target file carrying the marker"
        assert not any(name.endswith(("AGENTS.md", "GEMINI.md")) for name in marked), marked
        for name in targets:
            root = _instruction_root(name)
            assert any(path.startswith(root) for path in marked), (name, root, sorted(marked))
        for name in dropped:
            root = _instruction_root(name)
            assert not any(path.startswith(root) for path in marked), (name, root, sorted(marked))
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
    dropped = tuple(name for name in before if name not in after)
    _assert_route(scenario, route, targets=after, dropped=dropped)
    scenario.finish()


# Ops on one project per route (single target): each row exits 0 and keeps the route.
_SINGLE = "single-unchanged"


def _op_credits(route: str) -> list[dict[str, str]]:
    return [
        item
        for op in ("install", "compile", "update", "prune", "uninstall")
        for item in _assignments(op, _SINGLE, route)
    ] + _assignments("bundle-install", _SINGLE, route, _BUNDLE_PRIMS)


@pytest.mark.parametrize("route", sorted(HISTORIES))
def test_operations_keep_the_file_route(lifecycle_world, route) -> None:
    targets = HISTORIES[route][_SINGLE][0]
    use_policy(lifecycle_world, "allow")
    specs = {name: str(_author(lifecycle_world, name)) for name in ("f-doc", "f-two")}
    kinds = {name: ["skill"] for name in specs}
    scenario = Scenario(lifecycle_world, specs, kinds=kinds, targets=targets, policy="allow")
    scenario.run("install f-doc f-two", credit=_assignments("install", _SINGLE, route))
    _assert_route(scenario, route, targets=targets)
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
    scenario.run(
        ("install", str(bundle)),
        credit=_assignments("bundle-install", _SINGLE, route, _BUNDLE_PRIMS),
    )
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


def test_bundle_ships_root_skills_with_apm_dir(lifecycle_world) -> None:
    """A bundle of a package with .apm/ still ships its root skills (#4709)."""
    root = _author(lifecycle_world, "f-bun")
    manifest = root / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"] = {"apm": []}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    bundle = _pack_bundle(root, lifecycle_world.root / "skills-bundle", lifecycle_world.env)
    assert bundle.is_file(), bundle
    names = {name.split("/", 1)[1] for name in zipfile.ZipFile(bundle).namelist()}
    assert "instructions/f-bun.instructions.md" in names, sorted(names)
    assert "skills/f-bun/SKILL.md" in names, f"the bundle drops the root skill: {sorted(names)}"


_ALWAYS_ON = "Always-on marker for the Claude AGENTS.md route."


def test_claude_routes_an_always_on_instruction_to_agents_md(lifecycle_world) -> None:
    """#4005: Claude keeps only path-scoped rules in .claude/rules; ``apm compile``
    writes an always-on ('**') instruction to AGENTS.md and a CLAUDE.md importing it.
    The package's skill and its path-scoped instruction stay deployed across compile."""
    use_policy(lifecycle_world, "allow")
    root = skill_package(LocalPackageFactory(lifecycle_world.root / "route-always"), "f-all")
    instruction = root / ".apm" / "instructions" / "f-all.instructions.md"
    instruction.parent.mkdir(parents=True, exist_ok=True)
    instruction.write_text(
        f"---\napplyTo: '**'\ndescription: Always on.\n---\n\n{_ALWAYS_ON}\n", encoding="ascii"
    )
    scoped = instruction.with_name("f-all-py.instructions.md")
    scoped.write_text(_INSTRUCTION, encoding="ascii")
    scenario = Scenario(
        lifecycle_world,
        {"f-all": str(root)},
        kinds={"f-all": ["skill"]},
        targets=("claude",),
        policy="allow",
    )
    step = scenario.run("install f-all")
    project = lifecycle_world.project
    ruled = [p for p in (project / ".claude").rglob("*.md") if _ALWAYS_ON in p.read_text("utf-8")]
    assert not ruled, f"an always-on instruction became a Claude rule: {ruled}"
    assert "apm compile" in step.output, step.output
    scenario.assert_deployed()
    _assert_route(scenario, "deployed", targets=("claude",))
    compiled = scenario.run("compile", credit=_assignments("compile", _SINGLE, "deployed"))
    assert _ALWAYS_ON in (project / "AGENTS.md").read_text(encoding="utf-8"), compiled.output
    claude_md = (project / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    assert "@AGENTS.md" in claude_md, claude_md
    scenario.assert_deployed()
    _assert_route(scenario, "deployed", targets=("claude",))
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        "test_claude_routes_an_always_on_instruction_to_agents_md": _assignments(
            "compile", _SINGLE, "deployed"
        ),
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
