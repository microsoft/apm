"""Pack ships what the executable gate deployed, and a parked canvas does not block it (#4714).

PROMISE: P-INV-I4 -- pack never ships an executable the gate keeps from
deploying, and a parked executable never blocks packing what was deployed
(source: tests/lifecycle/oracles.py, docs/src/content/docs/reference/cli/pack.md).
PROMISE: P-INV-I8 -- every hint pack prints reaches its stated outcome when run.

CELL: OP=pack SRC=git PRIM=canvas SCOPE=project
      GATE={unapproved-exec, denied-executable, revoked-after-deploy}

Each row parks the canvas of a git package at one gate level, then runs
``pack --dry-run``: it exits 0, lists none of the canvas files, and prints one
``[!]`` line naming the parked canvas and its remedy. The row then runs every
command that line (and any line it leads to) prints, and the final pack
lists the canvas: the hints reach their stated outcome. The T1 generated
cells are credited by the M-GATE pack spine (#4700), not here.
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

import pytest

from tests.lifecycle.scenarios._kit import Scenario, serve_repository
from tests.lifecycle.world import use_policy
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I4", "P-INV-I8"),
]

CANVAS = "o-canvas"
SLUG = f"lifecycle/{CANVAS}"
_FILES = (f"extensions/{CANVAS}/extension.mjs", f"extensions/{CANVAS}/icon.txt")
_ARRANGE = {
    "unapproved-exec": (),
    "denied-executable": (f"deny {CANVAS}", "install"),
    "revoked-after-deploy": (f"approve {CANVAS}", "install", f"deny {CANVAS}", "install"),
}


def _served_canvas(world) -> str:
    """Publish ``lifecycle/o-canvas`` (a two-file canvas) and return its spec."""
    factory = LocalPackageFactory(world.root / "owned")
    package = factory.create(CANVAS, version="1.0.0")
    extension = "export default function render() {\n  return 'o-canvas';\n}\n"
    factory.add_canvas(package, CANVAS, extension, assets={PurePosixPath("icon.txt"): b"c\n"})
    repos = LocalGitRepositoryFactory(world.root / "repos", env=world.env)
    repository = repos.create(CANVAS, source_tree=package.root)
    repos.tag(repository, "v1.0.0", repos.commit(repository, message=f"{CANVAS} v1.0.0"))
    serve_repository(world, SLUG, repository.origin)
    return f"{SLUG}#v1.0.0"


def _pack(scenario: Scenario):
    return scenario.run("pack --dry-run", exit=0, read_only=True)


def _hint_lines(step) -> list[str]:
    """Output lines that name the parked canvas of ``o-canvas``."""
    text = f"{step.result.stdout}\n{step.result.stderr}"
    return [line for line in text.splitlines() if SLUG in line and "canvas" in line.lower()]


def _commands(line: str) -> list[tuple[str, ...]]:
    """Every quoted ``apm ...`` command in *line*, as argv without ``apm``."""
    return [tuple(shlex.split(found))[1:] for found in re.findall(r"'(apm [^']+)'", line)]


def _run_hint(scenario: Scenario, argv: tuple[str, ...]):
    """Run one printed hint verbatim; the model reads its package as ``o-canvas``."""
    as_model = scenario._args(tuple(CANVAS if arg == SLUG else arg for arg in argv))
    read_only = argv[:2] == ("policy", "explain")
    return scenario.run(argv, exit=0, read_only=read_only, requested=scenario.requested(as_model))


@pytest.mark.parametrize("gate", sorted(_ARRANGE))
def test_pack_omits_a_parked_canvas_and_its_hint_restores_it(lifecycle_world, gate) -> None:
    use_policy(lifecycle_world, "allow")
    spec = _served_canvas(lifecycle_world)
    scenario = Scenario(
        lifecycle_world, {CANVAS: spec}, kinds={CANVAS: ("canvas",)}, policy="allow"
    )
    scenario.run(f"install {CANVAS}")
    for line in _ARRANGE[gate]:
        scenario.run(line)
    scenario.assert_deployed()
    assert not (lifecycle_world.project / ".github/extensions" / CANVAS).exists()

    packed = _pack(scenario)
    for path in _FILES:
        assert path not in packed.output, f"{gate}: pack lists the parked {path}"
    hints = _hint_lines(packed)
    assert len(hints) == 1, f"{gate}: want one line naming the parked canvas: {hints}"
    (hint,) = hints
    assert hint.lstrip().startswith("[!]"), hint
    assert "'apm install' to record provenance" not in packed.output

    # Every printed hint is a test case: follow them until the canvas packs.
    queue = _commands(hint)
    assert queue, f"{gate}: the hint names no command: {hint}"
    while queue:
        argv = queue.pop(0)
        step = _run_hint(scenario, argv)
        if argv[:2] == ("policy", "explain"):
            # explain names the deny and the command that lifts it.
            lift = [c for c in _commands(step.output) if c[:1] == ("approve",)]
            assert lift, f"{gate}: explain names no way to lift the deny:\n{step.output}"
            queue.extend((lift[0], ("install",)))
    scenario.assert_deployed()
    restored = _pack(scenario)
    for path in _FILES:
        assert path in restored.output, f"{gate}: the approved canvas is not packed: {path}"
    assert not _hint_lines(restored)
    scenario.finish()
