"""One locked-source verdict for every materialized copy of a git package (#4356, #4353, #4363).

PROMISE: I3 -- ``install --frozen`` and ``audit --ci`` reach the same verdict
on the same state; I7 -- a refused command preserves durable state and says
why (source: .agents/skills/apm-lifecycle-tests/SKILL.md; owner
``package-content-hash`` in .apm/architecture/owners/install-deployment.json).
CELL: OP=audit-ci|install-frozen|install|update SRC=git PRIM=apm-package
SCOPE=project LOCK=unchanged DEPLOYED=warm CACHE=every-copy-differs FROZEN

When the project copy under ``apm_modules`` and the persistent git checkout
both differ from the locked ``content_hash``, every command reads the locked
source through the same cache and refuses with the shared
entry/expected/observed line. A refusal leaves the project, the lockfile and
``apm_modules`` byte for byte as they were.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
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
    pytest.mark.promise("P-INV-I3", "P-OWN-package-content-hash"),
]

_TARGETS = ("copilot",)
_SKILL = Path(".apm") / "skills" / "helper" / "SKILL.md"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _all_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _durable(world: ReplayWorld) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Project files (with the lockfile) and every apm_modules file, byte for byte."""
    return tree_digest(world.project), _all_files(world.project / "apm_modules")


def _edit_every_copy(world: ReplayWorld) -> list[Path]:
    checkouts = world.isolated.cache_root / "git" / "checkouts_v1"
    copies = [
        path
        for root in (world.project / "apm_modules", checkouts)
        for path in root.rglob(_SKILL.name)
        if ".git" not in path.parts and path.is_file()
    ]
    assert len(copies) >= 2, copies
    for path in copies:
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"\nCopy edit.\n")
    return copies


def _names_mismatch(result: CommandResult, expected: str) -> bool:
    text = re.sub(r"\s+", " ", result.stdout + result.stderr)
    return f"Content hash mismatch for test/pkg: expected {expected}, got sha256:" in text


def _installed(tmp_path: Path, apm_binary_path: Path) -> ReplayWorld:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.manifest(Shape([world.publish("pkg", apm_package)], "test/pkg", targets=_TARGETS))
    world.apm("install")
    # A clean cache passes both readers.
    world.apm("audit", "--ci")
    world.apm("install", "--frozen")
    return world


def _locked_content_hash(world: ReplayWorld) -> str:
    rows = load_yaml(world.project / "apm.lock.yaml")["dependencies"]
    return next(row["content_hash"] for row in rows if row["repo_url"] == "test/pkg")


def test_audit_ci_and_frozen_refuse_alike_when_every_copy_differs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    expected = _locked_content_hash(world)
    _edit_every_copy(world)
    before = _durable(world)

    # I3 paired probe on one state.
    audit = world.apm("audit", "--ci", expect=None)
    assert _durable(world) == before, "I5: audit --ci changed durable state"
    frozen = world.apm("install", "--frozen", expect=None)
    assert _durable(world) == before, "I7: refused frozen install changed durable state"
    plain = world.apm("install", expect=None)
    assert _durable(world) == before, "I7: refused install changed durable state"

    for refused in (audit, frozen, plain):
        assert refused.returncode == 1, _evidence(refused)
        assert _names_mismatch(refused, expected), _evidence(refused)


def test_update_refusal_names_the_mismatch_as_an_error(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    expected = _locked_content_hash(world)
    _edit_every_copy(world)
    before = _durable(world)

    update = world.apm("update", "--yes", expect=None)

    assert update.returncode == 1, _evidence(update)
    errors = [
        line
        for line in (update.stdout + update.stderr).splitlines()
        if line.lstrip().startswith("[x]")
    ]
    assert any("test/pkg" in line for line in errors), _evidence(update)
    assert _names_mismatch(update, expected), _evidence(update)
    assert _durable(world) == before, "I7: refused update changed durable state"
