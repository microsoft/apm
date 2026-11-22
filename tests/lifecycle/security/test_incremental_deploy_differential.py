"""Install after any input change equals a fresh full deploy of the same inputs (lane C).

PROMISE: P-INV-I1 -- the lock (the deployment ledger) matches what is deployed;
P-INV-I4 -- a stricter policy or a withdrawn approval never leaves an executable
deployed; P-LAW-ownership.preserve_unowned -- a user's file at a managed path
and a destination that leaves the project are never written through
(source: tests/lifecycle/oracles.py, install/exec_gate.py, utils/path_security.py).

Differential oracle for an incremental deploy ("deploy only what changed vs the
lockfile"): an incremental world converges, takes one change and runs
``apm install``; a fresh world receives the same final inputs (package source,
apm.yml, org policy, the user's approval store) and runs ``apm install`` once.
Their project and HOME bytes and their lock (identity fields normalized) must
match. Two changes have no fresh equivalent (a user's edit, a symlinked
destination): there the edited path keeps the user's bytes or the outside tree
is untouched, and everything else still equals the fresh deploy.

The change table and the state oracle live in tests/lifecycle/scenarios/_deploy_diff.py,
shared with the engine parity suite (one lane C oracle).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.lifecycle import driver
from tests.lifecycle.known import KnownFailure
from tests.lifecycle.scenarios._deploy_diff import (
    CHANGES,
    HOME_CACHES,
    SKILL_PATH,
    comparable,
    converge,
    diff,
    digest,
    set_inputs,
    symlink_destination,
)
from tests.lifecycle.world import World
from tests.lifecycle.world.clone import clone

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-INV-I4", "P-LAW-ownership.preserve_unowned"),
]

# kept_edit_message (integration/base_integrator.py): the entry, then why it was kept.
_KEPT = re.compile(r"c-alpha.*(modified since APM deployed it|may hold local edits)")


def _install(world: World):
    return driver.run(world, ("install",))


def _same_user_file(incremental: World, fresh: World) -> None:
    """The fresh twin holds the same user-written bytes at the managed path."""
    target = fresh.project / SKILL_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((incremental.project / SKILL_PATH).read_bytes())


def _same_symlink(incremental: World, fresh: World) -> None:
    del incremental
    (fresh.project / SKILL_PATH).parent.mkdir(parents=True, exist_ok=True)
    symlink_destination(fresh)


# Puts the same user-made on-disk state into the fresh twin before its install.
_PRE_PLACE = {
    "user-edited-deployed-file": _same_user_file,
    "symlinked-destination": _same_symlink,
}

# Retiring the last APM entry leaves the APM-created config file as an empty
# skeleton (and an empty mcp_target_servers lock field); a fresh deploy has neither.
_EMPTY_CONFIG_LEFT = KnownFailure(
    "#4852",
    "retiring the last APM entry leaves an empty APM-created config file",
    r"(\.mcp\.json|\.claude/settings\.json): incremental=.*fresh='<absent>'",
)
_KNOWN = {
    "approval-revoked": _EMPTY_CONFIG_LEFT,
    "policy-denies-executables": _EMPTY_CONFIG_LEFT,
    "target-removed": _EMPTY_CONFIG_LEFT,
}


def _cases() -> list:
    return [
        pytest.param(name, marks=_KNOWN[name].mark()) if name in _KNOWN else name
        for name in sorted(CHANGES)
    ]


@pytest.mark.parametrize("change", _cases())
def test_install_after_a_change_equals_a_fresh_deploy(
    lifecycle_template: World, tmp_path: Path, change: str
) -> None:
    spec = CHANGES[change]
    incremental = clone(lifecycle_template, tmp_path / "incremental")
    fresh = clone(lifecycle_template, tmp_path / "fresh")
    incremental.services.reset()
    fresh.services.reset()

    converge(incremental, spec.start)
    exempt = spec.apply(incremental)
    user_bytes = outside = None
    if change == "user-edited-deployed-file":
        user_bytes = (incremental.project / SKILL_PATH).read_bytes()
    if change == "symlinked-destination":
        outside = digest(incremental.root / "outside-skill", (), incremental)
    held = _raw(incremental)
    after = _install(incremental)

    # The fresh twin: the same final inputs, the user's approval store included, no history.
    set_inputs(fresh, spec.end)
    config = incremental.apm_home / "config.json"
    if config.is_file():
        text = config.read_text(encoding="utf-8").replace(str(incremental.root), str(fresh.root))
        (fresh.apm_home / "config.json").write_text(text, encoding="utf-8")
    pre_place = _PRE_PLACE.get(change)
    if pre_place is not None:
        pre_place(incremental, fresh)
    reference = _install(fresh)

    evidence = (
        f"\n--- incremental install\n{after.stdout}{after.stderr}"
        f"\n--- fresh install\n{reference.stdout}{reference.stderr}"
    )
    if pre_place is None:
        assert after.returncode == reference.returncode == 0, evidence
    else:
        _assert_user_state_preserved(change, (incremental, after), (fresh, reference), evidence)
        if change == "symlinked-destination":
            for world in (incremental, fresh):
                assert digest(world.root / "outside-skill", (), world) == outside, (
                    "install wrote through a symlinked destination" + evidence
                )
        if user_bytes is not None:
            for world in (incremental, fresh):
                assert (world.project / SKILL_PATH).read_bytes() == user_bytes, (
                    "install overwrote the user's file at a managed path" + evidence
                )
        if after.returncode != 0:
            # A refused install keeps every byte it found (I7).
            assert _raw(incremental) == held, "a refused install changed state" + evidence
        if after.returncode != 0 or reference.returncode != 0:
            return
    problems = diff(
        comparable(incremental, exempt), comparable(fresh, exempt), (incremental, fresh)
    )
    assert not problems, f"{change}: incremental != fresh deploy\n" + "\n".join(problems) + evidence


def _raw(world: World) -> dict[str, dict[str, str]]:
    return {
        "project": digest(world.project, (".git",), world),
        "home": digest(world.home, HOME_CACHES, world),
    }


def _said(result) -> str:
    return " ".join(f"{result.stdout} {result.stderr}".split())


def _assert_user_state_preserved(change: str, incremental, fresh, evidence: str) -> None:
    """Both twins keep the user's state and say so; their outcomes may differ by provenance.

    The incremental twin's receipts prove APM wrote the file, so it keeps the edit and
    installs the rest. The fresh twin has no such evidence: the file is unmanaged, and
    install may refuse the conflict instead. Either way nothing is overwritten silently.
    """
    for label, (_world, result) in (("incremental", incremental), ("fresh", fresh)):
        said = _said(result)
        if result.returncode == 0:
            assert _KEPT.search(said), (
                f"{label}: kept the user's state without saying so" + evidence
            )
        else:
            assert "c-alpha" in said and "[x]" in said, (
                f"{label}: refused without naming the destination" + evidence
            )
    if change == "symlinked-destination":
        assert incremental[1].returncode == fresh[1].returncode, (
            "the twins disagree on a symlinked destination" + evidence
        )
