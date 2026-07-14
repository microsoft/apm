"""Contracts for authoritative integration-test binary selection."""

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

    with pytest.raises(RuntimeError, match=r"APM_BINARY_PATH does not exist"):
        integration_conftest._resolve_apm_binary()


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

    with pytest.raises(RuntimeError, match=r"APM_BINARY_PATH is not executable"):
        integration_conftest._resolve_apm_binary()
