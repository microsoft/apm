"""APM creates a missing APM_HOME private to the user (#5257).

PROMISE: P-INV-I5 -- APM's user metadata lives in a directory only the user can
list or change: when a command creates APM_HOME it creates it with mode 0700,
and an existing APM_HOME keeps its mode (source: #5257 scope record).
CELL: OP=install|compile SCOPE=project APM_HOME=absent|existing-0755
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise("P-INV-I5"),
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits"),
]


def _describe(result: CommandResult) -> str:
    return f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("command", ["install", "compile"])
@pytest.mark.parametrize("existing", [False, True], ids=["created", "existing-0755"])
def test_a_command_creates_apm_home_as_0700_and_keeps_an_existing_mode(
    tmp_path: Path, apm_binary_path: Path, command: str, existing: bool
) -> None:
    """A new APM_HOME is 0700; an existing one is left at its own mode."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    apm_home = isolated.root / "fresh-apm-home"
    environment["APM_HOME"] = str(apm_home)
    if existing:
        apm_home.mkdir()
        apm_home.chmod(0o755)
    project = isolated.work_root / "project"
    project.mkdir(parents=True)
    (project / "apm.yml").write_text(
        "name: project\nversion: 1.0.0\ntargets: [copilot]\n", encoding="utf-8"
    )
    instructions = project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "style.instructions.md").write_text(
        '---\napplyTo: "**"\n---\nUse plain words.\n', encoding="utf-8"
    )
    old_umask = os.umask(0o022)
    try:
        result = ApmLifecycleRunner((str(apm_binary_path),)).run(
            (command,), scenario_id=command, cwd=project, env=environment
        )
    finally:
        os.umask(old_umask)

    assert result.returncode == 0, _describe(result)
    assert apm_home.is_dir(), _describe(result)
    assert _mode(apm_home) == (0o755 if existing else 0o700), oct(_mode(apm_home))
