"""The install fast entry decides --frozen and guards the lockfile like the command.

A bare ``apm install`` with ``CI`` set and a lockfile present runs as
``--frozen`` and says so, and a frozen fast run withholds every lockfile write
(``InstallService.frozen_writes``), exactly as ``commands/install.py`` does.
The engine is replaced by a stand-in that tries to rewrite the lockfile.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-LAW-lockfile.frozen_preserves_bytes"),
]

_LOCK = "lockfile_version: '1'\ngenerated_at: '2025-01-01T00:00:00Z'\ndependencies: []\n"
_NOTICE = "CI is set and apm.lock.yaml exists: installing as --frozen."


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "apm.yml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    (tmp_path / "apm.lock.yaml").write_text(_LOCK, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in ("CI", "APM_INSTALL_FAST_PATH"):
        monkeypatch.delenv(name, raising=False)

    def rewriting_engine(options, argv, root, *, frozen_disposition, decide_only):
        from apm_cli.deps.lockfile import LockFile

        lock = LockFile.read(root / "apm.lock.yaml")
        lock.mcp_servers = ["added-by-the-fast-run"]
        lock.write(root / "apm.lock.yaml")
        return True

    monkeypatch.setattr("apm_cli.install.diff_apply.try_apply", rewriting_engine)
    return tmp_path


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    result = CliRunner().invoke(cli, argv, obj={})
    captured = capsys.readouterr()
    return result.exit_code, result.output + captured.out + captured.err


def test_a_frozen_fast_run_never_rewrites_the_lockfile(project: Path, capsys) -> None:
    code, _ = _run(["install", "--frozen"], capsys)

    assert code == 0
    assert (project / "apm.lock.yaml").read_text(encoding="utf-8") == _LOCK


def test_ci_with_a_lockfile_runs_the_fast_path_frozen_and_says_so(
    project: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")

    code, output = _run(["install"], capsys)

    assert code == 0
    assert output.count(_NOTICE) == 1
    assert (project / "apm.lock.yaml").read_text(encoding="utf-8") == _LOCK


def test_a_plain_fast_run_may_write_the_lockfile(project: Path, capsys) -> None:
    """Control: the stand-in engine's write lands when nothing freezes it."""
    code, output = _run(["install"], capsys)

    assert code == 0
    assert _NOTICE not in output
    assert "added-by-the-fast-run" in (project / "apm.lock.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("restored", [True, False], ids=["restored", "restore-incomplete"])
def test_a_failed_restore_stops_and_never_runs_the_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, restored: bool
) -> None:
    """The real engine and entry: an incomplete restore exits 1 and names each path.

    Only a clean restore falls back: a spy on the full install command's
    callback counts one call there and none here.
    """
    from apm_cli.install.transaction import ApplyRestoreIncomplete

    (tmp_path / "apm.yml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    (tmp_path / "apm.lock.yaml").write_text(_LOCK, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in ("CI", "APM_INSTALL_FAST_PATH"):
        monkeypatch.delenv(name, raising=False)
    stuck = tmp_path / ".github" / "prompts" / "a.prompt.md"

    import importlib

    # The engine's first I/O inside its decision is the lockfile read's staged-write
    # probe; it fails once, and every later reader gets the real probe.
    authority = importlib.import_module("apm_cli.deps.lockfile_write_authority")
    real_probe = authority.has_staged_lockfile
    calls: list[Path] = []

    def failing_once(path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("disk full") if restored else ApplyRestoreIncomplete([(stuck, "in use")])
        return real_probe(path)

    monkeypatch.setattr(authority, "has_staged_lockfile", failing_once)
    # Spy on the full install command the fast entry falls back to: count, then run it.
    command = importlib.import_module("apm_cli.commands.install").install
    real_callback = command.callback
    full_runs: list[dict] = []

    def counting_callback(*args, **kwargs):
        full_runs.append(kwargs)
        return real_callback(*args, **kwargs)

    monkeypatch.setattr(command, "callback", counting_callback)

    code, output = _run(["install"], capsys)

    if restored:
        assert code == 0
        assert len(full_runs) == 1, "a clean restore falls back to the full pipeline once"
    else:
        assert code == 1
        assert full_runs == [], "the full pipeline never runs on a partly restored tree"
        assert "could not be restored" in output
        assert f"Deploy restore path {stuck}: in use" in output
