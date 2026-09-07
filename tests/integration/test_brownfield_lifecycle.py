"""Brownfield lifecycle: preview -> apply -> install elsewhere -> rerun, plus a failed apply.

Runs the real CLI through ``ApmLifecycleRunner`` inside an ``IsolatedApmEnvironment``
and proves, with ``LifecycleStateSnapshot`` before/after captures, that the
documented import-and-install journey preserves the original harness files and
keeps ``.apm/``, ``.apm/.import-sources.json`` and ``apm.yml`` consistent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_FAKE_TOKEN = "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE12"
_INSTALL_ARGS = ("install", "--target", "cursor", "--no-policy", "--parallel-downloads", "0")

_ORIGINALS: dict[str, str] = {
    ".claude/rules/python.md": '---\npaths:\n  - "src/**/*.py"\n---\nUse type hints.\n',
    ".claude/agents/reviewer.md": (
        "---\nname: reviewer\ndescription: Reviews code\ntools: Read, Grep\ncolor: blue\n---\nYou review.\n"
    ),
    ".claude/commands/fix.md": "---\ndescription: Fix lint\n---\nRun the linter on $ARGUMENTS\n",
    ".claude/skills/deploy/SKILL.md": "---\nname: deploy\ndescription: Deploy\n---\n# Deploy\n",
    ".claude/hooks/notify.sh": "#!/bin/sh\necho hi\n",
    ".claude/settings.json": json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"$CLAUDE_PROJECT_DIR"/.claude/hooks/notify.sh',
                            }
                        ],
                    },
                    {
                        "matcher": "Write",
                        "hooks": [{"type": "command", "command": "echo apm-owned"}],
                        "_apm_source": "some-package",
                    },
                ]
            }
        },
        indent=2,
    ),
    ".mcp.json": json.dumps(
        {
            "mcpServers": {
                "fixture": {
                    "command": "printf",
                    "args": ["fixture"],
                    "env": {"FIXTURE_TOKEN": "${FIXTURE_TOKEN}", "MODE": "fast"},
                }
            }
        },
        indent=2,
    ),
    "CLAUDE.md": "# Project\nHand-authored notes for Claude.\n",
}

_IMPORTED: tuple[str, ...] = (
    ".apm/instructions/python.instructions.md",
    ".apm/instructions/claude-root.instructions.md",
    ".apm/agents/reviewer.agent.md",
    ".apm/prompts/fix.prompt.md",
    ".apm/skills/deploy/SKILL.md",
    ".apm/hooks/claude-native.json",
    ".apm/.import-sources.json",
)

_CURSOR_OUTPUTS: tuple[str, ...] = (
    ".cursor/rules/python.mdc",
    ".cursor/rules/claude-root.mdc",
    ".cursor/agents/reviewer.md",
    ".cursor/commands/fix.md",
    ".agents/skills/deploy/SKILL.md",
    ".cursor/hooks.json",
    ".cursor/mcp.json",
)


def _seed(project: Path, extra: dict[str, str] | None = None) -> None:
    for rel, content in {**_ORIGINALS, **(extra or {})}.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (project / ".cursor").mkdir(exist_ok=True)


def _snapshot(project: Path, *, imported: bool = True) -> LifecycleStateSnapshot:
    paths = (*_ORIGINALS, *_CURSOR_OUTPUTS, *(_IMPORTED if imported else ()))
    tracked = [PurePosixPath(p) for p in paths]
    return LifecycleStateSnapshot.capture(
        project, targets=("claude", "cursor"), config_paths=tracked
    )


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nreturncode={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _originals_unchanged(before: LifecycleStateSnapshot, after: LifecycleStateSnapshot) -> None:
    for rel in _ORIGINALS:
        assert after.file(rel).content == before.file(rel).content, f"{rel} was modified"
        assert after.file(rel).content is not None, f"{rel} disappeared"


def _scenario(
    tmp_path: Path, apm_binary_path: Path
) -> tuple[Path, dict[str, str], ApmLifecycleRunner]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_E2E_TESTS"] = "1"
    environment["FIXTURE_TOKEN"] = "fixture-value"
    project = isolated.work_root / "project"
    project.mkdir()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=600
    )
    return project, environment, runner


def test_brownfield_preview_apply_install_rerun(tmp_path: Path, apm_binary_path: Path) -> None:
    project, environment, runner = _scenario(tmp_path, apm_binary_path)
    _seed(project)
    before = _snapshot(project)

    preview = runner.run(
        ("init", "--discover", "--format", "json"),
        scenario_id="preview",
        cwd=project,
        env=environment,
    )
    assert preview.returncode == 0, _evidence(preview)
    inventory = json.loads(preview.stdout)
    assert inventory["schema_version"] == 1 and not inventory["apm_yml_exists"]
    assert not (project / ".apm").exists() and not (project / "apm.yml").exists()
    assert _snapshot(project) == before, "preview must not change any durable state"

    applied = runner.run(
        ("init", "--discover", "--apply", "--yes", "--format", "json"),
        scenario_id="apply",
        cwd=project,
        env=environment,
    )
    assert applied.returncode == 0, _evidence(applied)
    payload = json.loads(applied.stdout)
    assert payload["write"]["status"] == "complete", payload["write"]
    after_apply = _snapshot(project)
    _originals_unchanged(before, after_apply)
    for rel in _IMPORTED:
        assert after_apply.file(rel).content, f"{rel} missing after apply"
    provenance = json.loads(after_apply.file(".apm/.import-sources.json").content)
    written_files = {p for p in payload["write"]["written"] if not p.startswith("hooks/scripts/")}
    assert set(provenance["entries"]) == written_files
    manifest = yaml.safe_load(after_apply.manifest_bytes)
    assert manifest["targets"] == ["claude", "cursor"]
    assert [e["name"] for e in manifest["dependencies"]["mcp"]] == ["fixture"]
    assert "${FIXTURE_TOKEN}" in after_apply.manifest_bytes.decode("utf-8")
    hooks = json.loads(after_apply.file(".apm/hooks/claude-native.json").content)
    commands = [h["command"] for e in hooks["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert commands == ["./.claude/hooks/notify.sh"], commands  # APM-owned entry excluded

    installed = runner.run(
        _INSTALL_ARGS, scenario_id="install-cursor", cwd=project, env=environment
    )
    assert installed.returncode == 0, _evidence(installed)
    after_install = _snapshot(project)
    _originals_unchanged(before, after_install)
    for rel in _CURSOR_OUTPUTS:
        assert after_install.file(rel).content, f"{rel} missing after install"
    rule = after_install.file(".cursor/rules/python.mdc").content.decode("utf-8")
    assert 'globs: "src/**/*.py"' in rule
    root_rule = after_install.file(".cursor/rules/claude-root.mdc").content.decode("utf-8")
    assert 'globs: "**"' in root_rule, "root context must stay always-on on Cursor"
    cursor_hooks = json.loads(after_install.file(".cursor/hooks.json").content)
    assert "echo apm-owned" not in json.dumps(cursor_hooks)
    mcp = json.loads(after_install.file(".cursor/mcp.json").content)
    assert "fixture" in mcp["mcpServers"]

    rerun = runner.run(
        ("init", "--discover", "--apply", "--yes", "--format", "json"),
        scenario_id="rerun",
        cwd=project,
        env=environment,
    )
    assert rerun.returncode == 0, _evidence(rerun)
    assert json.loads(rerun.stdout)["write"]["written"] == []
    reinstalled = runner.run(
        _INSTALL_ARGS, scenario_id="reinstall-cursor", cwd=project, env=environment
    )
    assert reinstalled.returncode == 0, _evidence(reinstalled)
    after_rerun = _snapshot(project)
    assert after_rerun.manifest_bytes == after_install.manifest_bytes
    assert after_rerun.files == after_install.files, "rerun must be byte-idempotent"
    assert after_rerun.semantic_bytes == after_install.semantic_bytes


def test_brownfield_partial_apply_is_unmistakable(tmp_path: Path, apm_binary_path: Path) -> None:
    project, environment, runner = _scenario(tmp_path, apm_binary_path)
    _seed(project, {".claude/rules/leaky.md": f"Use {_FAKE_TOKEN} in CI.\n"})
    before = _snapshot(project)

    applied = runner.run(
        ("init", "--discover", "--apply", "--yes", "--format", "json"),
        scenario_id="partial-apply",
        cwd=project,
        env=environment,
    )
    assert applied.returncode == 1, _evidence(applied)
    payload = json.loads(applied.stdout)
    assert payload["write"]["status"] == "partial"
    failed = {f["path"]: f["reason"] for f in payload["write"]["failed"]}
    assert "github-token" in failed[".claude/rules/leaky.md"]
    assert _FAKE_TOKEN not in applied.stdout and _FAKE_TOKEN not in applied.stderr
    after = _snapshot(project)
    _originals_unchanged(before, after)
    assert not (project / ".apm/instructions/leaky.instructions.md").exists()
    assert after.file(".apm/instructions/python.instructions.md").content
    provenance = json.loads(after.file(".apm/.import-sources.json").content)
    for rel in provenance["entries"]:
        assert (project / ".apm" / rel).exists(), f"provenance names a missing import {rel}"
    assert _FAKE_TOKEN not in b"".join(
        p.read_bytes() for p in (project / ".apm").rglob("*") if p.is_file()
    ).decode("utf-8", errors="ignore")


def test_brownfield_failed_apply_rolls_back(tmp_path: Path, apm_binary_path: Path) -> None:
    project, environment, runner = _scenario(tmp_path, apm_binary_path)
    _seed(project)
    (project / ".apm").write_text("a file blocks the import directory\n", encoding="utf-8")
    before = _snapshot(project, imported=False)

    applied = runner.run(
        ("init", "--discover", "--apply", "--yes"),
        scenario_id="failed-apply",
        cwd=project,
        env=environment,
    )
    assert applied.returncode == 1, _evidence(applied)
    assert "rolled back" in applied.stdout + applied.stderr
    after = _snapshot(project, imported=False)
    _originals_unchanged(before, after)
    assert (project / ".apm").is_file()
    assert not (project / "apm.yml").exists()
    assert after.manifest_bytes is None and after.lockfile_bytes is None
