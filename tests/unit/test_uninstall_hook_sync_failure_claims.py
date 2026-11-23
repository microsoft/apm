"""A hook file uninstall cannot remove keeps its lock claim (#4851)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-file-level-deploy-sync-cleanup"),
]

_HOOK = {
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo t"}]}]
    }
}


def _hook_package(root: Path) -> None:
    hooks = root / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    (root / "apm.yml").write_text("name: hooked\nversion: 1.0.0\n", encoding="ascii")
    (hooks / "pretool.json").write_text(json.dumps(_HOOK), encoding="ascii")


def _claims(project: Path) -> dict[str, list[str]]:
    data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8")) or {}
    return {
        row.get("name") or row.get("repo_url"): row.get("deployed_files") or []
        for row in data.get("dependencies") or []
    }


def test_uninstall_keeps_the_claim_of_a_hook_sync_could_not_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hook_package(tmp_path / "pkg")
    project = tmp_path / "app"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: app\nversion: 1.0.0\ntargets: [copilot]\n"
        "dependencies:\n  apm:\n    - path: ../pkg\n",
        encoding="ascii",
    )
    monkeypatch.chdir(project)
    runner = CliRunner()
    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output
    hooks = [path for path in _claims(project).get("hooked", []) if "/hooks/" in path]
    assert hooks, _claims(project)
    targets = {(project / path).resolve() for path in hooks}
    unlink = Path.unlink
    attempts: list[Path] = []

    def race_then_fail(self: Path, *args, **kwargs) -> None:
        # Selected cleanup's unlink is undone (the hook reappears), and the
        # later hook-sync unlink of the same file is denied.
        if self.resolve() in targets:
            attempts.append(self)
            if len(attempts) > 1:
                raise PermissionError(13, "Permission denied", str(self))
            return
        unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", race_then_fail)

    uninstall = runner.invoke(cli, ["uninstall", "../pkg"])

    monkeypatch.undo()
    assert len(attempts) >= 2, uninstall.output
    assert uninstall.exit_code == 1, uninstall.output
    assert all((project / path).is_file() for path in hooks)
    assert set(hooks) <= set(_claims(project).get("hooked", [])), _claims(project)
