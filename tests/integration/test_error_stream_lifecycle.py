"""Installed-CLI output contract: error lines go to stderr and are never hard-wrapped.

Promise (console owner, ``src/apm_cli/utils/console.py``): a ``[x]`` error line
is diagnostics, so it is written to stderr while stdout keeps the command's
progress output; and output that is piped or captured (not a TTY) is never
hard-wrapped, so a command, path or sentence stays on one copy-pasteable line.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]

_LONG_NAME = "a-deliberately-long-local-package-directory-name-that-exceeds-eighty-columns"


@dataclass(frozen=True)
class _Run:
    returncode: int
    stdout: str
    stderr: str


def _project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    environment = isolated.subprocess_env(overrides={"COLUMNS": "80"})
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml({"name": "consumer", "version": "1.0.0"}, project / "apm.yml")
    return project, environment


def _apm(binary: Path, project: Path, environment: dict[str, str], *args: str) -> _Run:
    result = subprocess.run(
        (str(binary), *args),
        cwd=project,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return _Run(result.returncode, result.stdout, result.stderr)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("install", "./missing"), id="install-missing-local-path"),
        pytest.param(("uninstall", "nothere"), id="uninstall-unknown-package"),
        pytest.param(("lifecycle", "trust"), id="lifecycle-trust-without-lifecycle-block"),
    ],
)
def test_error_lines_go_to_stderr(
    apm_binary_path: Path, tmp_path: Path, argv: tuple[str, ...]
) -> None:
    project, environment = _project(tmp_path)

    run = _apm(apm_binary_path, project, environment, *argv)

    assert run.returncode == 1, run.stdout + run.stderr
    assert "[x]" not in run.stdout, f"error lines leaked to stdout:\n{run.stdout}"
    assert "[x]" in run.stderr, f"stderr carries no error line:\n{run.stderr}"
    assert run.stdout.isascii() and run.stderr.isascii()


def test_piped_output_is_not_hard_wrapped(apm_binary_path: Path, tmp_path: Path) -> None:
    project, environment = _project(tmp_path)
    missing = f"./{_LONG_NAME}"

    run = _apm(apm_binary_path, project, environment, "install", missing)

    assert run.returncode == 1, run.stdout + run.stderr
    lines = (run.stdout + run.stderr).splitlines()
    head = f"Local package path does not exist: {missing}."
    line = next((item for item in lines if head in item), "")
    # The whole sentence, its long path and its remediation stay on one line.
    assert "Restore the source or correct the dependency path, then" in line, "\n".join(lines)
