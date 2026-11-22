"""Cache presence: cold, warm-valid and warm-stale caches across install, frozen and audit (#4104).

PROMISE: P-OWN-locked-copy-validity -- an existing apm_modules copy is reused
only when it holds its locked content; a stale copy is replaced, never
deployed (source: drift.py locked_copy_proof, install/cache_pin.py).
PROMISE: P-LAW-source.ref_cache_coherent -- what a cache serves equals what
the lock pins (source: tests/fixtures/lifecycle_bug_ledger/).
PROMISE: P-OWN-ci-audit-scratch-materialization -- ``audit --ci`` decides
from the lock and the deployed files without writing the workspace
(source: install/audit_replay.py).

Cache classes, for the git package ``lifecycle/p-skill#v1.0.0`` after one
install:

- cold: no ``apm_modules`` and no git checkout cache (APM_CACHE_DIR/git);
- warm-valid: both as the install left them;
- warm-stale: the ``apm_modules`` copy and the checkout cache hold edited
  bytes that differ from the locked content.

Each class x op row runs through ``driver.run`` with org policy on, applies
I1-I9 after every step (``install --frozen`` rows run the I3 pair) and pins
the exact changed set over project, HOME and the cache root.
"""

from __future__ import annotations

import hashlib
import re
import shutil

import pytest

from tests.lifecycle.scenarios._kit import Scenario, assert_changes
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
        "P-OWN-locked-copy-validity",
        "P-LAW-source.ref_cache_coherent",
        "P-OWN-ci-audit-scratch-materialization",
    ),
]

MODULE_SKILL = "apm_modules/lifecycle/p-skill/skills/p-skill/SKILL.md"
DEPLOYED_SKILL = ".agents/skills/p-skill/SKILL.md"
STALE = b"---\nname: p-skill\ndescription: Stale bytes.\n---\n\nNot the locked content.\n"
CLASSES = ("cold", "warm-valid", "warm-stale-modules", "warm-stale-checkout")
OPS = ("install", "install --frozen", "audit --ci")
# audit --ci replays the stale apm_modules copy and blames the intact deployed
# file; install --frozen on the same state refreshes the copy and succeeds.
# The exact changed set of each (class, op) beyond bookkeeping, as patterns.
CHANGES: dict[tuple[str, str], tuple[str, ...]] = {
    ("cold", "install"): ("project:apm_modules*", "cache:git*"),
    ("cold", "install --frozen"): ("project:apm_modules*", "cache:git*"),
    ("cold", "audit --ci"): (),
    ("warm-valid", "install"): (),
    ("warm-valid", "install --frozen"): (),
    ("warm-valid", "audit --ci"): (),
    ("warm-stale-modules", "install"): (f"project:{MODULE_SKILL}",),
    ("warm-stale-modules", "install --frozen"): (f"project:{MODULE_SKILL}",),
    ("warm-stale-modules", "audit --ci"): (),
    ("warm-stale-checkout", "install"): (),
    ("warm-stale-checkout", "install --frozen"): (),
    ("warm-stale-checkout", "audit --ci"): (),
}


def _checkouts(cache) -> list:
    return sorted((cache / "git" / "checkouts_v1").glob("*/*/full/skills/p-skill/SKILL.md"))


def _prepare(scenario: Scenario, cache, cache_class: str) -> None:
    project = scenario.world.project
    if cache_class == "cold":
        shutil.rmtree(project / "apm_modules")
        shutil.rmtree(cache / "git")
    elif cache_class == "warm-stale-modules":
        (project / MODULE_SKILL).write_bytes(STALE)
    elif cache_class == "warm-stale-checkout":
        checkouts = _checkouts(cache)
        assert checkouts, "the install left a checkout cache"
        for path in checkouts:
            path.write_bytes(STALE)
    if cache_class != "warm-valid":
        scenario.source_moved()


_ELAPSED = re.compile(r"\b\d+\.\ds\b")


def _memo_bytes(cache) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((cache / "noop_v1").glob("*"))}


def _drive(scenario: Scenario, op: str):
    return scenario.pair() if op == "install --frozen" else scenario.run(op)


def _assert_memo_fixed_point(scenario: Scenario, cache, op: str, step) -> None:
    """Fixed-point oracle for the noop_v1 exemption (QL ruling, #4775).

    The memo completes on the next repeat (pending -> full stamp); the one
    after that changes zero bytes anywhere, noop_v1 included, exits 0 and
    prints what the converged run printed.
    """
    _drive(scenario, op)
    memo = _memo_bytes(cache)
    fixed = _drive(scenario, op)
    assert fixed.result.returncode == 0, fixed.output
    assert fixed.changed == frozenset(), f"{op} at the memo fixed point changed {fixed.changed}"
    assert _memo_bytes(cache) == memo != {}, f"{op} at the memo fixed point rewrote noop_v1"
    assert _ELAPSED.sub("<T>", fixed.output) == _ELAPSED.sub("<T>", step.output)


def _digest(path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("op", OPS, ids=["install", "install-frozen", "audit-ci"])
@pytest.mark.parametrize("cache_class", CLASSES)
def test_cache_class_by_operation(lifecycle_world, cache_class, op) -> None:
    use_policy(lifecycle_world, "allow")
    cache = lifecycle_world.cache_root
    scenario = Scenario(
        lifecycle_world,
        {"p-skill": "lifecycle/p-skill#v1.0.0"},
        policy="allow",
        extra_roots={"cache": cache},
    )
    scenario.run("install p-skill")
    project = lifecycle_world.project
    locked = scenario.lock().get_package_dependencies()[0].deployed_file_hashes[DEPLOYED_SKILL]
    _prepare(scenario, cache, cache_class)

    audit = op == "audit --ci"
    step = scenario.run(op, read_only=True, exit=0) if audit else _drive(scenario, op)
    assert_changes(step.changed, CHANGES[cache_class, op], label=f"{cache_class} {op}")
    if not audit and cache_class in ("warm-valid", "warm-stale-checkout"):
        _assert_memo_fixed_point(scenario, cache, op, step)

    assert _digest(project / DEPLOYED_SKILL) == locked, "the deployed skill keeps its locked bytes"
    if op != "audit --ci":
        assert _digest(project / MODULE_SKILL) == locked, "apm_modules holds the locked content"
    scenario.assert_deployed()
    scenario.finish()
