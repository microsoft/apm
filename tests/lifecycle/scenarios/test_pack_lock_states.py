"""``apm pack`` over a stale lock and over a lock a newer APM wrote (#4097).

PROMISE: P-INV-I5 -- ``pack --dry-run`` writes nothing, and the bundle is
built from apm.lock.yaml: a dependency apm.yml declares but the lock does not
record is not packed (source: docs/src/content/docs/reference/cli/pack.md,
"The bundle is built from apm.lock.yaml").
PROMISE: P-OWN-lockfile-generator-compatibility -- a lock written by a newer
APM is refused before anything is written, and the refusal names the upgrade
(source: deps/lockfile_generator.py).

T4 (OP x SCOPE x LOCK) for OP=pack at project scope: pack has no user-scope
form. Each row applies I1-I9 through the scenario kit.
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.lifecycle.scenarios._kit import Scenario, assert_changes, credits
from tests.lifecycle.world import use_policy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I5", "P-OWN-lockfile-generator-compatibility"),
]

_PACK = {"OP": "pack", "SCOPE": "project"}


def _installed(world) -> Scenario:
    use_policy(world, "allow")
    specs = {name: world.packages[f"{name}:git"].spec for name in ("p-skill", "p-lsp")}
    scenario = Scenario(world, specs, policy="allow")
    scenario.run("install p-skill")
    return scenario


def _packed(step) -> set[str]:
    return {line.strip() for line in step.result.stdout.splitlines()}


def test_pack_builds_from_a_stale_lock(lifecycle_world) -> None:
    scenario = _installed(lifecycle_world)
    # Control: an approved, locked LSP package is packed (.lsp.json ships).
    scenario.run("install p-lsp")
    scenario.run("approve p-lsp")
    scenario.run("install")
    control = scenario.run("pack --dry-run", exit=0)
    assert ".lsp.json" in _packed(control), control.output
    manifest = lifecycle_world.project / "apm.yml"
    approvals = yaml.safe_load(manifest.read_text(encoding="utf-8"))["executables"]
    scenario.run("uninstall p-lsp")
    # The user declares p-lsp again, still approved, without locking it: the manifest
    # alone would ship .lsp.json, so its absence proves pack reads the lock.
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"]["apm"].append(scenario.specs["p-lsp"])
    data["executables"] = approvals
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    scenario.model = scenario.model.approve("p-lsp", scenario.kinds["p-lsp"]).with_lock("stale")
    step = scenario.run("pack --dry-run", exit=0, credit=[{**_PACK, "LOCK": "stale"}])
    listed = _packed(step)
    assert "skills/p-skill/SKILL.md" in listed, step.output
    assert ".lsp.json" not in listed, "a dependency the lock does not record is not packed"
    assert_changes(step.changed, (), label="pack --dry-run")
    scenario.finish()


_NEWER = r"Refusing to run 'apm pack' on apm\.lock\.yaml: it was written by APM 99\.0\.0"


def test_pack_refuses_a_lock_a_newer_apm_wrote(lifecycle_world) -> None:
    scenario = _installed(lifecycle_world)
    text = scenario.lock_path.read_text(encoding="utf-8")
    text, count = re.subn(r"^apm_version: .*$", "apm_version: 99.0.0", text, flags=re.M)
    assert count == 1, text
    scenario.lock_path.write_text(text, encoding="utf-8")
    scenario.model = scenario.model.with_lock("newer-generator")
    step = scenario.run("pack", exit=1, credit=[{**_PACK, "LOCK": "newer-generator"}], refusal=True)
    assert "Install APM 99.0.0 or newer" in step.output, "I7: the refusal names the upgrade"
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        "test_pack_builds_from_a_stale_lock": [{**_PACK, "LOCK": "stale"}],
        "test_pack_refuses_a_lock_a_newer_apm_wrote": [{**_PACK, "LOCK": "newer-generator"}],
    },
)
