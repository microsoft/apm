"""End-to-end regression for #1329: stale root hook _apm_source heals on reinstall."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CLI = [sys.executable, "-m", "apm_cli.cli"]


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(CLI + list(args), cwd=str(cwd), capture_output=True, text=True)


def test_root_hook_source_drift_heals_on_reinstall(tmp_path: Path) -> None:
    project = tmp_path / "myapp"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: myapp\nversion: 0.0.0\ntargets:\n  - claude\n",
        encoding="utf-8",
    )
    hooks_dir = project / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "pre.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "bash .codex/hooks/pre.sh"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    # Simulate a legacy install whose _apm_source came from an old checkout basename.
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "bash .codex/hooks/pre.sh"}],
                            "_apm_source": "old-checkout-name",
                        },
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo user-owned"}],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run(project, "install")
    assert result.returncode == 0, result.stderr or result.stdout

    entries = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    sidecar_path = project / ".claude" / "apm-hooks.json"
    sidecar = json.loads(sidecar_path.read_text())
    sidecar_sources = [
        e.get("_apm_source") for e in sidecar.get("PreToolUse", []) if isinstance(e, dict)
    ]
    user_owned = [
        e for e in entries if isinstance(e, dict) and e["hooks"][0]["command"] == "echo user-owned"
    ]

    assert sidecar_sources == ["_local/myapp"], (
        f"Expected single _local/myapp entry in sidecar, got {sidecar_sources}"
    )
    assert len(user_owned) == 1, "User-owned hook entry must survive healing"
