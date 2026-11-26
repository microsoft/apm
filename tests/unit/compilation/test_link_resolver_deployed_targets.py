"""Links to files the same install deploys name the deployed copy (#5523)."""

import os
import re
from pathlib import Path

import pytest

from apm_cli.compilation.link_resolver import UnifiedLinkResolver

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I7")]


def _assert_link_resolves(content: str, label: str, deployed_file: Path) -> None:
    """The rendered *label* link names an existing file from *deployed_file* (fresh clone)."""
    match = re.search(rf"\[{label}\]\(([^)#]+)", content)
    assert match is not None, content
    assert Path(os.path.normpath(deployed_file.parent / match.group(1))).is_file(), content


class TestDeployedFileLinks:
    """A link to a file the same install deploys names the deployed copy (#5523)."""

    def _world(self, tmp_path: Path):
        project = tmp_path / "project"
        package = project / "apm_modules" / "_local" / "pkg"
        agent = package / ".apm" / "agents" / "reviewer.agent.md"
        skill = package / ".apm" / "skills" / "demo" / "SKILL.md"
        notes = package / ".apm" / "notes.md"
        for path in (agent, skill, notes):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        deployed = project / ".agents" / "skills" / "demo"
        deployed.mkdir(parents=True)
        (deployed / "SKILL.md").write_text("x", encoding="utf-8")
        (deployed / "design.context.md").write_text("x", encoding="utf-8")
        resolver = UnifiedLinkResolver(project)
        resolver.package_root = package
        resolver.deployment_package_root = package
        resolver.deployed_link_targets = {
            skill.resolve(): project / ".agents" / "skills" / "demo" / "SKILL.md"
        }
        return project, agent, resolver

    def test_deployed_sibling_skill_links_to_deployed_copy(self, tmp_path):
        project, agent, resolver = self._world(tmp_path)
        content = "See [demo](../skills/demo/SKILL.md#use) and [notes](../notes.md)."
        deployed_agent = project / ".github" / "agents" / "reviewer.agent.md"
        result = resolver.resolve_links_for_installation(content, agent, deployed_agent)
        _assert_link_resolves(result, "demo", deployed_agent)
        assert "[demo](../../.agents/skills/demo/SKILL.md#use)" in result
        # Negative control: a file no primitive deploys keeps the apm_modules projection.
        assert "[notes](../../apm_modules/_local/pkg/.apm/notes.md)" in result

    def test_replay_renders_deployed_copy_in_live_frame(self, tmp_path):
        project, agent, resolver = self._world(tmp_path)
        scratch = tmp_path / "scratch"
        resolver.deployment_frame = (scratch, project)
        resolver.deployed_link_targets = {
            path: scratch / ".agents" / "skills" / "demo" / "SKILL.md"
            for path in resolver.deployed_link_targets
        }
        result = resolver.resolve_links_for_installation(
            "[demo](../skills/demo/SKILL.md)",
            agent,
            scratch / ".codex" / "agents" / "reviewer.toml",
        )
        assert result == "[demo](../../.agents/skills/demo/SKILL.md)"
        _assert_link_resolves(result, "demo", project / ".codex" / "agents" / "reviewer.toml")

    def test_context_file_inside_deployed_skill_links_to_deployed_copy(self, tmp_path):
        project, agent, resolver = self._world(tmp_path)
        context = agent.parents[1] / "skills" / "demo" / "design.context.md"
        context.write_text("x", encoding="utf-8")
        resolver.deployed_link_targets[context.resolve()] = (
            project / ".agents" / "skills" / "demo" / "design.context.md"
        )
        deployed_agent = project / ".github" / "agents" / "reviewer.agent.md"
        result = resolver.resolve_links_for_installation(
            "[design](../skills/demo/design.context.md)", agent, deployed_agent
        )
        assert result == "[design](../../.agents/skills/demo/design.context.md)"
        _assert_link_resolves(result, "design", deployed_agent)
