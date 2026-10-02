"""Installed CLI proof of Cursor native hooks and safe import coexistence."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.lifecycle_smoke, pytest.mark.requires_apm_binary]


@pytest.mark.parametrize("shared", [False, True], ids=["native-lifecycle", "import-conflict"])
def test_cursor_installed_cli_contract(tmp_path: Path, apm_binary_path: Path, shared: bool) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "cursor", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    sources = LocalPackageFactory(isolated.package_root)
    consumers = LocalPackageFactory(isolated.work_root)
    targets = ("claude", "cursor") if shared else ("cursor",)
    package = sources.create("cursor-native", targets=targets)
    source = package.root / ".apm/hooks/probe.py"
    source.parent.mkdir(parents=True)
    source.write_text('print(\'{"permission":"allow"}\')\n', encoding="utf-8")
    sources.add_hook(
        package,
        "gate",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f'"{sys.executable}" "${{PLUGIN_ROOT}}/.apm/hooks/probe.py"',
                                "timeout": 10,
                            }
                        ],
                    }
                ]
            },
        },
    )
    consumer = consumers.create(
        "consumer",
        dependencies=({"path": str(package.root)},),
        targets=targets,
    )
    config = consumer.root / ".cursor/hooks.json"
    config.parent.mkdir()
    user = {"version": 1, "hooks": {"afterFileEdit": [{"command": "echo user"}]}}
    config.write_text(json.dumps(user) + "\n", encoding="utf-8")
    before = config.read_bytes()
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    args = ("install", "--no-policy", "--parallel-downloads", "0")
    first = runner.run(args, scenario_id="cursor-install", cwd=consumer.root, env=environment)
    if shared:
        assert first.returncode != 0, first.stdout + first.stderr
        assert "Claude import" in first.stdout + first.stderr
        assert config.read_bytes() == before
        assert not (consumer.root / ".claude/settings.json").exists()
        assert not config.with_name("apm-hooks.json").exists()
        return
    assert first.returncode == 0, first.stdout + first.stderr
    native = json.loads(config.read_text())
    assert set(native["hooks"]) == {"preToolUse", "afterFileEdit"}
    entry = native["hooks"]["preToolUse"][0]
    assert entry["matcher"] == "Shell"
    assert "hooks" not in entry
    executed = subprocess.run(
        shlex.split(entry["command"]),
        cwd=consumer.root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert executed.returncode == 0, executed.stderr
    assert json.loads(executed.stdout) == {"permission": "allow"}
    first_bytes = config.read_bytes()
    second = runner.run(args, scenario_id="cursor-reinstall", cwd=consumer.root, env=environment)
    assert second.returncode == 0, second.stdout + second.stderr
    assert config.read_bytes() == first_bytes

    removed = runner.run(
        ("uninstall", str(package.root)),
        scenario_id="cursor-uninstall",
        cwd=consumer.root,
        env=environment,
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert json.loads(config.read_text()) == user
    assert not config.with_name("apm-hooks.json").exists()
