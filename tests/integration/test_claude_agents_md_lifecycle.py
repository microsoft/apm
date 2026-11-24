"""Claude projects load always-on and directory instructions from AGENTS.md (#3939).

Installed-CLI chain for ``targets: [claude]``: ``apm install`` keeps only the
path-scoped rule in ``.claude/rules/`` and asks for ``apm compile`` even for
local ``.apm/`` instructions; ``apm compile`` writes the root and the exact
``src/AGENTS.md`` (never hoisting the directory rule to the root, even when
it matches most of the tree) plus a ``CLAUDE.md`` beside each that imports
``@AGENTS.md``. A hand-authored ``CLAUDE.md`` without that import keeps every
rule in ``.claude/rules/`` and is never rewritten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._target_compile_kit import Scenario, output, write

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-claude-agents-md-instruction-routing"),
]

_INSTRUCTIONS = {
    "style": ('"**"', "STYLE_BODY always applies."),
    "src": ('"src/**"', "SRC_BODY applies under src."),
    "py": ('"**/*.py"', "PY_BODY applies to Python files."),
}


def _seed(project: Path) -> None:
    write(project / "apm.yml", "name: proj\nversion: 1.0.0\ntargets: [claude]\n")
    for stem, (apply_to, body) in _INSTRUCTIONS.items():
        write(
            project / f".apm/instructions/{stem}.instructions.md",
            f"---\ndescription: {stem}\napplyTo: {apply_to}\n---\n{body}\n",
        )
    # src/** matches most of the tree, so the shared optimizer would hoist it.
    for name in ("a", "b", "c", "d"):
        write(project / f"src/{name}/mod.py", "x = 1\n")
    write(project / "README.md", "# Project\n")


def _imports(path: Path) -> bool:
    return "@AGENTS.md" in path.read_text(encoding="utf-8").splitlines()


def test_claude_install_then_compile_routes_instructions_to_agents_md(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = scenario.isolated.work_root / "proj"
    _seed(project)

    installed = output(scenario.run("install", "--no-policy", cwd=project))
    rules = project / ".claude" / "rules"
    assert sorted(path.name for path in rules.glob("*.md")) == ["py.md"]
    assert "Run 'apm compile' to update AGENTS.md / CLAUDE.md." in installed, installed
    py_rule = (rules / "py.md").read_bytes()

    scenario.run("compile", cwd=project)

    root_agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    src_agents = (project / "src" / "AGENTS.md").read_text(encoding="utf-8")
    assert "STYLE_BODY" in root_agents
    assert "SRC_BODY" in src_agents
    assert "SRC_BODY" not in root_agents, "directory rule hoisted to the root"
    assert "PY_BODY" not in root_agents + src_agents, "path-scoped rule duplicated"
    assert _imports(project / "CLAUDE.md")
    assert _imports(project / "src" / "CLAUDE.md")
    assert (rules / "py.md").read_bytes() == py_rule

    # A no-op reinstall after compile does not ask again; a changed routed
    # instruction does.
    quiet = output(scenario.run("install", "--no-policy", cwd=project))
    assert "Run 'apm compile'" not in quiet, quiet
    source = project / ".apm/instructions/src.instructions.md"
    source.write_text(source.read_text(encoding="ascii") + "Updated.\n", encoding="ascii")
    changed = output(scenario.run("install", "--no-policy", cwd=project))
    assert "Run 'apm compile' to update AGENTS.md / CLAUDE.md." in changed, changed


def test_hand_authored_claude_md_keeps_rules_until_it_imports_agents_md(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = scenario.isolated.work_root / "proj"
    _seed(project)
    write(project / "CLAUDE.md", "# Team notes\n")

    installed = output(scenario.run("install", "--no-policy", cwd=project))
    rules = project / ".claude" / "rules"
    assert sorted(path.name for path in rules.glob("*.md")) == ["py.md", "src.md", "style.md"]
    assert "add '@AGENTS.md' to it" in installed, installed
    assert (project / "CLAUDE.md").read_text(encoding="ascii") == "# Team notes\n"

    write(project / "CLAUDE.md", "@AGENTS.md\n# Team notes\n")
    scenario.run("install", "--no-policy", cwd=project)
    scenario.run("compile", cwd=project)

    assert sorted(path.name for path in rules.glob("*.md")) == ["py.md"]
    assert "STYLE_BODY" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert (project / "CLAUDE.md").read_text(encoding="ascii") == "@AGENTS.md\n# Team notes\n"


# -- Never drop without a replacement (#5343) ------------------------------------

_TARGETS = pytest.mark.parametrize(
    "targets", ["[claude]", "[claude, copilot]"], ids=["claude", "claude-copilot"]
)


def _routed_project(scenario: Scenario, targets: str) -> Path:
    project = scenario.isolated.work_root / "proj"
    _seed(project)
    write(project / "apm.yml", f"name: proj\nversion: 1.0.0\ntargets: {targets}\n")
    return project


def _rules(project: Path) -> dict[str, str]:
    rules = project / ".claude" / "rules"
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(rules.glob("*.md"))}


@_TARGETS
def test_install_without_compile_keeps_every_rule_claude_loads(
    tmp_path: Path, apm_binary_path: Path, targets: str
) -> None:
    """No compiled AGENTS.md carries the routed instructions yet: install keeps their
    .claude/rules/ files, so Claude Code never loses one (#5343)."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = _routed_project(scenario, targets)

    installed = output(scenario.run("install", "--no-policy", cwd=project))
    rules = _rules(project)
    again = output(scenario.run("install", "--no-policy", cwd=project))

    assert sorted(rules) == ["py.md", "src.md", "style.md"], installed
    assert "STYLE_BODY" in rules["style.md"] and "SRC_BODY" in rules["src.md"]
    assert _rules(project) == rules, again
    assert "routed to AGENTS.md" in installed, installed
    assert not (project / "AGENTS.md").exists()


@_TARGETS
def test_compile_then_install_retires_the_rules_agents_md_carries(
    tmp_path: Path, apm_binary_path: Path, targets: str
) -> None:
    """Once a compiled AGENTS.md Claude loads carries a routed instruction, the next
    install retires its rule: exactly one copy, never none (#5343)."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = _routed_project(scenario, targets)
    scenario.run("install", "--no-policy", cwd=project)

    scenario.run("compile", cwd=project)
    settled = output(scenario.run("install", "--no-policy", cwd=project))

    assert sorted(_rules(project)) == ["py.md"], settled
    compiled = "".join(
        path.read_text(encoding="utf-8")
        for path in (project / "AGENTS.md", project / "src/AGENTS.md")
        if path.is_file()
    )
    # With Copilot too, the shared optimizer may place src/** in the root file (#4005).
    assert compiled.count("STYLE_BODY") == 1 and compiled.count("SRC_BODY") == 1, compiled
    assert "routed to AGENTS.md" not in settled, settled


@_TARGETS
def test_an_edited_routed_instruction_is_kept_once_until_it_is_compiled(
    tmp_path: Path, apm_binary_path: Path, targets: str
) -> None:
    """After compile, editing a routed instruction and reinstalling restores exactly one
    rule with the new body; compile then install retires it again (#5343)."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = _routed_project(scenario, targets)
    scenario.run("install", "--no-policy", cwd=project)
    scenario.run("compile", cwd=project)
    scenario.run("install", "--no-policy", cwd=project)
    source = project / ".apm/instructions/style.instructions.md"
    source.write_text(source.read_text(encoding="ascii") + "STYLE_EDIT.\n", encoding="ascii")

    edited = output(scenario.run("install", "--no-policy", cwd=project))
    rules = _rules(project)

    assert sorted(rules) == ["py.md", "style.md"], edited
    assert "STYLE_EDIT" in rules["style.md"]
    assert sum("STYLE_BODY" in text for text in rules.values()) == 1
    scenario.run("compile", cwd=project)
    scenario.run("install", "--no-policy", cwd=project)
    assert sorted(_rules(project)) == ["py.md"]
    assert "STYLE_EDIT" in (project / "AGENTS.md").read_text(encoding="utf-8")
