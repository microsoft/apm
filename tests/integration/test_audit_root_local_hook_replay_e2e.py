"""End-to-end audit replay proof for root-local hook source markers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.install.drift import (
    CheckLogger,
    ReplayConfig,
    diff_scratch_against_project,
    run_replay,
)
from apm_cli.integration.targets import resolve_targets


def _write_root_local_codex_hook_project(project: Path) -> None:
    """Create a project whose own .apm directory ships a Codex hook."""
    project.mkdir()
    (project / "apm.yml").write_bytes(b"name: apm-bugs\nversion: 0.0.0\ntargets:\n  - codex\n")
    (project / ".codex").mkdir()
    hooks_dir = project / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    skill_dir = project / ".apm" / "skills" / "proof-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(
        b"---\n"
        b"name: proof-skill\n"
        b"description: Local skill that makes the install lockfile track root content.\n"
        b"---\n"
        b"# Proof skill\n"
    )
    (hooks_dir / "pre.json").write_bytes(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo apm-managed",
                                }
                            ],
                        }
                    ]
                }
            },
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _install(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real install command in-process."""
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["install"], catch_exceptions=False)
    assert result.exit_code == 0, (
        f"apm install failed: exit={result.exit_code}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _codex_marker(root: Path) -> str:
    """Return the first APM ownership marker from Codex hooks.json."""
    hooks_path = root / ".codex" / "hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    entries = data["hooks"]["PreToolUse"]
    assert len(entries) == 1
    marker = entries[0]["_apm_source"]
    assert isinstance(marker, str)
    return marker


@pytest.mark.integration
def test_audit_replay_preserves_root_local_hook_marker_without_phantom_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install then replay-audit a root-local hook without marker drift."""
    project = tmp_path / "apm-bugs"
    _write_root_local_codex_hook_project(project)
    _install(project, monkeypatch)

    installed_marker = _codex_marker(project)
    lockfile_path = get_lockfile_path(project)
    lockfile = LockFile.read(lockfile_path)
    assert lockfile is not None

    scratch_root = run_replay(
        ReplayConfig(
            project_root=project,
            lockfile_path=lockfile_path,
            targets=frozenset({"codex"}),
        ),
        CheckLogger(verbose=False),
    )
    replay_marker = _codex_marker(scratch_root)

    assert installed_marker == "_local/apm-bugs"
    assert replay_marker == installed_marker
    assert replay_marker != "apm-bugs"

    targets = resolve_targets(project, explicit_target=["codex"])
    findings = diff_scratch_against_project(scratch_root, project, lockfile, targets)
    assert findings == []
