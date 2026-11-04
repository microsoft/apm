"""``apm compile`` fires the ``pre-compile`` lifecycle event before it reads sources.

A trusted pre-compile script can generate primitives under ``.apm/`` from the
project's own toolchain (for example a framework that composes guidance from
installed dependencies), and compile then reads what it wrote. Like every
lifecycle script, a failing one never stops the compile.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]

GENERATOR = """\
from pathlib import Path
import sys

target = Path(".apm/instructions/generated.instructions.md")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text('---\\napplyTo: "**"\\n---\\nGenerated guidance from the toolchain.\\n')
Path("generator-ran").write_text("yes")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
"""

LOCK_PROBE = """\
from pathlib import Path
from filelock import FileLock, Timeout

try:
    FileLock(str(Path.home() / ".apm" / ".apm-lifecycle.lock")).acquire(timeout=0)
    state = "free"
except Timeout:
    state = "held"
Path("lock-state").write_text(state)
"""


def _project(tmp_path: Path, *, exit_code: int = 0) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "gen.py").write_text(GENERATOR)
    entry = {
        "type": "command",
        "description": "Generate instructions",
        "run": f'"{sys.executable}" gen.py {exit_code}',
    }
    (project / "apm.yml").write_text(
        yaml.safe_dump({"name": "demo", "version": "0.1.0", "lifecycle": {"pre-compile": [entry]}})
    )
    return project


def _commit_instruction(project: Path) -> None:
    instructions = project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "kept.instructions.md").write_text(
        '---\napplyTo: "**"\ndescription: kept\n---\nCommitted guidance.\n'
    )


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apmhome"))
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    with patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=None):
        yield tmp_path


def _compile(project: Path, monkeypatch: pytest.MonkeyPatch, *args: str, trusted: bool = True):
    monkeypatch.chdir(project)
    with patch("apm_cli.core.script_trust.is_fingerprint_trusted", return_value=trusted):
        return CliRunner().invoke(cli, ["compile", "--target", "codex", *args])


def test_generated_instructions_reach_agents_md(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    result = _compile(project, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Generated guidance from the toolchain." in (project / "AGENTS.md").read_text()


def test_failing_script_does_not_stop_compile(isolated: Path, monkeypatch) -> None:
    project = _project(isolated, exit_code=3)
    result = _compile(project, monkeypatch)
    assert result.exit_code == 0, result.output
    assert (project / "AGENTS.md").is_file()


def test_untrusted_script_is_skipped_with_notice(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    _commit_instruction(project)
    result = _compile(project, monkeypatch, trusted=False)
    assert result.exit_code == 0, result.output
    assert not (project / "generator-ran").exists()
    assert "apm lifecycle trust" in " ".join(result.output.split())


def test_apm_no_scripts_skips_scripts(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    _commit_instruction(project)
    monkeypatch.setenv("APM_NO_SCRIPTS", "1")
    result = _compile(project, monkeypatch)
    assert result.exit_code == 0, result.output
    assert not (project / "generator-ran").exists()


@pytest.mark.parametrize(
    "flags",
    [("--dry-run",), ("--validate",), ("--root", "out")],
    ids=["dry-run", "validate", "root"],
)
def test_read_only_and_redirected_modes_run_no_scripts(
    isolated: Path, monkeypatch, flags: tuple[str, ...]
) -> None:
    project = _project(isolated)
    _commit_instruction(project)
    _compile(project, monkeypatch, *flags)
    assert not (project / "generator-ran").exists()


def test_watch_mode_runs_no_scripts(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    _commit_instruction(project)
    with patch("apm_cli.commands.compile.cli._watch_mode") as watch:
        result = _compile(project, monkeypatch, "--watch")
    assert result.exit_code == 0, result.output
    watch.assert_called_once()
    assert not (project / "generator-ran").exists()


def test_scripts_run_under_the_lifecycle_lock(isolated: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(isolated / "home"))
    monkeypatch.setenv("USERPROFILE", str(isolated / "home"))
    project = _project(isolated)
    (project / "gen.py").write_text(LOCK_PROBE + GENERATOR)
    result = _compile(project, monkeypatch)
    assert result.exit_code == 0, result.output
    assert (project / "lock-state").read_text() == "held"
