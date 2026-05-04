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
