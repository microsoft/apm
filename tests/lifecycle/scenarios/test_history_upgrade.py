"""History: upgrade from every supported prior lock generator (#4100).

PROMISE: P-OWN-lockfile-generator-compatibility -- the running CLI replays and
rewrites a lockfile an older (or unstamped) APM generated, and refuses one a
newer APM generated before changing anything (source:
deps/lockfile_generator.py).
PROMISE: P-OWN-legacy-mcp-ownership-key-migration -- the first install after an
upgrade moves APM-written copilot MCP entries out of the retired
``.github/mcp.json`` into ``.mcp.json``; a retired file APM wrote that the
move leaves empty is deleted while Copilot stays selected (#4524) (source:
install/mcp/retired_destinations.py, install/mcp/ownership.py).

Prior generators come from ``tests/fixtures/upgrade_0361/t2``: the consumer
``plural`` (targets claude and copilot, one ``path: ../pkg`` dependency, one
self-defined HTTP MCP server) exactly as APM 0.35.1 and 0.36.0 wrote it,
lockfile_version 1. Two more rows derive from the 0.35.1 lock: no
``apm_version`` (generators before the stamp) and ``apm_version: unknown``
(the sentinel a CLI writes when it cannot read its own version). A newer
generator is the refusal neighbor.

Each row runs through ``driver.run`` with org policy on, applies I1-I9 after
every step, pins the exact changed set of the upgrade and replays it with
the I3 pair and a converged rerun (I2).

Target history (T-6a, #4100): a project changes its declared targets between
two commands. TGT follows ruling #3 (the ``TARGET_CHANGES`` comment in
``tests/lifecycle/generator/factors.py``): the shape is the declared set
BEFORE the step; widened adds at least one target and drops none; narrowed
drops at least one previously declared target and may add others (a switch
counts as narrowed). Each history row deploys at the old targets, changes
``targets:`` in apm.yml, runs one command and credits the scenario-lane pairs
its factor assignment implies (``CREDITS``) through a real-CLI receipt.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from tests.lifecycle.generator.rules import FROZEN_TARGET_CHANGE, FROZEN_TARGET_CHANGE_MESSAGES
from tests.lifecycle.oracles import Evidence, check_obligation
from tests.lifecycle.scenarios._kit import Known, Scenario, assert_changes, credits
from tests.lifecycle.world import use_policy

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
        "P-OWN-lockfile-generator-compatibility",
        "P-OWN-legacy-mcp-ownership-key-migration",
    ),
]

FIXTURES = Path(__file__).parents[2] / "fixtures" / "upgrade_0361" / "t2"
TARGETS = ("claude", "copilot")
DEPLOYED = (
    ".agents/skills/pkg-skill/SKILL.md",
    ".claude/rules/style.md",
    ".claude/skills/pkg-skill/SKILL.md",
    ".github/instructions/style.instructions.md",
)
# Generator label -> (fixture release, apm_version line rewrite or None to keep).
PRIOR = {
    "0.35.1": ("v0.35.1", None),
    "0.36.0": ("v0.36.0", None),
    "unstamped": ("v0.35.1", ""),
    "unknown": ("v0.35.1", "apm_version: unknown\n"),
}
# What the first install after the upgrade writes.
UPGRADE_WRITES = (
    "project:apm.lock.yaml",
    "project:.github/mcp.json",
    "project:.apm-local*",
    "project:apm_modules*",
)


def _check_out(world, release: str, generator_line: str | None) -> None:
    """Replace the world project with the fixture consumer and its sibling package."""
    source = FIXTURES / release
    shutil.rmtree(world.project)
    shutil.copytree(source / "plural", world.project)
    shutil.copytree(source / "pkg", world.project.parent / "pkg")
    if generator_line is not None:
        lock = world.project / "apm.lock.yaml"
        text = lock.read_text(encoding="utf-8")
        text, count = re.subn(r"^apm_version: .*\n", generator_line, text, flags=re.M)
        assert count == 1, "the fixture lock records its generator once"
        lock.write_text(text, encoding="utf-8")


def _servers(path: Path) -> set[str]:
    return set(json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}))


def _start(world, release: str, generator_line: str | None) -> Scenario:
    _check_out(world, release, generator_line)
    use_policy(world, "allow")
    scenario = Scenario(
        world, {"pkg": "../pkg"}, kinds={"pkg": ("other-file",)}, targets=TARGETS, policy="allow"
    )
    scenario.adopt("pkg")
    return scenario


@pytest.mark.parametrize("generator", sorted(PRIOR))
def test_upgrade_from_prior_generator(lifecycle_world, generator) -> None:
    scenario = _start(lifecycle_world, *PRIOR[generator])
    project = lifecycle_world.project
    deployed = {path: (project / path).read_bytes() for path in DEPLOYED}
    assert "srv" in _servers(project / ".github/mcp.json")

    step = scenario.run("install")
    assert_changes(step.changed, UPGRADE_WRITES, label=f"upgrade from {generator}")
    data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert data["apm_version"] == _running_version(scenario)
    assert {path: (project / path).read_bytes() for path in DEPLOYED} == deployed
    assert "srv" in _servers(project / ".mcp.json")
    # The fixture's retired file holds only APM's entry and Copilot stays
    # declared, so the healed, emptied file is deleted (#4524).
    assert not (project / ".github/mcp.json").exists(), "the emptied retired file is deleted"

    scenario.pair()
    rerun = scenario.run("install")
    assert_changes(rerun.changed, (), label="converged install after upgrade")
    scenario.finish()


def test_newer_generator_is_refused_before_any_change(lifecycle_world) -> None:
    scenario = _start(lifecycle_world, "v0.36.0", "apm_version: 99.0.0\n")
    step = scenario.run("install", exit=1)
    assert_changes(step.changed, (), label="newer generator")
    assert "newer than this APM" in step.output, "I7: the refusal names its reason"
    scenario.finish()


def _running_version(scenario: Scenario) -> str:
    result = scenario.run("--version").result
    match = re.search(r"\d+\.\d+\.\d+\S*", result.stdout)
    assert match, result.stdout
    return match.group(0)


# -- target history (T-6a) ----------------------------------------------------------

# TGT value -> (declared targets before the step, after it); ruling #3.
TARGET_HISTORIES = {
    "single-narrowed": (("copilot",), ("claude",)),
    "single-widened": (("copilot",), ("copilot", "claude")),
    "multi-narrowed": (("copilot", "claude"), ("copilot",)),
    # Not cursor: Cursor-native hooks next to Claude's are a designed refusal.
    "multi-widened": (("copilot", "claude"), ("copilot", "claude", "gemini")),
}
# The world context every history row holds: project scope, served sources online,
# files deployed by the previous command and left untouched.
_CONTEXT = {"SCOPE": "project", "NET": "online", "DEPLOYED": "clean"}
# Package -> the units it proves: (SRC, PRIM, GATE) once its executables are approved.
_UNITS = {
    "p-skill:git": (("git", "skill", "none"),),
    "p-child": (("local-path", "skill", "none"),),
    "p-exec": (
        ("local-path", "hook", "allowed"),
        ("local-path", "mcp", "allowed"),
        ("local-path", "bin", "allowed"),
    ),
}
_PACKAGES = tuple(_UNITS)


def _assignments(op: str, tgt: str, packages, **modifiers: str) -> list[dict[str, str]]:
    return [
        {"OP": op, "TGT": tgt, "SRC": src, "PRIM": prim, "GATE": gate, **_CONTEXT, **modifiers}
        for name in packages
        for src, prim, gate in _UNITS[name]
    ]


def _history(world, tgt: str, packages=_PACKAGES) -> Scenario:
    """Deploy *packages* (executables approved) at the old targets, then declare the new ones."""
    before, after = TARGET_HISTORIES[tgt]
    use_policy(world, "allow")
    scenario = Scenario(world, packages, targets=before, policy="allow")
    scenario.run(f"install {' '.join(packages)}")
    if "p-exec" in packages:
        scenario.run("approve p-exec")
        scenario.run("install")
    scenario.assert_deployed()
    scenario.set_targets(*after)
    return scenario


@pytest.mark.parametrize("tgt", sorted(TARGET_HISTORIES))
def test_install_after_a_target_change(lifecycle_world, tgt) -> None:
    scenario = _history(lifecycle_world, tgt)
    scenario.run("install", credit=_assignments("install", tgt, _PACKAGES))
    scenario.assert_deployed()
    scenario.pair()
    rerun = scenario.run("install")
    assert_changes(rerun.changed, (), label=f"converged install after {tgt}")
    scenario.finish()


# Row -> (command after the target change, OP, packages whose units it proves,
# factor overrides). Every row must exit 0: scenario-lane pairs are success pairs.
HISTORY_ROWS = {
    "update": ("update --yes", "update", _PACKAGES, {}),
    "uninstall": ("uninstall p-child", "uninstall", ("p-child",), {}),
    "prune": ("prune", "prune", _PACKAGES, {}),
    "compile": ("compile", "compile", _PACKAGES, {}),
    "lock": ("lock", "lock", _PACKAGES, {}),
    "audit": ("audit --ci", "audit", _PACKAGES, {}),
    "pack": ("pack", "pack", _PACKAGES, {}),
    "deny": ("deny p-exec", "approve-deny", ("p-exec",), {"GATE": "denied-executable"}),
    "install-dry-run": ("install --dry-run", "install", _PACKAGES, {"DRYRUN": "on"}),
    "install-force": ("install --force", "install", _PACKAGES, {"FORCE": "on"}),
    "install-select": ("install p-child", "install", ("p-child",), {"SELECT": "on"}),
    "install-update": ("install --update", "install", _PACKAGES, {"UPDATE": "on"}),
}


def _row_assignments(row: str, tgt: str) -> list[dict[str, str]]:
    _, op, packages, overrides = HISTORY_ROWS[row]
    return [{**item, **overrides} for item in _assignments(op, tgt, packages)]


# uninstall deploys the remaining packages to a newly declared target without
# recording the files in apm.lock.yaml (I1); strict until #4636 is fixed.
_UNINSTALL_UNRECORDED = Known("#4636", r"^deployed file is not in the lock: ")
KNOWN_ROWS = {
    ("uninstall", tgt): {"I1": _UNINSTALL_UNRECORDED}
    for tgt in ("single-narrowed", "single-widened")
}
# On multi-widened the same path shows #4636 as a repeated "Skipped .gemini/commands/"
# hint (I9), which the strict output cell uninstall-new-target-skipped-hint-repeats
# owns (tests/utils/lifecycle_evidence.py); the row credits nothing until it is fixed.
UNCREDITED_ROWS = {*KNOWN_ROWS, ("uninstall", "multi-widened")}


@pytest.mark.parametrize("tgt", sorted(TARGET_HISTORIES))
@pytest.mark.parametrize("row", sorted(HISTORY_ROWS))
def test_command_after_a_target_change(lifecycle_world, row, tgt) -> None:
    scenario = _history(lifecycle_world, tgt)
    line = HISTORY_ROWS[row][0]
    credit = () if (row, tgt) in UNCREDITED_ROWS else _row_assignments(row, tgt)
    scenario.run(line, credit=credit, known=KNOWN_ROWS.get((row, tgt)))
    scenario.finish()


# --frozen refuses any declared-target change before it changes anything (U11): a
# dropped target (#4517) and an added one. This row witnesses the refusal cell U11
# maps every changed-TGT x FROZEN pair to, and is the detecting gate of that
# designed refusal (H5): if frozen ever accepts a target change, the cell fails.


# A widened refusal leaves merged hook config behind (I7); strict until #4638.
_FROZEN_LEFT_HOOK_CONFIG = Known(
    "#4638", r"^failed install-frozen changed durable files: .*apm-hooks\.json"
)


@pytest.mark.parametrize("tgt", sorted(TARGET_HISTORIES))
def test_frozen_refuses_a_target_change(lifecycle_world, tgt) -> None:
    scenario = _history(lifecycle_world, tgt)
    narrowed, widened = FROZEN_TARGET_CHANGE_MESSAGES
    if tgt.endswith("widened"):
        step = scenario.run("install --frozen", exit=1, known={"I7": _FROZEN_LEFT_HOOK_CONFIG})
        assert re.search(widened, " ".join(step.output.split())), step.output
        scenario.finish()
        return
    step = scenario.run("install --frozen", exit=1)
    assert_changes(step.changed, (), label=f"frozen after {tgt}")
    check_obligation(
        FROZEN_TARGET_CHANGE,
        strength="e2e-evidence",
        before=step.before,
        after=step.after,
        result=step.result,
        evidence=Evidence(
            exit=1,
            output_matches=(narrowed,),
            unchanged={root: ("*",) for root in scenario.snapshot_roots()},
        ),
    )
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        **{
            f"test_install_after_a_target_change[{tgt}]": _assignments("install", tgt, _PACKAGES)
            for tgt in TARGET_HISTORIES
        },
        **{
            f"test_command_after_a_target_change[{tgt}-{row}]": _row_assignments(row, tgt)
            for row in HISTORY_ROWS
            for tgt in TARGET_HISTORIES
            if (row, tgt) not in UNCREDITED_ROWS
        },
    },
)
