"""Research probe (CI only): which real subdir deps lever S serves, and why the rest fall back."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.perf.generator import consumer_manifest
from tests.fixtures.perf.real_network import REAL_PACKAGES


@pytest.mark.parametrize("world", ["plain", "bench-isolated"])
def test_real_engagement(tmp_path: Path, world: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(consumer_manifest("probe", list(REAL_PACKAGES), ("copilot",)))
    env = {
        **os.environ,
        "APM_LOG_LEVEL": "DEBUG",
        "APM_CACHE_DIR": str(tmp_path / "cache"),
        "HOME": str(tmp_path / "home"),
    }
    if world == "bench-isolated":
        import contextlib

        from tests.fixtures.perf import counting_tunnel
        from tests.fixtures.perf.offline import isolated_environment

        _isolated, base = isolated_environment(tmp_path / "env")
        stack = contextlib.ExitStack()
        tunnel = stack.enter_context(counting_tunnel())
        env = tunnel.child_environment(base)
        env["APM_LOG_LEVEL"] = "DEBUG"
        for key in sorted(env):
            if (
                key.startswith(
                    ("GIT_", "SSL_", "REQUESTS_", "CURL_", "HTTP", "ALL_PROXY", "NO_PROXY")
                )
                or "proxy" in key.lower()
            ):
                print("ENV", world, key, "=", env[key][-80:])
        for args in (
            ["--get-all", "http.sslbackend"],
            ["--get", "http.sslcainfo"],
            ["--list", "--show-origin"],
        ):
            out = subprocess.run(
                ["git", "config", "--show-origin", *args], env=env, capture_output=True, text=True
            )
            print("GITCFG", args[-1], repr(out.stdout[-600:]))
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "-m", "apm_cli.cli", "install"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    for line in (result.stdout + result.stderr).splitlines():
        if "in-process" in line or "In-process" in line or "falls back" in line:
            print("ENGAGE", world, line[-300:])
    assert result.returncode == 0, result.stderr[-3000:]
