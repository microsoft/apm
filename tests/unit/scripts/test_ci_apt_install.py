"""Bounded apt install: a stalled mirror must fail over, not hang the job.

Regression coverage for the lifecycle shards' "Install Unix shell startup
test runtimes" step, which hung for ~7.5 min on unhealthy Ubuntu mirrors.
The script is driven against stub sudo/dpkg/getent/apt-get executables;
GNU timeout and sed are the real ones, so these run on Linux runners only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or shutil.which("timeout") is None,
        reason="drives GNU timeout and GNU sed",
    ),
]

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ci-apt-install.sh"
PRIMARY_SOURCES = "Types: deb\nURIs: http://azure.archive.ubuntu.com/ubuntu/\nSuites: noble\n"

STUBS = {
    "sudo": '#!/bin/sh\nexec "$@"\n',
    "getent": "#!/bin/sh\nexit 0\n",
    "dpkg": (
        "#!/bin/sh\n"
        'if [ "$1" = "--print-architecture" ]; then echo amd64; exit 0; fi\n'
        'echo "dpkg $*" >> "$STUB_LOG"\n'
    ),
    "apt-get": (
        "#!/bin/sh\n"
        'echo "apt-get $*" >> "$STUB_LOG"\n'
        'if grep -q "$STUB_HANG_PATTERN" "$CI_APT_SOURCES_FILE"; then sleep 30; fi\n'
    ),
}


def _run(tmp_path: Path, hang_pattern: str) -> tuple[subprocess.CompletedProcess[str], float]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    sources = tmp_path / "ubuntu.sources"
    sources.write_text(PRIMARY_SOURCES, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CI_APT_SOURCES_FILE": str(sources),
        "APT_CALL_TIMEOUT": "1",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_HANG_PATTERN": hang_pattern,
    }
    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(SCRIPT), "fish", "zsh"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, time.monotonic() - started


def _active_uris(tmp_path: Path) -> list[tuple[str | None, str | None, str]]:
    lines = (tmp_path / "ubuntu.sources").read_text(encoding="utf-8").splitlines()
    urls = [line.split(":", 1)[1].strip() for line in lines if line.startswith("URIs:")]
    return [(urlparse(url).scheme, urlparse(url).hostname, urlparse(url).path) for url in urls]


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()


def test_healthy_primary_installs_without_fallback(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, hang_pattern="^NEVER$")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ubuntu.sources").read_text(encoding="utf-8") == PRIMARY_SOURCES
    calls = _calls(tmp_path)
    assert [call.split()[-1] for call in calls if "apt-get" in call] == ["update", "zsh"]
    assert not any(call.startswith("dpkg") for call in calls)


def test_stalled_primary_is_time_boxed_and_falls_over(tmp_path: Path) -> None:
    result, elapsed = _run(tmp_path, hang_pattern="azure.archive")
    assert result.returncode == 0, result.stderr
    assert elapsed < 20, f"stalled mirror was not time-boxed ({elapsed:.1f}s)"
    assert _active_uris(tmp_path) == [("http", "mirrors.edge.kernel.org", "/ubuntu/")]
    calls = _calls(tmp_path)
    assert "dpkg --configure -a" in calls
    assert calls[-1].endswith("install -y fish zsh")


def test_stalled_fallback_fails_within_bound(tmp_path: Path) -> None:
    result, elapsed = _run(tmp_path, hang_pattern="^URIs:")
    assert result.returncode != 0
    assert elapsed < 20, f"stalled fallback was not time-boxed ({elapsed:.1f}s)"
