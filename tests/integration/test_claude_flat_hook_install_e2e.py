"""End-to-end coverage for Claude flat hook normalization (#2062)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CLI = [sys.executable, "-m", "apm_cli.cli"]
TIMEOUT = 180
pytestmark = pytest.mark.integration


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the APM CLI in *cwd* and return the completed subprocess."""
    return subprocess.run(
        CLI + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def test_install_claude_nests_flat_hooks_and_preserves_settings(tmp_path: Path) -> None:
    """The real install path writes valid Claude groups without replacing settings."""
    workspace = tmp_path / "workspace"
    package = workspace / "flat-hooks-package"
    consumer = workspace / "consumer"
    hooks_dir = package / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    consumer.joinpath(".claude").mkdir(parents=True)

    package.joinpath("apm.yml").write_text(
        "name: flat-hooks-package\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    hooks_dir.joinpath("test.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"type": "command", "command": "echo installed"}]}}),
        encoding="utf-8",
    )
    consumer.joinpath("apm.yml").write_text(
        """name: flat-hooks-consumer
version: 1.0.0
dependencies:
  apm:
    - path: ../flat-hooks-package
""",
        encoding="utf-8",
    )
    existing_group = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "echo existing"}],
    }
    consumer.joinpath(".claude", "settings.json").write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "hooks": {"PreToolUse": [existing_group]},
            }
        ),
        encoding="utf-8",
    )

    result = _run(consumer, "install", "--target", "claude")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    settings = json.loads(consumer.joinpath(".claude", "settings.json").read_text(encoding="utf-8"))
    sidecar = json.loads(consumer.joinpath(".claude", "apm-hooks.json").read_text(encoding="utf-8"))
    installed_group = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "echo installed"}],
    }
    assert settings["permissions"] == {"allow": ["Read"]}
    assert settings["hooks"]["PreToolUse"] == [existing_group, installed_group]
    assert sidecar["PreToolUse"] == [{**installed_group, "_apm_source": "flat-hooks-package"}]
