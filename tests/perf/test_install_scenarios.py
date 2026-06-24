"""Perf scenarios: discovery cost on giant repos + multi-target installs.

Run with::

    PYTEST_PERF=1 pytest tests/perf -v -s

Acceptance numbers (post-#1533 fix):

- awd-cli T=1: 5.0s baseline -> <=1.5s
- awd-cli T=7: 19.7s baseline -> <=3.0s
- Kubernetes T=1: 205s baseline -> <=5s
- TypeScript T=1: 297s baseline -> <=10s

Tests print measurements but do NOT assert wall-time bounds (wall
times are environment-sensitive). Compare against the table above.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import (
    PERF_ENABLED,
    clean_apm_modules,
    make_local_packages,
    write_manifest,
)

pytestmark = pytest.mark.skipif(
    not PERF_ENABLED, reason="Perf scenarios are opt-in (PYTEST_PERF=1)"
)


def _apm_install(cwd: Path, verbose: bool = False) -> tuple[float, str]:
    cmd = [sys.executable, "-m", "apm_cli.cli", "install"]
    if verbose:
        cmd.append("--verbose")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    return time.perf_counter() - t0, proc.stdout + proc.stderr


def _discover_only(base: Path) -> dict[str, float | int | bool]:
    from apm_cli.primitives.discovery import clear_discovery_cache, discover_primitives

    clear_discovery_cache()
    t0 = time.perf_counter()
    coll = discover_primitives(str(base))
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    coll2 = discover_primitives(str(base))
    warm = time.perf_counter() - t0
    return {
        "cold_s": cold,
        "warm_s": warm,
        "primitives": len(coll.all_primitives()),
        "warm_speedup": (cold / warm) if warm > 0 else float("inf"),
        "cached_identity": coll is coll2,
    }


def test_discover_kubernetes(kubernetes_clone: Path) -> None:
    r = _discover_only(kubernetes_clone)
    print(
        f"\n[kubernetes] discover cold={r['cold_s']:.3f}s "
        f"warm={r['warm_s'] * 1000:.3f}ms primitives={r['primitives']} "
        f"speedup={r['warm_speedup']:.0f}x"
    )
    assert r["cached_identity"] is True


def test_discover_typescript(typescript_clone: Path) -> None:
    r = _discover_only(typescript_clone)
    print(
        f"\n[typescript] discover cold={r['cold_s']:.3f}s "
        f"warm={r['warm_s'] * 1000:.3f}ms primitives={r['primitives']} "
        f"speedup={r['warm_speedup']:.0f}x"
    )
    assert r["cached_identity"] is True


def test_install_self_awd_cli() -> None:
    repo = Path(os.environ.get("APM_PERF_AWD_REPO", "/Users/danielmeppiel/Repos/awd-cli"))
    if not repo.exists():
        pytest.skip(f"awd-cli repo not at {repo}")
    wall, _ = _apm_install(repo)
    print(f"\n[awd-cli T=1] install wall={wall:.3f}s")


def test_install_multi_target_breadth(tmp_path: Path) -> None:
    make_local_packages(tmp_path, [f"pkg-{i}" for i in range(5)])
    write_manifest(
        tmp_path,
        """\
name: perf-multi-target
version: 0.0.1
description: 7 targets x 5 local packages
dependencies:
  apm:
    - local: ./packages/pkg-0
    - local: ./packages/pkg-1
    - local: ./packages/pkg-2
    - local: ./packages/pkg-3
    - local: ./packages/pkg-4
targets:
  - copilot
  - claude
  - codex
  - gemini
  - cursor
  - opencode
  - aider
""",
    )
    clean_apm_modules(tmp_path)
    wall, output = _apm_install(tmp_path, verbose=True)
    print(f"\n[multi-target T=7] install wall={wall:.3f}s")
    for ln in output.splitlines():
        if ln.startswith("Perf:"):
            print(f"  {ln}")
