"""Converters, the --write flow, idempotency and the CLI surface."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.adopt.converters import ConvertContext, ConvertResult, register_builtin_converters
from apm_cli.adopt.converters.mcp import placeholder_name, to_manifest_entry
from apm_cli.adopt.converters.root_context import strip_managed_section
from apm_cli.adopt.converters.rules import RulesConverter
from apm_cli.adopt.model import Finding, HarnessKind, Importability, Ownership, Scope
from apm_cli.adopt.redact import Redactor, looks_like_secret
from apm_cli.cli import cli
from apm_cli.integration.hook_file_routing import _hook_file_allowed_targets
from tests.unit.adopt.conftest import FAKE_TOKEN

pytestmark = pytest.mark.component


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and ".apm" not in p.parts and p.name != "apm.yml"
    }


def _finding(path: Path, kind: HarnessKind, tool: str, fmt: str, converter: str) -> Finding:
    return Finding(
        id="x",
        tool=tool,
        scope=Scope.PROJECT,
        kind=kind,
        display_path=path.name,
        importability=Importability.CONVERTIBLE,
        ownership=Ownership.HOST_OWNED,
        converter_id=converter,
        abs_path=path,
        format_id=fmt,
    )


@pytest.mark.parametrize(
    ("fmt", "source", "expected_apply_to", "warn"),
    [
        ("cursor_rules", '---\ndescription: D\nglobs: "**/*.ts"\n---\nbody\n', "**/*.ts", False),
        (
            "cursor_rules",
            "---\ndescription: D\nalwaysApply: true\nglobs: x\n---\nbody\n",
            "**",
            True,
        ),
        ("cursor_rules", "---\ndescription: D\n---\nbody\n", None, True),
        (
            "claude_rules",
            '---\npaths:\n  - "src/**"\n  - "lib/**"\n---\nbody\n',
            "src/**,lib/**",
            False,
        ),
        (
            "kiro_steering",
            '---\ninclusion: fileMatch\nfileMatchPattern: "*.py"\n---\nbody\n',
            "*.py",
            False,
        ),
        ("kiro_steering", "---\ninclusion: manual\n---\nbody\n", None, True),
        ("kiro_steering", "---\ninclusion: always\n---\nbody\n", "**", False),
        ("claude_rules", "Plain always-on rule body\n", "**", False),
        ("windsurf_rules", '---\ntrigger: glob\nglobs: "*.go"\n---\nbody\n', "*.go", False),
        (
            "antigravity_rules",
            "---\ntrigger: model_decision\ndescription: D\n---\nbody\n",
            None,
            True,
        ),
    ],
)
def test_rules_converter_maps_scoping(
    tmp_path: Path, fmt: str, source: str, expected_apply_to, warn: bool
):
    src = tmp_path / "rule.md"
    src.write_text(source, encoding="utf-8")
    dest = tmp_path / ".apm" / "instructions" / "rule.instructions.md"
    ctx = ConvertContext(project_root=tmp_path, scope=Scope.PROJECT, redactor=Redactor(tmp_path))
    result = RulesConverter().convert(
        _finding(src, HarnessKind.RULE, "t", fmt, f"{fmt}->instruction"), dest, ctx=ctx
    )
    post = yaml.safe_load(dest.read_text(encoding="utf-8").split("---")[1])
    assert post.get("applyTo") == expected_apply_to
    assert post["description"]
    assert any(c.severity == "warning" for c in result.changes) is warn


def test_strip_managed_section_keeps_outside_text():
    result = ConvertResult()
    text = "keep\n<!-- apm:start -->\ngenerated\n<!-- apm:end -->\ntail\n"
    assert strip_managed_section(text, result) == "keep\n\ntail\n"
    assert result.lossy


def test_mcp_entry_redacts_secrets_and_keeps_placeholders():
    result = ConvertResult()
    entry = to_manifest_entry(
        "claude",
        "github",
        {
            "command": "npx -y pkg",
            "env": {"GITHUB_TOKEN": FAKE_TOKEN, "OTHER": "${env:OTHER}", "MODE": "fast"},
        },
        result,
    )
    assert entry["command"] == "npx" and entry["args"] == ["-y", "pkg"]
    assert entry["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
    assert entry["env"]["OTHER"] == "${OTHER}"
    assert entry["env"]["MODE"] == "fast"
    assert FAKE_TOKEN not in json.dumps(entry)
    assert FAKE_TOKEN not in " ".join(c.reason for c in result.changes)
    remote = to_manifest_entry(
        "codex",
        "r",
        {"url": "https://u:p@h/x?token=abc", "bearer_token_env_var": "TOK"},
        ConvertResult(),
    )
    assert remote["transport"] == "http"
    from urllib.parse import urlsplit

    parsed = urlsplit(remote["url"])
    assert parsed.hostname == "h" and parsed.username == "u"
    assert parsed.password == "${R_URL_PASSWORD}"
    assert remote["headers"]["Authorization"] == "Bearer ${TOK}"
    assert placeholder_name("github", "GITHUB_TOKEN") == "${GITHUB_TOKEN}"
    assert looks_like_secret("api_key", "x") and not looks_like_secret("MODE", "fast")


def test_generated_hook_filenames_stay_universal():
    for tool in (
        "claude",
        "cursor",
        "codex",
        "gemini",
        "windsurf",
        "antigravity",
        "kiro",
        "copilot",
    ):
        assert _hook_file_allowed_targets(Path(f"{tool}-native.json")) is None


def test_registry_has_every_classified_converter():
    from apm_cli.adopt.classify import classification_rows
    from apm_cli.adopt.converters import CONVERTERS

    register_builtin_converters()
    for (_tool, _kind), rule in classification_rows().items():
        if rule.converter:
            assert CONVERTERS.get(rule.converter.replace("{format}", "cursor_rules")) is not None, (
                rule
            )


def test_discover_default_is_read_only(in_project: Path):
    result = CliRunner().invoke(cli, ["init", "--discover"])
    assert result.exit_code == 0, result.output
    assert not (in_project / ".apm").exists()
    assert not (in_project / "apm.yml").exists()
    assert "claude" in result.output and "Re-run with --apply" in result.output


def test_discover_json_schema(in_project: Path):
    result = CliRunner().invoke(cli, ["init", "--discover", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert set(payload) >= {"findings", "errors", "detected_tools", "proposed_targets", "counts"}
    first = payload["findings"][0]
    assert set(first) == {
        "id",
        "tool",
        "scope",
        "kind",
        "path",
        "importability",
        "ownership",
        "risk",
        "converter",
        "proposed_target",
        "size_bytes",
        "notes",
        "evidence",
    }
    assert FAKE_TOKEN not in result.output
    yaml_result = CliRunner().invoke(cli, ["init", "--discover", "--format", "yaml"])
    assert yaml_result.exit_code == 0 and yaml.safe_load(yaml_result.output)["schema_version"] == 1


def test_write_flags_require_discover(in_project: Path):
    result = CliRunner().invoke(cli, ["init", "--apply", "--yes"])
    assert result.exit_code == 2 and "require --discover" in result.output
    assert CliRunner().invoke(cli, ["init", "--write", "--yes"]).exit_code == 2


def test_hidden_alias_matches_init(in_project: Path):
    a = CliRunner().invoke(cli, ["discover", "--format", "json"])
    b = CliRunner().invoke(cli, ["init", "--discover", "--format", "json"])
    assert a.exit_code == 0 and json.loads(a.output)["findings"] == json.loads(b.output)["findings"]
    listing = CliRunner().invoke(cli, ["--help"]).output.split("Commands:")[-1]
    assert not any(line.split()[:1] == ["discover"] for line in listing.splitlines())


def test_write_materializes_merges_and_is_idempotent(in_project: Path):
    before = _snapshot(in_project)
    result = CliRunner().invoke(cli, ["init", "--discover", "--write", "--yes"])
    assert result.exit_code == 0, result.output
    apm = in_project / ".apm"
    assert (apm / "instructions/python.instructions.md").is_file()
    assert (apm / "instructions/style.instructions.md").is_file()
    assert (apm / "instructions/docs.instructions.md").is_file()
    assert (apm / "instructions/claude-root.instructions.md").is_file()
    assert (apm / "agents/reviewer.agent.md").is_file()
    assert (apm / "prompts/frontend-fix.prompt.md").is_file()
    assert (apm / "skills/deploy/SKILL.md").is_file()
    assert (apm / "skills/shared-skill/SKILL.md").is_file()
    hooks = json.loads((apm / "hooks/claude-native.json").read_text(encoding="utf-8"))
    commands = [h["command"] for e in hooks["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert commands == ["./.claude/hooks/notify.sh"]  # APM-owned entry excluded, path normalised
    assert not (apm / "hooks/scripts").exists()
    manifest = yaml.safe_load((in_project / "apm.yml").read_text(encoding="utf-8"))
    assert manifest["targets"] == ["claude", "copilot", "cursor"]
    names = {e["name"] for e in manifest["dependencies"]["mcp"]}
    assert names == {"github", "remote"}
    raw_manifest = (in_project / "apm.yml").read_text(encoding="utf-8")
    assert FAKE_TOKEN not in raw_manifest and "${GITHUB_TOKEN}" in raw_manifest
    assert FAKE_TOKEN not in result.output
    assert _snapshot(in_project) == before  # originals untouched
    provenance = json.loads((apm / ".import-sources.json").read_text(encoding="utf-8"))
    assert "instructions/python.instructions.md" in provenance["entries"]

    again = CliRunner().invoke(cli, ["init", "--discover", "--write", "--yes", "--format", "json"])
    assert again.exit_code == 0, again.output
    payload = json.loads(again.output)
    assert payload["write"]["written"] == []
    assert yaml.safe_load((in_project / "apm.yml").read_text(encoding="utf-8")) == manifest


def test_write_copies_hook_scripts_when_requested(in_project: Path):
    result = CliRunner().invoke(
        cli, ["init", "--discover", "--write", "--yes", "--include-hook-scripts"]
    )
    assert result.exit_code == 0, result.output
    script = in_project / ".apm/hooks/scripts/notify.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK) == os.access(in_project / ".claude/hooks/notify.sh", os.X_OK)
    hooks = json.loads((in_project / ".apm/hooks/claude-native.json").read_text(encoding="utf-8"))
    assert hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "./scripts/notify.sh"


def test_write_refuses_without_yes_when_not_interactive(in_project: Path):
    result = CliRunner().invoke(cli, ["init", "--discover", "--write"])
    assert result.exit_code == 1
    assert "pass --yes to apply" in result.output.replace("\n", " ")
    assert not (in_project / ".apm").exists()


def test_write_skips_locally_modified_import(in_project: Path):
    assert CliRunner().invoke(cli, ["init", "--discover", "--write", "--yes"]).exit_code == 0
    target = in_project / ".apm/instructions/python.instructions.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
    (in_project / ".claude/rules/python.md").write_text("changed source\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", "--discover", "--write", "--yes", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert "local edit" in target.read_text(encoding="utf-8")
    assert (
        "instructions/python.instructions.md" not in json.loads(result.output)["write"]["written"]
    )


def test_global_scope_scans_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".claude/agents").mkdir(parents=True)
    (home / ".claude/agents/helper.md").write_text(
        "---\ndescription: H\n---\nhelp\n", encoding="utf-8"
    )
    (home / ".cursor").mkdir()
    (home / ".cursor/mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"s": {"command": "x", "env": {"API_KEY": "sk-secretsecretsecret123"}}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["init", "--discover", "--global", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    paths = {f["path"] for f in payload["findings"]}
    assert "~/.claude/agents/helper.md" in paths
    assert any(p.startswith("~/.cursor/mcp.json#s") for p in paths)
    assert "sk-secret" not in result.output
    assert payload["scopes"] == ["user"]


def test_lenient_frontmatter_for_claude_style_descriptions(tmp_path: Path):
    """Claude Code accepts unquoted descriptions containing colons; so must the import."""
    from apm_cli.adopt.converters.agents import AgentsConverter
    from apm_cli.adopt.converters.base import lenient_frontmatter

    src = tmp_path / "reviewer.md"
    src.write_text(
        "---\nname: reviewer\ndescription: Use this agent when: code changes. Examples:\\n<example>x</example>\n"
        "model: opus\ncolor: green\ntools:\n  - Read\n  - Grep\n---\n\nBody.\n",
        encoding="utf-8",
    )
    parsed = lenient_frontmatter(src.read_text(encoding="utf-8"))
    assert parsed is not None
    multiline = lenient_frontmatter(
        "---\nname: x\ndescription: Use when needed. Examples:\n<example>\nContext: The user asks.\n"
        'user: "do it"\nassistant: "ok"\n</example>\nmodel: opus\n---\nbody\n'
    )
    assert multiline is not None
    assert set(multiline[0]) == {"name", "description", "model"}
    assert "Context: The user asks." in multiline[0]["description"]
    meta, body = parsed
    assert (
        meta["name"] == "reviewer" and meta["tools"] == ["Read", "Grep"] and body.strip() == "Body."
    )
    dest = tmp_path / ".apm/agents/reviewer.agent.md"
    ctx = ConvertContext(project_root=tmp_path, scope=Scope.PROJECT, redactor=Redactor(tmp_path))
    result = AgentsConverter().convert(
        _finding(src, HarnessKind.AGENT, "claude", "claude_agent", "claude_agent->agent"),
        dest,
        ctx=ctx,
    )
    assert any(c.path == "frontmatter" and c.action == "transformed" for c in result.changes)
    reparsed = yaml.safe_load(dest.read_text(encoding="utf-8").split("---")[1])
    assert reparsed["description"].startswith("Use this agent when: code changes.")
    assert reparsed["tools"] == ["Read", "Grep"] and "color" not in reparsed


def test_apply_alias_and_credential_refusal(in_project: Path):
    """--apply is the primary spelling; files carrying literal secrets are never copied."""
    from tests.unit.adopt.conftest import write

    write(in_project / ".claude/rules/leaky.md", f"Use token {FAKE_TOKEN} for CI.\n")
    result = CliRunner().invoke(cli, ["init", "--discover", "--apply", "--yes", "--format", "json"])
    assert result.exit_code == 1, result.output  # partial import is unmistakable in exit status
    payload = json.loads(result.output)
    assert payload["write"]["status"] == "partial"
    assert (in_project / ".apm/instructions/python.instructions.md").is_file()
    assert not (in_project / ".apm/instructions/leaky.instructions.md").exists()
    provenance = json.loads((in_project / ".apm/.import-sources.json").read_text(encoding="utf-8"))
    assert set(provenance["entries"]) == set(payload["write"]["written"]) - {
        p for p in payload["write"]["written"] if p.startswith("skills/")
    } | {p for p in provenance["entries"] if p.startswith("skills/")}
    failed = {f["path"]: f["reason"] for f in payload["write"]["failed"]}
    assert "github-token" in failed[".claude/rules/leaky.md"]
    assert FAKE_TOKEN not in result.output
    assert FAKE_TOKEN not in "".join(
        p.read_text(encoding="utf-8") for p in (in_project / ".apm").rglob("*.md")
    )


def test_failed_apply_rolls_back_everything(in_project: Path):
    """A commit failure leaves no .apm/ files, no provenance and no apm.yml behind."""
    (in_project / ".apm").write_text("not a directory\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", "--discover", "--apply", "--yes", "--format", "json"])
    assert result.exit_code == 1, result.output
    assert (in_project / ".apm").is_file()
    assert not (in_project / "apm.yml").exists()
    assert "rolled back" in (
        result.output + result.stderr if hasattr(result, "stderr") else result.output
    )
