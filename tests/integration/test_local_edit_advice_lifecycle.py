"""Local-edit advice is true for every primitive kind, and running it works (RA-009).

PROMISE: outcome.status_matches_state -- the advice ``apm audit --ci`` prints
for a locally edited managed file names what ``apm install`` really does to
that kind, and running it restores the locked content (source:
src/apm_cli/install/local_edits.py, src/apm_cli/core/remediation.py).
CELL: OP=audit,install PRIM=instruction|agent|prompt|command|rule|skill
SCOPE=project DEPLOYED=edited.

A plain ``apm install`` overwrites an edited instruction, agent, prompt,
command or rule, and must say so at warning level, naming the files. It keeps
an edited skill file, so the advice for skills deletes the file first. Each
case induces the edit, reads the structured remediation the installed CLI
attaches to the failing check, runs exactly that advice, and observes the
locked bytes return.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _new_scenario,
    _result_evidence,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-remediation-advice"),
]

_AUDIT = ("audit", "--ci", "--no-policy", "--format", "json")
_EDIT = "\nLOCAL EDIT\n"


@dataclass(frozen=True)
class _Kind:
    name: str
    target: str
    kept_by_install: bool


KINDS = (
    _Kind("instruction", "copilot", kept_by_install=False),
    _Kind("agent", "copilot", kept_by_install=False),
    _Kind("prompt", "copilot", kept_by_install=False),
    _Kind("command", "claude", kept_by_install=False),
    _Kind("rule", "cursor", kept_by_install=False),
    _Kind("skill", "copilot", kept_by_install=True),
)


def _author(scenario, package, kind: str) -> None:
    sources = scenario.sources
    front = f"---\ndescription: local-edit fixture {kind}\n---\n# {kind}\n"
    if kind == "skill":
        sources.add_skill(package, "kit", f"---\nname: kit\ndescription: s\n---\n# {kind}\n")
    elif kind in ("instruction", "rule"):
        sources.add_instruction(package, "kit", f"---\napplyTo: '**'\n{front[4:]}")
    elif kind == "agent":
        sources.add_agent(package, "kit", front)
    elif kind == "prompt":
        sources.add_prompt(package, "kit", front)
    else:
        sources.add_command(package, "kit", front)


def _world(tmp_path: Path, apm_binary_path: Path, kind: _Kind):
    scenario = _new_scenario(tmp_path / kind.name, apm_binary_path)
    package = scenario.sources.create(f"kit-{kind.name}")
    _author(scenario, package, kind.name)
    repository = scenario.repositories.create(package.name, source_tree=package.root)
    commit = scenario.repositories.commit(repository, message="seed")
    remote = f"https://github.com/test-owner/{package.name}"
    env = scenario.repositories.url_rewrite_subprocess_env(repository, remote)
    consumer = scenario.consumers.create(
        "app",
        dependencies=({"git": remote, "ref": commit.sha, "alias": package.name},),
        targets=(kind.target,),
    )

    def apm(*args: str, ok: tuple[int, ...] = (0,)):
        result = scenario.runner.run(args, scenario_id=kind.name, cwd=consumer.root, env=env)
        assert result.returncode in ok, _result_evidence(result)
        return result

    return consumer.root, apm


def _deployed_file(root: Path) -> str:
    lock = load_yaml(root / "apm.lock.yaml")
    files = [
        path
        for dep in lock["dependencies"]
        for path in dep.get("deployed_files", [])
        if not path.endswith("/") and (root / path).is_file()
    ]
    assert len(files) == 1, files
    return files[0]


def _content_integrity(apm) -> dict:
    payload = json.loads(apm(*_AUDIT, ok=(0, 1)).stdout)
    return next(check for check in payload["checks"] if check["name"] == "content-integrity")


def _edit(root: Path, path: str, locked: bytes) -> None:
    (root / path).write_bytes(locked + _EDIT.encode())


@pytest.mark.parametrize("kind", KINDS, ids=lambda kind: kind.name)
def test_local_edit_advice_is_true_and_restores_each_kind(
    tmp_path: Path, apm_binary_path: Path, kind: _Kind
) -> None:
    root, apm = _world(tmp_path, apm_binary_path, kind)
    apm("install", "--no-policy")
    edited = _deployed_file(root)
    locked = (root / edited).read_bytes()
    _edit(root, edited, locked)

    reported = _content_integrity(apm)
    assert reported["passed"] is False, reported
    assert "plain install keeps local edits" not in reported["message"]
    remediation = reported.get("remediation")
    assert isinstance(remediation, dict), f"no structured remediation: {reported}"
    assert remediation["command"] in reported["message"]
    assert remediation["before"].startswith("delete "), remediation

    # The advice states what a plain install does to this kind: observe it.
    plain = apm("install", "--no-policy")
    output = plain.stdout + plain.stderr
    if kind.kept_by_install:
        assert remediation["id"] == "drift-restore-edited-skill"
        assert "refuses to overwrite an edited skill" in remediation["fixes"]
        assert (root / edited).read_bytes().endswith(_EDIT.encode())
    else:
        assert remediation["id"] == "drift-restore-edited-overwrite"
        assert "overwrites these edited" in remediation["fixes"]
        assert (root / edited).read_bytes() == locked
        notice = [line for line in output.splitlines() if "This install overwrites" in line]
        assert notice and notice[0].startswith("[!]"), output
        assert edited in " ".join(output.split()), output
        _edit(root, edited, locked)

    # Executing the printed remedy (delete, then the command) clears audit --ci.
    (root / edited).unlink()
    argv = shlex.split(remediation["command"])
    assert argv[0] == "apm"
    apm(*argv[1:], "--no-policy")
    assert (root / edited).read_bytes() == locked
    assert _content_integrity(apm)["passed"] is True
