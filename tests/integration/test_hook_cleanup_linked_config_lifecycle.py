"""Hook cleanup replaces a linked merged hook config instead of writing through it (#5181).

PROMISE: P-INV-I5 -- each command writes only its declared write set: removing a
package's hooks rewrites the project's merged hook config by replacing that
file, so a config path that is a link leaves the link's target unchanged
(source: #5181 scope record; install refuses a linked config in hook_ownership).
CELL: OP=uninstall SRC=local PRIM=hook(merged) SCOPE=project TGT=claude
      CONFIG=link-to-outside-file
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise("P-INV-I5", "P-INV-I7"),
    pytest.mark.requires_apm_binary,
]

_INSTALL = ("install", "--parallel-downloads", "0")
_USER_ENTRY = {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo user"}]}


def _describe(result: CommandResult) -> str:
    return f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def _commands(config: Path) -> list[str]:
    data = json.loads(config.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for entries in data.get("hooks", {}).values()
        for entry in entries
        for hook in entry.get("hooks", [])
    ]


def test_uninstall_replaces_a_linked_hook_config_and_leaves_its_target_unchanged(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The cleaned config becomes a regular file; the linked file keeps its bytes."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    sources = LocalPackageFactory(isolated.package_root)
    package = sources.create("hooked", targets=("claude",))
    sources.add_hook(
        package,
        "gate",
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pkg"}]}
                ]
            }
        },
    )
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=({"path": str(package.root)},), targets=("claude",)
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    installed = runner.run(_INSTALL, scenario_id="install", cwd=consumer.root, env=environment)
    assert installed.returncode == 0, _describe(installed)

    config = consumer.root / ".claude" / "settings.json"
    assert "echo pkg" in _commands(config), config.read_text(encoding="utf-8")
    outside = isolated.root / "outside" / "settings.json"
    outside.parent.mkdir()
    data = json.loads(config.read_text(encoding="utf-8"))
    data["hooks"].setdefault("PostToolUse", []).append(_USER_ENTRY)
    outside.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    config.unlink()
    config.symlink_to(outside)
    outside_before = outside.read_bytes()

    removed = runner.run(
        ("uninstall", str(package.root)),
        scenario_id="uninstall",
        cwd=consumer.root,
        env=environment,
    )

    assert removed.returncode == 0, _describe(removed)
    assert outside.read_bytes() == outside_before
    assert not config.is_symlink(), _describe(removed)
    assert config.is_file(), _describe(removed)
    commands = _commands(config)
    assert "echo pkg" not in commands, commands
    assert "echo user" in commands, commands
    for stream in (removed.stdout, removed.stderr):
        assert stream.isascii(), _describe(removed)
