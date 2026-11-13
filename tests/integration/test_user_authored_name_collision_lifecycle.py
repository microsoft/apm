"""User-authored files that share a name with APM output survive every lifecycle step.

docs/enterprise/security.md (Collision detection): untracked primitive files
keep warning-and-skip behavior; only ``--force`` overwrites them.
docs/reference/cli/uninstall.md: uninstall deletes only files tracked in the
lockfile, so hand-authored content in the same harness folders is left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._target_compile_kit import (
    Scenario,
    deployed_files,
    output,
    write,
    write_package,
    write_project,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

INSTALL = ("install", "--no-policy")

RULE_COLLISIONS = {
    ".claude/rules/global.md": "USER claude rule\n",
    ".cursor/rules/global.mdc": "---\nalwaysApply: true\n---\nUSER cursor rule\n",
}


def _seed(root: Path, files: dict[str, str]) -> dict[str, bytes]:
    for relative, text in files.items():
        write(root / relative, text)
    return {relative: (root / relative).read_bytes() for relative in files}


def _assert_bytes(root: Path, expected: dict[str, bytes], step: str) -> None:
    for relative, data in expected.items():
        path = root / relative
        assert path.is_file(), f"{step}: user file {relative} was deleted"
        assert path.read_bytes() == data, f"{step}: user file {relative} was rewritten"


def _edit_package(package: Path) -> None:
    for path in (package / ".apm").rglob("*.md"):
        path.write_text(path.read_text(encoding="ascii") + "Package v2.\n", encoding="ascii")


def test_same_name_user_rules_survive_install_update_uninstall(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    package = write_package(tmp_path / "pkg")
    project = write_project(tmp_path / "proj", ("claude", "cursor"))
    user = _seed(project, RULE_COLLISIONS)

    first = scenario.run(*INSTALL, cwd=project)
    _assert_bytes(project, user, "install")
    assert "local files exist" in output(first)
    assert not set(RULE_COLLISIONS) & set(deployed_files(project))
    assert (project / ".claude/rules/py.md").is_file()
    assert (project / ".cursor/rules/py.mdc").is_file()

    scenario.run(*INSTALL, cwd=project)
    _assert_bytes(project, user, "repeat install")

    _edit_package(package)
    scenario.run(*INSTALL, cwd=project)
    _assert_bytes(project, user, "install after package edit")
    assert "Package v2." in (project / ".claude/rules/py.md").read_text(encoding="utf-8")

    scenario.run("uninstall", "../pkg", cwd=project)
    _assert_bytes(project, user, "uninstall")
    assert not (project / ".claude/rules/py.md").exists()
    assert not (project / ".cursor/rules/py.mdc").exists()

    forced = _seed(tmp_path / "forced", {".claude/rules/global.md": "USER claude rule\n"})
    forced_project = write_project(tmp_path / "forced", ("claude",))
    scenario.run(*INSTALL, "--force", cwd=forced_project)
    assert (forced_project / ".claude/rules/global.md").read_bytes() != forced[
        ".claude/rules/global.md"
    ]


def test_same_name_user_rule_survives_global_install_and_uninstall(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    package = write_package(tmp_path / "pkg")
    home = scenario.isolated.home
    user = _seed(home, {".claude/rules/global.md": "USER home rule\n"})

    scenario.run("install", "-g", str(package), "--target", "claude", "--no-policy", cwd=tmp_path)
    _assert_bytes(home, user, "install -g")
    assert (home / ".claude/rules/py.md").is_file()

    scenario.run("uninstall", "-g", str(package), cwd=tmp_path)
    _assert_bytes(home, user, "uninstall -g")
    assert not (home / ".claude/rules/py.md").exists()


def test_project_instruction_edit_still_refreshes_native_rule(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """apm#1662 guard: APM-deployed rules from the project's own .apm/ keep updating."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = tmp_path / "proj"
    write(project / "apm.yml", "name: proj\nversion: 1.0.0\ntargets: [claude, cursor]\n")
    source = project / ".apm/instructions/style.instructions.md"
    write(source, '---\napplyTo: "**/*.py"\n---\nSTYLE_V1\n')

    scenario.run(*INSTALL, cwd=project)
    assert "STYLE_V1" in (project / ".claude/rules/style.md").read_text(encoding="utf-8")

    source.write_text('---\napplyTo: "**/*.py"\n---\nSTYLE_V2\n', encoding="ascii")
    scenario.run(*INSTALL, cwd=project)
    for rule in (".claude/rules/style.md", ".cursor/rules/style.mdc"):
        text = (project / rule).read_text(encoding="utf-8")
        assert "STYLE_V2" in text and "STYLE_V1" not in text, rule


