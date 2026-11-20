"""``apm audit --ci`` and ``apm install --frozen`` agree on changed local dependency content (#4447).

PROMISE: I3 -- ``install --frozen`` and ``audit --ci`` reach the same verdict
on the same state (source: .agents/skills/apm-lifecycle-tests/SKILL.md).
CELL: OP=audit-ci|install-frozen SRC=local PRIM=apm-package SCOPE=project
LOCK=unchanged LOCAL-SOURCE=changed-since-lock FROZEN

A local dependency whose source changed after install no longer matches its
locked snapshot. Both readers refuse and name the dependency, and neither
writes the project or the lockfile.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, apm_package, tree_digest
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3"),
]

_LOCAL = "../local-pkg"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_audit_ci_and_frozen_refuse_alike_a_changed_local_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    source = world.project.parent / "local-pkg"
    apm_package(source, "local-pkg")
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [_LOCAL]},
        },
        world.project / "apm.yml",
    )
    world.apm("install")
    world.apm("audit", "--ci")
    world.apm("install", "--frozen")

    # A change that deploys nothing new: only the locked snapshot can see it.
    (source / "NOTES.md").write_text("Changed since the lock.\n", encoding="ascii")
    before = tree_digest(world.project)

    # I3 paired probe on one state.
    audit = world.apm("audit", "--ci", expect=None)
    assert tree_digest(world.project) == before, "I5: audit --ci wrote"
    frozen = world.apm("install", "--frozen", expect=None)
    assert tree_digest(world.project) == before, "I7: refused frozen install wrote"

    for refused in (audit, frozen):
        # Join wrapped table cells: drop the borders, then collapse whitespace.
        text = " ".join((refused.stdout + refused.stderr).replace("|", " ").split())
        assert refused.returncode == 1, _evidence(refused)
        assert f"local dependency {_LOCAL!r} content differs from its locked snapshot" in text, (
            _evidence(refused)
        )
