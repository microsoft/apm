"""APMLifecycle: a fresh clone installs over a legacy self-owned rule row (#5336).

PROMISE: P-INV-I7 -- a committed lockfile written by an earlier APM records a
``.claude/rules/`` file the project's own instruction deployed (owner ``.``).
On a fresh clone (no deployment receipt in APM_HOME) ``apm install`` exits 0:
the ``.`` owner is the project's own content, never a local dependency path,
the hash-matching file is kept byte-identical and never removed, and a
hand-authored CLAUDE.md is never overwritten. A second install changes no byte.
CELL: OP=install SRC=local(self) PRIM=instruction TGT=claude SCOPE=project
LOCK=v1-self-row RECEIPT=absent APPLY_TO=front/**|** CLAUDE_MD=absent|import
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_file_hash
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.e2e,
    pytest.mark.promise("P-INV-I7", "P-INV-I2"),
]

_RULE = ".claude/rules/front.md"


def _evidence(result: CommandResult) -> str:
    return f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "apm_modules" not in path.parts
    }


def _clone(tmp_path: Path, apply_to: str, claude_md: str | None) -> tuple[Path, dict[str, str]]:
    """A checkout as a fresh clone holds it: sources, the deployed rule and a v1 lockfile."""
    base_env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=base_env)
    env = isolated.subprocess_env(overrides={"NO_COLOR": "1"})
    project = isolated.work_root / "clone"
    (project / "front").mkdir(parents=True)
    (project / "front" / ".gitkeep").write_text("", encoding="utf-8")
    (project / "apm.yml").write_text(
        "name: repro\nversion: 1.0.0\ntargets:\n- claude\n", encoding="utf-8"
    )
    instructions = project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "front.instructions.md").write_text(
        f'---\napplyTo: "{apply_to}"\n---\nFront only.\n', encoding="utf-8"
    )
    rule = project / _RULE
    rule.parent.mkdir(parents=True)
    rule.write_text(f"---\npaths:\n- {apply_to}\n---\nFront only.\n", encoding="utf-8")
    if claude_md is not None:
        (project / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    digest = compute_file_hash(rule)
    (project / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\n"
        "dependencies: []\n"
        "deployments:\n"
        "- kind: project-relative\n"
        "  target: claude\n"
        f"  value: {_RULE}\n"
        "  runtime: null\n"
        "  scope: project\n"
        "  owners:\n"
        "  - .\n"
        "  active_owner: .\n"
        f"  content_hash: {digest}\n"
        "local_deployed_files:\n"
        f"- {_RULE}\n"
        "local_deployed_file_hashes:\n"
        f"  {_RULE}: {digest}\n",
        encoding="utf-8",
    )
    assert not (isolated.config_root / "deployment-receipts").exists()
    return project, env


@pytest.mark.parametrize("claude_md", [None, "@AGENTS.md\n"], ids=["no-claude-md", "import"])
@pytest.mark.parametrize("apply_to", ["front/**", "**"], ids=["directory", "always-on"])
def test_a_fresh_clone_installs_over_a_legacy_self_owned_rule_row(
    tmp_path: Path, apm_binary_path: Path, apply_to: str, claude_md: str | None
) -> None:
    project, env = _clone(tmp_path, apply_to, claude_md)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    rule_before = (project / _RULE).read_bytes()

    first = runner.run(("install",), cwd=project, env=env, scenario_id="fresh-clone-install")

    assert first.returncode == 0, _evidence(first)
    assert "path segments must not be empty" not in first.stdout + first.stderr
    assert (project / _RULE).read_bytes() == rule_before, "the hash-matching rule is kept"
    if claude_md is not None:
        assert (project / "CLAUDE.md").read_text(encoding="utf-8") == claude_md
    else:
        assert not (project / "CLAUDE.md").exists()

    converged = _tree(project)
    again = runner.run(("install",), cwd=project, env=env, scenario_id="fresh-clone-reinstall")

    assert again.returncode == 0, _evidence(again)
    assert _tree(project) == converged, "a second install changes no byte"
