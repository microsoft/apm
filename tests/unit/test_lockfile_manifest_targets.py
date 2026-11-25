"""The lock records only the targets apm.yml declares (#4517, steward ruling).

``manifest_targets`` was written from the reconcile universe, so ``target:
claude`` locked claude plus every gated and always-on target. It records the
declaration now, and the dropped-target computation that reads it is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.manifest_reconcile import dropped_target_files

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-manifest-target-declaration")]


def _lock_for(project: Path, manifest: str) -> LockFile:
    (project / "apm.yml").write_text(manifest, encoding="utf-8")
    LockFile().write(project / "apm.lock.yaml")
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    return lock


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ("target: claude\n", ["claude"]),
        ("targets: [claude]\n", ["claude"]),
        ("targets: [copilot, claude]\n", ["claude", "copilot"]),
        ("", []),
    ],
)
def test_lock_records_only_the_declared_targets(
    tmp_path: Path, declaration: str, expected: list[str]
) -> None:
    lock = _lock_for(tmp_path, f"name: p\nversion: 1.0.0\n{declaration}")
    assert lock.manifest_targets == expected


def test_dropped_target_from_a_multi_target_declaration_is_unchanged(tmp_path: Path) -> None:
    lock = _lock_for(tmp_path, "name: p\nversion: 1.0.0\ntargets: [copilot, claude]\n")
    lock.dependencies["p"] = LockedDependency(
        repo_url="p",
        deployed_files=[".claude/skills/s/SKILL.md", ".github/skills/s/SKILL.md"],
    )
    (tmp_path / "apm.yml").write_text("name: p\nversion: 1.0.0\ntargets: [copilot]\n")

    assert dropped_target_files(tmp_path, lock) == {".claude/skills/s/SKILL.md": "claude"}
