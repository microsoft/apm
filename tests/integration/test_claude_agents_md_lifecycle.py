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


# -- Install is all Claude needs: never drop without a replacement (#5343) -------

_TARGETS = pytest.mark.parametrize(
    "targets", ["[claude]", "[claude, copilot]"], ids=["claude", "claude-copilot"]
)
# The 0.37 .claude/rules/ layout: applyTo becomes paths:, verbatim.
_RULES_037 = {
    "nested.md": '---\npaths:\n  - "src/a/**"\n---\n\nNESTED_BODY applies under src/a.\n',
    "py.md": '---\npaths:\n  - "**/*.py"\n---\n\nPY_BODY applies to Python files.\n',
    "src.md": '---\npaths:\n  - "src/**"\n---\n\nSRC_BODY applies under src.\n',
    "style.md": '---\npaths:\n  - "**"\n---\n\nSTYLE_BODY always applies.\n',
}
_NO_COPY = "Claude Code has no copy of"


def _routed_project(scenario: Scenario, targets: str) -> Path:
    project = scenario.isolated.work_root / "proj"
    _seed(project)
    write(
        project / ".apm/instructions/nested.instructions.md",
        '---\ndescription: nested\napplyTo: "src/a/**"\n---\nNESTED_BODY applies under src/a.\n',
    )
    write(project / "apm.yml", f"name: proj\nversion: 1.0.0\ntargets: {targets}\n")
    return project


def _rules(project: Path) -> dict[str, str]:
    rules = project / ".claude" / "rules"
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(rules.glob("*.md"))}


def _claude_copies(project: Path, marker: str) -> int:
    """How many files Claude loads carry *marker*: rules plus compiled AGENTS.md."""
    compiled = [path for path in project.rglob("AGENTS.md") if "apm_modules" not in path.parts]
    texts = [*_rules(project).values(), *(p.read_text(encoding="utf-8") for p in compiled)]
    return sum(marker in text for text in texts)


@_TARGETS
def test_a_no_compile_project_keeps_the_037_rules_byte_for_byte(
    tmp_path: Path, apm_binary_path: Path, targets: str
) -> None:
    """A 0.37 project that never compiles upgrades with zero diff: every instruction,
    a nested folder's too, stays a native rule at the author's scope (#5343)."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = _routed_project(scenario, targets)

    installed = output(scenario.run("install", "--no-policy", cwd=project))
    again = output(scenario.run("install", "--no-policy", cwd=project))

    assert _rules(project) == _RULES_037, installed
    assert not (project / "AGENTS.md").exists()
    assert _NO_COPY not in installed + again, installed
    assert "Claude Code does not load" not in installed + again, installed


@_TARGETS
def test_a_compiled_agents_md_retires_the_rule_and_names_where_it_moved(
    tmp_path: Path, apm_binary_path: Path, targets: str
) -> None:
    """With a compiled AGENTS.md that already carries an instruction (a 0.38 project
    that compiled), install writes no rule for it; one Claude copy, never two (#5343)."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = _routed_project(scenario, targets)
    scenario.run("install", "--no-policy", cwd=project)
    for name in ("style.md", "src.md", "nested.md"):  # what a 0.38 install left behind
        (project / ".claude/rules" / name).unlink(missing_ok=True)
    scenario.run("compile", cwd=project)

    settled = output(scenario.run("install", "--no-policy", cwd=project))

    assert sorted(_rules(project)) == ["py.md"], settled
    # Routed instructions only: compile's file-type duplicate with Copilot is a 0.40 issue.
    for marker in ("STYLE_BODY", "SRC_BODY", "NESTED_BODY"):
        assert _claude_copies(project, marker) == 1, (marker, settled)
    assert _NO_COPY not in settled, settled


def test_install_retires_a_rule_a_later_compile_carries_and_names_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Claude + Copilot compile into AGENTS.md; the next install retires the rule it now
    carries and names where it moved, one line per rule (#5343)."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = _routed_project(scenario, "[claude, copilot]")
    scenario.run("install", "--no-policy", cwd=project)
    scenario.run("compile", cwd=project)

    settled = output(scenario.run("install", "--no-policy", cwd=project))

    assert sorted(_rules(project)) == ["py.md"], settled
    assert "Retired .claude/rules/style.md: AGENTS.md now carries it" in settled, settled
    for marker in ("STYLE_BODY", "SRC_BODY", "NESTED_BODY"):
        assert _claude_copies(project, marker) == 1, (marker, settled)


@_TARGETS
def test_an_edited_routed_instruction_keeps_exactly_one_claude_copy(
    tmp_path: Path, apm_binary_path: Path, targets: str
) -> None:
    """After AGENTS.md carries it, an edited instruction comes back as one rule with the
    new body, and a compile plus install leaves one copy again (#5343)."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    project = _routed_project(scenario, targets)
    scenario.run("install", "--no-policy", cwd=project)
    for name in ("style.md", "src.md", "nested.md"):
        (project / ".claude/rules" / name).unlink(missing_ok=True)
    scenario.run("compile", cwd=project)
    scenario.run("install", "--no-policy", cwd=project)
    source = project / ".apm/instructions/style.instructions.md"
    source.write_text(source.read_text(encoding="ascii") + "STYLE_EDIT.\n", encoding="ascii")

    edited = output(scenario.run("install", "--no-policy", cwd=project))

    assert "STYLE_EDIT" in _rules(project).get("style.md", ""), edited
    assert _claude_copies(project, "STYLE_EDIT") == 1, edited
    scenario.run("compile", cwd=project)
    scenario.run("install", "--no-policy", cwd=project)
    assert _claude_copies(project, "STYLE_EDIT") == 1