def test_uninstall_without_lockfile_keeps_user_instruction_files(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    write_package(tmp_path / "pkg")
    project = write_project(tmp_path / "proj", ("copilot", "cursor", "claude"))
    scenario.run(*INSTALL, cwd=project)
    (project / "apm.lock.yaml").unlink()
    user = _seed(
        project,
        {
            ".cursor/rules/mine.mdc": "USER cursor rule\n",
            ".github/instructions/mine.instructions.md": "USER copilot instruction\n",
            ".claude/rules/mine.md": "USER claude rule\n",
        },
    )

    scenario.run("uninstall", "../pkg", cwd=project)
    _assert_bytes(project, user, "uninstall without lockfile")


def test_identity_and_case_only_collisions_survive_full_chain(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    package = write_package(tmp_path / "pkg")
    (package / ".apm/skills").rename(tmp_path / "skills-parked")
    project = write_project(tmp_path / "proj", ("copilot", "claude", "codex", "opencode", "gemini"))
    user = _seed(
        project,
        {
            ".github/instructions/Global.instructions.md": "USER case-only instruction\n",
            ".github/prompts/hello.prompt.md": "USER prompt\n",
            ".github/agents/REV.agent.md": "USER case-only agent\n",
            ".claude/agents/rev.md": "USER claude agent\n",
            ".claude/commands/Hello.md": "USER case-only command\n",
            ".codex/agents/rev.toml": 'name = "user"\n',
            ".gemini/commands/hello.toml": 'prompt = "user"\n',
            ".opencode/agents/rev.md": "USER opencode agent\n",
            ".opencode/commands/hello.md": "USER opencode command\n",
        },
    )

    installed = scenario.run(*INSTALL, cwd=project)
    _assert_bytes(project, user, "install")
    assert "local files exist" in output(installed)
    claimed = {path.lower() for path in deployed_files(project)}
    assert not {path.lower() for path in user} & claimed

    _edit_package(package)
    scenario.run(*INSTALL, cwd=project)
    _assert_bytes(project, user, "install after package edit")
    scenario.run("update", "--yes", cwd=project)
    _assert_bytes(project, user, "update")
    scenario.run("prune", cwd=project)
    _assert_bytes(project, user, "prune")
    scenario.run("uninstall", "../pkg", cwd=project)
    _assert_bytes(project, user, "uninstall")


def test_identical_user_hook_and_root_context_survive_uninstall_and_compile_clean(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    package = write_package(tmp_path / "pkg")
    hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pkg-hook"}]}
    write(package / ".apm/hooks/lint.json", json.dumps({"hooks": {"PreToolUse": [hook]}}))
    project = write_project(tmp_path / "proj", ("claude", "codex", "copilot"))
    settings = {"hooks": {"PreToolUse": [hook]}, "permissions": {"allow": ["Bash(ls)"]}}
    user = _seed(
        project,
        {
            ".claude/settings.json": json.dumps(settings, indent=2) + "\n",
            ".codex/hooks.json": json.dumps({"hooks": {"PreToolUse": [hook]}}) + "\n",
            "AGENTS.md": "# User AGENTS\n",
            "CLAUDE.md": "# User CLAUDE\n",
            ".github/copilot-instructions.md": "USER copilot instructions\n",
        },
    )

    scenario.run(*INSTALL, cwd=project)
    claude = json.loads((project / ".claude/settings.json").read_text(encoding="utf-8"))
    assert claude["permissions"] == settings["permissions"]
    assert hook in claude["hooks"]["PreToolUse"]
    roots = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")
    _assert_bytes(project, {name: user[name] for name in roots}, "install")

    scenario.run("compile", cwd=project)
    scenario.run("compile", "--clean", cwd=project)
    _assert_bytes(project, {name: user[name] for name in roots}, "compile --clean")

    scenario.run("uninstall", "../pkg", cwd=project)
    for name in (".claude/settings.json", ".codex/hooks.json"):
        hooks = json.loads((project / name).read_text(encoding="utf-8"))["hooks"]
        assert hooks["PreToolUse"] == [hook], f"{name}: identical user hook not preserved"
    claude = json.loads((project / ".claude/settings.json").read_text(encoding="utf-8"))
    assert claude["permissions"] == settings["permissions"]
    _assert_bytes(project, {name: user[name] for name in roots}, "uninstall")
    assert not (project / "apm.lock.yaml").exists() or not load_yaml(project / "apm.lock.yaml").get(
        "dependencies"
    )


def test_jsonc_mcp_configs_are_never_rewritten_from_scratch(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """VS Code, Copilot, Gemini and Kiro configs with comments fail closed, bytes intact."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    write(
        tmp_path / "pkg/apm.yml",
        "name: pkg\nversion: 1.0.0\ndependencies:\n  mcp:\n"
        "    - name: pkgsrv\n      registry: false\n      transport: stdio\n"
        "      command: pkgcmd\n",
    )
    project = write_project(tmp_path / "proj", ("vscode", "copilot", "gemini", "kiro"))
    servers = (
        '{\n  // user comment\n  "mcpServers": {"mine": {"command": "mine"}},\n  "theme": "x"\n}\n'
    )
    configs = {
        ".vscode/mcp.json": '{\n  // user comment\n  "servers": {"mine": {"command": "mine"}}\n}\n',
        ".github/mcp.json": servers,
        ".gemini/settings.json": servers,
        ".kiro/settings/mcp.json": servers,
    }
    user = _seed(project, configs)

    refused = scenario.run(*INSTALL, cwd=project, expect=1)
    _assert_bytes(project, user, "install with JSONC configs")
    assert output(refused).count("Could not parse") >= len(configs), output(refused)

    for relative in configs:
        key = "servers" if relative.startswith(".vscode") else "mcpServers"
        write(project / relative, json.dumps({key: {"mine": {"command": "mine"}}}) + "\n")
    scenario.run(*INSTALL, cwd=project)
    scenario.run("uninstall", "../pkg", cwd=project)
    for relative in configs:
        key = "servers" if relative.startswith(".vscode") else "mcpServers"
        data = json.loads((project / relative).read_text(encoding="utf-8"))
        assert set(data[key]) == {"mine"}, relative
