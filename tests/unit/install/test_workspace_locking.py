"""Unit contracts for workspace lifecycle serialization."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from filelock import Timeout

from apm_cli.cli import cli
from apm_cli.install.locking import (
    LifecycleBusyError,
    acquire_lifecycle_lock,
    deployment_root_lock,
    lifecycle_lock,
)

pytestmark = pytest.mark.windows_compat


def test_lifecycle_lock_is_reentrant_per_process() -> None:
    first = lifecycle_lock()
    second = lifecycle_lock()

    assert first is second
    with first, second:
        assert first.is_locked


def test_lifecycle_lock_uses_isolated_windows_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Global locking follows Path.home instead of the runner's real profile."""
    home = tmp_path / "windows-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    lock = acquire_lifecycle_lock()
    try:
        assert Path(lock.lock_file) == (home / ".apm" / ".apm-lifecycle.lock").resolve()
    finally:
        lock.release()


def test_lifecycle_lock_honors_apm_home(tmp_path: Path, monkeypatch) -> None:
    apm_home = tmp_path / "isolated-metadata"
    monkeypatch.setenv("APM_HOME", str(apm_home))

    lock = acquire_lifecycle_lock()
    try:
        assert Path(lock.lock_file) == (apm_home / ".apm-lifecycle.lock").resolve()
    finally:
        lock.release()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires elevated Windows rights")
def test_lifecycle_lock_rejects_symlink_without_touching_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    lock_dir = home / ".apm"
    lock_dir.mkdir(parents=True)
    victim = tmp_path / "victim"
    original = b"preserve me"
    victim.write_bytes(original)
    (lock_dir / ".apm-lifecycle.lock").symlink_to(victim)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    with pytest.raises(LifecycleBusyError, match="Replace the symlink"):
        acquire_lifecycle_lock()

    assert victim.read_bytes() == original
    assert (lock_dir / ".apm-lifecycle.lock").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires elevated Windows rights")
def test_deployment_root_lock_rejects_symlink_without_touching_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    lock_dir = home / ".apm-local"
    lock_dir.mkdir(parents=True)
    victim = tmp_path / "victim"
    original = b"preserve me"
    victim.write_bytes(original)
    (lock_dir / ".apm-lifecycle.lock").symlink_to(victim)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))

    with pytest.raises(LifecycleBusyError, match="Replace the symlink"):
        acquire_lifecycle_lock()

    assert victim.read_bytes() == original
    assert (lock_dir / ".apm-lifecycle.lock").is_symlink()
    assert not lifecycle_lock().is_locked


def test_busy_error_names_lock_path_and_wait(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    lock = lifecycle_lock()

    with (
        patch.object(lock, "acquire", side_effect=Timeout(lock.lock_file)) as acquire,
        patch("apm_cli.utils.console._rich_info") as info,
        pytest.raises(LifecycleBusyError, match=r"waited 0.25s.*\.apm-lifecycle\.lock"),
    ):
        acquire_lifecycle_lock(timeout=0.25)
    assert acquire.call_count == 2
    info.assert_called_once_with(
        "Another APM operation is running; waiting up to 0.25s.",
        symbol="info",
    )
    assert not deployment_root_lock().is_locked


_HOLD_LIFECYCLE_LOCK = """
import sys
from apm_cli.install.locking import acquire_lifecycle_lock
lock = acquire_lifecycle_lock(timeout=5)
print("held", flush=True)
sys.stdin.read()
lock.release()
"""


def test_different_apm_homes_still_serialize_on_the_deployment_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Two APM homes share projects and harness dirs, so they must not mutate at once."""
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APM_HOME": str(tmp_path / "apm-home-a"),
    }
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LIFECYCLE_LOCK],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home-b"))
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with (
            patch("apm_cli.utils.console._rich_info"),
            pytest.raises(LifecycleBusyError, match=r"shared by every APM_HOME.*\.apm-local"),
        ):
            acquire_lifecycle_lock(timeout=0.25)
        assert not lifecycle_lock().is_locked
    finally:
        holder.communicate(input="", timeout=30)

    lock = acquire_lifecycle_lock(timeout=5)
    lock.release()


def test_install_root_redirect_teardown_error_releases_lock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text("name: fixture\nversion: 1.0.0\n", encoding="ascii")
    redirect = MagicMock()
    redirect.__enter__.return_value = None
    redirect.__exit__.side_effect = RuntimeError("teardown failed")

    with patch("apm_cli.install.root_redirect.install_root_redirect", return_value=redirect):
        result = CliRunner().invoke(cli, ["install"])

    assert result.exit_code != 0
    assert not lifecycle_lock().is_locked


def test_uninstall_cleanup_error_releases_lock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text("name: fixture\nversion: 1.0.0\n", encoding="ascii")

    with patch(
        "apm_cli.commands.uninstall.cli._cleanup_staged_local_refreshes",
        side_effect=RuntimeError("cleanup failed"),
    ):
        result = CliRunner().invoke(cli, ["uninstall", "missing"])

    assert result.exit_code != 0
    assert not lifecycle_lock().is_locked


@pytest.mark.parametrize("args", (("prune", "--dry-run"), ("deps", "clean", "--dry-run")))
def test_read_only_dry_run_commands_skip_lifecycle_lock(
    tmp_path: Path,
    monkeypatch,
    args: tuple[str, ...],
) -> None:
    """Read-only previews should not contend with lifecycle mutations."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text("name: fixture\nversion: 1.0.0\n", encoding="ascii")
    (tmp_path / "apm_modules").mkdir()

    with patch(
        "apm_cli.install.locking.lifecycle_operation",
        side_effect=AssertionError("dry run acquired lifecycle lock"),
    ):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
