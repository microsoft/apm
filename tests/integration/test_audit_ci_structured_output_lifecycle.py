"""APMLifecycle: apm audit --ci in JSON/SARIF mode always prints one document (#4287).

PROMISE: I8 -- structured audit output carries the result, including a failed
install replay, and keeps exit 1 (source: issue #4287,
docs/src/content/docs/reference/cli/audit.md).
CELL: OP=audit SRC=local PRIM=prompt SCOPE=project LOCK=present DEPLOYED=yes
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I8", "P-LAW-outcome.status_matches_state"),
]

_TARGET = "copilot"
_SYMLINK_ERROR = "Local single-file dependency is a symlink: ./p.prompt.md."


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
@pytest.mark.parametrize("fmt", ["json", "sarif"])
def test_audit_ci_structured_output_reports_a_failed_replay(
    tmp_path: Path, apm_binary_path: Path, fmt: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    project.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=project, env=env, check=True)
    prompt = "---\ndescription: d\n---\nhello\n"
    (project / "real.prompt.md").write_text(prompt, encoding="ascii")
    (project / "p.prompt.md").write_text(prompt, encoding="ascii")
    (project / "apm.yml").write_text(
        f"name: consumer\nversion: 1.0.0\ntargets: [{_TARGET}]\n"
        "dependencies:\n  apm:\n    - ./p.prompt.md\n",
        encoding="ascii",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    installed = runner.run(("install",), cwd=project, env=env)
    assert installed.returncode == 0, _evidence(installed)
    (project / "p.prompt.md").unlink()
    (project / "p.prompt.md").symlink_to("real.prompt.md")
    before = LifecycleStateSnapshot.capture(project, targets=(_TARGET,))

    result = runner.run(("audit", "--ci", "--format", fmt), cwd=project, env=env)

    assert result.returncode == 1, _evidence(result)
    assert (result.stdout + result.stderr).isascii(), _evidence(result)
    assert "Error:" not in result.stderr, _evidence(result)
    document = json.loads(result.stdout)
    if fmt == "json":
        failed = {c["name"]: c["message"] for c in document["checks"] if not c["passed"]}
        assert failed["drift"].startswith(f"drift replay failed: {_SYMLINK_ERROR}"), failed
    else:
        messages = [r["message"]["text"] for r in document["runs"][0]["results"]]
        assert any(_SYMLINK_ERROR in text for text in messages), messages
    assert LifecycleStateSnapshot.capture(project, targets=(_TARGET,)) == before
