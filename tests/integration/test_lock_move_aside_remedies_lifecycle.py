"""An unusable lock is quarantined whole, never edited (RA-004).

PROMISE: a printed remediation, run as printed and non-interactively, clears
the state that reported it (owner ``remediation-advice``). Rule B: no remedy
edits ``apm.lock.yaml``; when the lock cannot be read, the only manual step is
to move it aside, then let APM regenerate it.
CELL: OP=pack SRC=local PRIM=mcp SCOPE=project LOCK=unreadable
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-remediation-advice"),
]

_SKILL = "---\nname: s\ndescription: d\n---\nhi\n"
_REMEDY = (
    "Move the unreadable apm.lock.yaml aside, then run 'apm install' to regenerate "
    "apm.lock.yaml, then run 'apm pack' again."
)


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _text(result: CommandResult) -> str:
    return re.sub(r"\s+", " ", result.stdout + result.stderr)


def test_unreadable_lock_mcp_ownership_remedy_converges(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "proj"
    (project / ".apm" / "skills" / "s").mkdir(parents=True)
    subprocess.run(("git", "init", "-q"), cwd=project, env=env, check=True)
    (project / "apm.yml").write_text(
        "name: proj\nversion: 1.0.0\nlicense: MIT\ntargets: [claude]\n", encoding="ascii"
    )
    (project / ".apm" / "skills" / "s" / "SKILL.md").write_text(_SKILL, encoding="ascii")
    (project / ".mcp.json").write_text('{"mcpServers": {"mine": {"command": "echo"}}}\n')
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    def apm(*args: str) -> CommandResult:
        result = runner.run(args, cwd=project, env=env)
        assert result.returncode == 0, _evidence(result)
        return result

    apm("install")
    lock = project / "apm.lock.yaml"
    lock.write_text('lockfile_version: "1"\ndependencies: [unclosed\n', encoding="ascii")

    warned = apm("pack")
    assert _REMEDY in _text(warned), _evidence(warned)

    # The printed remedy, as printed: quarantine the whole lock, regenerate it.
    lock.rename(project / "apm.lock.yaml.aside")
    apm("install")
    assert lock.is_file()
    for generated in (project / ".claude-plugin").glob("*"):
        generated.unlink()
    packed = apm("pack")
    assert "Root .mcp.json left out" not in _text(packed), _evidence(packed)
