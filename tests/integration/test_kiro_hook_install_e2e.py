"""End-to-end local install coverage for Kiro v1 hook deployment."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CLI = [sys.executable, "-m", "apm_cli.cli"]


def test_install_transforms_kiro_v1_hook_and_preserves_unrelated_config(
    tmp_path: Path,
) -> None:
    """A real local install writes the Kiro v1 runtime shape without data loss."""
    consumer = tmp_path / "consumer"
    package = tmp_path / "kiro-hooks"
    hooks_dir = package / "hooks"
    consumer_hooks = consumer / ".kiro" / "hooks"
    hooks_dir.mkdir(parents=True)
    consumer_hooks.mkdir(parents=True)

    (consumer / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ndependencies:\n  apm: []\n",
        encoding="utf-8",
    )
    (package / "apm.yml").write_text(
        "name: kiro-hooks\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (hooks_dir / "check.py").write_text("print('checked')\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "preToolUse": [
                        {
                            "matcher": "write",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python ${PLUGIN_ROOT}/hooks/check.py",
                                    "timeout": 12,
                                },
                                {
                                    "type": "askAgent",
                                    "prompt": "Check this write for policy drift.",
                                },
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    unrelated = {"version": "v1", "hooks": [{"name": "user-owned"}]}
    unrelated_path = consumer_hooks / "user-hook.json"
    unrelated_path.write_text(json.dumps(unrelated), encoding="utf-8")

    result = subprocess.run(
        [*CLI, "install", str(package), "--target", "kiro"],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, (
        f"install stdout:\n{result.stdout}\ninstall stderr:\n{result.stderr}"
    )
    generated = consumer_hooks / "kiro-hooks-hooks-pretooluse-1.json"
    assert json.loads(generated.read_text(encoding="utf-8")) == {
        "version": "v1",
        "hooks": [
            {
                "name": "kiro-hooks PreToolUse 1",
                "trigger": "PreToolUse",
                "matcher": "write",
                "action": {
                    "type": "command",
                    "command": "python .kiro/hooks/kiro-hooks/hooks/check.py",
                },
                "timeout": 12,
            }
        ],
    }
    deployed_script = consumer_hooks / "kiro-hooks" / "hooks" / "check.py"
    assert deployed_script.read_text(encoding="utf-8") == "print('checked')\n"
    generated_agent = consumer_hooks / "kiro-hooks-hooks-pretooluse-2.json"
    assert json.loads(generated_agent.read_text(encoding="utf-8")) == {
        "version": "v1",
        "hooks": [
            {
                "name": "kiro-hooks PreToolUse 2",
                "trigger": "PreToolUse",
                "matcher": "write",
                "action": {
                    "type": "agent",
                    "prompt": "Check this write for policy drift.",
                },
            }
        ],
    }
    assert json.loads(unrelated_path.read_text(encoding="utf-8")) == unrelated
