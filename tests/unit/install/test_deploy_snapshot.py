"""Audit-gated deploy snapshot: restore is byte-exact, and audit off costs nothing (#4085)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apm_cli.install.deploy_snapshot import DeployTargetSnapshot, capture_audit_gated_snapshot


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
    created_root = project / ".claude"
    created_root.mkdir()
    before = _tree(project)
    paths = [agents, project / ".gitignore", project / ".agents" / "skills"]
    store_parent = tmp_path / "apm-home" / "install-snapshots"

    snapshot = DeployTargetSnapshot.capture(paths, store_parent, [created_root])
    (agents / "kept.md").write_bytes(b"v2 \xe2\x80\xae\n")
    (agents / "new.md").write_bytes(b"new\n")
    (agents / "link.md").unlink()
    (project / ".gitignore").write_bytes(b"node_modules/\napm_modules/\n")
    (project / ".agents" / "skills" / "s").mkdir(parents=True)
    (project / ".agents" / "skills" / "s" / "SKILL.md").write_bytes(b"skill\n")

    assert snapshot.restore() == []
    expected = {k: v for k, v in before.items() if k != ".claude"}
    assert _tree(project) == expected
    assert not store_parent.exists()
