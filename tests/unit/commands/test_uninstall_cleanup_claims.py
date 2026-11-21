"""Uninstall cleanup claims leave paths another owner still holds (#4703)."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-deployment-provenance-state")]


def test_cleanup_claims_exclude_paths_the_local_owner_still_holds() -> None:
    """A removed package never claims a file a local bundle also deployed (#4703)."""
    from apm_cli.commands.uninstall.cli import _collect_deployed_cleanup_state

    shared = ".github/instructions/f-doc.instructions.md"
    skill = ".agents/skills/f-doc/SKILL.md"
    lockfile = LockFile(
        dependencies={
            "removed": LockedDependency(
                repo_url="removed",
                deployed_files=[shared, skill],
                deployed_file_hashes={shared: "sha256:shared", skill: "sha256:skill"},
            ),
        },
        local_deployed_files=[shared],
        local_deployed_file_hashes={shared: "sha256:shared"},
    )

    paths, hashes = _collect_deployed_cleanup_state(lockfile, {"removed"})
    local_paths, _ = _collect_deployed_cleanup_state(lockfile, {"removed", "."})

    assert paths == {skill}
    assert hashes == {skill: "sha256:skill"}
    assert local_paths == {shared, skill}
