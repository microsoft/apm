"""Cache presence: cold, warm-valid and warm-stale caches across install, frozen and audit (#4104).

PROMISE: P-OWN-locked-copy-validity -- an existing apm_modules copy is reused
only when it holds its locked content; a stale copy is replaced, never
deployed (source: drift.py locked_copy_proof, install/cache_pin.py).
PROMISE: P-LAW-source.ref_cache_coherent -- what a cache serves equals what
the lock pins (source: tests/fixtures/lifecycle_bug_ledger.json).
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
import shutil

import pytest

from tests.lifecycle.scenarios._kit import Known, Scenario, assert_changes
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
STALE_COPY_AUDIT = {"R": Known("#4165", r"exited 1, expected \[0\]")}
STALE_COPY_PAIR = {"I3": Known("#4165", r"audit --ci exited 1 but install --frozen exited 0")}
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

    stale_copy = cache_class == "warm-stale-modules"
    if op == "install --frozen":
        step = scenario.pair(
            audit_known=STALE_COPY_AUDIT if stale_copy else None,
            frozen_known=STALE_COPY_PAIR if stale_copy else None,
        )
    elif op == "audit --ci":
        step = scenario.run(
            op, read_only=True, exit=0, known=STALE_COPY_AUDIT if stale_copy else None
        )
    else:
        step = scenario.run(op)
    assert_changes(step.changed, CHANGES[cache_class, op], label=f"{cache_class} {op}")

    assert _digest(project / DEPLOYED_SKILL) == locked, "the deployed skill keeps its locked bytes"
    if op != "audit --ci":
        assert _digest(project / MODULE_SKILL) == locked, "apm_modules holds the locked content"
    scenario.assert_deployed()
    scenario.finish()
