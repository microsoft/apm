"""``apm compile`` fires the ``pre-compile`` lifecycle event before it reads sources.

A trusted pre-compile script can generate primitives under ``.apm/`` from the
project's own toolchain (for example a framework that composes guidance from
installed dependencies), and compile then reads what it wrote. Like every
lifecycle script, a failing one never stops the compile.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.policy.discovery import discover_policy_with_chain as _REAL_DISCOVER_WITH_CHAIN

pytestmark = pytest.mark.component

GENERATOR = """\
from pathlib import Path
import sys

target = Path(".apm/instructions/generated.instructions.md")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text('---\\napplyTo: "**"\\n---\\nGenerated guidance from the toolchain.\\n')
Path("generator-ran").write_text(sys.stdin.read())
with open("generator-runs", "a") as runs:
    runs.write("run\\n")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
"""

LOCK_PROBE = """\
import os
from pathlib import Path
from filelock import FileLock, Timeout

def held(path):
    try:
        FileLock(str(path)).acquire(timeout=0)
    except Timeout:
        return True
    return False

metadata = Path(os.environ["APM_HOME"]) / ".apm-lifecycle.lock"
deployment = Path.home() / ".apm-local" / ".apm-lifecycle.lock"
state = "held" if held(metadata) and held(deployment) else "free"
Path("lock-state").write_text(state)
"""


def _project(tmp_path: Path, *, exit_code: int = 0, event: str = "pre-compile") -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "gen.py").write_text(GENERATOR)
    entry = {
        "type": "command",
        "description": "Generate instructions",
        "run": f'"{sys.executable}" gen.py {exit_code}',
    }
    manifest = {
        "name": "demo",
        "version": "0.1.0",
        "dependencies": {"apm": ["acme/guidelines#v1.2.0"]},
        "lifecycle": {event: [entry]},
    }
    (project / "apm.yml").write_text(yaml.safe_dump(manifest))
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


def _compile(
    project: Path, monkeypatch: pytest.MonkeyPatch, *args: str, trusted: bool | None = True
):
    """Run compile; ``trusted=None`` uses the real trust store."""
    monkeypatch.chdir(project)
    if trusted is None:
        return CliRunner().invoke(cli, ["compile", "--target", "codex", *args])
    with patch("apm_cli.core.script_trust.is_fingerprint_trusted", return_value=trusted):
        return CliRunner().invoke(cli, ["compile", "--target", "codex", *args])


def _output(result) -> str:
    return " ".join(result.output.split())


def test_generated_instructions_reach_agents_md(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    result = _compile(project, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Generated guidance from the toolchain." in (project / "AGENTS.md").read_text()


def test_event_fires_once_per_compile(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    result = _compile(project, monkeypatch)
    assert result.exit_code == 0, result.output
    assert (project / "generator-runs").read_text() == "run\n"


def test_audit_runs_no_scripts(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    _commit_instruction(project)
    monkeypatch.chdir(project)
    with patch("apm_cli.core.script_trust.is_fingerprint_trusted", return_value=True):
        CliRunner().invoke(cli, ["audit"])
        CliRunner().invoke(cli, ["audit", "--ci"])
    assert not (project / "generator-ran").exists()


def test_script_receives_the_event_payload(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    _compile(project, monkeypatch)
    payload = json.loads((project / "generator-ran").read_text())
    assert payload["event"] == "pre-compile"
    assert payload["scope"] == "project"
    assert [(p["name"], p["reference"]) for p in payload["packages"]] == [
        ("acme/guidelines", "v1.2.0")
    ]


def test_failing_script_does_not_stop_compile(isolated: Path, monkeypatch) -> None:
    project = _project(isolated, exit_code=3)
    result = _compile(project, monkeypatch)
    assert result.exit_code == 0, result.output
    assert (project / "AGENTS.md").is_file()
    assert "Lifecycle pre-compile command script exited with code 3; continuing" in _output(result)


def test_untrusted_script_is_skipped_with_notice(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    _commit_instruction(project)
    result = _compile(project, monkeypatch, trusted=False)
    assert result.exit_code == 0, result.output
    assert not (project / "generator-ran").exists()
    assert "Skipped 1 untrusted project script(s). Run 'apm lifecycle trust'" in _output(result)


def test_untrusted_scripts_for_other_events_add_no_notice(isolated: Path, monkeypatch) -> None:
    project = _project(isolated, event="post-install")
    _commit_instruction(project)
    result = _compile(project, monkeypatch, trusted=False)
    assert result.exit_code == 0, result.output
    assert "untrusted" not in result.output


def test_org_deny_all_suppresses_scripts(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    _commit_instruction(project)
    policy = SimpleNamespace(policy=SimpleNamespace(executables=SimpleNamespace(deny_all=True)))
    with patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=policy):
        result = _compile(project, monkeypatch)
    assert result.exit_code == 0, result.output
    assert not (project / "generator-ran").exists()


def test_org_deny_all_suppresses_scripts_through_real_policy_discovery(
    isolated: Path, monkeypatch
) -> None:
    """The shared runner hands policy discovery a root it can join paths onto."""
    project = _project(isolated)
    _commit_instruction(project)
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    leaf = SimpleNamespace(
        policy=SimpleNamespace(extends=None, executables=SimpleNamespace(deny_all=True)),
        cached=False,
    )
    with (
        patch("apm_cli.policy.discovery.discover_policy_with_chain", _REAL_DISCOVER_WITH_CHAIN),
        patch("apm_cli.policy.discovery.discover_policy", return_value=leaf) as discover,
    ):
        result = _compile(project, monkeypatch)
    assert result.exit_code == 0, result.output
    assert not (project / "generator-ran").exists()
    root = discover.call_args.args[0]
    assert isinstance(root, Path)
    assert root.resolve() == project.resolve()
    assert "suppressed by org policy (executables.deny_all)" in _output(result)


def test_trust_record_from_before_pre_compile_needs_fresh_trust(
    isolated: Path, monkeypatch
) -> None:
    project = _project(isolated)
    _commit_instruction(project)
    lifecycle = yaml.safe_load((project / "apm.yml").read_text())["lifecycle"]
    canonical = json.dumps(lifecycle, sort_keys=True, separators=(",", ":"))
    store = isolated / "apmhome" / "scripts-trust.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": {
                    str((project / "apm.yml").resolve()): hashlib.sha256(
                        canonical.encode("utf-8")
                    ).hexdigest()
                },
            }
        )
    )

    skipped = _compile(project, monkeypatch, trusted=None)
    assert skipped.exit_code == 0, skipped.output
    assert not (project / "generator-ran").exists()
    assert "Skipped 1 untrusted project script(s)" in _output(skipped)

    trusted = CliRunner().invoke(cli, ["lifecycle", "trust"])
    assert trusted.exit_code == 0, trusted.output
    ran = _compile(project, monkeypatch, trusted=None)
    assert ran.exit_code == 0, ran.output
    assert (project / "generator-ran").exists()


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
    result = _compile(project, monkeypatch, *flags)
    assert result.exit_code == 0, result.output
    assert not (project / "generator-ran").exists()


def test_global_mode_runs_no_scripts(isolated: Path, monkeypatch) -> None:
    project = _project(isolated)
    monkeypatch.chdir(project)
    with (
        patch("apm_cli.commands.compile.cli._handle_global_flag", return_value=0) as handle,
        patch("apm_cli.commands.compile.cli._fire_pre_compile") as fire,
    ):
        result = CliRunner().invoke(cli, ["compile", "--global"])
    assert result.exit_code == 0, result.output
    handle.assert_called_once()
    fire.assert_not_called()


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
