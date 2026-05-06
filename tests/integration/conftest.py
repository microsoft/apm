"""Integration test configuration: marker-driven skip + shared fixtures.

This conftest replaces the manual per-file ``pytest.mark.skipif`` boilerplate
with declarative markers that auto-skip when the required precondition is
absent. Tests apply markers via module-level ``pytestmark`` or per-test
decorators; the precondition logic lives here, exactly once.

It also exposes ``make_copilot_project`` for tests that need a project
directory whose target auto-detection resolves to ``copilot``. Under #1154
the bare ``.github/`` directory is no longer a copilot signal -- the file
``.github/copilot-instructions.md`` is required.

See microsoft/apm#1166 for the design rationale.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


def make_copilot_project(tmp_path: Path, name: str = "test-project") -> Path:
    """Create a temp project with a valid copilot signal.

    Materializes ``<tmp_path>/<name>/.github/copilot-instructions.md`` so
    auto-detection resolves to the copilot target without ambiguity.

    Args:
        tmp_path: pytest ``tmp_path`` fixture.
        name: Project directory name (default ``"test-project"``).

    Returns:
        The created project root.
    """
    project = tmp_path / name
    project.mkdir()
    github_dir = project / ".github"
    github_dir.mkdir()
    (github_dir / "copilot-instructions.md").write_bytes(b"# Copilot instructions\n")
    return project


def _has_github_token() -> bool:
    return bool(os.environ.get("GITHUB_APM_PAT") or os.environ.get("GITHUB_TOKEN"))


def _has_ado_pat() -> bool:
    return bool(os.environ.get("ADO_APM_PAT"))


def _has_ado_bearer() -> bool:
    if os.getenv("APM_TEST_ADO_BEARER") != "1":
        return False
    az_bin = shutil.which("az")
    if az_bin is None:
        return False
    try:
        result = subprocess.run(
            [
                az_bin,
                "account",
                "get-access-token",
                "--resource",
                "499b84ac-1321-427f-aa17-267ca6975798",
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.returncode == 0 and result.stdout.startswith("eyJ")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_e2e_mode() -> bool:
    return os.environ.get("APM_E2E_TESTS", "").lower() in ("1", "true", "yes")


def _is_network_integration() -> bool:
    return os.environ.get("APM_RUN_INTEGRATION_TESTS") == "1"


def _is_inference_mode() -> bool:
    return os.environ.get("APM_RUN_INFERENCE_TESTS") == "1"


def _has_apm_binary() -> bool:
    if os.environ.get("APM_BINARY_PATH"):
        return Path(os.environ["APM_BINARY_PATH"]).is_file()
    return shutil.which("apm") is not None


def _has_runtime(name: str) -> bool:
    if shutil.which(name):
        return True
    runtime_path = Path.home() / ".apm" / "runtimes" / name
    return runtime_path.is_file() and os.access(runtime_path, os.X_OK)


_MARKER_CHECKS: dict[str, tuple[Callable[[], bool], str]] = {
    "requires_e2e_mode": (_is_e2e_mode, "APM_E2E_TESTS=1 not set"),
    "requires_github_token": (
        _has_github_token,
        "GITHUB_APM_PAT or GITHUB_TOKEN not set",
    ),
    "requires_ado_pat": (_has_ado_pat, "ADO_APM_PAT not set"),
    "requires_ado_bearer": (
        _has_ado_bearer,
        "az CLI + APM_TEST_ADO_BEARER=1 required",
    ),
    "requires_network_integration": (
        _is_network_integration,
        "APM_RUN_INTEGRATION_TESTS=1 not set",
    ),
    "requires_apm_binary": (
        _has_apm_binary,
        "apm binary not found on PATH (set APM_BINARY_PATH or build via scripts/build-binary.sh)",
    ),
    "requires_runtime_codex": (
        lambda: _has_runtime("codex"),
        "codex runtime not available (run apm runtime setup codex)",
    ),
    "requires_runtime_copilot": (
        lambda: _has_runtime("copilot"),
        "GitHub Copilot CLI runtime not available (run apm runtime setup copilot)",
    ),
    "requires_runtime_llm": (
        lambda: _has_runtime("llm"),
        "llm runtime not available (run apm runtime setup llm)",
    ),
    "requires_inference": (
        _is_inference_mode,
        "APM_RUN_INFERENCE_TESTS=1 not set",
    ),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip items whose marker precondition is not met.

    The skip decision is made once at collection time, so ``-v`` output shows
    the test as ``SKIPPED`` with a clear reason, exactly mirroring the prior
    ``pytestmark = pytest.mark.skipif(...)`` behavior.
    """
    for item in items:
        for marker_name, (check_fn, reason) in _MARKER_CHECKS.items():
            if item.get_closest_marker(marker_name) and not check_fn():
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def apm_binary_path() -> Path:
    """Resolve the apm binary path for tests that need to shell out to it.

    Resolution order:
      1. ``APM_BINARY_PATH`` env var (CI sets this after the build step).
      2. ``shutil.which("apm")`` lookup on ``PATH``.
      3. ``./dist/<platform>/apm`` (local build convention).

    Skips the test if no binary is found, with a message pointing at the
    build script.
    """
    env_path = os.environ.get("APM_BINARY_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.is_file():
            return candidate.resolve()

    on_path = shutil.which("apm")
    if on_path:
        return Path(on_path).resolve()

    import platform as plat

    os_name = plat.system().lower()
    arch = plat.machine().lower()
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    binary_name = f"apm-{os_name}-{arch_map.get(arch, arch)}"
    local_path = Path("dist") / binary_name / "apm"
    if local_path.is_file():
        return local_path.resolve()

    pytest.skip("No apm binary found (set APM_BINARY_PATH or build via scripts/build-binary.sh)")
    raise RuntimeError("unreachable")  # for type-checker
