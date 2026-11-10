"""Transient Windows denials during staged activation are retried (#3386).

Fault injection supplements the real native-handle evidence in
``tests/integration/test_resolution_failure_lifecycle.py``.
"""

from __future__ import annotations

import errno
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver, DownloadedPackageError
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils import file_ops

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]

_ATTEMPTS = 1 + file_ops._DEFAULT_MAX_RETRIES


def _windows_denial(winerror: int, source: Path, target: Path) -> OSError:
    error = OSError(errno.EACCES, "injected Windows denial", str(source), None, str(target))
    error.winerror = winerror
    return error


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Run on the Windows classification path without real backoff delays."""
    monkeypatch.setattr(sys, "platform", "win32")
    recorded: list[float] = []
    monkeypatch.setattr(file_ops.time, "sleep", recorded.append)
    return recorded


def _deny(
    monkeypatch: pytest.MonkeyPatch,
    matches: Callable[[Path, Path], bool],
    failures: int,
    winerror: int = 32,
) -> list[Path]:
    """Deny matching renames *failures* times, then let the real rename run."""
    real_replace = Path.replace
    attempts: list[Path] = []

    def replace(source: Path, target: Path) -> Path:
        if matches(Path(source), Path(target)):
            attempts.append(Path(source))
            if len(attempts) <= failures:
                raise _windows_denial(winerror, source, target)
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    return attempts


def _staged_package(
    tmp_path: Path, existing: bool
) -> tuple[Path, Path, ResolutionStagingSession, Path]:
    modules = tmp_path / "apm_modules"
    live = modules / "owner" / "package"
    if existing:
        live.mkdir(parents=True)
        (live / "marker").write_bytes(b"prior install")
    staging = ResolutionStagingSession(modules)
    candidate = staging.prepare_replacement(live)
    candidate.mkdir()
    (candidate / "marker").write_bytes(b"candidate")
    return modules, live, staging, candidate


@pytest.mark.parametrize("existing", [False, True], ids=["first-install", "replacement"])
@pytest.mark.parametrize("failures", [1, 2])
@pytest.mark.parametrize("winerror", [5, 32])
def test_transient_activation_denial_retries_then_activates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    existing: bool,
    failures: int,
    winerror: int,
) -> None:
    """A rename that clears within the retry budget activates without error."""
    _, live, staging, candidate = _staged_package(tmp_path, existing)
    attempts = _deny(monkeypatch, lambda source, _target: source == candidate, failures, winerror)

    assert staging.publish_replacement(candidate) == live.resolve()

    assert len(attempts) == failures + 1
    assert len(sleeps) == failures
    assert (live / "marker").read_bytes() == b"candidate"
    assert staging.commit() == []
    assert not (live.parent.parent / ".apm-resolution-staging").exists()


def test_transient_backup_denial_retries_and_keeps_rollback_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """Moving the prior package aside is part of replacement activation."""
    _, live, staging, candidate = _staged_package(tmp_path, existing=True)
    attempts = _deny(
        monkeypatch,
        lambda source, target: source == live.resolve() and "backups" in target.parts,
        failures=1,
    )

    staging.publish_replacement(candidate)

    assert len(attempts) == 2
    assert (live / "marker").read_bytes() == b"candidate"
    assert staging.rollback() == []
    assert (live / "marker").read_bytes() == b"prior install"


def test_transient_restore_denial_retries_and_restores_prior_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """A persistent activation denial still restores prior bytes past a brief hold."""
    _, live, staging, candidate = _staged_package(tmp_path, existing=True)
    activation = _deny(monkeypatch, lambda source, _target: source == candidate, _ATTEMPTS)
    restore = _deny(
        monkeypatch, lambda source, _target: source.parent.name == "backups", failures=1
    )

    with pytest.raises(OSError, match="injected Windows denial") as caught:
        staging.publish_replacement(candidate)

    assert "Could not restore" not in str(caught.value)
    assert len(activation) == _ATTEMPTS
    assert len(restore) == 2
    assert (live / "marker").read_bytes() == b"prior install"
    assert staging.rollback() == []
    assert (live / "marker").read_bytes() == b"prior install"


def test_persistent_activation_and_restore_denial_retains_recovery_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """Exhausted retries keep both errors and the backup for recovery."""
    _, live, staging, candidate = _staged_package(tmp_path, existing=True)
    activation = _deny(monkeypatch, lambda source, _target: source == candidate, _ATTEMPTS)
    restore = _deny(monkeypatch, lambda source, _target: source.parent.name == "backups", _ATTEMPTS)

    with pytest.raises(OSError) as caught:
        staging.publish_replacement(candidate)

    message = str(caught.value)
    assert f"Could not activate {candidate}" in message
    assert "Could not restore prior package" in message
    assert message.count("injected Windows denial") == 2
    assert len(activation) == _ATTEMPTS
    assert len(restore) == _ATTEMPTS
    assert sum(sleeps) <= 2 * _ATTEMPTS * file_ops._DEFAULT_MAX_DELAY
    backups = list(staging._staging_root.glob("backups/*/marker"))
    assert [backup.read_bytes() for backup in backups] == [b"prior install"]
    assert not live.exists()


@pytest.mark.parametrize("update_refs", [False, True], ids=["install", "update"])
@pytest.mark.parametrize("existing", [False, True], ids=["first-install", "replacement"])
def test_resolver_activation_survives_transient_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    update_refs: bool,
    existing: bool,
) -> None:
    """Install and update share the resolver activation owner and both recover."""
    modules = tmp_path / "apm_modules"
    ref = DependencyReference(repo_url="org/pkg", reference="main")
    live = ref.get_install_path(modules)
    if existing:
        live.mkdir(parents=True)
        (live / "apm.yml").write_text("name: pkg\nversion: 0.9.0\n", encoding="utf-8")
    staging = ResolutionStagingSession(modules)
    candidate = staging.prepare_replacement(live)
    candidate.mkdir()
    (candidate / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
    attempts = _deny(monkeypatch, lambda source, _target: source == candidate, failures=2)
    resolver = APMDependencyResolver(
        apm_modules_dir=modules,
        download_callback=lambda *_args, **_kwargs: candidate,
        activation_callback=staging.publish_replacement,
        update_refs=update_refs,
    )

    package = resolver._try_load_dependency_package(ref)

    assert package is not None
    assert package.version == "1.0.0"
    assert len(attempts) == 3
    assert staging.commit() == []


def test_resolver_persistent_denial_names_operation_path_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """A denial that never clears still fails with the original diagnostic."""
    modules = tmp_path / "apm_modules"
    ref = DependencyReference(repo_url="org/pkg", reference="main")
    live = ref.get_install_path(modules)
    live.mkdir(parents=True)
    (live / "apm.yml").write_text("name: pkg\nversion: 0.9.0\n", encoding="utf-8")
    staging = ResolutionStagingSession(modules)
    candidate = staging.prepare_replacement(live)
    candidate.mkdir()
    (candidate / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
    attempts = _deny(monkeypatch, lambda source, _target: source == candidate, _ATTEMPTS)
    activate = MagicMock(side_effect=staging.publish_replacement)
    resolver = APMDependencyResolver(
        apm_modules_dir=modules,
        download_callback=lambda *_args, **_kwargs: candidate,
        activation_callback=activate,
        update_refs=True,
    )

    with pytest.raises(DownloadedPackageError) as caught:
        resolver._try_load_dependency_package(ref)

    message = str(caught.value)
    assert "Failed to activate downloaded dependency" in message
    assert "injected Windows denial" in message
    assert repr(str(candidate)) in message
    assert repr(str(live.resolve())) in message
    assert len(attempts) == _ATTEMPTS
    assert (live / "apm.yml").read_text(encoding="utf-8") == "name: pkg\nversion: 0.9.0\n"
    assert staging.rollback() == []
    assert (live / "apm.yml").read_text(encoding="utf-8") == "name: pkg\nversion: 0.9.0\n"


def test_posix_permission_denial_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX EACCES is a real permission problem: one attempt, no backoff."""
    monkeypatch.setattr(sys, "platform", "linux")
    sleeps = MagicMock()
    monkeypatch.setattr(file_ops.time, "sleep", sleeps)
    _, live, staging, candidate = _staged_package(tmp_path, existing=True)
    real_replace = Path.replace
    attempts: list[Path] = []

    def replace(source: Path, target: Path) -> Path:
        if Path(source) == candidate:
            attempts.append(Path(source))
            raise PermissionError(errno.EACCES, "posix denial", str(source))
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)

    with pytest.raises(PermissionError, match="posix denial"):
        staging.publish_replacement(candidate)

    assert len(attempts) == 1
    sleeps.assert_not_called()
    assert (live / "marker").read_bytes() == b"prior install"
