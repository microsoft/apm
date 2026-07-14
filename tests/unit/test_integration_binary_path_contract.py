"""Contracts for authoritative integration-test binary selection."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration import conftest as integration_conftest
from tests.integration import test_ado_e2e, test_plugin_e2e


@pytest.fixture(autouse=True)
def _clear_binary_resolution_cache() -> None:
    integration_conftest._resolve_apm_binary.cache_clear()
    yield
    integration_conftest._resolve_apm_binary.cache_clear()


def test_explicit_binary_path_is_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit executable must win over local and PATH fallbacks."""
    configured = tmp_path / "configured-apm"
    configured.write_text("#!/bin/sh\n", encoding="utf-8")
    configured.chmod(0o755)
    fallback = tmp_path / "fallback-apm"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o755)
    monkeypatch.setenv("APM_BINARY_PATH", str(configured))
    monkeypatch.setattr(integration_conftest, "_local_dist_apm_binary", lambda: fallback)
    monkeypatch.setattr(integration_conftest.shutil, "which", lambda _name: str(fallback))

    assert integration_conftest._resolve_apm_binary() == configured.resolve()


def test_missing_explicit_binary_path_fails_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing configured artifact must not fall back to another binary."""
    missing = tmp_path / "missing-apm"
    fallback = tmp_path / "fallback-apm"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o755)
    monkeypatch.setenv("APM_BINARY_PATH", str(missing))
    monkeypatch.setattr(integration_conftest, "_local_dist_apm_binary", lambda: fallback)
    monkeypatch.setattr(integration_conftest.shutil, "which", lambda _name: str(fallback))

    with pytest.raises(pytest.UsageError, match=r"APM_BINARY_PATH does not exist"):
        integration_conftest._resolve_apm_binary()


def test_directory_explicit_binary_path_fails_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing directory must not satisfy the executable-file contract."""
    configured = tmp_path / "configured-apm"
    configured.mkdir()
    fallback = tmp_path / "fallback-apm"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o755)
    monkeypatch.setenv("APM_BINARY_PATH", str(configured))
    monkeypatch.setattr(integration_conftest, "_local_dist_apm_binary", lambda: fallback)
    monkeypatch.setattr(integration_conftest.shutil, "which", lambda _name: str(fallback))

    with pytest.raises(pytest.UsageError, match=r"APM_BINARY_PATH does not exist or is not a file"):
        integration_conftest._resolve_apm_binary()


def test_empty_explicit_binary_path_fails_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present empty configuration must not enable fallback discovery."""
    fallback = tmp_path / "fallback-apm"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o755)
    monkeypatch.setenv("APM_BINARY_PATH", "")
    monkeypatch.setattr(integration_conftest, "_local_dist_apm_binary", lambda: fallback)
    monkeypatch.setattr(integration_conftest.shutil, "which", lambda _name: str(fallback))

    with pytest.raises(pytest.UsageError, match=r"APM_BINARY_PATH is set but empty"):
        integration_conftest._resolve_apm_binary()


@pytest.mark.parametrize(
    ("configured_kind", "expected_error"),
    (
        ("empty", "APM_BINARY_PATH is set but empty."),
        ("missing", "APM_BINARY_PATH does not exist or is not a file:"),
        ("directory", "APM_BINARY_PATH does not exist or is not a file:"),
        pytest.param(
            "non-executable",
            "APM_BINARY_PATH is not executable:",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="Windows os.access(X_OK) does not model executable bits",
            ),
        ),
    ),
)
def test_silent_adopt_consumer_rejects_invalid_explicit_binary(
    tmp_path: Path,
    configured_kind: str,
    expected_error: str,
) -> None:
    """The silent-adopt E2E must inherit every canonical failure mode."""
    fallback = tmp_path / "apm"
    fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback.chmod(0o755)
    env = os.environ.copy()
    if configured_kind == "empty":
        env["APM_BINARY_PATH"] = ""
    elif configured_kind == "missing":
        env["APM_BINARY_PATH"] = str(tmp_path / "missing-apm")
    elif configured_kind == "directory":
        configured = tmp_path / "configured-apm"
        configured.mkdir()
        env["APM_BINARY_PATH"] = str(configured)
    else:
        configured = tmp_path / "configured-apm"
        configured.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        configured.chmod(0o644)
        env["APM_BINARY_PATH"] = str(configured)
    env["GITHUB_APM_PAT"] = "test-token"
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/integration/test_silent_adopt_existing_files_e2e.py",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert f"ERROR: {expected_error}" in combined
    assert "INTERNALERROR" not in combined


def test_non_executable_explicit_binary_path_fails_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-executable configured artifact must not fall back to PATH."""
    configured = tmp_path / "configured-apm"
    configured.write_text("not executable\n", encoding="utf-8")
    fallback = tmp_path / "fallback-apm"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o755)
    monkeypatch.setenv("APM_BINARY_PATH", str(configured))
    monkeypatch.setattr(integration_conftest, "_local_dist_apm_binary", lambda: fallback)
    monkeypatch.setattr(integration_conftest.shutil, "which", lambda _name: str(fallback))
    monkeypatch.setattr(integration_conftest.os, "access", lambda _path, _mode: False)

    with pytest.raises(pytest.UsageError, match=r"APM_BINARY_PATH is not executable"):
        integration_conftest._resolve_apm_binary()


def test_ado_consumer_executes_injected_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ADO helper must launch the injected validated executable."""
    configured = tmp_path / "configured-apm"
    captured: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(test_ado_e2e.subprocess, "run", fake_run)

    test_ado_e2e.run_apm_command(configured, "--version", tmp_path)

    assert captured == [[str(configured), "--version"]]


def test_plugin_consumer_executes_injected_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plugin fixture and helper must preserve the injected executable."""
    configured = tmp_path / "configured-apm"
    captured: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(test_plugin_e2e.subprocess, "run", fake_run)
    command = test_plugin_e2e.apm_command.__wrapped__(configured)

    test_plugin_e2e._run_apm_command(command, ["--version"], tmp_path)

    assert captured == [[str(configured), "--version"]]
