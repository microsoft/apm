"""Audit-gated deploy snapshot: restore is byte-exact, and audit off costs nothing (#4085)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apm_cli.install.deploy_snapshot import DeployTargetSnapshot, capture_audit_gated_snapshot
from apm_cli.install.transaction import InstallTransaction


def _tree(root: Path) -> dict[str, bytes | str]:
    state: dict[str, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[rel] = f"-> {os.readlink(path)}"
        elif path.is_dir():
            state[rel] = "<dir>"
        else:
            state[rel] = path.read_bytes()
    return state


def test_audit_off_takes_no_snapshot() -> None:
    ctx = SimpleNamespace(dry_run=False, lockfile_only=False)
    decision = SimpleNamespace(mode="off")
    with (
        patch("apm_cli.core.install_audit.decide_for_install", return_value=decision),
        patch.object(DeployTargetSnapshot, "capture") as capture,
    ):
        assert capture_audit_gated_snapshot(ctx) is None
    capture.assert_not_called()


def test_restore_returns_every_path_to_its_captured_bytes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "kept.md").write_bytes(b"v1\n")
    (agents / "link.md").symlink_to("kept.md")
    (project / ".gitignore").write_bytes(b"node_modules/\n")
    before = _tree(project)
    paths = [agents, project / ".gitignore", project / ".agents" / "skills"]
    store_parent = tmp_path / "apm-home" / "install-snapshots"

    snapshot = DeployTargetSnapshot.capture(paths, store_parent)
    (agents / "kept.md").write_bytes(b"v2 \xe2\x80\xae\n")
    (agents / "new.md").write_bytes(b"new\n")
    (agents / "link.md").unlink()
    (project / ".gitignore").write_bytes(b"node_modules/\napm_modules/\n")
    (project / ".agents" / "skills" / "s").mkdir(parents=True)
    (project / ".agents" / "skills" / "s" / "SKILL.md").write_bytes(b"skill\n")

    assert snapshot.restore() == []
    assert _tree(project) == before
    assert not store_parent.exists()


def test_rollback_removes_only_empty_target_roots_the_attempt_created(tmp_path: Path) -> None:
    """#4159: audit off, no snapshot; created roots left empty still go."""
    project = tmp_path / "project"
    project.mkdir()
    empty_root = project / ".github"
    used_root = project / ".claude"
    preexisting = project / ".cursor"
    for root in (empty_root, used_root, preexisting):
        root.mkdir()
    (used_root / "user.md").write_bytes(b"mine\n")
    transaction = InstallTransaction(
        manifest_path=project / "apm.yml",
        apm_modules_dir=project / "apm_modules",
        validation=None,
        logger=None,
        acquire_lock=False,
    )
    transaction.track_created_target_roots([empty_root, used_root])

    transaction.rollback()

    assert not empty_root.exists()
    assert (used_root / "user.md").read_bytes() == b"mine\n"
    assert preexisting.is_dir()
