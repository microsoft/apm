"""A frozen install names the locked package it could not fetch (#4909).

PROMISE: I7 -- a failed command preserves durable state and says why; the
``[x]`` line of ``install --frozen`` names the package whose locked commit
could not be re-downloaded, not a consistency error derived from its missing
manifest (source: .agents/skills/apm-lifecycle-tests/SKILL.md; owner
``InstallService.enforce_frozen_restored`` in src/apm_cli/install/service.py).
CELL: OP=install-frozen SRC=git PRIM=apm-package SCOPE=project LOCK=unchanged
DEPLOYED=warm CACHE=cold NET=commit-unavailable FROZEN
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.integration._install_audit_replay_kit import (
    ReplayWorld,
    Shape,
    apm_package,
    tree_digest,
)
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

_SKILL = Path(".apm") / "skills" / "helper" / "SKILL.md"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _drop_commit_content(world: ReplayWorld) -> None:
    """Remove one blob of the locked commit from the served repository."""
    origin = world.repos["pkg"].origin
    blob = world.git(origin, "rev-parse", f"HEAD:{_SKILL.as_posix()}").strip()
    loose = origin / "objects" / blob[:2] / blob[2:]
    assert loose.is_file(), loose
    loose.chmod(0o644)
    loose.unlink()


def _durable(world: ReplayWorld) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Project files (with the lockfile) and every apm_modules file, byte for byte."""
    modules = world.project / "apm_modules"
    return tree_digest(world.project), {
        path.relative_to(modules).as_posix(): path.read_bytes()
        for path in sorted(modules.rglob("*"))
        if path.is_file()
    }


def _error_lines(result: CommandResult) -> list[str]:
    return [
        line.strip()
        for line in (result.stdout + result.stderr).splitlines()
        if line.lstrip().startswith("[x]")
    ]


def test_frozen_names_the_locked_package_it_could_not_fetch(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.manifest(Shape([world.publish("pkg", apm_package)], "test/pkg", targets=("copilot",)))
    world.apm("install")

    # The locked commit can no longer be fetched and no local copy holds it.
    _drop_commit_content(world)
    copy = world.project / "apm_modules" / "test" / "pkg" / _SKILL
    copy.chmod(0o644)
    copy.write_bytes(copy.read_bytes() + b"\nCopy edit.\n")
    shutil.rmtree(world.isolated.cache_root)
    world.isolated.cache_root.mkdir(parents=True)
    before = _durable(world)

    frozen = world.apm("install", "--frozen", expect=None)

    assert frozen.returncode != 0, _evidence(frozen)
    assert _durable(world) == before, "I7: refused frozen install changed durable state"
    errors = _error_lines(frozen)
    # The first [x] line is the restore failure and names the package.
    assert errors, _evidence(frozen)
    assert "could not be restored" in errors[0], _evidence(frozen)
    assert "test/pkg" in errors[0], _evidence(frozen)
    # The fetch failure itself is printed as the reason.
    text = " ".join((frozen.stdout + frozen.stderr).split())
    assert "Failed to install test/pkg:" in text, _evidence(frozen)
    # The derived consistency error never replaces the cause.
    assert "restored MCP declarations differ" not in text, _evidence(frozen)
    for line in (frozen.stdout + frozen.stderr).splitlines():
        assert line.isascii(), line
