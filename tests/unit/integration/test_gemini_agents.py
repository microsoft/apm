"""Gemini CLI agent deployment (#2990): .gemini/agents/<name>.md.

Gemini CLI v0.70.0 loads local subagents from ``.gemini/agents/*.md`` and
``~/.gemini/agents/*.md`` with a strict frontmatter schema, so APM renders
an allowlisted frontmatter and skips agents Gemini would reject or that it
cannot carry without changing their access.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
)
from apm_cli.utils.diagnostics import CATEGORY_WARNING, DiagnosticCollector
from tests.utils.deployment_evidence import prove_overwrites, receipted_evidence

pytestmark = [pytest.mark.component, pytest.mark.promise("req-tg-009")]

# Keys accepted by the Gemini CLI v0.70.0 local-agent schema.
_GEMINI_SCHEMA_KEYS = {
    "kind",
    "name",
    "description",
    "display_name",
    "tools",
    "mcp_servers",
    "model",
    "temperature",
    "max_turns",
    "timeout_mins",
}


def _package_info(package_dir: Path, name: str = "test-pkg") -> PackageInfo:
    return PackageInfo(
        package=APMPackage(
            name=name,
            version="1.0.0",
            package_path=package_dir,
            source=f"github.com/test/{name}",
        ),
        install_path=package_dir,
        resolved_reference=ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        ),
        installed_at=datetime.now().isoformat(),
    )


def _write_agent(project: Path, filename: str, content: str) -> Path:
    agents = project / "pkg" / ".apm" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    source = agents / filename
    source.write_text(content, encoding="utf-8")
    return source


def _integrate(project: Path, **kwargs):
    (project / ".gemini").mkdir(exist_ok=True)
    diagnostics = kwargs.pop("diagnostics", DiagnosticCollector())
    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["gemini"],
        _package_info(project / "pkg"),
        project,
        diagnostics=diagnostics,
        **kwargs,
    )
    return result, diagnostics


def _frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, text
    return yaml.safe_load(match.group(1))


def _warnings(diagnostics: DiagnosticCollector) -> list[str]:
    return [d.message for d in diagnostics.by_category().get(CATEGORY_WARNING, [])]


def test_gemini_agent_renders_only_schema_keys(tmp_path: Path) -> None:
    _write_agent(
        tmp_path,
        "reviewer.agent.md",
        "---\n"
        "name: reviewer\n"
        "description: Reviews diffs\n"
        "display_name: Reviewer\n"
        "tools:\n- read_file\n- grep_search\n- mcp_github_*\n"
        "model: gemini-3-flash-preview\n"
        "temperature: 0.2\n"
        "max_turns: 10\n"
        "timeout_mins: 5\n"
        "color: blue\n"
        "argument-hint: '[path]'\n"
        "handoffs: []\n"
        "---\n\n# Reviewer\n\nReview the diff.\n",
    )

    result, diagnostics = _integrate(tmp_path)

    deployed = tmp_path / ".gemini" / "agents" / "reviewer.md"
    assert result.files_integrated == 1
    assert result.target_paths == [deployed]
    text = deployed.read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert fm == {
        "name": "reviewer",
        "description": "Reviews diffs",
        "tools": ["read_file", "grep_search", "mcp_github_*"],
        "model": "gemini-3-flash-preview",
        "display_name": "Reviewer",
        "temperature": 0.2,
        "max_turns": 10,
        "timeout_mins": 5,
    }
    assert set(fm) <= _GEMINI_SCHEMA_KEYS
    assert text.endswith("# Reviewer\n\nReview the diff.\n")
    assert diagnostics.by_category() == {}


def test_gemini_agent_name_defaults_to_file_stem_and_tools_stay_omitted(tmp_path: Path) -> None:
    _write_agent(tmp_path, "build-worker.md", "---\ndescription: Runs the build\n---\nBuild.\n")

    result, _ = _integrate(tmp_path)

    assert result.files_integrated == 1
    fm = _frontmatter((tmp_path / ".gemini" / "agents" / "build-worker.md").read_text())
    # No synthesized tools list: an omitted 'tools' inherits the session's tools.
    assert fm == {"name": "build-worker", "description": "Runs the build"}


def test_gemini_agent_with_utf8_bom_deploys_without_bom(tmp_path: Path) -> None:
    # Gemini's frontmatter regex accepts a leading BOM (skillLoader.ts FRONTMATTER_REGEX).
    agents = tmp_path / "pkg" / ".apm" / "agents"
    agents.mkdir(parents=True)
    (agents / "bom.agent.md").write_bytes(b"\xef\xbb\xbf---\ndescription: With BOM\n---\nBody\n")

    result, diagnostics = _integrate(tmp_path)

    assert result.files_integrated == 1
    text = (tmp_path / ".gemini" / "agents" / "bom.md").read_text(encoding="utf-8")
    assert _frontmatter(text) == {"name": "bom", "description": "With BOM"}
    assert diagnostics.by_category() == {}


def test_gemini_agent_infinite_temperature_is_kept(tmp_path: Path) -> None:
    # Gemini's z.number() accepts Infinity and rejects only NaN.
    _write_agent(tmp_path, "t.agent.md", "---\ndescription: d\ntemperature: .inf\n---\nB\n")

    result, _ = _integrate(tmp_path)

    assert result.files_integrated == 1
    deployed = tmp_path / ".gemini" / "agents" / "t.md"
    assert _frontmatter(deployed.read_text(encoding="utf-8"))["temperature"] == float("inf")


def test_gemini_agent_empty_tools_list_is_kept(tmp_path: Path) -> None:
    _write_agent(tmp_path, "x.agent.md", "---\ndescription: No tools\ntools: []\n---\nBody\n")

    result, _ = _integrate(tmp_path)

    assert result.files_integrated == 1
    assert _frontmatter((tmp_path / ".gemini" / "agents" / "x.md").read_text())["tools"] == []


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        ("a.agent.md", "Body only, no frontmatter.\n", "'description' is required"),
        ("a.agent.md", "---\nname: a\n---\nBody\n", "'description' is required"),
        ("a.agent.md", "---\ndescription: '  '\n---\nBody\n", "'description' is required"),
        ("a.agent.md", "---\nname: Code Reviewer\ndescription: d\n---\nB\n", "lowercase slug"),
        ("Reviewer.agent.md", "---\ndescription: d\n---\nB\n", "lowercase slug"),
        ("a.agent.md", "---\ndescription: d\ntools: [Read, Grep]\n---\nB\n", "'Grep', 'Read'"),
        ("a.agent.md", "---\ndescription: d\ntools: [codebase]\n---\nB\n", "'codebase'"),
        ("a.agent.md", "---\ndescription: d\ntools: read_file\n---\nB\n", "must be a list"),
        ("a.agent.md", "---\ndescription: d\ntools: [mcp__x]\n---\nB\n", "'mcp__x'"),
        ("a.agent.md", "---\ndescription: d\nmodel: claude-sonnet-4-5\n---\nB\n", "'model'"),
        ("a.agent.md", "---\ndescription: d\nmodel: sonnet\n---\nB\n", "'model'"),
        ("a.agent.md", "---\ndescription: d\nmcp_servers: {}\n---\nB\n", "'mcp_servers'"),
        ("a.agent.md", "---\ndescription: d\nmcpServers: {}\n---\nB\n", "'mcpServers'"),
        ("a.agent.md", "---\ndescription: d\nmcp-servers: {}\n---\nB\n", "'mcp-servers'"),
        ("a.agent.md", "---\ndescription: d\ndisallowedTools: [x]\n---\nB\n", "'disallowedTools'"),
        ("a.agent.md", "---\ndescription: d\npermissionMode: plan\n---\nB\n", "'permissionMode'"),
        ("a.agent.md", "---\ndescription: d\nkind: remote\n---\nB\n", "only local agents"),
        ("a.agent.md", "---\ndescription: d\ntemperature: hot\n---\nB\n", "'temperature'"),
        ("a.agent.md", "---\ndescription: d\ntemperature: .nan\n---\nB\n", "'temperature'"),
        ("a.agent.md", "---\ndescription: d\nmax_turns: 0\n---\nB\n", "'max_turns'"),
        ("a.agent.md", "---\ndescription: d\ntimeout_mins: true\n---\nB\n", "'timeout_mins'"),
        ("a.agent.md", "---\ndescription: d\ndisplay_name: 3\n---\nB\n", "'display_name'"),
        ("a.agent.md", "---\ndescription: [unclosed\n---\nB\n", "not valid YAML"),
        ("a.agent.md", "---\n- a\n- b\n---\nB\n", "'description' is required"),
        ("_hidden.agent.md", "---\ndescription: d\n---\nB\n", "starts with '_'"),
    ],
)
def test_gemini_agent_rejected_with_warning_and_nothing_written(
    tmp_path: Path, filename: str, content: str, expected: str
) -> None:
    _write_agent(tmp_path, filename, content)

    result, diagnostics = _integrate(tmp_path)

    assert result.files_integrated == 0
    assert result.files_skipped == 1
    assert result.target_paths == []
    # Fail closed before any mutation: the agents directory is not created.
    assert not (tmp_path / ".gemini" / "agents").exists()
    [message] = _warnings(diagnostics)
    assert expected in message
    assert "agent will not be deployed" in message


def test_gemini_tool_rejection_names_the_full_approved_set(tmp_path: Path) -> None:
    from apm_cli.integration.agent_integrator import GEMINI_AGENT_BUILTIN_TOOLS

    _write_agent(tmp_path, "a.agent.md", "---\ndescription: d\ntools: [Read]\n---\nB\n")

    _, diagnostics = _integrate(tmp_path)

    [message] = _warnings(diagnostics)
    assert all(tool in message for tool in GEMINI_AGENT_BUILTIN_TOOLS)
    assert "mcp_<server>_<tool>" in message
    assert not (tmp_path / ".gemini" / "agents" / "a.md").exists()


def test_gemini_invalid_agent_does_not_block_siblings(tmp_path: Path) -> None:
    _write_agent(tmp_path, "good.agent.md", "---\ndescription: Good\n---\nGood.\n")
    _write_agent(tmp_path, "bad.agent.md", "---\ndescription: Bad\ntools: [Bash]\n---\nBad.\n")

    result, diagnostics = _integrate(tmp_path)

    assert result.files_integrated == 1
    assert result.files_skipped == 1
    assert (tmp_path / ".gemini" / "agents" / "good.md").exists()
    assert not (tmp_path / ".gemini" / "agents" / "bad.md").exists()
    assert len(_warnings(diagnostics)) == 1


def test_gemini_preflight_runs_before_adopt(tmp_path: Path) -> None:
    """A pre-seeded target byte-identical to an invalid source is not adopted."""
    bad = "---\ndescription: Bad\ntools: [Read]\n---\nBad.\n"
    _write_agent(tmp_path, "agent.agent.md", bad)
    preset = tmp_path / ".gemini" / "agents" / "agent.md"
    preset.parent.mkdir(parents=True)
    preset.write_text(bad, encoding="utf-8")

    result, diagnostics = _integrate(tmp_path, managed_files={".gemini/agents/agent.md"})

    assert result.files_adopted == 0
    assert result.files_integrated == 0
    assert result.target_paths == []
    assert "'Read'" in _warnings(diagnostics)[0]
    assert preset.read_text(encoding="utf-8") == bad


def test_gemini_reinstall_adopts_and_update_rewrites(tmp_path: Path, monkeypatch) -> None:
    prove_overwrites(monkeypatch)
    source = _write_agent(tmp_path, "w.agent.md", "---\ndescription: v1\n---\nOne.\n")
    first, _ = _integrate(tmp_path)
    deployed = first.target_paths[0]
    managed = {".gemini/agents/w.md"}

    again, _ = _integrate(tmp_path, managed_files=managed)
    assert again.files_integrated == 0
    assert again.files_adopted == 1
    assert again.target_paths == [deployed]

    source.write_text("---\ndescription: v2\n---\nTwo.\n", encoding="utf-8")
    updated, _ = _integrate(tmp_path, managed_files=managed)
    assert updated.files_integrated == 1
    assert _frontmatter(deployed.read_text())["description"] == "v2"
    assert deployed.read_text().endswith("Two.\n")


def test_gemini_user_authored_file_is_not_overwritten(tmp_path: Path) -> None:
    _write_agent(tmp_path, "mine.agent.md", "---\ndescription: From package\n---\nPkg.\n")
    user_file = tmp_path / ".gemini" / "agents" / "mine.md"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("---\nname: mine\ndescription: Mine\n---\nUser.\n", encoding="utf-8")

    result, _ = _integrate(tmp_path, managed_files=set())

    assert result.files_integrated == 0
    assert result.files_skipped == 1
    assert user_file.read_text(encoding="utf-8").endswith("User.\n")


def test_gemini_sync_removes_managed_agent(tmp_path: Path) -> None:
    managed = tmp_path / ".gemini" / "agents" / "old.md"
    managed.parent.mkdir(parents=True)
    managed.write_text("---\nname: old\ndescription: Old\n---\nOld.\n", encoding="utf-8")
    unmanaged = managed.parent / "keep.md"
    unmanaged.write_text("---\nname: keep\ndescription: Keep\n---\nKeep.\n", encoding="utf-8")

    result = AgentIntegrator().sync_for_target(
        KNOWN_TARGETS["gemini"],
        APMPackage(name="test-pkg", version="1.0.0", package_path=tmp_path),
        tmp_path,
        managed_files={".gemini/agents/old.md"},
        evidence=receipted_evidence(tmp_path),
    )

    assert result["files_removed"] == 1
    assert not managed.exists()
    assert unmanaged.exists()


def test_gemini_agent_body_links_resolve_from_deployed_location(tmp_path: Path) -> None:
    package_dir = tmp_path / "apm_modules" / "owner" / "pkg"
    agents = package_dir / ".apm" / "agents"
    agents.mkdir(parents=True)
    skill = package_dir / ".apm" / "skills" / "guide" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: guide\ndescription: Guide\n---\nGuide.\n", encoding="utf-8")
    (agents / "reviewer.agent.md").write_text(
        "---\ndescription: Review\n---\nUse the [guide](../skills/guide/SKILL.md#usage).\n",
        encoding="utf-8",
    )
    (tmp_path / ".gemini").mkdir()

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["gemini"], _package_info(package_dir), tmp_path
    )

    deployed = tmp_path / ".gemini" / "agents" / "reviewer.md"
    assert result.links_resolved == 1
    link = re.search(r"\]\(([^)]+)\)", deployed.read_text()).group(1)
    path, _, fragment = link.partition("#")
    assert fragment == "usage"
    assert (deployed.parent / path).resolve() == skill.resolve()


def test_gemini_agent_user_invocable_false_warns_lossy(tmp_path: Path) -> None:
    _write_agent(
        tmp_path, "i.agent.md", "---\ndescription: Internal\nuser-invocable: false\n---\nB\n"
    )

    result, diagnostics = _integrate(tmp_path)

    assert result.files_integrated == 1
    lossy = [d for ds in diagnostics.by_category().values() for d in ds]
    assert any("Gemini agent i.agent.md" in d.message for d in lossy)
    deployed = tmp_path / ".gemini" / "agents" / "i.md"
    assert _frontmatter(deployed.read_text(encoding="utf-8")) == {
        "name": "i",
        "description": "Internal",
    }


def test_gemini_agents_skipped_without_gemini_dir(tmp_path: Path) -> None:
    _write_agent(tmp_path, "a.agent.md", "---\ndescription: d\n---\nB\n")

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["gemini"], _package_info(tmp_path / "pkg"), tmp_path
    )

    assert result.files_integrated == 0
    assert not (tmp_path / ".gemini").exists()
