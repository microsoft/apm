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

import os
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
def world_install(request):
    """``install(*args)`` in a fresh tree-10 project; returns (code, stdout, stderr)."""
    work = Path(tempfile.mkdtemp(prefix="apm-attest-"))
    try:
        recipe = getattr(request, "param", "tree-10")
        tree = build_tree(TREE_RECIPES[recipe], work / "tree")
        with serve_offline(tree, work / "world") as world:
            project = work / "project"
            shutil.copytree(tree.project, project)
            env = {k: v for k, v in world.env.items() if k not in _NO_HIT}
            env["APM_INSTALL_FAST_PATH"] = "verdict"
            # The engine under test: the apm_cli this test imports, not another install.
            env["PYTHONPATH"] = str(Path(apm_cli.__file__).resolve().parents[1])

            def install(
                *args: str, pythonpath: str = "", fast: str = "verdict"
            ) -> tuple[int, str, str]:
                run_env = dict(env, APM_INSTALL_FAST_PATH=fast)
                if pythonpath:
                    run_env["PYTHONPATH"] = os.pathsep.join((pythonpath, env["PYTHONPATH"]))
                done = subprocess.run(
                    [*_ENTRY, "install", *args],
                    cwd=project,
                    env=run_env,
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


@pytest.mark.parametrize("world_install", ["tree-10-claude"], indirect=True)
def test_a_valid_attestation_never_skips_the_claude_carried_rules_refusal(world_install) -> None:
    """#5394: with claude targeted and a root AGENTS.md that may carry its rules, the
    fast path declines; an attestation install #1 wrote must not take precedence."""
    install, project, _world = world_install
    (project / "AGENTS.md").write_text("# Project notes\n", encoding="utf-8")
    _assert_attested(install())

    second = install()

    _assert_full_pipeline_ran(second)


@pytest.mark.parametrize("world_install", ["tree-10-claude"], indirect=True)
def test_a_root_agents_md_after_a_replay_record_misses_and_the_full_run_decides(
    world_install,
) -> None:
    """#5434: the replayed no-op was recorded with no carrier; a root AGENTS.md that
    appears later can decide Claude rules, so the next install is not a replay and
    prints what the full pipeline prints for that state."""
    install, project, _world = world_install
    _assert_attested(install())
    _assert_attested_hit(install())
    (project / "AGENTS.md").write_text("# Project notes\n", encoding="utf-8")
    before = _tree_bytes(project)

    after, full = install(), install(fast="0")

    assert after[0] == 0, after[1] + after[2]
    assert "hit" not in _traces(after[2]), after[2]
    assert "attested hit" not in _traces(after[2]), after[2]
    assert full[0] == 0 and not _traces(full[2]), full[1] + full[2]
    assert _normalized(after[1]) == _normalized(full[1])
    assert _tree_bytes(project) == before


def _assert_full_pipeline_ran(result: tuple[int, str, str]) -> None:
    """The record matched ("attested hit" is the check, not the outcome), yet the fast
    path declined: the full pipeline re-attested and no attested commit recorded output."""
    code, stdout, stderr = result
    assert code == 0, stdout + stderr
    assert "attested" in _traces(stderr), stderr
    assert "recorded" not in _traces(stderr), stderr


def test_every_fast_answer_prints_what_the_full_pipeline_prints(world_install) -> None:
    """#4770 differential: the attested hit and the output replay print what the full
    pipeline (every fast path off) prints for the same state, and no answer moves a byte."""
    install, project, _world = world_install
    _assert_attested(install())
    before = _tree_bytes(project)

    attested, replayed, full = install(), install(), install(fast="0")

    _assert_attested_hit(attested)
    assert "hit" in _traces(replayed[2]), replayed[2]
    assert full[0] == 0 and not _traces(full[2]), full[1] + full[2]
    assert _normalized(attested[1]) == _normalized(replayed[1]) == _normalized(full[1])
    assert _tree_bytes(project) == before


def test_a_frozen_install_that_retains_an_orphan_never_attests(world_install) -> None:
    """Copilot #5383: --frozen keeps a dependency apm.yml dropped and says so; that state
    is never attested, so the next frozen install re-judges it and says so again."""
    install, project, _world = world_install
    _assert_attested(install())
    dropped = _drop_a_dependency(project)

    retained, again = install("--frozen"), install("--frozen")

    assert retained[0] == again[0] == 0, retained[1] + retained[2]
    assert "attested" not in _traces(retained[2]), retained[2]
    assert "attested hit" not in _traces(again[2]), again[2]
    _assert_names(dropped, retained[1], again[1])


def _drop_a_dependency(project: Path) -> str:
    import yaml

    manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
    dropped = manifest["dependencies"]["apm"].pop()  # the last: no MCP server to restore
    (project / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return str(dropped).rsplit("/", 1)[-1].split("#", 1)[0]


def _assert_names(name: str, *outputs: str) -> None:
    for output in outputs:
        assert name in output, output


def test_the_first_frozen_install_after_an_install_is_an_attested_hit(world_install) -> None:
    """#4770 (CI's path): a plain install's attestation serves the first --frozen; the
    frozen hit records nothing (W3d AF5), so the next --frozen is served again."""
    install, _project, _world = world_install
    _assert_attested(install())

    first, second = install("--frozen"), install("--frozen")

    for run in (first, second):
        _assert_attested_hit(run)
        assert "recorded" not in _traces(run[2]), run[2]


@pytest.mark.parametrize(
    "scenario",
    ["converged", "manifest drift", "lock-only edge", "local dependency", "only apm"],
)
def test_a_frozen_install_answers_as_the_full_frozen_pipeline(world_install, scenario) -> None:
    """W3d AF4: whatever the fast path decides, --frozen prints, exits and leaves the
    lock exactly as the full frozen pipeline (every fast path off) does."""
    install, project, _world = world_install
    if scenario == "local dependency":
        _add_local_dependency(project)
        first = install()
        assert first[0] == 0 and "attested" not in _traces(first[2]), first[2]  # unprovable
    elif scenario == "only apm":
        first = install("--only", "apm")  # argv-exact: never served, never attested
        assert first[0] == 0 and not _traces(first[2]), first[2]
    else:
        _assert_attested(install())
    if scenario == "manifest drift":
        _declare(project, "perf-org/not-locked#v1.0.0")
    elif scenario == "lock-only edge":
        _drop_a_dependency(project)
    lock = project / "apm.lock.yaml"
    before = (lock.read_bytes(), lock.stat().st_mtime_ns)

    fast, full = install("--frozen"), install("--frozen", fast="0")

    _assert_same_frozen_answer(fast, full)
    if scenario == "converged":  # the hit itself answered, its declared MCP gate live
        _assert_attested_hit(fast)
        assert "MCP 'pkg-000-mcp'" in fast[1], fast[1]
    else:
        assert "attested hit" not in _traces(fast[2]), fast[2]
    assert (lock.read_bytes(), lock.stat().st_mtime_ns) == before, "--frozen rewrote the lock"


@pytest.mark.parametrize(("args", "shown"), [((), 0), (("--frozen",), 1)])
def test_a_full_run_prints_the_direct_mcp_trust_notice_as_main_does(
    world_install, args, shown
) -> None:
    """W3d #5493 N1': only the frozen preflight's own tape defers the once-per-command
    trust notice. A silent pre-pass (a permissive logger) still marks it shown, so a full
    run prints it as main does: never on a plain no-op, once on --frozen."""
    install, _project, _world = world_install
    _assert_attested(install())

    code, stdout, stderr = install(*args, fast="0")

    assert code == 0, stdout + stderr
    assert stdout.count("Trusting direct dependency MCP 'pkg-000-mcp'") == shown, stdout


def test_the_speculative_frozen_preflight_never_migrates_personal_consent(
    world_install,
) -> None:
    """Copilot #5423: the fast path's frozen preflight (verdict_attest.refuse_frozen_drift)
    reads a legacy approvals.yml and never migrates it; the full install owns that write."""
    install, project, world = world_install
    _assert_attested(install())
    home = Path(world.apm_home)
    legacy = home / "approvals.yml"
    legacy.write_text("perf-org/pkg-000: {hooks: true}\n", encoding="utf-8")
    config = home / "config.json"
    before = (legacy.read_bytes(), config.read_bytes() if config.exists() else None)
    code = "from pathlib import Path; from apm_cli.install import verdict_attest as v; "
    code += "v.refuse_frozen_drift(Path.cwd())"

    done = subprocess.run(
        [sys.executable, "-c", code], cwd=project, env=world_env(world), capture_output=True
    )

    assert done.returncode == 0, done.stderr
    _assert_consent_untouched(legacy, config, before)


def world_env(world) -> dict:
    env = {k: v for k, v in world.env.items() if k not in _NO_HIT}
    env["PYTHONPATH"] = str(Path(apm_cli.__file__).resolve().parents[1])
    return env


def _assert_consent_untouched(legacy: Path, config: Path, before) -> None:
    after = (
        legacy.read_bytes() if legacy.exists() else None,
        config.read_bytes() if config.exists() else None,
    )
    assert after == before, "a speculative frozen preflight migrated personal consent"


def test_the_frozen_preflight_decides_before_any_fast_decision(world_install) -> None:
    """Copilot #5423: an MCP server apm.yml declares but the lock does not record is a
    frozen refusal the preflight owner makes before any fast decision (decide mode
    stops before the render, so no later phase could catch it)."""
    import yaml

    install, project, _world = world_install
    _assert_attested(install())
    manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
    server = {"name": "root-mcp", "registry": False, "transport": "stdio", "command": "true"}
    manifest["dependencies"]["mcp"] = [server]
    (project / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    before = (project / "apm.lock.yaml").read_bytes()

    code, _stdout, stderr = install("--frozen", fast="decide")

    assert "install fast path: full run (frozen preflight)" in stderr, stderr
    assert code != 0 and (project / "apm.lock.yaml").read_bytes() == before


def _add_local_dependency(project: Path) -> None:
    package = project / "local-pkg"
    (package / ".apm" / "instructions").mkdir(parents=True)
    (package / "apm.yml").write_text("name: local-pkg\nversion: 1.0.0\n", encoding="utf-8")
    (package / ".apm" / "instructions" / "local.instructions.md").write_text(
        "---\napplyTo: '**'\n---\nLocal rule.\n", encoding="utf-8"
    )
    _declare(project, "./local-pkg")


def _declare(project: Path, entry: str) -> None:
    import yaml

    manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
    manifest["dependencies"]["apm"].append(entry)
    (project / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def _assert_same_frozen_answer(fast, full) -> None:
    assert fast[0] == full[0], fast[1] + fast[2] + "\n---\n" + full[1] + full[2]
    assert _normalized(fast[1]) == _normalized(full[1])
    assert _untraced(fast[2]) == _untraced(full[2])


def _untraced(stderr: str) -> str:
    kept = (
        line
        for line in stderr.splitlines()
        if not line.startswith((_TRACE, "[i] install fast path:"))
    )
    return _normalized("\n".join(kept))


def _tree_bytes(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


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


# Armed by a sentinel file next to it, so run 1 and run 2 share one PYTHONPATH (keyed).
_BROKEN_RELEASE = """
import os

if os.path.exists(os.path.join(os.path.dirname(__file__), "armed")):
    import apm_cli.install.fast_output as _held

    def _broken(self):
        raise OSError(32, "injected broken pipe while releasing held output")

    _held.HeldOutput.release = _broken
"""


def test_an_attested_hit_whose_output_fails_never_runs_the_full_pipeline(
    world_install, tmp_path
) -> None:
    """Copilot #5383: printing a committed attested no-op fails (a broken pipe): the
    command exits non-zero and the full pipeline never starts on top of it."""
    install, _project, _world = world_install
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(_BROKEN_RELEASE, encoding="utf-8")
    _assert_attested(install(pythonpath=str(hook)))
    (hook / "armed").write_text("", encoding="ascii")

    code, stdout, stderr = install(pythonpath=str(hook))

    assert code != 0, stdout + stderr
    assert "attested hit" in _traces(stderr), stderr
    assert "Installing dependencies" not in stdout, stdout + stderr
