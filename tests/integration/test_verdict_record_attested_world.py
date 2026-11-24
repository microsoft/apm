"""Attested record over the real CLI entrypoint (component: the engine under test,
not a packaged binary): a converged install lets the next unchanged one skip the proof.

PROMISE: P-INV-I2 -- repeating a converged install changes no bytes and prints
the same no-op, whichever engine answers (W3d attested-record ruling A8).

The real CLI over the tree-10 offline world: install #1 attests the state it
committed; install #2 matches that attestation (no planning, no staging) and
prints exactly what the full proof prints for the same state; a deployed file
edited after install #1 makes install #2 prove its verdict itself.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import apm_cli
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

pytestmark = [
    pytest.mark.component,
    pytest.mark.integration,
    pytest.mark.promise("P-INV-I2"),
]

_NO_HIT = ("CI", "GITHUB_ACTIONS", "TF_BUILD", "BUILD_ID", "GITLAB_CI", "BUILDKITE")
_TRACE = "[i] verdict record: "


# The console script's own entry (pyproject: apm = apm_cli.entrypoint:main): the
# record wraps the CLI there; ``python -m apm_cli.cli`` would bypass it.
_ENTRY = (sys.executable, "-c", "import sys; from apm_cli.entrypoint import main; sys.exit(main())")


@pytest.fixture
def world_install():
    """``install(*args)`` in a fresh tree-10 project; returns (code, stdout, stderr)."""
    work = Path(tempfile.mkdtemp(prefix="apm-attest-"))
    try:
        tree = build_tree(TREE_RECIPES["tree-10"], work / "tree")
        with serve_offline(tree, work / "world") as world:
            project = work / "project"
            shutil.copytree(tree.project, project)
            env = {k: v for k, v in world.env.items() if k not in _NO_HIT}
            env["APM_INSTALL_FAST_PATH"] = "verdict"
            # The engine under test: the apm_cli this test imports, not another install.
            env["PYTHONPATH"] = str(Path(apm_cli.__file__).resolve().parents[1])

            def install(*args: str) -> tuple[int, str, str]:
                done = subprocess.run(
                    [*_ENTRY, "install", *args],
                    cwd=project,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                return done.returncode, done.stdout, done.stderr

            yield install, project, world
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _traces(stderr: str) -> list[str]:
    return [line[len(_TRACE) :] for line in stderr.splitlines() if line.startswith(_TRACE)]


def _normalized(stdout: str) -> str:
    return re.sub(r"\d+\.\d+s", "<t>s", stdout)


def _records(world) -> list[Path]:
    return sorted((Path(world.apm_home) / "verdicts").rglob("*.json"))


def test_the_install_after_a_converged_install_matches_its_attestation(world_install) -> None:
    install, _project, world = world_install
    first = install()
    _assert_attested(first)

    second = install()
    _assert_attested_hit(second)

    # The same state, proved by the engine itself: the attested hit printed the same.
    for record in _records(world):
        record.unlink()
    proved = install()
    _assert_same_no_op(second, proved)


def test_a_deployed_file_edited_after_the_attested_install_is_proved(world_install) -> None:
    install, project, _world = world_install
    _assert_attested(install())
    deployed = project / _a_deployed_file(project)
    deployed.write_text(deployed.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    second = install()

    _assert_not_attested_hit(second)


def _a_deployed_file(project: Path) -> str:
    import yaml

    lock = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    rels = sorted(
        rel
        for dep in lock.get("dependencies") or []
        for rel in dep.get("deployed_file_hashes") or {}
    )
    assert rels, "the tree-10 lock records deployed files"
    return rels[0]


def _assert_attested(result: tuple[int, str, str]) -> None:
    code, stdout, stderr = result
    assert code == 0, stdout + stderr
    assert "attested" in _traces(stderr), stderr


def _assert_attested_hit(result: tuple[int, str, str]) -> None:
    code, stdout, stderr = result
    assert code == 0, stdout + stderr
    assert "attested hit" in _traces(stderr), stderr
    assert "up to date" in stdout, stdout


def _assert_not_attested_hit(result: tuple[int, str, str]) -> None:
    code, stdout, stderr = result
    assert code == 0, stdout + stderr
    assert "attested hit" not in _traces(stderr), stderr


def _assert_same_no_op(attested, proved) -> None:
    assert proved[0] == attested[0] == 0
    assert "attested hit" not in _traces(proved[2]), proved[2]
    assert _normalized(attested[1]) == _normalized(proved[1])
