"""Local install/update regression adapted from #3199 without policy bypass.

Click drives the normal approval and install pipeline; no hook executes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

pytestmark = [pytest.mark.component, pytest.mark.integration]


def test_install_update_write_nested_hook_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal local approval preserves nested JSON on install and update."""
    package = tmp_path / "hookpkg"
    hooks = package / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    (package / "apm.yml").write_text("name: nested-env\nversion: 1.0.0\n", encoding="utf-8")
    sentinel = tmp_path / "HOOK_EXECUTED"
    script = tmp_path / "must_not_run.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}"'
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "nested-env-consumer",
                "version": "1.0.0",
                "targets": ["claude", "codex"],
                "dependencies": {"apm": ["../hookpkg"]},
                "executables": {"allow": {}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr("apm_cli.commands._helpers.check_for_updates", lambda *_a, **_k: None)
    runner = CliRunner()

    for operation in ("install", "update"):
        env = {"APM_TEST": "value", "NESTED": {"LIST": ["a", {"b": 1}]}}
        handler = {
            "type": "command",
            "command": command,
            "timeout": 5,
            "env": env,
            "x-extension": {"enabled": True, "weight": 1.5, "note": None},
        }
        source = hooks / "pre.json"
        source.write_text(
            json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [handler]}]}}),
            encoding="utf-8",
        )
        original = source.read_bytes()
        clear_apm_yml_cache()
        if operation == "install":
            pending = runner.invoke(cli, ["install"], catch_exceptions=False)
            assert pending.exit_code == 0, pending.output
            assert not (project / ".claude" / "settings.json").exists()
            assert not (project / ".codex" / "hooks.json").exists()
            assert not sentinel.exists()
        approval = runner.invoke(cli, ["approve", "../hookpkg"], catch_exceptions=False)
        assert approval.exit_code == 0, approval.output
        manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
        assert manifest["executables"]["allow"]["../hookpkg#1.0.0"]["hooks"] is True

        if operation == "update":
            # An unchanged local lock is a no-op; exercise update's real write path.
            (project / "apm.lock.yaml").unlink()
            (project / ".claude" / "settings.json").unlink()
            (project / ".codex" / "hooks.json").unlink()
        args = ["update", "--yes"] if operation == "update" else ["install"]
        result = runner.invoke(cli, args, catch_exceptions=False)

        assert result.exit_code == 0, result.output
        for config in (".claude/settings.json", ".codex/hooks.json"):
            document = json.loads((project / config).read_text(encoding="utf-8"))
            [entry] = document["hooks"]["PreToolUse"]
            assert entry["matcher"] == "Bash", config
            [rendered] = entry["hooks"]
            assert rendered == handler, config
        assert source.read_bytes() == original
        assert not sentinel.exists()
