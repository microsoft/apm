"""APMLifecycle: an instruction routed to AGENTS.md is never dropped silently (#5344).

PROMISE: P-INV-I7 -- with targets [claude, copilot] and no ``apm compile``,
an always-on or single-directory instruction routes to the compiled
``AGENTS.md`` (#4005) and no ``.claude/rules/`` file carries it. ``apm
install`` warns and names it, and ``apm audit --ci`` fails naming it, until a
compiled ``AGENTS.md`` Claude loads carries it; after ``apm compile`` both are
quiet. Nothing else changes: install writes no rule and CLAUDE.md is untouched.
CELL: OP=install,audit SRC=local(self) PRIM=instruction TGT=claude+copilot
SCOPE=project COMPILE=absent|done APPLY_TO=**
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.e2e,
    pytest.mark.promise("P-INV-I7"),
]

_NAME = "always.instructions.md"
_COPILOT_ONLY = "copilot-only.instructions.md"
_LEAD = "Claude Code does not load"
_SUMMARY = f"{_LEAD} 1 instruction(s) routed to AGENTS.md yet. Run 'apm compile'"


def _evidence(result: CommandResult) -> str:
    return f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"


def _text(result: CommandResult) -> str:
    return " ".join(f"{result.stdout} {result.stderr}".split())


def _project(tmp_path: Path, binary: Path) -> tuple[Path, dict[str, str], ApmLifecycleRunner]:
    base_env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=base_env)
    env = isolated.subprocess_env(overrides={"NO_COLOR": "1"})
    project = isolated.work_root / "project"
    instructions = project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (project / "apm.yml").write_text(
        "name: dropped\nversion: 1.0.0\ntargets:\n- claude\n- copilot\n"
        "dependencies:\n  apm:\n  - ./copilot-only\n",
        encoding="utf-8",
    )
    (instructions / _NAME).write_text(
        '---\napplyTo: "**"\n---\nAlways follow the house style.\n', encoding="utf-8"
    )
    # A dependency whose own targets exclude Claude is never routed for Claude.
    package = project / "copilot-only"
    (package / ".apm" / "instructions").mkdir(parents=True)
    (package / "apm.yml").write_text(
        "name: copilot-only\nversion: 1.0.0\ntargets:\n- copilot\n", encoding="utf-8"
    )
    (package / ".apm" / "instructions" / _COPILOT_ONLY).write_text(
        '---\napplyTo: "**"\n---\nCopilot only.\n', encoding="utf-8"
    )
    return project, env, ApmLifecycleRunner((str(binary),))


def test_an_instruction_routed_to_agents_md_is_named_until_compiled(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project, env, runner = _project(tmp_path, apm_binary_path)

    install = runner.run(("install",), cwd=project, env=env, scenario_id="install")

    assert install.returncode == 0, _evidence(install)
    # One summary line with the count; file names only under --verbose.
    assert "[!]" in install.stdout + install.stderr, _evidence(install)
    assert _SUMMARY in _text(install), _evidence(install)
    assert "Use --verbose to list them." in _text(install), _evidence(install)
    assert _text(install).count(_LEAD) == 1, _evidence(install)
    assert _NAME not in _text(install), _evidence(install)
    assert not (project / ".claude" / "rules" / "always.md").exists()
    assert not (project / "CLAUDE.md").exists()

    audit = runner.run(("audit", "--ci"), cwd=project, env=env, scenario_id="audit")

    # A warning in 0.39.0: reported, never failing CI.
    assert audit.returncode == 0, _evidence(audit)
    assert "claude-instructions-compiled" in _text(audit), _evidence(audit)
    assert _NAME in _text(audit), _evidence(audit)
    assert _COPILOT_ONLY not in _text(install) + _text(audit), _evidence(audit)

    verbose = runner.run(("install", "--verbose"), cwd=project, env=env, scenario_id="verbose")

    assert verbose.returncode == 0, _evidence(verbose)
    assert _SUMMARY in _text(verbose), _evidence(verbose)
    assert f"Not compiled for Claude Code: {_NAME}" in _text(verbose), _evidence(verbose)
    assert _COPILOT_ONLY not in _text(verbose), _evidence(verbose)

    compiled = runner.run(("compile",), cwd=project, env=env, scenario_id="compile")
    assert compiled.returncode == 0, _evidence(compiled)
    again = runner.run(("install",), cwd=project, env=env, scenario_id="reinstall")
    audit_after = runner.run(("audit", "--ci"), cwd=project, env=env, scenario_id="audit-after")

    assert again.returncode == 0, _evidence(again)
    assert _LEAD not in _text(again), _evidence(again)
    assert audit_after.returncode == 0, _evidence(audit_after)
    assert _NAME not in _text(audit_after), _evidence(audit_after)

    # A fresh clone has the committed AGENTS.md but no compile record in APM home.
    shutil.rmtree(Path(env["APM_HOME"]) / "compiled-agents-md")
    fresh = runner.run(("audit", "--ci"), cwd=project, env=env, scenario_id="audit-fresh")
    fresh_install = runner.run(("install",), cwd=project, env=env, scenario_id="install-fresh")

    assert fresh.returncode == 0, _evidence(fresh)
    assert fresh_install.returncode == 0, _evidence(fresh_install)
    assert _NAME not in _text(fresh), _evidence(fresh)
    assert _LEAD not in _text(fresh_install), _evidence(fresh_install)
