"""APMLifecycle: an agent's link to a sibling skill survives a fresh clone (#5523).

PROMISE: P-INV-I7 -- a deployed agent that links to a skill the same install
deploys resolves to the committed deployed skill, not to gitignored
apm_modules/, so a clone without apm_modules has no broken agent links
(source: issue #5523)
CELL: OP=install|audit SRC=local PRIM=agent+skill SRC_TYPE=plugin
TGT=copilot+codex SCOPE=project SELECT={all, excludes-linked-skill}
DEPLOYED=apm_modules-{present,missing}
"""

from __future__ import annotations

import os
import posixpath
import shutil
from pathlib import Path

import pytest

from apm_cli.integration.targets import KNOWN_TARGETS
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

_TARGETS = ("copilot", "codex")
# The deployed agent per target: Markdown for Copilot, a TOML agent for Codex.
_AGENTS = {
    "copilot": ".github/agents/demo-reviewer.agent.md",
    "codex": ".codex/agents/demo-reviewer.toml",
}


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _reporter_world(root: Path, skills_selection: str) -> None:
    """The exact package and manifest from the #5523 report.

    A non-empty *skills_selection* adds a second skill and selects only it,
    so the linked demo-skill is not deployed (the negative control).
    """
    pkg = root / "pkg"
    skills = ("demo-skill", "other-skill") if skills_selection else ("demo-skill",)
    declared = ", ".join(f'"skills/{skill}"' for skill in skills)
    _write(
        pkg / "plugin.json",
        '{\n  "name": "demo-plugin",\n  "version": "0.1.0",\n'
        f'  "skills": [{declared}],\n'
        '  "agents": ["agents/demo-reviewer.agent.md"]\n}\n',
    )
    for skill in skills:
        _write(
            pkg / "skills" / skill / "SKILL.md",
            f"---\nname: {skill}\ndescription: Demo skill.\n---\n# {skill}\n",
        )
    _write(
        pkg / "agents" / "demo-reviewer.agent.md",
        "---\nname: demo-reviewer\ndescription: Demo agent.\n---\n"
        "See [demo-skill](../skills/demo-skill/SKILL.md).\n",
    )
    _write(
        root / "apm.yml",
        "name: repro\nversion: 0.1.0\ntargets:\n  - copilot\n  - codex\n"
        "dependencies:\n  apm:\n    - path: ./pkg\n" + skills_selection,
    )


def _skill_link(agent_text: str) -> str:
    """Return the single rewritten demo-skill link target in a deployed agent."""
    marker = "[demo-skill]("
    start = agent_text.index(marker) + len(marker)
    return agent_text[start : agent_text.index(")", start)]


@pytest.mark.parametrize(
    ("skills_selection", "linked_skill_deployed"),
    [
        pytest.param("", True, id="sibling-skill-deployed"),
        pytest.param("      skills: [other-skill]\n", False, id="sibling-skill-not-selected"),
    ],
)
def test_agent_link_to_sibling_skill_resolves_on_a_fresh_clone(
    tmp_path: Path, apm_binary_path: Path, skills_selection: str, linked_skill_deployed: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    project = isolated.work_root / "repro"
    _reporter_world(project, skills_selection)

    def apm(*args: str) -> CommandResult:
        result = runner.run(args, cwd=project, env=env)
        for stream in (result.stdout, result.stderr):
            assert stream.isascii(), _evidence(result)
        return result

    installed = apm("install")
    assert installed.returncode == 0, _evidence(installed)
    deployed_skill = posixpath.join(KNOWN_TARGETS["copilot"].skills_rel_root, "demo-skill")
    assert (project / deployed_skill / "SKILL.md").is_file() is linked_skill_deployed
    for target in _TARGETS:
        assert posixpath.join(KNOWN_TARGETS[target].skills_rel_root, "demo-skill") == (
            deployed_skill
        )

    links = {}
    for target, agent_path in _AGENTS.items():
        agent = project / agent_path
        link = _skill_link(agent.read_text(encoding="utf-8"))
        links[target] = link
        resolved = Path(posixpath.normpath(posixpath.join(agent.parent.as_posix(), link)))
        if linked_skill_deployed:
            # Decisive: the link names the committed deployed skill file.
            assert resolved == project / deployed_skill / "SKILL.md", (target, link)
        else:
            # Negative control: a skill this install does not deploy keeps
            # today's apm_modules projection.
            assert "apm_modules/" in link, (target, link)
            assert resolved.is_file(), (target, link)

    # I2: a converged install changes no bytes.
    before = LifecycleStateSnapshot.capture(project, targets=_TARGETS)
    again = apm("install")
    assert again.returncode == 0, _evidence(again)
    assert LifecycleStateSnapshot.capture(project, targets=_TARGETS) == before
    # I3: audit replay renders the same links, so it reports no drift.
    ci = apm("audit", "--ci")
    assert ci.returncode == 0, _evidence(ci)

    # A fresh clone: committed deployed files, no gitignored apm_modules.
    shutil.rmtree(project / "apm_modules")
    audit = apm("audit")
    missing = [
        line.strip()
        for line in (audit.stdout + audit.stderr).splitlines()
        if "links to apm_modules/" in line
    ]
    if linked_skill_deployed:
        assert missing == [], _evidence(audit)
        for target, agent_path in _AGENTS.items():
            agent = project / agent_path
            resolved = Path(
                posixpath.normpath(posixpath.join(agent.parent.as_posix(), links[target]))
            )
            assert resolved.is_file(), (target, links[target])
    else:
        assert len(missing) == len(_AGENTS), _evidence(audit)
