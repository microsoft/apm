"""A converged install writes nothing, not even byte-identical bytes (#4790).

PROMISE: P-LAW-idempotency.byte_stable -- repeating a converged operation leaves
durable bytes unchanged; P-INV-I2 -- idempotence (source: CHANGELOG 0.38.0
"Fixed": a converged ``apm install`` no longer rewrites identical
``apm_modules/*/.apm-pin`` cache markers, so a no-op install writes nothing).

Every digest-based no-write oracle is blind to a rewrite of identical bytes, so
this cell compares identity stamps (inode, mtime_ns, ctime_ns; ``snapshot.capture_stamps``)
of the cache markers, the lock and every deployed file across a repeated install;
tests/quality/test_snapshot_stamps.py holds the stamp oracle's own controls.
"""

from __future__ import annotations

import pytest

from tests.lifecycle import driver
from tests.lifecycle.snapshot import capture_stamps, touched
from tests.lifecycle.world import World

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-LAW-idempotency.byte_stable", "P-INV-I2"),
]

_PINS = ("apm_modules/**/.apm-pin",)
_DURABLE = ("apm.lock.yaml", ".github/**/*", ".agents/**/*", ".claude/**/*", "AGENTS.md")


def _ok(world: World, *argv: str) -> str:
    result = driver.run(world, argv)
    assert result.returncode == 0, f"{argv}: {result.stdout}{result.stderr}"
    return result.stdout + result.stderr


@pytest.mark.parametrize("scope", ["cache-markers", "lock-and-deployed"])
def test_a_converged_install_touches_no_file(lifecycle_world: World, scope: str) -> None:
    world = lifecycle_world
    _ok(world, "install", world.packages["p-skill:git"].spec)
    _ok(world, "install")
    patterns = _PINS if scope == "cache-markers" else _DURABLE
    before = capture_stamps(world.project, patterns)
    assert before, f"precondition: nothing matched {patterns}"

    result = driver.run(world, ("install",))

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    rewritten = touched(before, capture_stamps(world.project, patterns))
    assert not rewritten, "a converged install wrote:\n" + "\n".join(rewritten) + f"\n{output}"
