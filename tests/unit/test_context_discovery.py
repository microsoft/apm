"""Tests for brownfield agent context discovery."""

import os

import yaml

from apm_cli.context_discovery import (
    IMPORT_APM_NATIVE,
    IMPORT_CONVERTIBLE,
    IMPORT_IGNORED,
    IMPORT_REFERENCE_ONLY,
    build_proposed_manifest,
    discover_agent_context,
    execute_migration,
    format_discovery_result,
    redact_text,
)


def _config(name: str = "demo") -> dict[str, str]:
    return {
        "name": name,
        "version": "1.0.0",
        "description": f"APM project for {name}",
        "author": "Tester",
    }


def test_discovers_project_context_and_detects_target(tmp_path):
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "review.md").write_text("review", encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("[mcp_servers]\n", encoding="utf-8")
    (tmp_path / ".apm" / "instructions").mkdir(parents=True)
    (tmp_path / ".apm" / "instructions" / "python.instructions.md").write_text(
        "---\napplyTo: '**/*.py'\n---\nUse pytest.\n",
        encoding="utf-8",
    )

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    files = {finding.display_path: finding for finding in result.findings}
    assert files[".apm/instructions/python.instructions.md"].importability == IMPORT_APM_NATIVE
    assert files[".claude/commands/review.md"].importability == IMPORT_CONVERTIBLE
    assert files[".codex/config.toml"].importability == IMPORT_REFERENCE_ONLY
    assert result.detected_target == "all"
    assert result.proposed_manifest["target"] == "all"


def test_user_scope_scans_only_known_agent_locations(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "review.md").write_text("review", encoding="utf-8")
    (home / "nested" / "project").mkdir(parents=True)
    (home / "nested" / "project" / "AGENTS.md").write_text("do not scan", encoding="utf-8")

    result = discover_agent_context(tmp_path, _config(), home_dir=home, system_dirs=())

    paths = {finding.display_path for finding in result.findings}
    assert "~/.claude/commands/review.md" in paths
    assert "~/nested/project/AGENTS.md" not in paths


def test_existing_manifest_proposal_preserves_fields(tmp_path):
    existing = {
        "name": "existing",
        "version": "2.0.0",
        "dependencies": {"apm": ["owner/repo"]},
        "scripts": {"start": "codex"},
    }

    manifest = build_proposed_manifest(
        _config(),
        tmp_path,
        detected_target="codex",
        existing_manifest=existing,
    )

    assert manifest["name"] == "existing"
    assert manifest["version"] == "2.0.0"
    assert manifest["dependencies"]["apm"] == ["owner/repo"]
    assert manifest["dependencies"]["mcp"] == []
    assert manifest["scripts"] == {"start": "codex"}
    assert manifest["includes"] == "auto"
    assert manifest["target"] == "codex"


def test_binary_file_is_classified_as_ignored(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_bytes(b"\x00not text")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    assert len(result.findings) == 1
    assert result.findings[0].importability == IMPORT_IGNORED
    assert result.findings[0].reason == "binary file ignored"


def test_symlinked_context_file_is_ignored(tmp_path):
    target = tmp_path / "target.md"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / ".claude" / "commands" / "linked.md"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(target, link)
    except OSError:
        return

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    assert result.findings == ()


def test_display_paths_redact_token_like_segments(tmp_path):
    home = tmp_path / "home"
    token_file = home / ".claude" / "commands" / "ghp_1234567890abcdef.md"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("secret filename", encoding="utf-8")

    result = discover_agent_context(tmp_path, _config(), home_dir=home, system_dirs=())

    assert result.findings[0].display_path == "~/.claude/commands/<redacted-token>.md"


def test_all_agent_folders_at_project_scope(tmp_path):
    """Each supported tool is found with the correct tool name and importability."""
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    (tmp_path / ".cursor" / "rules" / "style.md").write_text("cursor rule", encoding="utf-8")

    (tmp_path / ".opencode" / "agents").mkdir(parents=True)
    (tmp_path / ".opencode" / "agents" / "coder.md").write_text("opencode agent", encoding="utf-8")

    (tmp_path / ".windsurf" / "rules").mkdir(parents=True)
    (tmp_path / ".windsurf" / "rules" / "style.md").write_text("windsurf rule", encoding="utf-8")

    (tmp_path / ".gemini" / "commands").mkdir(parents=True)
    (tmp_path / ".gemini" / "commands" / "review.md").write_text("gemini command", encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    by_tool = {f.tool: f for f in result.findings}
    assert by_tool["cursor"].importability == IMPORT_CONVERTIBLE
    assert by_tool["cursor"].kind == "rule"
    assert by_tool["opencode"].importability == IMPORT_CONVERTIBLE
    assert by_tool["opencode"].kind == "agent"
    assert by_tool["windsurf"].importability == IMPORT_CONVERTIBLE
    assert by_tool["windsurf"].kind == "rule"
    assert by_tool["gemini"].importability == IMPORT_CONVERTIBLE
    assert by_tool["gemini"].kind == "command"


def test_user_scope_display_paths_use_tilde(tmp_path):
    """User-scoped findings display as ~/.tool/... paths."""
    home = tmp_path / "home"
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "fix.md").write_text("fix", encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=home,
        system_dirs=(),
    )

    user_findings = [f for f in result.findings if f.scope == "user"]
    assert len(user_findings) == 1
    assert user_findings[0].display_path == "~/.claude/commands/fix.md"


def test_system_scope_discovery(tmp_path):
    """Files in a system directory get scope='system' and importability='reference-only'."""
    sys_dir = tmp_path / "sys"
    (sys_dir / "codex").mkdir(parents=True)
    (sys_dir / "codex" / "config.toml").write_text("[mcp_servers]\n", encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(sys_dir,),
    )

    system_findings = [f for f in result.findings if f.scope == "system"]
    assert len(system_findings) == 1
    assert system_findings[0].tool == "codex"
    assert system_findings[0].importability == IMPORT_REFERENCE_ONLY


def test_empty_project_yields_no_findings(tmp_path):
    """An empty project produces no findings and a sensible text output."""
    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    assert result.findings == ()
    text = format_discovery_result(result, "text")
    assert "No known agent context files found." in text


def test_url_credential_redaction():
    """redact_text strips userinfo from https://user:pass@host/... style paths."""
    dirty = "clone url: https://user:secret@github.example.com/org/repo.git"
    clean = redact_text(dirty)
    assert "secret" not in clean
    assert "github.example.com" in clean


def test_yaml_format_output(tmp_path):
    """format_discovery_result with output_format='yaml' returns valid YAML with a 'files' key."""
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "review.md").write_text("review", encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    output = format_discovery_result(result, "yaml")
    parsed = yaml.safe_load(output)
    assert "files" in parsed
    assert len(parsed["files"]) == 1
    assert parsed["files"][0]["tool"] == "claude"


# ---------------------------------------------------------------------------
# Harness discovery: hooks, commands, styles
# ---------------------------------------------------------------------------


def test_discovers_apm_native_hook_json(tmp_path):
    """hooks/*.json at project root is discovered as apm/hook IMPORT_APM_NATIVE."""
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "validate.json").write_text('{"hooks":{}}', encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    hooks = [f for f in result.findings if f.kind == "hook"]
    assert len(hooks) == 1
    assert hooks[0].tool == "apm"
    assert hooks[0].importability == IMPORT_APM_NATIVE
    assert hooks[0].display_path == "hooks/validate.json"


def test_discovers_apm_dot_apm_hook_json(tmp_path):
    """hooks/*.json under .apm/hooks/ is discovered as apm/hook IMPORT_APM_NATIVE."""
    (tmp_path / ".apm" / "hooks").mkdir(parents=True)
    (tmp_path / ".apm" / "hooks" / "pre-commit.json").write_text('{"hooks":{}}', encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    hooks = [f for f in result.findings if f.kind == "hook"]
    assert len(hooks) == 1
    assert hooks[0].tool == "apm"
    assert hooks[0].importability == IMPORT_APM_NATIVE


def test_discovers_apm_native_hook_script(tmp_path):
    """hooks/scripts/*.sh is discovered as apm/hook-script IMPORT_APM_NATIVE."""
    (tmp_path / "hooks" / "scripts").mkdir(parents=True)
    (tmp_path / "hooks" / "scripts" / "validate.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    scripts = [f for f in result.findings if f.kind == "hook-script"]
    assert len(scripts) == 1
    assert scripts[0].tool == "apm"
    assert scripts[0].importability == IMPORT_APM_NATIVE


def test_discovers_apm_native_command_prompt(tmp_path):
    """`.apm/prompts/**/*.prompt.md` is discovered as apm/command IMPORT_APM_NATIVE."""
    (tmp_path / ".apm" / "prompts").mkdir(parents=True)
    (tmp_path / ".apm" / "prompts" / "review.prompt.md").write_text(
        "Review the diff.", encoding="utf-8"
    )

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    cmds = [f for f in result.findings if f.kind == "command" and f.tool == "apm"]
    assert len(cmds) == 1
    assert cmds[0].importability == IMPORT_APM_NATIVE


def test_discovers_apm_native_style(tmp_path):
    """`.apm/styles/*.style.md` is discovered as apm/style IMPORT_APM_NATIVE."""
    (tmp_path / ".apm" / "styles").mkdir(parents=True)
    (tmp_path / ".apm" / "styles" / "response.style.md").write_text(
        "Always reply in plain text.", encoding="utf-8"
    )

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    styles = [f for f in result.findings if f.kind == "style"]
    assert len(styles) == 1
    assert styles[0].tool == "apm"
    assert styles[0].importability == IMPORT_APM_NATIVE


def test_discovers_copilot_prompt_command(tmp_path):
    """.github/prompts/**/*.prompt.md is discovered as copilot/command."""
    (tmp_path / ".github" / "prompts").mkdir(parents=True)
    (tmp_path / ".github" / "prompts" / "code-review.prompt.md").write_text(
        "Review the PR.", encoding="utf-8"
    )

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    cmds = [f for f in result.findings if f.kind == "command" and f.tool == "copilot"]
    assert len(cmds) == 1
    assert cmds[0].importability == IMPORT_CONVERTIBLE


def test_discovers_copilot_hook(tmp_path):
    """.github/hooks/*.json is discovered as copilot/hook IMPORT_CONVERTIBLE."""
    (tmp_path / ".github" / "hooks").mkdir(parents=True)
    (tmp_path / ".github" / "hooks" / "my-hook.json").write_text(
        '{"version":1,"hooks":{}}', encoding="utf-8"
    )

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    hooks = [f for f in result.findings if f.kind == "hook" and f.tool == "copilot"]
    assert len(hooks) == 1
    assert hooks[0].importability == IMPORT_CONVERTIBLE


def test_discovers_copilot_hook_script(tmp_path):
    """.github/hooks/scripts/**/*.sh is discovered as copilot/hook-script."""
    (tmp_path / ".github" / "hooks" / "scripts").mkdir(parents=True)
    (tmp_path / ".github" / "hooks" / "scripts" / "validate.sh").write_text(
        "#!/bin/bash\n", encoding="utf-8"
    )

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    scripts = [f for f in result.findings if f.kind == "hook-script" and f.tool == "copilot"]
    assert len(scripts) == 1
    assert scripts[0].importability == IMPORT_CONVERTIBLE


def test_discovers_codex_command(tmp_path):
    """.codex/commands/**/*.md is discovered as codex/command IMPORT_CONVERTIBLE."""
    (tmp_path / ".codex" / "commands").mkdir(parents=True)
    (tmp_path / ".codex" / "commands" / "deploy.md").write_text("Run deployment.", encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    cmds = [f for f in result.findings if f.kind == "command" and f.tool == "codex"]
    assert len(cmds) == 1
    assert cmds[0].importability == IMPORT_CONVERTIBLE


def test_discovers_project_style_guide(tmp_path):
    """STYLE.md at project root is discovered as agents/style IMPORT_CONVERTIBLE."""
    (tmp_path / "STYLE.md").write_text("Use clear prose.", encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    styles = [f for f in result.findings if f.kind == "style"]
    assert len(styles) == 1
    assert styles[0].tool == "agents"
    assert styles[0].importability == IMPORT_CONVERTIBLE


def test_discovers_claude_hook_script(tmp_path):
    """.claude/hooks/scripts/**/*.sh is discovered as claude/hook-script."""
    (tmp_path / ".claude" / "hooks" / "scripts").mkdir(parents=True)
    (tmp_path / ".claude" / "hooks" / "scripts" / "pre-tool.sh").write_text(
        "#!/bin/bash\n", encoding="utf-8"
    )

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    scripts = [f for f in result.findings if f.kind == "hook-script" and f.tool == "claude"]
    assert len(scripts) == 1
    assert scripts[0].importability == IMPORT_CONVERTIBLE


def test_harness_mixed_kinds_in_single_project(tmp_path):
    """A project with hook, hook-script, command, and style is all discovered correctly."""
    # APM hook
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "validate.json").write_text("{}", encoding="utf-8")
    # Copilot command
    (tmp_path / ".github" / "prompts").mkdir(parents=True)
    (tmp_path / ".github" / "prompts" / "review.prompt.md").write_text("Review.", encoding="utf-8")
    # Codex command
    (tmp_path / ".codex" / "commands").mkdir(parents=True)
    (tmp_path / ".codex" / "commands" / "deploy.md").write_text("Deploy.", encoding="utf-8")
    # Style guide
    (tmp_path / "STYLE.md").write_text("Be concise.", encoding="utf-8")

    result = discover_agent_context(
        tmp_path,
        _config(),
        home_dir=tmp_path / "home",
        system_dirs=(),
    )

    kinds = {f.kind for f in result.findings}
    assert "hook" in kinds
    assert "command" in kinds
    assert "style" in kinds
    tools = {f.tool for f in result.findings}
    assert "apm" in tools
    assert "copilot" in tools
    assert "codex" in tools
    assert "agents" in tools


# ---------------------------------------------------------------------------
# Migration plan tests
# ---------------------------------------------------------------------------


def test_migration_plan_claude_to_apm(tmp_path):
    """Claude command/agent/root-instructions map to .apm/ equivalents."""
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "review.md").write_text("review", encoding="utf-8")
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "backend.md").write_text("agent", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("instructions", encoding="utf-8")

    result = discover_agent_context(tmp_path, _config(), home_dir=tmp_path / "home", system_dirs=())

    plan = {str(a.dest.relative_to(tmp_path)): a for a in result.migration_plan}
    assert ".apm/prompts/review.prompt.md" in plan
    assert ".apm/agents/backend.agent.md" in plan
    assert ".apm/instructions/CLAUDE.instructions.md" in plan


def test_migration_plan_skips_reference_only(tmp_path):
    """Settings and reference-only files are not included in the migration plan."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

    result = discover_agent_context(tmp_path, _config(), home_dir=tmp_path / "home", system_dirs=())

    assert result.migration_plan == ()


def test_migration_plan_skips_user_scope(tmp_path):
    """User-scope findings are not migrated (they belong to the user, not the project)."""
    home = tmp_path / "home"
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "fix.md").write_text("fix", encoding="utf-8")

    result = discover_agent_context(tmp_path, _config(), home_dir=home, system_dirs=())

    assert result.migration_plan == ()


def test_execute_migration_creates_files(tmp_path):
    """execute_migration copies source files to their .apm/ destinations."""
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "review.md").write_text("review", encoding="utf-8")

    result = discover_agent_context(tmp_path, _config(), home_dir=tmp_path / "home", system_dirs=())

    applied = execute_migration(list(result.migration_plan))

    assert len(applied) == 1
    dest = tmp_path / ".apm" / "prompts" / "review.prompt.md"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "review"


def test_execute_migration_idempotent(tmp_path):
    """Running execute_migration twice does not overwrite or error."""
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "review.md").write_text("original", encoding="utf-8")

    result = discover_agent_context(tmp_path, _config(), home_dir=tmp_path / "home", system_dirs=())

    execute_migration(list(result.migration_plan))
    dest = tmp_path / ".apm" / "prompts" / "review.prompt.md"
    dest.write_text("modified", encoding="utf-8")

    applied = execute_migration(list(result.migration_plan))
    assert applied == []  # nothing applied -- dest already exists
    assert dest.read_text(encoding="utf-8") == "modified"  # not overwritten


def test_migration_preview_shown_in_text_output(tmp_path):
    """Text output includes the migration plan section when convertible files exist."""
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "review.md").write_text("review", encoding="utf-8")

    result = discover_agent_context(tmp_path, _config(), home_dir=tmp_path / "home", system_dirs=())

    text = format_discovery_result(result, "text")
    assert "Migration plan" in text
    assert ".apm/prompts/review.prompt.md" in text
    assert "--write" in text


def test_full_claude_to_codex_migration(tmp_path):
    """End-to-end: discover -> migration plan -> execute -> .apm/ populated."""
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "review.md").write_text("review cmd", encoding="utf-8")
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "backend.md").write_text("backend", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Instructions", encoding="utf-8")

    result = discover_agent_context(tmp_path, _config(), home_dir=tmp_path / "home", system_dirs=())

    assert len(result.migration_plan) == 3
    applied = execute_migration(list(result.migration_plan))
    assert len(applied) == 3

    assert (tmp_path / ".apm" / "prompts" / "review.prompt.md").exists()
    assert (tmp_path / ".apm" / "agents" / "backend.agent.md").exists()
    assert (tmp_path / ".apm" / "instructions" / "CLAUDE.instructions.md").exists()


def test_migration_plan_includes_misplaced_apm_native(tmp_path):
    """APM-native files outside .apm/ or .github/ (e.g. .claude/agents/*.agent.md) are migrated."""
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "agentic-workflows.agent.md").write_text(
        "---\ndescription: Workflows\n---", encoding="utf-8"
    )

    result = discover_agent_context(tmp_path, _config(), home_dir=tmp_path / "home", system_dirs=())

    plan = {str(a.dest.relative_to(tmp_path)): a for a in result.migration_plan}
    assert ".apm/agents/agentic-workflows.agent.md" in plan


def test_migration_plan_skips_apm_native_in_github(tmp_path):
    """.github/agents/*.agent.md (already compiler-visible) is NOT migrated."""
    (tmp_path / ".github" / "agents").mkdir(parents=True)
    (tmp_path / ".github" / "agents" / "my-agent.agent.md").write_text(
        "---\ndescription: x\n---", encoding="utf-8"
    )

    result = discover_agent_context(tmp_path, _config(), home_dir=tmp_path / "home", system_dirs=())

    agent_dests = [str(a.dest.relative_to(tmp_path)) for a in result.migration_plan]
    assert not any("my-agent" in d for d in agent_dests)


def test_migration_plan_skips_apm_native_in_apm_dir(tmp_path):
    """.apm/agents/*.agent.md (canonical location) is NOT migrated."""
    (tmp_path / ".apm" / "agents").mkdir(parents=True)
    (tmp_path / ".apm" / "agents" / "my-agent.agent.md").write_text(
        "---\ndescription: x\n---", encoding="utf-8"
    )

    result = discover_agent_context(tmp_path, _config(), home_dir=tmp_path / "home", system_dirs=())

    agent_dests = [str(a.dest.relative_to(tmp_path)) for a in result.migration_plan]
    assert not any("my-agent" in d for d in agent_dests)
