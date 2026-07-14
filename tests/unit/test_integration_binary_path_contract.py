"""Contracts for authoritative integration-test binary selection."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration import conftest as integration_conftest


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


def test_silent_adopt_consumer_rejects_empty_explicit_binary(
    tmp_path: Path,
) -> None:
    """The silent-adopt E2E must fail through the canonical resolver."""
    fallback = tmp_path / "apm"
    fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback.chmod(0o755)
    env = os.environ.copy()
    env["APM_BINARY_PATH"] = ""
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
    assert "ERROR: APM_BINARY_PATH is set but empty." in combined
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
