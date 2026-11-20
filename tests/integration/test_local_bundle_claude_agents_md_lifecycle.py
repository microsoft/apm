"""Claude bundle installs converge on AGENTS.md (#4034).

``apm install <bundle>`` into a ``targets: [claude]`` project routes each
plugin-native instruction through the #3939 owner: always-on and directory
instructions are staged as compile input, so ``apm compile`` writes them to
the root and ``src/AGENTS.md`` (imported by ``CLAUDE.md``); a path-scoped one
becomes a ``.claude/rules/`` file rendered with ``paths:``. Controls: a
hand-authored ``CLAUDE.md`` keeps the legacy rules layout with the #3939
warning, repeat installs and ``--frozen`` stay byte-stable, ``apm prune``
keeps every deployed and compiled file and clears the staging like any
compile-only target's (#1207, #3705), and other targets deploy the same
bundle as before.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._target_compile_kit import Scenario, output, tree_digest, write
from tests.integration.test_install_local_bundle_e2e import _make_plugin_bundle

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-claude-agents-md-instruction-routing"),
]

_FILES = {
    "instructions/style.instructions.md": "---\napplyTo: '**'\n---\nSTYLE_BODY always applies.\n",
    "instructions/src.instructions.md": "---\napplyTo: 'src/**'\n---\nSRC_BODY applies under src.\n",
    "instructions/py.instructions.md": "---\napplyTo: '**/*.py'\n---\nPY_BODY applies to Python.\n",
}
_STAGED = Path("apm_modules/test-plugin/.apm/instructions")


def _project(root: Path, targets: str) -> Path:
    write(root / "apm.yml", f"name: proj\nversion: 1.0.0\ntargets: [{targets}]\n")
    for name in ("a", "b"):
        write(root / f"src/{name}/mod.py", "x = 1\n")
    return root


def _rules(project: Path) -> list[str]:
    return sorted(path.name for path in (project / ".claude/rules").glob("*.md"))


def test_claude_bundle_install_then_compile_converges_on_agents_md(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    bundle = _make_plugin_bundle(tmp_path, pack_target="claude", files=dict(_FILES))
    project = _project(scenario.isolated.work_root / "proj", "claude")

    installed = output(scenario.run("install", str(bundle), "--no-policy", cwd=project))
    assert _rules(project) == ["py.md"]
    rule = (project / ".claude/rules/py.md").read_text(encoding="utf-8")
    assert '  - "**/*.py"' in rule and "applyTo" not in rule
    assert sorted(p.name for p in (project / _STAGED).iterdir()) == [
        "src.instructions.md",
        "style.instructions.md",
    ]
    assert "Run 'apm compile'" in installed, installed

    layout = tree_digest(project, skip=())
    scenario.run("install", str(bundle), "--no-policy", cwd=project)
    scenario.run("install", "--frozen", "--no-policy", cwd=project)
    assert tree_digest(project, skip=()) == layout, "repeat install churned the bundle layout"

    scenario.run("compile", cwd=project)
    root_agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    src_agents = (project / "src/AGENTS.md").read_text(encoding="utf-8")
    assert "STYLE_BODY" in root_agents and "SRC_BODY" not in root_agents
    assert "SRC_BODY" in src_agents
    assert "PY_BODY" not in root_agents + src_agents
    for claude_md in (project / "CLAUDE.md", project / "src/CLAUDE.md"):
        assert "@AGENTS.md" in claude_md.read_text(encoding="utf-8").splitlines()
    scenario.run("audit", "--ci", "--no-policy", cwd=project)

    compiled = tree_digest(project, skip=("apm_modules", "apm.lock.yaml"))
    scenario.run("prune", cwd=project)
    assert tree_digest(project, skip=("apm_modules", "apm.lock.yaml")) == compiled
    assert not (project / _STAGED).exists()
    pruned = load_yaml(project / "apm.lock.yaml")
    assert "local_staged_files" not in pruned
    assert pruned["local_deployed_files"] == [".claude/rules/py.md"]


def test_hand_authored_claude_md_keeps_rendered_bundle_rules(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    bundle = _make_plugin_bundle(tmp_path, pack_target="claude", files=dict(_FILES))
    project = _project(scenario.isolated.work_root / "proj", "claude")
    write(project / "CLAUDE.md", "# Team notes\n")

    installed = output(scenario.run("install", str(bundle), "--no-policy", cwd=project))

    assert _rules(project) == ["py.md", "src.md", "style.md"]
    assert '  - "src/**"' in (project / ".claude/rules/src.md").read_text(encoding="utf-8")
    assert not (project / _STAGED).exists()
    assert "add '@AGENTS.md' to it" in installed, installed
    assert (project / "CLAUDE.md").read_text(encoding="ascii") == "# Team notes\n"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (
            "copilot",
            [
                ".github/instructions/py.instructions.md",
                ".github/instructions/src.instructions.md",
                ".github/instructions/style.instructions.md",
            ],
        ),
        ("cursor", [".cursor/rules/py.mdc", ".cursor/rules/src.mdc", ".cursor/rules/style.mdc"]),
        (
            "codex",
            [
                f"{_STAGED.as_posix()}/py.instructions.md",
                f"{_STAGED.as_posix()}/src.instructions.md",
                f"{_STAGED.as_posix()}/style.instructions.md",
            ],
        ),
    ],
)
def test_other_targets_deploy_the_bundle_as_before(
    tmp_path: Path, apm_binary_path: Path, target: str, expected: list[str]
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    bundle = _make_plugin_bundle(tmp_path, pack_target=target, files=dict(_FILES))
    project = _project(scenario.isolated.work_root / "proj", target)

    scenario.run("install", str(bundle), "--no-policy", cwd=project)

    written = sorted(
        Path(os.path.relpath(path, project)).as_posix()
        for path in project.rglob("*")
        if path.is_file()
        and path.name not in ("apm.yml", "apm.lock.yaml", "mod.py")
        # Local deployment receipts (0.37) are APM state, not deployed output.
        and path.relative_to(project).parts[0] != ".apm-local"
    )
    assert written == expected


@pytest.mark.parametrize(
    "scoped",
    [
        '---\npaths:\n  - "src/**"\n---\n\nSCOPED_BODY\n',
        "---\napplyTo: '**'\npaths:\n  - \"src/**\"\n---\n\nSCOPED_BODY\n",
    ],
    ids=["paths-only", "paths-and-applyTo"],
)
def test_claude_packed_bundle_rules_deploy_byte_identical(
    tmp_path: Path, apm_binary_path: Path, scoped: str
) -> None:
    """A Claude-packed plugin bundle ships ``.claude/rules/`` files as ``instructions/``.

    A rule already in Claude format (it has ``paths:``) is copied as packed,
    even beside ``applyTo``: never re-rendered (which would replace the
    authored ``paths:``) and never staged.
    """
    scenario = Scenario.create(tmp_path, apm_binary_path)
    files = {**_FILES, "instructions/scoped.md": scoped}
    bundle = _make_plugin_bundle(tmp_path, pack_target="claude", files=files)
    project = _project(scenario.isolated.work_root / "proj", "claude")

    scenario.run("install", str(bundle), "--no-policy", cwd=project)

    assert _rules(project) == ["py.md", "scoped.md"]
    assert (project / ".claude/rules/scoped.md").read_bytes() == scoped.encode("ascii")
    assert not (project / _STAGED / "scoped.md").exists()
