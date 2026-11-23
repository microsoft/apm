"""A hook approval pins every byte that deploys with the hook (#4688).

PROMISE: P-OWN-executable-trust-fingerprint -- every executable-trust digest
(the hook approval pin) binds the whole hook bundle install deploys: the hook
document, the scripts it names and every other file copied from their source
roots; P-INV-I4 -- a changed, unapproved byte never deploys (source:
security/trust_fingerprint.py ``hook_file_fingerprint``; CHANGELOG 0.38.0
"Breaking changes").

An approved hook package publishes a revision that changes ONE file. Inside the
bundle (a helper the hook document never names, a nested asset), ``apm update``
must park the whole hook: neither the new nor the old bytes stay deployed, the
update says the definition changed since approval, and ``apm audit --ci``
passes because a parked hook is withheld, not deployed. ``apm approve`` re-pins
it. Outside the bundle (the package README),
the approval stands: the negative control.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from tests.lifecycle import driver
from tests.lifecycle.world import World
from tests.lifecycle.world.services import ORG
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-OWN-executable-trust-fingerprint", "P-INV-I4"),
]

_NAME = "p-hookpin"
_SPEC = f"{ORG}/{_NAME}#main"
_HOOK = {
    "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "sh ./pin/run.sh"}]}]}
}
_FILES = {
    "run.sh": '#!/bin/sh\n. "$(dirname "$0")/lib.sh"\necho run-v1\n',
    "lib.sh": "echo lib-v1\n",
    "data/config.txt": "asset-v1\n",
}
# change id -> (repository path, new content, marker that must not deploy unapproved)
_CHANGES = {
    "helper": (".apm/hooks/pin/lib.sh", "echo lib-v2\n", "lib-v2"),
    "nested-asset": (".apm/hooks/pin/data/config.txt", "asset-v2\n", "asset-v2"),
}
_OUTSIDE = ("README.md", "# p-hookpin, v2 readme\n")


def _publish(world: World) -> None:
    factory = LocalPackageFactory(world.root / "authored-hookpin")
    package = factory.create(_NAME, version="1.0.0")
    factory.add_hook(package, "pin", _HOOK)
    for rel, content in _FILES.items():
        factory.add_hook_asset(package, "pin", PurePosixPath(rel), content)
    (package.root / "README.md").write_text("# p-hookpin\n", encoding="utf-8")
    world.services.publish(f"{ORG}/{_NAME}", package.root)


def _ok(world: World, *argv: str):
    result = driver.run(world, argv)
    assert result.returncode == 0, f"{argv}: {result.stdout}{result.stderr}"
    return result


def _holding(world: World, marker: str) -> list[str]:
    """Every project file (outside .git and the package cache) that carries *marker*."""
    found = []
    for path in sorted(world.project.rglob("*")):
        rel = path.relative_to(world.project).as_posix()
        if rel.startswith((".git/", "apm_modules/")) or not path.is_file():
            continue
        if marker.encode() in path.read_bytes():
            found.append(rel)
    return found


def _approved_and_deployed(world: World) -> None:
    _publish(world)
    _ok(world, "install", _SPEC)
    _ok(world, "approve", _NAME)
    _ok(world, "install")
    for marker in ("lib-v1", "asset-v1", "run-v1"):
        assert _holding(world, marker), f"the approved hook bundle did not deploy {marker}"
    _ok(world, "audit", "--ci")


@pytest.mark.parametrize("change", sorted(_CHANGES))
def test_a_changed_bundle_file_parks_the_hook_until_it_is_approved_again(
    lifecycle_world: World, change: str
) -> None:
    world = lifecycle_world
    _approved_and_deployed(world)
    path, content, marker = _CHANGES[change]
    world.services.publish_revision(f"{ORG}/{_NAME}", {path: content})

    updated = driver.run(world, ("update", "--yes"))
    evidence = f"\n--- update\n{updated.stdout}{updated.stderr}"
    # Parking is a successful update, never a partly failed one.
    assert updated.returncode == 0, "the update failed" + evidence
    for held in (marker, "lib-v1", "asset-v1", "run-v1"):
        assert not _holding(world, held), f"{held} stayed deployed for a parked hook" + evidence
    said = " ".join((updated.stdout + updated.stderr).split())
    assert _NAME in said and "definition changed since approval" in said, (
        "the parked hook was not reported" + evidence
    )
    _ok(world, "audit", "--ci")

    _ok(world, "approve", _NAME)
    _ok(world, "install")
    assert _holding(world, marker), "the re-approved bundle did not deploy" + evidence
    _ok(world, "audit", "--ci")


def test_a_change_outside_the_hook_bundle_keeps_the_approval(lifecycle_world: World) -> None:
    world = lifecycle_world
    _approved_and_deployed(world)
    world.services.publish_revision(f"{ORG}/{_NAME}", {_OUTSIDE[0]: _OUTSIDE[1]})

    updated = driver.run(world, ("update", "--yes"))
    evidence = f"\n--- update\n{updated.stdout}{updated.stderr}"
    assert updated.returncode == 0, evidence
    for marker in ("lib-v1", "asset-v1", "run-v1"):
        assert _holding(world, marker), f"{marker} was retired by an unrelated change" + evidence
    assert "changed since approval" not in updated.stdout + updated.stderr, evidence
    _ok(world, "audit", "--ci")
